#!/usr/bin/env python3
"""Real-world coverage profile view over an existing live run or replay output directory.

A profile is a curated, post-hoc applicability view: for one practical Mandala workflow it
classifies each release-suite case as CORE, CONDITIONAL, or NOT_APPLICABLE
(tests/evals/profiles.json), derives which safety contracts are in that profile's scope from
current fixture declarations, and filters existing recomputed coverage evidence by that scope.
It is not a pass rate, a score, a release gate, a live suite, or proof of verification.

CORE and CONDITIONAL evidence are computed separately; conditional evidence never fills in
missing CORE evidence, and NOT_APPLICABLE cases contribute nothing. Nothing is executed:
no agent, no Mandala CLI, no recorded command, no network access.

Exit codes: 0 report or listing generated; 2 malformed catalog, unknown profile, malformed,
unsupported, or unsafe source/output, or fixture mismatch. Behavioral failures recorded in
the source are reported data, never an exit status here.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import copy
import re
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

APPLICABILITY = validate.APPLICABILITY
STATES = eval_coverage.STATES
NOT_PRESENT = "NOT_PRESENT"
# The source agent name comes from untrusted summary.json; only a plain name may appear in a default output path.
PLAIN_AGENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SEMANTICS = {
    "profile": "A curated applicability view over recorded evaluation evidence. It is not a pass rate, safety score, release gate, or proof of verification, and it does not replace the release suite.",
    "CORE": "The case represents behavior intrinsic to the normal workflow of this profile. CORE is workflow applicability, not severity, importance, success, or verification.",
    "CONDITIONAL": "The case is relevant only when its documented condition occurs. Conditional evidence is reported separately and never satisfies missing CORE evidence.",
    "NOT_APPLICABLE": "The case is outside this curated profile view only. It is not globally irrelevant, and its evidence is not counted in this profile.",
    "contract_applicability": "Derived from profile case applicability and current fixture contract declarations (CORE > CONDITIONAL > NOT_APPLICABLE). It is profile scope, not evidence; recorded checks never change it.",
    "core_evidence": "Recomputed coverage evidence restricted to this contract's CORE cases.",
    "conditional_evidence": "Recomputed coverage evidence restricted to this contract's CONDITIONAL cases.",
    "coverage_state": eval_coverage.SEMANTICS["coverage_state"] + " AUTOMATED_OBSERVED includes automated FAIL evidence; observed is not successful.",
    "manual_review_required_by": eval_coverage.SEMANTICS["manual_review_required_by"],
    "source_status": f"The status recorded in the source, or {NOT_PRESENT} when the source does not contain the profile case. Missing cases are never synthesized.",
    "evidence_counted": "Whether the source recorded counted checks or manual-review requirements for this CORE or CONDITIONAL case. Its evidence counts only for contracts its current fixture declares; NOT_APPLICABLE cases never count.",
    "core_evidence_counts": "Evidence states of core_evidence over contracts whose applicability is CORE. Not a score.",
    "conditional_evidence_counts": "Evidence states of conditional_evidence over contracts whose applicability is CONDITIONAL. Conditional evidence for a CORE contract is shown only in that contract's row. Not a score.",
    "unprofiled_source_cases": "Source case aliases that are not classified by this profile. They are listed and ignored, not counted.",
}


def default_output_agent(agent) -> str:
    return agent if isinstance(agent, str) and PLAIN_AGENT.fullmatch(agent) else "unknown"


def profiled_view(source: artifacts.Source, aliases: List[str]) -> artifacts.Source:
    """The already-validated source restricted to profiled aliases; unprofiled case artifacts are never read or counted."""
    view = copy.copy(source)
    view.summary = dict(source.summary, cases=[entry for entry in source.summary["cases"] if entry["case"] in aliases])
    return view


def evidence_bucket(entry: dict, cases: List[str]) -> dict:
    """One contract's base coverage entry filtered to the given profile cases."""
    allowed = set(cases)
    bucket = {
        "fixture_referenced_by": [case for case in entry["fixture_referenced_by"] if case in allowed],
        "automatic_pass_checks": [hit for hit in entry["automatic_pass_checks"] if hit["case"] in allowed],
        "automatic_fail_checks": [hit for hit in entry["automatic_fail_checks"] if hit["case"] in allowed],
        "automatic_unobservable_checks": [hit for hit in entry["automatic_unobservable_checks"] if hit["case"] in allowed],
        "manual_review_required_by": [case for case in entry["manual_review_required_by"] if case in allowed],
    }
    bucket["coverage_state"] = eval_coverage.coverage_state(bucket)
    return bucket


