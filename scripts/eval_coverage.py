#!/usr/bin/env python3
"""Detailed safety-contract coverage for a live run or replay output directory.

Recomputed from case result artifacts and tests/evals/contracts.json; a summary's own
coverage field is never trusted. This is not a pass rate: it says, per contract, whether it
was only in declared fixture scope, observed by automated PASS/FAIL checks, only reached
by UNOBSERVABLE checks, or still requires manual response review.

Exit codes: 0 report generated; 2 malformed or unsupported input. Behavioral failures are
reported data, never an exit status here.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import re
import sys
from typing import Dict, List, Optional

if __package__:
    from . import live_eval_artifacts as artifacts
    from . import validate
else:
    import live_eval_artifacts as artifacts
    import validate

STATES = ("AUTOMATED_OBSERVED", "UNOBSERVABLE_ONLY", "MANUAL_REQUIRED_ONLY", "FIXTURE_ONLY", "NOT_EXERCISED")
SEMANTICS = {
    "fixture_referenced_by": "Declared evaluation scope of a case fixture. It is not evidence that the contract was exercised or satisfied.",
    "automatic_pass_checks": "Automated deterministic checks that observed the contract and passed.",
    "automatic_fail_checks": "Automated deterministic checks that observed the contract and failed. FAIL is observed evidence, not success.",
    "automatic_unobservable_checks": "Automated checks that mapped to the contract but could not observe or attribute the evidence.",
    "manual_review_required_by": "Cases whose recorded response still requires manual review for this contract. Manual review required is not a manual review result; the harness records no reviewer verdict.",
    "coverage_state": "One convenience label, by precedence AUTOMATED_OBSERVED > UNOBSERVABLE_ONLY > MANUAL_REQUIRED_ONLY > FIXTURE_ONLY > NOT_EXERCISED. It is not a pass rate.",
}

PLAIN_AGENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def default_output_agent(agent) -> str:
    return agent if isinstance(agent, str) and PLAIN_AGENT.fullmatch(agent) else "unknown"


def _string_list(value, label: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise artifacts.ArtifactError(f"{label} is not a list of strings")
    return value


def _checks(value, label: str) -> List[dict]:
    if not isinstance(value, list):
        raise artifacts.ArtifactError(f"{label} checks is not a list")
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("status") not in artifacts.CHECK_STATUSES:
            raise artifacts.ArtifactError(f"{label} has a malformed check: {item!r:.80}")
        _string_list(item.get("contracts"), f"{label} check {item['id']} contracts")
    return value


def _manual(value, label: str) -> List[dict]:
    if not isinstance(value, list):
        raise artifacts.ArtifactError(f"{label} manual_review is not a list")
    for note in value:
        if not isinstance(note, dict):
            raise artifacts.ArtifactError(f"{label} has a malformed manual review entry")
        _string_list(note.get("contracts"), f"{label} manual review contracts")
    return value


def case_rows(source: artifacts.Source) -> List[dict]:
    """Per-case evidence extracted from case artifacts.

    Only completed (live) or replayed (replay) cases contribute checks and manual-review
    requirements; incomplete or environment cases keep their fixture scope only.
    """
    rows = []
    for alias in source.aliases:
        entry = source.case_entry(alias)
        label = f"cases/{alias}/result.json"
        result = source.case_result(alias)
        if result is None:
            if source.kind == "live" and entry.get("status") == "NOT_RUN":
                rows.append({"case": alias, "fixture": entry.get("fixture"), "status": "NOT_RUN", "scope": [], "checks": [], "manual": [], "result_artifact": False})
                continue
            raise artifacts.ArtifactError(f"{label} is missing for a case listed in summary.json")
        scope = _string_list(result.get("contracts"), f"{label} contracts")
        if source.kind == "live":
            status = result.get("status")
            if status not in artifacts.LIVE_STATUSES:
                raise artifacts.ArtifactError(f"{label} has unknown status {status!r}")
            evidence = status in artifacts.GRADED_STATUSES
        else:
            replay_status = result.get("replay_status")
            if replay_status not in ("REPLAYED", "UNREPLAYABLE"):
                raise artifacts.ArtifactError(f"{label} has unknown replay_status {replay_status!r}")
            evidence = replay_status == "REPLAYED"
            status = result.get("graded_status") if evidence else "UNREPLAYABLE"
            if evidence and status not in artifacts.GRADED_STATUSES:
                raise artifacts.ArtifactError(f"{label} has unknown graded_status {status!r}")
        checks = _checks(result.get("checks"), label)
        manual = _manual(result.get("manual_review", []), label)
        rows.append({"case": alias, "fixture": result.get("fixture"), "status": status, "scope": scope, "checks": checks if evidence else [],
                     "manual": manual if evidence else [], "result_artifact": True})
    return rows


def build_coverage(source: artifacts.Source, contracts: Optional[Dict[str, dict]] = None) -> dict:
    contracts = validate.load_contracts() if contracts is None else contracts
    rows = case_rows(source)
    referenced = set()
    for row in rows:
        referenced.update(row["scope"])
        for item in row["checks"]:
            referenced.update(item["contracts"])
        for note in row["manual"]:
            referenced.update(note["contracts"])
    unknown = sorted(referenced - set(contracts))
    if unknown:
        raise artifacts.ArtifactError(f"unknown safety contract IDs in artifacts: {unknown}")
    table = []
    for cid in sorted(contracts):
        def checks_with(status: str) -> List[dict]:
            return sorted(({"case": row["case"], "check": item["id"]} for row in rows for item in row["checks"] if item["status"] == status and cid in item["contracts"]),
                          key=lambda hit: (hit["case"], hit["check"]))
        entry = {
            "id": cid, "slug": contracts[cid]["slug"], "area": contracts[cid]["area"],
            "fixture_referenced_by": sorted({row["case"] for row in rows if cid in row["scope"]}),
            "automatic_pass_checks": checks_with(artifacts.cases_mod.PASS),
            "automatic_fail_checks": checks_with(artifacts.cases_mod.FAIL),
            "automatic_unobservable_checks": checks_with(artifacts.cases_mod.UNOBSERVABLE),
            "manual_review_required_by": sorted({row["case"] for row in rows for note in row["manual"] if cid in note["contracts"]}),
        }
        entry["coverage_state"] = coverage_state(entry)
        table.append(entry)
    counts = {state: sum(1 for entry in table if entry["coverage_state"] == state) for state in STATES}
    return {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.COVERAGE_TYPE, "generated_at": artifacts.utc_now(),
        "source": {"kind": source.kind, "id": source.run_id, "agent": source.agent, "suite": source.suite, "complete": source.summary.get("complete") is True,
                   "cases": len(rows), "cases_with_results": sum(1 for row in rows if row["result_artifact"])},
        "semantics": SEMANTICS, "counts": counts, "unknown_contract_ids": unknown, "contracts": table,
        "cases": [{"case": row["case"], "fixture": row["fixture"], "status": row["status"], "result_artifact": row["result_artifact"],
                   "evidence_counted": bool(row["checks"] or row["manual"])} for row in sorted(rows, key=lambda row: row["case"])],
    }


def coverage_state(entry: dict) -> str:
    if entry["automatic_pass_checks"] or entry["automatic_fail_checks"]:
        return "AUTOMATED_OBSERVED"
    if entry["automatic_unobservable_checks"]:
        return "UNOBSERVABLE_ONLY"
    if entry["manual_review_required_by"]:
        return "MANUAL_REQUIRED_ONLY"
    if entry["fixture_referenced_by"]:
        return "FIXTURE_ONLY"
    return "NOT_EXERCISED"


def render_coverage(coverage: dict) -> str:
    source = coverage["source"]

    def hits(items: List[dict]) -> str:
        return ", ".join(f"{hit['case']}:{hit['check']}" for hit in items) or "-"

    lines = [
        f"# Safety contract coverage: {source['agent'] or 'unknown agent'} / {source['suite'] or 'unknown suite'}", "",
        f"- Source: {source['kind']} `{source['id']}`{'' if source['complete'] else ' (INCOMPLETE)'}",
        f"- Cases: {source['cases']} ({source['cases_with_results']} with result artifacts)",
        "- " + ", ".join(f"{state} {coverage['counts'][state]}" for state in STATES), "",
        "This is not a pass rate and not a safety claim.", "",
        "- Fixture scope is declared evaluation scope, not observed evidence.",
        "- Automated PASS and FAIL both count as observed deterministic evidence; they are listed separately and FAIL is not success.",
        "- UNOBSERVABLE checks mapped to the contract but could not observe or attribute the evidence.",
        "- Manual review required is not manual review passed; the harness records no reviewer verdict.",
        "- Environment, unsupported, not-run, and unreplayable cases contribute fixture scope only.", "",
        "| Contract | Area | Coverage state | Fixture scope | Automated PASS | Automated FAIL | Automated UNOBSERVABLE | Manual review required |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in coverage["contracts"]:
        lines.append(f"| {entry['id']} ({entry['slug']}) | {entry['area']} | {entry['coverage_state']} | {', '.join(entry['fixture_referenced_by']) or '-'} | "
                     f"{hits(entry['automatic_pass_checks'])} | {hits(entry['automatic_fail_checks'])} | {hits(entry['automatic_unobservable_checks'])} | "
                     f"{', '.join(entry['manual_review_required_by']) or '-'} |")
    lines += ["", "## Cases", "", "| Case | Fixture | Status | Evidence counted |", "| --- | --- | --- | --- |"]
    for case in coverage["cases"]:
        lines.append(f"| {case['case']} | `{case['fixture']}` | {case['status']} | {'yes' if case['evidence_counted'] else 'scope only'} |")
    return "\n".join(lines) + "\n"


def write_coverage(directory: Path, coverage: dict) -> None:
    artifacts.write_json(directory / "coverage.json", coverage)
    (directory / "coverage.md").write_text(render_coverage(coverage), encoding="utf-8")


def write_own_coverage(run_dir: Path) -> dict:
    """Producer path: recompute coverage from a run/replay directory this process just wrote, and store it there."""
    coverage = build_coverage(artifacts.Source(str(run_dir)))
    write_coverage(run_dir, coverage)
    return coverage


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Detailed safety-contract coverage for a live agent run directory or replay output directory (offline).")
    parser.add_argument("source", help="agent-level live run directory (.eval-live/<run>/<agent>) or replay output directory")
    parser.add_argument("--output-dir", help="empty directory for coverage.json and coverage.md (default .eval-live/coverage/<id>)")
    args = parser.parse_args(argv)
    try:
        source = artifacts.Source(args.source)
        coverage = build_coverage(source)
        target = Path(args.output_dir) if args.output_dir else artifacts.OUTPUT_ROOT / "coverage" / artifacts.new_id("coverage", default_output_agent(source.agent))
        output = artifacts.prepare_output_dir(target, [source.root])
        write_coverage(output, coverage)
    except (artifacts.ArtifactError, OSError) as exc:
        print(f"eval-coverage: {exc}", file=sys.stderr)
        return 2
    print("coverage: " + ", ".join(f"{state} {coverage['counts'][state]}" for state in STATES) + f" -> {output / 'coverage.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
