#!/usr/bin/env python3
"""Local live-agent evaluation harness for the Mandala Skill.

Runs selected behavioral fixtures against Codex or Claude Code in disposable projects,
grades command traces and Mandala state deterministically, and writes artifacts under
.eval-live/. It never runs in CI, never installs anything, and never edits the Skill.

Exit codes: 0 all requested cases AUTO_PASS; 1 at least one AUTO_FAIL;
2 preflight, configuration, adapter, or environment problems (or inconclusive evidence).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

if __package__:
    from . import eval_coverage
    from . import live_eval_artifacts as artifacts
    from . import live_eval_cases as cases_mod
    from . import validate
    from .live_eval_adapters import ADAPTERS, run_process, sanitized_env
else:
    import eval_coverage
    import live_eval_artifacts as artifacts
    import live_eval_cases as cases_mod
    import validate
    from live_eval_adapters import ADAPTERS, run_process, sanitized_env

ROOT = validate.ROOT
SCHEMA_VERSION = 1
CANONICAL = ROOT / "src" / "mandala"
GENERATED = ROOT / "dist" / "mandala"
OUTPUT_ROOT = ROOT / ".eval-live"
REQUIRED_CLI = "mandala v0.4.0"
DEFAULT_TIMEOUT = 300.0
PLACEHOLDER = "<project-root>"


def load_inputs() -> tuple:
    contracts = validate.load_contracts()
    behavior = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
    validate.validate_eval_metadata(behavior, contracts)
    manifest = validate.validate_live_suites(json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8")), behavior)
    return contracts, {case["id"]: case for case in behavior}, manifest


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_quiet(argv: List[str], env: Dict[str, str], cwd: Path = ROOT, timeout: float = 60) -> tuple:
    try:
        result = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)


# ---------------------------------------------------------------------------
# Preflight


def preflight(adapter, env: Dict[str, str], multi_turn: bool, allow_global_skill: bool) -> dict:
    items = []

    def add(name: str, ok: bool, detail: str) -> None:
        items.append({"check": name, "ok": ok, "detail": detail})

    try:
        same = validate.package_files(CANONICAL) == validate.package_files(GENERATED)
        add("generated-skill", same, "dist/mandala matches src/mandala" if same else "dist/mandala differs from src/mandala; run make build")
    except (ValueError, OSError, UnicodeError) as exc:
        add("generated-skill", False, f"{exc}; run make build")
    mandala = shutil.which("mandala", path=env.get("PATH"))
    add("mandala-on-path", mandala is not None, "mandala found on PATH" if mandala else "mandala not found on PATH (install is not automatic)")
    version = None
    if mandala:
        code, stdout, _ = run_quiet(["mandala", "--version"], env)
        version = stdout.strip() if code == 0 else None
        add("mandala-version", version == REQUIRED_CLI, f"mandala --version: {version!r} (required {REQUIRED_CLI!r})")
    executable = shutil.which(adapter.executable, path=env.get("PATH"))
    add("agent-executable", executable is not None, f"{adapter.executable} found" if executable else f"{adapter.executable} not found on PATH")
    agent_version = None
    if executable:
        code, stdout, _ = run_quiet([adapter.executable, "--version"], env)
        agent_version = (stdout.strip() or None) if code == 0 else None
        add("agent-version", agent_version is not None, f"{adapter.executable} --version: {agent_version!r}")
        for label, argv in adapter.help_commands().items():
            code, stdout, stderr = run_quiet(argv, env)
            text = stdout + stderr
            missing = [token for token in adapter.required_help[label] if token not in text]
            add(f"agent-capability:{label}", code == 0 and not missing, "structured output, session resume, and permission flags present" if not missing else f"missing from `{' '.join(argv)}`: {missing}")
        if multi_turn:
            resume_ok = all(item["ok"] for item in items if item["check"].startswith("agent-capability"))
            add("multi-turn", resume_ok, "same-session resume flags present; each turn also verifies the session ID" if resume_ok else "same-session resume is not available")
    for directory in adapter.global_skill_dirs(env):
        if directory.exists():
            identical = directory.is_dir() and _tree_bytes(directory) == _tree_bytes(GENERATED)
            detail = f"user-level skill {directory} {'matches' if identical else 'differs from'} the generated package"
            if not identical and allow_global_skill:
                detail += "; continuing because --allow-global-skill-conflict was given (results are flagged)"
            add("global-skill-isolation", identical or allow_global_skill, detail)
    try:
        OUTPUT_ROOT.mkdir(exist_ok=True)
        add("output-root", os.access(str(OUTPUT_ROOT), os.W_OK), f"{OUTPUT_ROOT.relative_to(ROOT)}/ writable")
    except OSError as exc:
        add("output-root", False, str(exc))
    return {"agent": adapter.name, "ok": all(item["ok"] for item in items), "checks": items, "agent_version": agent_version, "mandala_cli_version": version}


def _tree_bytes(directory: Path) -> Dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


# ---------------------------------------------------------------------------
# Running cases


class Recorder:
    def __init__(self) -> None:
        self.events: List[dict] = []

    def record(self, **event) -> dict:
        event = {"schema_version": SCHEMA_VERSION, "sequence": len(self.events) + 1, **event}
        self.events.append(event)
        return event

    def extend(self, events: List[dict], phase: str) -> None:
        for event in events:
            self.record(phase=phase, **event)


def normalize_paths(value, project: Path):
    variants = sorted({str(project), os.path.realpath(str(project)), "/private" + str(project)}, key=len, reverse=True)
    if isinstance(value, str):
        for variant in variants:
            value = value.replace(variant, PLACEHOLDER)
        return value
    if isinstance(value, list):
        return [normalize_paths(item, project) for item in value]
    if isinstance(value, dict):
        return {key: normalize_paths(item, project) for key, item in value.items()}
    return value


def run_case(alias: str, entry: dict, fixture: dict, adapter, env: Dict[str, str], case_dir: Path, timeout: float, keep_dir: Optional[Path]) -> dict:
    grader = cases_mod.GRADERS[entry["grader"]]
    prompts = [turn["prompt"] for turn in fixture["turns"]] if "turns" in fixture else [fixture["prompt"]]
    case_dir.mkdir(parents=True)
    result = {
        "schema_version": SCHEMA_VERSION, "case": alias, "fixture": fixture["id"], "agent": adapter.name,
        "contracts": sorted(fixture["contracts"]), "status": "NOT_RUN", "manual_review_required": bool(grader["manual_review"]),
        "manual_review": grader["manual_review"], "checks": [], "setup": None, "turns": len(prompts), "turns_completed": 0,
        "session_id": None, "model": None, "skill_loaded": None, "skill_files_read": [], "state": {},
        "artifacts": {"raw": [], "normalized": "normalized.jsonl", "final": "final.txt", "stderr": "stderr.txt"}, "error": None,
    }
    recorder = Recorder()
    finals: List[str] = []
    stderr_parts: List[str] = []
    # Full snapshots for offline replay; normalized.jsonl truncates evaluator output.
    before: Optional[dict] = None
    after_turn: Dict[int, dict] = {}

    def finish() -> dict:
        evidence = artifacts.build_evidence(alias, fixture["id"], len(prompts), result["turns_completed"], before, after_turn)
        return _finish_case(result, recorder, project, case_dir, finals, stderr_parts, keep_dir, evidence)

    with tempfile.TemporaryDirectory(prefix="mandala-live-") as workspace:
        project = Path(os.path.realpath(workspace)) / "project"
        project.mkdir()
        if ROOT == project or ROOT in project.parents or project in ROOT.parents:
            raise RuntimeError("disposable project overlaps the repository")
        shutil.copytree(str(GENERATED), str(adapter.skill_dir(project)))
        evaluator = cases_mod.Evaluator(project, env, recorder.record)
        # Optional case hook (P1): the agent alone gets a modified environment; the evaluator keeps `env`.
        agent_env = grader["agent_env"](env, Path(os.path.realpath(workspace))) if grader.get("agent_env") else env
        try:
            grader["setup"](evaluator)
            result["setup"] = {"status": "ok", "commands": sum(1 for event in recorder.events if event["actor"] == "evaluator")}
            before = evaluator.snapshot()
        except (cases_mod.SetupError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result.update(status="ENVIRONMENT_ERROR", setup={"status": "failed"}, error={"category": "setup", "message": str(exc)})
            return finish()
        session_id = None
        for number, prompt in enumerate(prompts, start=1):
            code, stdout, stderr, timed_out = run_process(adapter.turn_argv(project, session_id), prompt, project, agent_env, timeout)
            raw_name = f"raw-turn{number}.jsonl"
            (case_dir / raw_name).write_text(stdout, encoding="utf-8")
            result["artifacts"]["raw"].append(raw_name)
            stderr_parts.append(f"--- turn {number} (exit {code}) ---\n{stderr[-4000:]}")
            parsed = adapter.parse(stdout.splitlines(), number)
            recorder.extend(parsed.events, "turn")
            finals.append(f"--- Turn {number} ---\n{parsed.final_text or ''}\n")
            result["model"] = result["model"] or parsed.model
            error = turn_error(number, len(prompts), parsed, session_id, adapter, code, timed_out, timeout)
            if error:
                status = "UNSUPPORTED" if error[0] == "unsupported" else "ENVIRONMENT_ERROR"
                result.update(status=status, error={"category": error[0], "message": error[1]})
                return finish()
            if adapter.skill_load_observable:
                result["skill_loaded"] = True
            session_id = parsed.session_id
            result["session_id"] = session_id
            result["turns_completed"] = number
            evaluator.turn = number
            after_turn[number] = evaluator.snapshot()
            if number == 1 and len(prompts) > 1 and grader.get("between"):
                evaluator.phase, evaluator.turn = "between", 1
                try:
                    grader["between"](evaluator)
                except (cases_mod.SetupError, OSError, ValueError, subprocess.SubprocessError) as exc:
                    result.update(status="ENVIRONMENT_ERROR", error={"category": "setup", "message": f"between turns: {exc}"})
                    return finish()
                evaluator.phase = "turn"
        result["skill_files_read"] = skill_files_read(recorder.events, project)
        result["checks"], result["status"] = artifacts.grade_recorded(entry["grader"], recorder.events, str(project), before, after_turn, len(prompts))
        result["state"] = {"before": cases_mod.state_summary(before), "after": cases_mod.state_summary(after_turn[len(prompts)])}
        return finish()


def turn_error(number: int, turns: int, parsed, session_id: Optional[str], adapter, code: Optional[int], timed_out: bool, timeout: float) -> Optional[tuple]:
    """(category, message) when a turn cannot count as valid evidence; None when it can."""
    if timed_out:
        return ("timeout", f"turn {number} exceeded {timeout:g}s")
    if parsed.environment_error:
        return ("agent", parsed.environment_error)
    if code != 0:
        return ("agent", f"{adapter.executable} exited {code}")
    if number == 1 and not parsed.session_id and turns > 1:
        return ("unsupported", "no session identity to continue the conversation")
    if number > 1 and (not parsed.session_id or parsed.session_id != session_id):
        return ("unsupported", f"turn {number} did not continue session {session_id} (got {parsed.session_id})")
    if adapter.skill_load_observable and "mandala" not in (parsed.skills or []):
        return ("skill", "project-local mandala Skill was not loaded")
    return None


def skill_files_read(events: List[dict], project: Path) -> List[str]:
    """Which mandala SKILL.md copies the agent visibly read (project-local or user-level)."""
    seen = set()
    for event in events:
        if event.get("actor") != "agent":
            continue
        text = event.get("command") or json.dumps(event.get("input") or "")
        if "skills/mandala/SKILL.md" not in text:
            continue
        local = ".codex/skills/mandala/SKILL.md" in text or ".claude/skills/mandala/SKILL.md" in text
        inside = str(project) in text or not any(marker in text for marker in ("~/", str(Path.home())))
        seen.add("project" if local and inside else "user")
    return sorted(seen)


def annotate_execution(events: List[dict], project: Path) -> List[dict]:
    """Add execution status and resolved Mandala targets to agent command events; `command` stays raw."""
    annotated = []
    for event in events:
        if event.get("actor") == "agent" and event.get("kind") in ("command", "command_denied"):
            if event["kind"] == "command_denied":
                execution = "permission_denied"
            elif event.get("incomplete") or event.get("exit_code") is None:
                execution = "not_observed"
            else:
                execution = "executed"
            calls = cases_mod.mandala_invocations(event, str(project))
            event = {**event, "execution": execution, "mandala": [
                {"action": call["action"], "args": call["args"], "project_argument": call["project_raw"],
                 "resolved_project": str(project) if call["targets_project"] else None, "targets_project": call["targets_project"],
                 "certain": call["certain"] and execution != "permission_denied", "exit_code": call["exit_code"] if execution != "permission_denied" else None}
                for call in calls]}
        annotated.append(event)
    return annotated


def _finish_case(result: dict, recorder: Recorder, project: Path, case_dir: Path, finals: List[str], stderr_parts: List[str], keep_dir: Optional[Path], evidence: Optional[dict] = None) -> dict:
    normalized = normalize_paths(annotate_execution(recorder.events, project), project)
    if evidence is not None:
        (case_dir / "evidence.json").write_text(json.dumps(normalize_paths(evidence, project), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (case_dir / "normalized.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in normalized), encoding="utf-8")
    (case_dir / "final.txt").write_text(normalize_paths("\n".join(finals), project), encoding="utf-8")
    (case_dir / "stderr.txt").write_text(normalize_paths("\n".join(stderr_parts), project), encoding="utf-8")
    result = normalize_paths(result, project)
    (case_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if keep_dir is not None and project.exists():
        shutil.copytree(str(project), str(keep_dir), symlinks=True)
    return result


# ---------------------------------------------------------------------------
# Reporting


def coverage(results: List[dict], contracts: Dict[str, dict]) -> dict:
    """Contract IDs touched by completed cases. Exercised = fixture contracts, automated checks, or manual notes."""
    ran = [result for result in results if result["turns_completed"] > 0]
    checked = sorted({cid for result in ran for item in result["checks"] if item["status"] in (cases_mod.PASS, cases_mod.FAIL) for cid in item["contracts"]})
    manual = sorted({cid for result in ran for note in result["manual_review"] for cid in note["contracts"]})
    exercised = sorted({cid for result in ran for cid in result["contracts"]} | set(checked) | set(manual))
    return {"exercised": exercised, "automatically_checked": checked, "manual_review": manual, "not_exercised": sorted(set(contracts) - set(exercised))}


def write_summary(run_dir: Path, agent: str, meta: dict, results: List[dict], contracts: Dict[str, dict], pre: dict) -> dict:
    counts: Dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = {
        "schema_version": SCHEMA_VERSION, **meta, "agent": agent, "agent_version": pre.get("agent_version"),
        "model": next((result["model"] for result in results if result.get("model")), None),
        "mandala_cli_version": pre.get("mandala_cli_version"), "preflight": pre, "finished_at": utc_now(),
        "counts": dict(sorted(counts.items())), "manual_review_required": sum(1 for result in results if result["manual_review_required"] and result["turns_completed"]),
        "contract_coverage": coverage(results, contracts),
        "cases": [{key: result[key] for key in ("case", "fixture", "status", "manual_review_required", "error")} for result in results],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(summary, results, contracts), encoding="utf-8")
    return summary


def render_report(summary: dict, results: List[dict], contracts: Dict[str, dict]) -> str:
    def label(cid: str) -> str:
        return f"{cid} ({contracts[cid]['slug']})" if cid in contracts else cid

    counts = summary["counts"]
    lines = [
        f"# Mandala live evaluation: {summary['agent']} / {summary['suite']}", "",
        f"- Run: `{summary['run_id']}`",
        f"- Agent: {summary['agent']} {summary.get('agent_version') or '(version unknown)'}; model: {summary.get('model') or 'not observable'}",
        f"- Skill revision: `{summary['skill_git_sha']}`{' (uncommitted changes)' if summary.get('skill_dirty') else ''}",
        f"- SKILL.md SHA-256: `{summary['skill_sha256']}`",
        f"- Mandala CLI: {summary.get('mandala_cli_version') or 'unavailable'}",
        f"- AUTO_PASS {counts.get('AUTO_PASS', 0)}, AUTO_FAIL {counts.get('AUTO_FAIL', 0)}, INCONCLUSIVE {counts.get('INCONCLUSIVE', 0)}, "
        f"ENVIRONMENT_ERROR {counts.get('ENVIRONMENT_ERROR', 0)}, UNSUPPORTED {counts.get('UNSUPPORTED', 0)}, NOT_RUN {counts.get('NOT_RUN', 0)}",
        f"- Cases needing manual response review: {summary['manual_review_required']}", "",
        "AUTO_PASS means the automated trace/state checks passed. It does not prove the response is correct; manual response review may still be required.", "",
    ]
    if not summary.get("complete", True):
        lines += ["## INCOMPLETE RUN", "", "This run was interrupted. It is not a complete suite result; rerun the suite for evidence.", ""]
    if not summary["preflight"]["ok"]:
        lines += ["## Preflight failed", ""] + [f"- {item['check']}: {item['detail']}" for item in summary["preflight"]["checks"] if not item["ok"]] + [""]
    for result in results:
        lines += [f"## {result['case']} `{result['fixture']}`: {result['status']}", ""]
        if result.get("error"):
            lines += [f"Error ({result['error']['category']}): {result['error']['message']}", ""]
        if result["checks"]:
            lines += ["| Check | Contracts | Status | Evidence |", "| --- | --- | --- | --- |"]
            for item in result["checks"]:
                evidence = item["evidence"].replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {item['id']} | {', '.join(item['contracts']) or '-'} | {item['status']} | {evidence} |")
            lines.append("")
        for note in result["manual_review"]:
            lines.append(f"- Manual review ({', '.join(label(cid) for cid in note['contracts']) or 'task'}): {note['note']}")
        lines += [f"- Artifacts: `cases/{result['case']}/` (result.json, normalized.jsonl, evidence.json, {', '.join(result['artifacts']['raw']) or 'no raw trace'}, final.txt)", ""]
    cov = summary["contract_coverage"]
    lines += ["## Contract coverage", "",
              f"- Exercised: {', '.join(cov['exercised']) or 'none'}",
              f"- Automatically checked: {', '.join(cov['automatically_checked']) or 'none'}",
              f"- Manual review: {', '.join(cov['manual_review']) or 'none'}",
              f"- Not exercised by this run: {', '.join(cov['not_exercised']) or 'none'}", "",
              "Raw traces are local evaluation artifacts. Review them before sharing.", ""]
    return "\n".join(lines)


def write_rich_coverage(run_dir: Path) -> bool:
    """Detailed coverage.json/coverage.md recomputed from the case artifacts just written (additive to summary.json)."""
    try:
        eval_coverage.write_own_coverage(run_dir)
    except (ValueError, OSError) as exc:
        print(f"eval-live: coverage report failed: {exc}", file=sys.stderr)
        return False
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_revision(env: Dict[str, str]) -> tuple:
    code, stdout, _ = run_quiet(["git", "rev-parse", "HEAD"], env)
    _, status, _ = run_quiet(["git", "status", "--porcelain", "--untracked-files=no"], env)
    return (stdout.strip() if code == 0 else None), bool(status.strip())


def exit_status(results: List[dict], preflight_ok: bool) -> int:
    statuses = {result["status"] for result in results}
    if "AUTO_FAIL" in statuses:
        return 1
    if not preflight_ok or statuses - {"AUTO_PASS"}:
        return 2
    return 0


def prepare_output(path: Optional[str], run_id: str) -> Path:
    if path is None:
        target = OUTPUT_ROOT / run_id
    else:
        target = Path(path).resolve()
        inside_repo = target == ROOT or ROOT in target.parents
        inside_output_root = target == OUTPUT_ROOT or OUTPUT_ROOT in target.parents
        # Inside the repository only the ignored .eval-live/ tree may hold live artifacts.
        if (inside_repo and not inside_output_root) or ".mandala" in target.parts:
            raise ValueError(f"output directory not allowed: {target}")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError(f"output directory exists and is not empty: {target}")
    target.mkdir(parents=True, exist_ok=path is not None)
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local live-agent evaluations of the Mandala Skill. Never run in CI.")
    parser.add_argument("--agent", choices=["codex", "claude", "all"], help="agent CLI to evaluate")
    parser.add_argument("--suite", help="named suite from tests/evals/live_suites.json (default: release)")
    parser.add_argument("--case", action="append", help="live case alias such as R1 (repeatable)")
    parser.add_argument("--list", action="store_true", help="list live cases and suites")
    parser.add_argument("--preflight", action="store_true", help="check prerequisites only")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"seconds per agent turn (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--model", help="model passed to the agent CLI; default is the agent's own default")
    parser.add_argument("--output-dir", help="empty directory for artifacts (default .eval-live/<run-id>)")
    parser.add_argument("--keep-workdirs", action="store_true", help="copy disposable projects into the run directory")
    parser.add_argument("--allow-global-skill-conflict", action="store_true", help="run even if a user-level mandala Skill differs (results are flagged)")
    args = parser.parse_args(argv)
    try:
        contracts, fixtures, manifest = load_inputs()
    except (ValueError, OSError) as exc:
        print(f"eval-live: invalid evaluation metadata: {exc}", file=sys.stderr)
        return 2
    if args.list:
        for alias, entry in manifest["cases"].items():
            ids = ", ".join(sorted(fixtures[entry["fixture"]]["contracts"]))
            print(f"{alias}  {entry['fixture']}  turns={len(fixtures[entry['fixture']].get('turns', [1]))}  contracts={ids}")
        for name, members in manifest["suites"].items():
            print(f"suite {name}: {' '.join(members)}")
        return 0
    if not args.agent:
        parser.error("--agent is required unless --list is given")
    if args.case and args.suite:
        parser.error("use --suite or --case, not both")
    suite = args.suite or ("cases" if args.case else "release")
    aliases = args.case or manifest["suites"].get(suite)
    if not aliases or any(alias not in manifest["cases"] for alias in aliases):
        parser.error(f"unknown suite or case: {args.case or suite}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    env = sanitized_env()
    agents = ["codex", "claude"] if args.agent == "all" else [args.agent]
    multi_turn = any("turns" in fixtures[manifest["cases"][alias]["fixture"]] for alias in aliases)
    if args.preflight:
        reports = [preflight(ADAPTERS[name](args.model), env, multi_turn, args.allow_global_skill_conflict) for name in agents]
        print(json.dumps(reports, indent=2))
        return 0 if all(report["ok"] for report in reports) else 2
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{args.agent}-{suite}-{secrets.token_hex(3)}"
    try:
        run_root = prepare_output(args.output_dir, run_id)
    except (ValueError, OSError) as exc:
        print(f"eval-live: {exc}", file=sys.stderr)
        return 2
    sha, dirty = git_revision(env)
    signal.signal(signal.SIGTERM, _terminate)
    codes = []
    for name in agents:
        adapter = ADAPTERS[name](args.model)
        run_dir = run_root / name
        run_dir.mkdir()
        meta = {"run_id": run_id, "started_at": utc_now(), "suite": suite, "cases_requested": aliases, "skill_git_sha": sha, "skill_dirty": dirty,
                "skill_sha256": sha256(CANONICAL / "SKILL.md"), "cli_contract_sha256": sha256(CANONICAL / "references" / "cli-contract.md"),
                "timeout_seconds": args.timeout, "model_requested": args.model, "global_skill_conflict_allowed": args.allow_global_skill_conflict}
        pre = preflight(adapter, env, multi_turn, args.allow_global_skill_conflict)
        results = []
        interrupted = False
        try:
            for alias in aliases:
                entry = manifest["cases"][alias]
                fixture = fixtures[entry["fixture"]]
                if not pre["ok"]:
                    results.append(not_run(alias, fixture, "preflight", "preflight failed"))
                    continue
                print(f"[{name}] {alias} {fixture['id']} ...", file=sys.stderr, flush=True)
                keep = run_dir / "workdirs" / alias if args.keep_workdirs else None
                result = run_case(alias, entry, fixture, adapter, env, run_dir / "cases" / alias, args.timeout, keep)
                print(f"[{name}] {alias} {result['status']}", file=sys.stderr, flush=True)
                results.append(result)
        except KeyboardInterrupt:
            interrupted = True
            done = {result["case"] for result in results}
            results += [not_run(alias, fixtures[manifest["cases"][alias]["fixture"]], "interrupted", "run interrupted before this case completed") for alias in aliases if alias not in done]
        meta["complete"] = not interrupted and len(results) == len(aliases)
        summary = write_summary(run_dir, name, meta, results, contracts, pre)
        coverage_ok = write_rich_coverage(run_dir)
        print(f"[{name}] {summary['counts']}{' (INTERRUPTED)' if interrupted else ''} -> {run_dir.relative_to(ROOT) if ROOT in run_dir.parents else run_dir}/report.md")
        code = 2 if interrupted else exit_status(results, pre["ok"])
        codes.append(code if coverage_ok or code == 1 else 2)
        if interrupted:
            break  # do not start another agent after an interruption
    return 1 if 1 in codes else max(codes)


def not_run(alias: str, fixture: dict, category: str, message: str) -> dict:
    return {"case": alias, "fixture": fixture["id"], "status": "NOT_RUN", "manual_review_required": False, "manual_review": [], "contracts": sorted(fixture["contracts"]),
            "checks": [], "turns_completed": 0, "model": None, "artifacts": {"raw": []}, "error": {"category": category, "message": message}}


def _terminate(signum, frame):  # noqa: ARG001 - signal handler signature
    raise KeyboardInterrupt  # SIGTERM: stop the agent group and record an incomplete run


if __name__ == "__main__":
    raise SystemExit(main())
