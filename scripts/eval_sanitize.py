#!/usr/bin/env python3
"""Privacy-reduced share bundle for a live run or replay output directory.

The bundle is built from a whitelist of structured fields: manifest.json, summary.json,
coverage.json, coverage.md, report.md, and reduced cases/<alias>/result.json. Raw traces,
normalized traces, evidence snapshots, final responses, stderr, kept workdirs, check evidence
text, error messages, and session/thread IDs are never copied. Known local paths in retained
metadata are replaced with placeholders.

The sanitizer reduces known local identifiers and excludes high-risk artifacts. It does not
prove that the resulting bundle contains no sensitive information. Review the bundle before
sharing. Share bundles are for review/sharing, not deterministic re-grade (replayable: false).

Exit codes: 0 bundle written; 2 malformed, unsupported, or unsafe input/output.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import sys
from typing import Dict, List, Optional

if __package__:
    from . import eval_coverage
    from . import live_eval_artifacts as artifacts
else:
    import eval_coverage
    import live_eval_artifacts as artifacts

INCLUDED = ["manifest.json", "summary.json", "coverage.json", "coverage.md", "report.md", "cases/<alias>/result.json (reduced)"]
EXCLUDED = [
    "raw agent traces (raw-turn*.jsonl)", "normalized traces (normalized.jsonl)", "state evidence snapshots (evidence.json)",
    "final responses (final.txt)", "agent stderr (stderr.txt)", "kept workdirs", "agent session files", "global agent configuration",
    "check evidence text", "commands and command output", "error message text", "session and thread IDs", "preflight details",
    "manual review note text", "absolute local paths",
]
PLACEHOLDERS = ["<repo-root>", "<home>", "<source-run>", "<tmp>"]
NOTICE = ("The sanitizer reduces known local identifiers and excludes high-risk artifacts. It does not prove that the resulting "
          "bundle contains no sensitive information. Review the bundle before sharing.")
NOT_REPLAYABLE = "Sanitized share bundles are for review/sharing, not deterministic re-grade. Replay uses the original local artifacts."
LIVE_SUMMARY_FIELDS = ("run_id", "agent", "agent_version", "model", "suite", "complete", "started_at", "finished_at", "skill_git_sha", "skill_dirty",
                       "skill_sha256", "cli_contract_sha256", "mandala_cli_version", "counts", "manual_review_required")
REPLAY_SOURCE_FIELDS = ("run_id", "agent", "agent_version", "model", "suite", "complete", "mandala_cli_version", "skill_git_sha", "skill_dirty", "skill_sha256", "source_summary_sha256")
REPLAY_CURRENT_FIELDS = ("git_sha", "dirty", "skill_sha256", "skill_hashes_match")


def _scalar(value):
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def _counts(value) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): count for key, count in sorted(value.items()) if isinstance(count, int) and not isinstance(count, bool)}


def _error(value) -> Optional[dict]:
    return {"category": value["category"]} if isinstance(value, dict) and isinstance(value.get("category"), str) else None


def reduce_case(source: artifacts.Source, alias: str) -> Optional[dict]:
    result = source.case_result(alias)
    if result is None:
        return None
    reduced = {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.SANITIZED_TYPE, "part": "case-result",
        "case": alias, "fixture": _scalar(result.get("fixture")), "contracts": sorted(item for item in result.get("contracts") or [] if isinstance(item, str)),
        "manual_review_required": result.get("manual_review_required") is True,
        "manual_review": [{"contracts": sorted(cid for cid in note.get("contracts") or [] if isinstance(cid, str))}
                          for note in result.get("manual_review") or [] if isinstance(note, dict)],
        "checks": [{"id": _scalar(item.get("id")), "contracts": sorted(cid for cid in item.get("contracts") or [] if isinstance(cid, str)), "status": _scalar(item.get("status"))}
                   for item in result.get("checks") or [] if isinstance(item, dict)],
        "error": _error(result.get("error")),
    }
    if source.kind == "replay":
        for key in ("source_status", "replay_status", "graded_status", "status_changed", "semantic_review"):
            reduced[key] = _scalar(result.get(key))
    else:
        reduced["status"] = _scalar(result.get("status"))
    return reduced


def reduce_summary(source: artifacts.Source, cases: List[dict]) -> dict:
    summary = source.summary
    reduced = {"schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.SANITIZED_TYPE, "part": "summary", "source_kind": source.kind}
    if source.kind == "live":
        for key in LIVE_SUMMARY_FIELDS:
            reduced[key] = _counts(summary.get(key)) if key == "counts" else _scalar(summary.get(key))
    else:
        reduced["replay_id"] = _scalar(summary.get("replay_id"))
        for key in ("started_at", "finished_at", "complete"):
            reduced[key] = _scalar(summary.get(key))
        reduced["source"] = {key: _scalar((summary.get("source") or {}).get(key)) for key in REPLAY_SOURCE_FIELDS}
        reduced["current"] = {key: _scalar((summary.get("current") or {}).get(key)) for key in REPLAY_CURRENT_FIELDS}
        counts = summary.get("counts") or {}
        reduced["counts"] = {**_counts({key: value for key, value in counts.items() if key != "graded"}), "graded": _counts(counts.get("graded"))}
    status_keys = ("source_status", "replay_status", "graded_status", "status_changed") if source.kind == "replay" else ("status",)
    reduced["cases"] = []
    for alias in source.aliases:
        entry = source.case_entry(alias)
        case = next((item for item in cases if item["case"] == alias), None)
        row = {"case": alias, "fixture": _scalar((case or entry).get("fixture"))}
        for key in status_keys:
            row[key] = _scalar((case or entry).get(key))
        row["error"] = _error((case or entry).get("error"))
        reduced["cases"].append(row)
    return reduced


def render_report(summary: dict, cases: List[dict], coverage: dict) -> str:
    """Report built from sanitized data only; the source report is never copied."""
    lines = []
    if summary["source_kind"] == "live":
        lines += [f"# Mandala live evaluation (sanitized): {summary['agent']} / {summary['suite']}", "",
                  f"- Run: `{summary['run_id']}`{'' if summary['complete'] else ' (INCOMPLETE)'}",
                  f"- Agent: {summary['agent']} {summary.get('agent_version') or '(version unknown)'}; model: {summary.get('model') or 'not observable'}",
                  f"- Skill revision: `{summary['skill_git_sha']}`; SKILL.md SHA-256 `{summary['skill_sha256']}`",
                  f"- Mandala CLI: {summary.get('mandala_cli_version') or 'unavailable'}",
                  "- Status counts: " + (", ".join(f"{key} {value}" for key, value in summary["counts"].items()) or "none"),
                  f"- Cases needing manual response review: {summary['manual_review_required']}", ""]
    else:
        source, current = summary["source"], summary["current"]
        lines += [f"# Mandala eval replay (sanitized): {source['agent']} / {source['suite']}", "",
                  f"- Replay: `{summary['replay_id']}` of run `{source['run_id']}`",
                  f"- Source SKILL.md `{source['skill_sha256']}`; current SKILL.md `{current['skill_sha256']}` at `{current['git_sha']}`",
                  "- Counts: " + ", ".join(f"{key} {value}" for key, value in summary["counts"].items()), ""]
    lines += ["AUTO_PASS covers automated trace/state checks only; manual response review may still be required. "
              "This bundle omits traces, responses, evidence text, and error messages.", "", NOTICE, "", NOT_REPLAYABLE, ""]
    for case in cases:
        status = f"{case['source_status']} -> {case['graded_status'] or case['replay_status']}" if summary["source_kind"] == "replay" else case["status"]
        lines += [f"## {case['case']} `{case['fixture']}`: {status}", ""]
        if case["error"]:
            lines += [f"Error category: {case['error']['category']}", ""]
        if case["checks"]:
            lines += ["| Check | Contracts | Status |", "| --- | --- | --- |"]
            lines += [f"| {item['id']} | {', '.join(item['contracts']) or '-'} | {item['status']} |" for item in case["checks"]]
            lines.append("")
        if case["manual_review_required"]:
            lines += ["- Manual review required (not recorded here as a result).", ""]
    lines += ["## Contract coverage", "", "- " + ", ".join(f"{state} {count}" for state, count in coverage["counts"].items()), "- See coverage.md.", ""]
    return "\n".join(lines)


def sanitize(source: artifacts.Source, output: Path) -> dict:
    locations = artifacts.known_locations(source.root)
    coverage = eval_coverage.build_coverage(source)
    cases = [case for case in (reduce_case(source, alias) for alias in source.aliases) if case is not None]
    summary = reduce_summary(source, cases)
    hashes = {"summary.json": artifacts.sha256_bytes(source.summary_bytes)}
    for alias in source.aliases:
        path = source.case_file(alias, "result.json")
        if path is not None:
            hashes[f"cases/{alias}/result.json"] = artifacts.sha256_file(path)
    manifest = {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.SANITIZED_TYPE, "part": "manifest", "created_at": artifacts.utc_now(),
        "source_kind": source.kind, "source_run_id": _scalar(source.summary.get("run_id")) if source.kind == "live" else _scalar((source.summary.get("source") or {}).get("run_id")),
        "source_replay_id": _scalar(source.summary.get("replay_id")) if source.kind == "replay" else None,
        "source_agent": _scalar(source.agent), "source_suite": _scalar(source.suite),
        "included_artifacts": INCLUDED, "excluded_artifact_classes": EXCLUDED,
        "redaction_policy": {"method": "whitelist of structured fields", "path_placeholders": PLACEHOLDERS,
                             "identifiers": "session and thread IDs are removed, not pseudonymized", "secret_scanning": False, "notice": NOTICE},
        "source_structured_artifact_hashes": dict(sorted(hashes.items())),
        "replayable": False, "replay_note": NOT_REPLAYABLE,
    }
    summary, cases, coverage, manifest = (artifacts.redact(item, locations) for item in (summary, cases, coverage, manifest))
    artifacts.write_json(output / "manifest.json", manifest)
    artifacts.write_json(output / "summary.json", summary)
    eval_coverage.write_coverage(output, coverage)
    (output / "report.md").write_text(artifacts.redact(render_report(summary, cases, coverage), locations), encoding="utf-8")
    for case in cases:
        (output / "cases" / case["case"]).mkdir(parents=True)
        artifacts.write_json(output / "cases" / case["case"] / "result.json", case)
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write a privacy-reduced, non-replayable share bundle from a live run or replay output directory. "
                                                 "This is not a secret scanner; review the bundle before sharing.")
    parser.add_argument("source", help="agent-level live run directory or replay output directory")
    parser.add_argument("--output-dir", required=True, help="empty destination directory (inside this repository it must be under .eval-live/)")
    args = parser.parse_args(argv)
    try:
        source = artifacts.Source(args.source)
        eval_coverage.build_coverage(source)  # validate before creating the output directory
        output = artifacts.prepare_output_dir(Path(args.output_dir), [source.root])
        sanitize(source, output)
    except (artifacts.ArtifactError, OSError) as exc:
        print(f"eval-sanitize: {exc}", file=sys.stderr)
        return 2
    print(f"sanitized bundle -> {output}\n{NOTICE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