def build_profile_report(source: artifacts.Source, profile: dict, manifest: dict, cases: List[dict],
                         contracts: Optional[Dict[str, dict]] = None) -> dict:
    contracts = validate.SAFETY_CONTRACTS if contracts is None else contracts
    profiled = [entry["case"] for entry in profile["cases"]]
    base = eval_coverage.build_coverage(profiled_view(source, profiled), contracts)
    base_cases = {row["case"]: row for row in base["cases"]}
    expected = validate.release_fixture_contracts(manifest, cases)
    for alias in profiled:
        if alias not in base_cases:
            continue
        entry = source.case_entry(alias)
        recorded = [base_cases[alias]["fixture"]] + ([entry["fixture"]] if "fixture" in entry else [])
        for fixture in recorded:
            if fixture != expected[alias][0]:
                raise artifacts.ArtifactError(f"source case {alias} records fixture {fixture!r}; the current profile catalog maps {alias} to {expected[alias][0]!r}")
    applicability = {entry["case"]: entry["applicability"] for entry in profile["cases"]}
    case_rows = []
    for entry in profile["cases"]:
        alias = entry["case"]
        recorded = base_cases.get(alias)
        case_rows.append({
            "case": alias, "fixture": expected[alias][0], "applicability": entry["applicability"], "condition": entry["condition"], "rationale": entry["rationale"],
            "source_present": recorded is not None,
            "source_status": recorded["status"] if recorded is not None else NOT_PRESENT,
            "result_artifact": bool(recorded and recorded["result_artifact"]),
            "evidence_counted": bool(recorded and recorded["evidence_counted"]) and entry["applicability"] != "NOT_APPLICABLE",
        })
    derived = validate.derive_contract_applicability(profile, manifest, cases, contracts)
    base_rows = {row["id"]: row for row in base["contracts"]}
    contract_rows = []
    for cid in sorted(derived):
        scope = derived[cid]
        contract_rows.append({
            "id": cid, "slug": contracts[cid]["slug"], "area": contracts[cid]["area"], "applicability": scope["applicability"],
            "core_cases": list(scope["core_cases"]), "conditional_cases": list(scope["conditional_cases"]),
            "core_evidence": evidence_bucket(base_rows[cid], scope["core_cases"]),
            "conditional_evidence": evidence_bucket(base_rows[cid], scope["conditional_cases"]),
        })
    return {
        "schema_version": artifacts.SCHEMA_VERSION, "artifact_type": artifacts.PROFILE_REPORT_TYPE, "generated_at": artifacts.utc_now(),
        "profile": {"id": profile["id"], "title": profile["title"], "description": profile["description"]},
        "source": {key: base["source"][key] for key in ("kind", "id", "agent", "suite", "complete")},
        "semantics": SEMANTICS,
        "case_applicability_counts": {value: sum(1 for alias in profiled if applicability[alias] == value) for value in APPLICABILITY},
        "contract_applicability_counts": {value: sum(1 for row in contract_rows if row["applicability"] == value) for value in APPLICABILITY},
        "core_evidence_counts": {state: sum(1 for row in contract_rows if row["applicability"] == "CORE" and row["core_evidence"]["coverage_state"] == state) for state in STATES},
        "conditional_evidence_counts": {state: sum(1 for row in contract_rows if row["applicability"] == "CONDITIONAL" and row["conditional_evidence"]["coverage_state"] == state) for state in STATES},
        "unprofiled_source_cases": sorted(alias for alias in source.aliases if alias not in applicability),
        "cases": case_rows,
        "contracts": contract_rows,
    }


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("`", "'").replace("\r", " ").replace("\n", " ")


