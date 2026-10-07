#!/usr/bin/env python3
"""Offline replay / re-grade of recorded live-evaluation evidence.

Replay re-runs the CURRENT deterministic graders over already-recorded normalized events and
Mandala state snapshots. It is a re-grade, not a re-execution: it never launches Codex,
Claude Code, or Mandala CLI, makes no network request, never runs a recorded command, and
never modifies the source artifacts. Final prose is not re-graded; manual review requirements
are carried over as "not replayed".

Snapshots come from cases/<alias>/evidence.json when present, otherwise they are rebuilt from
evaluator snapshot events in normalized.jsonl (artifacts recorded before evidence.json existed).
Insufficient or ambiguous evidence makes a case UNREPLAYABLE; nothing is guessed.

Exit codes, in precedence order: 2 input/schema/configuration problem, any UNREPLAYABLE
case, or any replayed INCONCLUSIVE case (even when another case is AUTO_FAIL); otherwise
1 when at least one replayed case is AUTO_FAIL; otherwise 0.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys
from typing import Dict, List, Optional

if __package__:
    from . import eval_coverage
    from . import live_eval_artifacts as artifacts
    from . import validate
else:
    import eval_coverage
    import live_eval_artifacts as artifacts
    import validate

cases_mod = artifacts.cases_mod
ROOT = artifacts.ROOT
REPLAYED, UNREPLAYABLE = "REPLAYED", "UNREPLAYABLE"
INSUFFICIENT = ("ENVIRONMENT_ERROR", "UNSUPPORTED", "NOT_RUN")
PROVENANCE_FILES = (
    "scripts/live_eval_cases.py", "scripts/live_eval_artifacts.py", "scripts/eval_replay.py",
    "tests/evals/cases.json", "tests/evals/live_suites.json", "tests/evals/contracts.json",
)


def load_current() -> tuple:
    """Current fixtures and live manifest, validated the same way the live harness does."""
    contracts = validate.load_contracts()
    behavior = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
    validate.validate_eval_metadata(behavior, contracts)
    manifest = validate.validate_live_suites(json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8")), behavior)
    return {case["id"]: case for case in behavior}, manifest


class CaseInput:
    """Loaded and schema-checked source artifacts for one case (no grading yet)."""

    def __init__(self, source: artifacts.Source, alias: str):
        self.alias = alias
        self.entry = source.case_entry(alias)
        self.result = source.case_result(alias)
        self.hashes: Dict[str, str] = {}
        self.events: Optional[List[dict]] = None
        self.evidence = None
        if self.result is None:
            return
        self.hashes["result.json"] = artifacts.sha256_file(source.case_file(alias, "result.json"))
        normalized = source.case_file(alias, "normalized.jsonl")
        if normalized is not None:
            self.hashes["normalized.jsonl"] = artifacts.sha256_file(normalized)
            self.events = artifacts.load_jsonl(normalized, f"cases/{alias}/normalized.jsonl")
            for number, event in enumerate(self.events, start=1):
                if event.get("schema_version") != artifacts.SCHEMA_VERSION:
                    raise artifacts.ArtifactError(f"cases/{alias}/normalized.jsonl event {number} has unsupported schema_version {event.get('schema_version')!r}")
        evidence = source.case_file(alias, "evidence.json")
        if evidence is not None:
            self.hashes["evidence.json"] = artifacts.sha256_file(evidence)
            self.evidence = artifacts.load_json(evidence, f"cases/{alias}/evidence.json")
            artifacts.require_schema(self.evidence, f"cases/{alias}/evidence.json", artifacts.EVIDENCE_TYPE)


def replay_case(case: CaseInput, fixtures: Dict[str, dict], manifest: dict) -> dict:
    result = case.result or {}
    source_status = result.get("status", case.entry.get("status"))
    record = {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.REPLAY_CASE_TYPE,
        "case": case.alias, "fixture": result.get("fixture", case.entry.get("fixture")), "contracts": sorted(result.get("contracts") or []),
        "source_status": source_status, "replay_status": UNREPLAYABLE, "graded_status": None, "status_changed": None,
        "checks": [], "manual_review_required": bool(result.get("manual_review_required")), "manual_review": result.get("manual_review") or [],
        "semantic_review": "not replayed", "source_artifact_hashes": dict(sorted(case.hashes.items())),
        "snapshot_source": None, "legacy_snapshot_reconstruction": None, "turns_expected": None, "turns_completed": result.get("turns_completed"), "error": None,
    }

    def unreplayable(category: str, message: str) -> dict:
        record["error"] = {"category": category, "message": message}
        return record

    if case.result is None:
        return unreplayable("missing-evidence", f"source case has no result.json (source status {source_status})")
    entry = manifest["cases"].get(case.alias)
    if entry is None:
        return unreplayable("unknown-case", f"case alias {case.alias} is not in the current live manifest")
    if entry["fixture"] != record["fixture"]:
        return unreplayable("fixture-mismatch", f"source fixture {record['fixture']!r} differs from the current {case.alias} fixture {entry['fixture']!r}")
    if entry["grader"] not in cases_mod.GRADERS or entry["fixture"] not in fixtures:
        return unreplayable("unknown-grader", f"no current grader for {case.alias}")
    fixture = fixtures[entry["fixture"]]
    turns = len(fixture["turns"]) if "turns" in fixture else 1
    record["turns_expected"] = turns
    if source_status in INSUFFICIENT:
        return unreplayable("insufficient-evidence", f"source status {source_status} has no complete deterministic evidence")
    if source_status not in artifacts.GRADED_STATUSES:
        return unreplayable("insufficient-evidence", f"unknown source status {source_status!r}")
    if result.get("turns") != turns or result.get("turns_completed") != turns:
        return unreplayable("turn-mismatch", f"source recorded {result.get('turns_completed')!r} of {result.get('turns')!r} turns; current fixture has {turns}")
    if case.events is None:
        return unreplayable("missing-evidence", "normalized.jsonl is missing")
    if case.evidence is not None:
        before, after_turn, reason = artifacts.snapshots_from_evidence(case.evidence, case.alias, record["fixture"], turns)
        record.update(snapshot_source="evidence.json", legacy_snapshot_reconstruction=False)
    else:
        before, after_turn, reason = artifacts.legacy_snapshots(case.events, turns)
        record.update(snapshot_source="normalized.jsonl", legacy_snapshot_reconstruction=True)
    if reason:
        return unreplayable("snapshot", reason)
    # <project-root> holds shell metacharacters; restore an absolute path so recorded commands parse as they did live.
    project = artifacts.synthetic_project_root()
    events = artifacts.replace_text(case.events, artifacts.PLACEHOLDER, project)
    try:
        checks, status = artifacts.grade_recorded(entry["grader"], events, project, before, after_turn, turns)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return unreplayable("grader", f"current grader could not interpret the recorded evidence: {type(exc).__name__}")
    record.update(replay_status=REPLAYED, graded_status=status, status_changed=status != source_status,
                  checks=artifacts.replace_text(checks, project, artifacts.PLACEHOLDER))
    return record


def select(source: artifacts.Source, cases: Optional[List[str]], suite: Optional[str], manifest: dict) -> List[str]:
    if suite is not None:
        if suite not in manifest["suites"]:
            raise artifacts.ArtifactError(f"unknown suite in the current live manifest: {suite}")
        return list(manifest["suites"][suite])
    if cases:
        for alias in cases:
            if not artifacts.ALIAS.fullmatch(alias):
                raise artifacts.ArtifactError(f"invalid case alias: {alias}")
        return list(dict.fromkeys(cases))
    return source.aliases


def exit_code(records: List[dict]) -> int:
    """2 beats 1: an incomplete re-grade is never reported as a clean behavioral failure."""
    if any(record["replay_status"] != REPLAYED or record["graded_status"] not in ("AUTO_PASS", "AUTO_FAIL") for record in records):
        return 2
    if any(record["graded_status"] == "AUTO_FAIL" for record in records):
        return 1
    return 0


def render_report(summary: dict, records: List[dict]) -> str:
    source, current, counts = summary["source"], summary["current"], summary["counts"]
    lines = [
        f"# Mandala eval replay: {source['agent']} / {source['suite']}", "",
        f"- Replay: `{summary['replay_id']}` of run `{source['run_id']}`",
        f"- Source Skill: `{source['skill_git_sha']}`, SKILL.md `{source['skill_sha256']}`",
        f"- Current repository: `{current['git_sha']}`{' (uncommitted changes)' if current['dirty'] else ''}, SKILL.md `{current['skill_sha256']}`"
        f" ({'same' if current['skill_hashes_match'] else 'different'} SKILL.md)",
        "- Current grader identity: the file SHA-256 values in summary.json"
        + (" (the working tree is dirty, so the Git SHA alone does not identify the graders)" if current["dirty"] else ""),
        f"- Cases {counts['cases']}: replayed {counts['replayed']}, unreplayable {counts['unreplayable']}, status changed {counts['status_changed']}", "",
        "Replay re-grades recorded evidence with the current deterministic graders. It did not run an agent or Mandala CLI, "
        "and it does not re-grade final prose: manual response review is not replayed.", "",
    ]
    for record in records:
        change = "" if record["replay_status"] != REPLAYED else (" (CHANGED)" if record["status_changed"] else " (same)")
        lines += [f"## {record['case']} `{record['fixture']}`: {record['source_status']} -> {record['graded_status'] or record['replay_status']}{change}", ""]
        if record["error"]:
            lines += [f"UNREPLAYABLE ({record['error']['category']}): {record['error']['message']}", ""]
        else:
            lines += [f"Snapshots from `{record['snapshot_source']}`{' (legacy reconstruction)' if record['legacy_snapshot_reconstruction'] else ''}.", ""]
        if record["checks"]:
            lines += ["| Check | Contracts | Status | Evidence |", "| --- | --- | --- | --- |"]
            for item in record["checks"]:
                evidence = item["evidence"].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {item['id']} | {', '.join(item['contracts']) or '-'} | {item['status']} | {evidence} |")
            lines.append("")
        for note in record["manual_review"]:
            lines.append(f"- Manual review required, not replayed ({', '.join(note.get('contracts') or []) or 'task'}): {note.get('note', '')}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Re-grade recorded live-eval evidence offline with the current deterministic graders. Runs no agent and no Mandala CLI.")
    parser.add_argument("source", help="agent-level live run directory, e.g. .eval-live/<run-id>/codex")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--case", action="append", help="case alias to replay (repeatable)")
    group.add_argument("--suite", help="replay the members of a current live suite")
    parser.add_argument("--output-dir", help="empty directory for replay output (default .eval-live/replays/<replay-id>)")
    args = parser.parse_args(argv)
    started = artifacts.utc_now()
    try:
        fixtures, manifest = load_current()
        source = artifacts.Source(args.source, allowed=("live",))
        aliases = select(source, args.case, args.suite, manifest)
        recorded = set(source.aliases)
        inputs = {alias: CaseInput(source, alias) for alias in aliases if alias in recorded}
        replay_id = artifacts.new_id("replay", source.agent)
        target = Path(args.output_dir) if args.output_dir else artifacts.OUTPUT_ROOT / "replays" / replay_id
        output = artifacts.prepare_output_dir(target, [source.root])
    except (artifacts.ArtifactError, ValueError, OSError) as exc:
        print(f"eval-replay: {exc}", file=sys.stderr)
        return 2
    records = []
    for alias in aliases:
        if alias in inputs:
            record = replay_case(inputs[alias], fixtures, manifest)
        else:
            record = {"schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.REPLAY_CASE_TYPE, "case": alias,
                      "fixture": manifest["cases"].get(alias, {}).get("fixture"), "contracts": [], "source_status": None,
                      "replay_status": UNREPLAYABLE, "graded_status": None, "status_changed": None, "checks": [],
                      "manual_review_required": False, "manual_review": [], "semantic_review": "not replayed", "source_artifact_hashes": {},
                      "snapshot_source": None, "legacy_snapshot_reconstruction": None, "turns_expected": None, "turns_completed": None,
                      "error": {"category": "not-recorded", "message": f"case {alias} is not recorded in the source run"}}
        records.append(record)
        case_dir = output / "cases" / alias
        case_dir.mkdir(parents=True)
        artifacts.write_json(case_dir / "result.json", record)
    git_sha, dirty = artifacts.git_revision()
    current_skill = artifacts.sha256_file(ROOT / "src" / "mandala" / "SKILL.md")
    summary_source = source.summary
    graded: Dict[str, int] = {}
    for record in records:
        if record["graded_status"]:
            graded[record["graded_status"]] = graded.get(record["graded_status"], 0) + 1
    replayed = sum(1 for record in records if record["replay_status"] == REPLAYED)
    summary = {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.REPLAY_SUMMARY_TYPE, "replay_id": replay_id,
        "started_at": started, "finished_at": artifacts.utc_now(), "selection": {"cases": args.case, "suite": args.suite},
        "source": {
            "run_id": summary_source.get("run_id"), "agent": summary_source.get("agent"), "agent_version": summary_source.get("agent_version"),
            "model": summary_source.get("model"), "suite": summary_source.get("suite"), "complete": summary_source.get("complete") is True,
            "mandala_cli_version": summary_source.get("mandala_cli_version"), "skill_git_sha": summary_source.get("skill_git_sha"),
            "skill_dirty": summary_source.get("skill_dirty"), "skill_sha256": summary_source.get("skill_sha256"),
            "source_summary_sha256": artifacts.sha256_bytes(source.summary_bytes),
        },
        "current": {
            "git_sha": git_sha, "dirty": dirty, "skill_sha256": current_skill,
            "skill_hashes_match": current_skill == summary_source.get("skill_sha256"),
            "files": {name: artifacts.sha256_file(ROOT / name) for name in PROVENANCE_FILES},
        },
        "complete": summary_source.get("complete") is True and replayed == len(records),
        "counts": {"cases": len(records), "replayed": replayed, "unreplayable": len(records) - replayed,
                   "status_changed": sum(1 for record in records if record["status_changed"]), "graded": dict(sorted(graded.items()))},
        "cases": [{key: record[key] for key in ("case", "fixture", "source_status", "replay_status", "graded_status", "status_changed", "manual_review_required")}
                  for record in records],
    }
    artifacts.write_json(output / "summary.json", summary)
    (output / "report.md").write_text(render_report(summary, records), encoding="utf-8")
    code = exit_code(records)
    try:
        eval_coverage.write_own_coverage(output)
    except (artifacts.ArtifactError, OSError) as exc:
        print(f"eval-replay: coverage report failed: {exc}", file=sys.stderr)
        code = 2
    shown = output.relative_to(ROOT) if artifacts.is_within(output, ROOT) else output
    print(f"replay: {summary['counts']} -> {shown}/report.md")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