def render_profile(report: dict) -> str:
    profile, source = report["profile"], report["source"]

    def hits(items: List[dict]) -> str:
        return _cell(", ".join(f"{hit['case']}:{hit['check']}" for hit in items) or "-")

    def bucket(evidence: dict, cases: List[str]) -> str:
        if not cases:
            return "-"
        return (f"{evidence['coverage_state']} (PASS {hits(evidence['automatic_pass_checks'])}; FAIL {hits(evidence['automatic_fail_checks'])}; "
                f"UNOBSERVABLE {hits(evidence['automatic_unobservable_checks'])}; manual review required {', '.join(evidence['manual_review_required_by']) or '-'})")

    def counts(values: Dict[str, int]) -> str:
        return ", ".join(f"{key} {count}" for key, count in values.items())

    lines = [
        f"# Coverage profile: {profile['title']} (`{profile['id']}`)", "",
        "This is a curated applicability view over recorded evaluation evidence.",
        "It is not a pass rate, safety score, release gate, or proof of verification.", "",
        "- CORE is workflow applicability, not success, severity, or importance.",
        "- CONDITIONAL evidence is shown separately and does not satisfy missing CORE evidence.",
        "- NOT_APPLICABLE applies only to this profile; its cases contribute no evidence here and are not globally irrelevant.",
        "- Manual review required is not manual review passed; the harness records no reviewer verdict.",
        "- Automated FAIL is observed evidence, not success. AUTOMATED_OBSERVED includes FAIL.",
        "- Contract applicability is derived from current fixture declarations, not from recorded evidence.",
        "- A profile does not replace the release suite.", "",
        "## Profile", "",
        f"- ID: `{profile['id']}`",
        f"- Title: {profile['title']}",
        f"- Description: {profile['description']}", "",
        "## Source", "",
        f"- Kind: {source['kind']}",
        f"- ID: `{_cell(source['id'])}`",
        f"- Agent: {_cell(source['agent'] or 'unknown')}",
        f"- Suite: {_cell(source['suite'] or 'unknown')}",
        f"- Complete: {'yes' if source['complete'] else 'no (INCOMPLETE)'}", "",
        "## Summary", "",
        f"- Case applicability: {counts(report['case_applicability_counts'])}",
        f"- Contract applicability: {counts(report['contract_applicability_counts'])}",
        f"- CORE contract evidence states (core evidence of CORE contracts): {counts(report['core_evidence_counts'])}",
        f"- CONDITIONAL contract evidence states (conditional evidence of CONDITIONAL contracts): {counts(report['conditional_evidence_counts'])}",
        "- Conditional evidence for a CORE contract appears only in that contract's row and never changes its CORE evidence state.", "",
        "A NOT_EXERCISED count of 0 is not success: FIXTURE_ONLY, MANUAL_REQUIRED_ONLY, and UNOBSERVABLE_ONLY contracts were not observed by automated checks.", "",
        "## Cases", "",
        "| Case | Fixture | Applicability | Condition | Source status | Evidence counted | Rationale |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        counted = "yes" if case["evidence_counted"] else ("not counted (N/A)" if case["applicability"] == "NOT_APPLICABLE" and case["source_present"] else "no")
        lines.append(f"| {case['case']} | `{case['fixture']}` | {case['applicability']} | {_cell(case['condition'] or '-')} | "
                     f"{case['source_status']} | {counted} | {_cell(case['rationale'])} |")
    lines += ["", "## Contracts", "",
              "| Contract | Area | Applicability | CORE cases | CORE evidence | CONDITIONAL cases | CONDITIONAL evidence |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["contracts"]:
        lines.append(f"| {row['id']} ({row['slug']}) | {row['area']} | {row['applicability']} | {', '.join(row['core_cases']) or '-'} | "
                     f"{bucket(row['core_evidence'], row['core_cases'])} | {', '.join(row['conditional_cases']) or '-'} | "
                     f"{bucket(row['conditional_evidence'], row['conditional_cases'])} |")
    if report["unprofiled_source_cases"]:
        lines += ["", "## Unprofiled source cases", "",
                  "Listed and ignored; this profile does not classify them and counts none of their evidence.", "",
                  ", ".join(f"`{alias}`" for alias in report["unprofiled_source_cases"])]
    return "\n".join(lines) + "\n"


def write_profile(directory: Path, report: dict) -> None:
    artifacts.write_json(directory / "profile.json", report)
    (directory / "profile.md").write_text(render_profile(report), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Real-world coverage profile view over an existing live agent run directory or replay output directory (offline; runs nothing).")
    parser.add_argument("source", nargs="?", help="agent-level live run directory (.eval-live/<run>/<agent>) or replay output directory")
    parser.add_argument("--profile", help="profile ID from tests/evals/profiles.json")
    parser.add_argument("--output-dir", help="empty directory for profile.json and profile.md (default .eval-live/profiles/<id>)")
    parser.add_argument("--list-profiles", action="store_true", help="list profile IDs and titles and exit; needs no source")
    args = parser.parse_args(argv)
    if args.list_profiles:
        if args.source or args.profile or args.output_dir:
            parser.error("--list-profiles takes no source, --profile, or --output-dir")
    elif not args.source or not args.profile:
        parser.error("a source directory and --profile are required (or use --list-profiles)")
    try:
        profiles, manifest, cases = validate.load_profile_catalog()
    except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
        print(f"eval-profile: invalid profile catalog: {exc}", file=sys.stderr)
        return 2
    if args.list_profiles:
        for pid in sorted(profiles):
            print(f"{pid}\t{profiles[pid]['title']}")
        return 0
    if args.profile not in profiles:
        print(f"eval-profile: unknown profile {args.profile!r}; known: {', '.join(sorted(profiles))}", file=sys.stderr)
        return 2
    try:
        source = artifacts.Source(args.source)
        report = build_profile_report(source, profiles[args.profile], manifest, cases)
        target = Path(args.output_dir) if args.output_dir else artifacts.OUTPUT_ROOT / "profiles" / artifacts.new_id("profile", default_output_agent(source.agent))
        output = artifacts.prepare_output_dir(target, [source.root])
        write_profile(output, report)
    except (artifacts.ArtifactError, OSError) as exc:
        print(f"eval-profile: {exc}", file=sys.stderr)
        return 2
    print(f"profile {args.profile}: contracts " + ", ".join(f"{key} {count}" for key, count in report["contract_applicability_counts"].items())
          + f" -> {output / 'profile.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
