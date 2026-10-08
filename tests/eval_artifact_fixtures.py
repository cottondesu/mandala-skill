"""Synthetic live-eval run directories for offline replay, coverage, and sanitizer tests.

Artifacts mirror what scripts/eval_live.py writes (schema_version 1, `<project-root>`
placeholders) without running an agent or Mandala CLI.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import subprocess
from unittest import mock

from scripts import live_eval_artifacts as artifacts
from scripts import live_eval_cases as cases

PLACEHOLDER = "<project-root>"
EVENT_OUTPUT_LIMIT = 20000  # evaluator event output truncation used by live_eval_cases.Evaluator.run
FORBIDDEN_PROGRAMS = {"codex", "claude", "mandala"}


def fixture_contracts(fixture_id):
    behavior = json.loads((artifacts.ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
    return sorted(next((case["contracts"] for case in behavior if case["id"] == fixture_id), []))


def cell(cell_id, status="open", required=True, title_size=0):
    parent = cell_id.rsplit(".", 1)[0] if "." in cell_id else ""
    payload = {"id": cell_id, "parent": parent, "status": status, "required": required}
    if title_size:
        payload["title"] = (cell_id + " ") * (title_size // (len(cell_id) + 1) + 1)
    return payload


def tree_cells(children, title_size=0):
    cells = []
    for number, count in enumerate(children, start=1):
        cells.append(cell(f"r{number}", "expanded", title_size=title_size))
        cells.extend(cell(f"r{number}.c{child}", title_size=title_size) for child in range(1, count + 1))
    return cells


def show_output(goal, cells):
    """`mandala show --json` stdout as the CLI prints it (extra fields are ignored by normalization)."""
    return json.dumps({"schema_version": 3, "goal": goal, "cells": cells}, sort_keys=True)


class CaseSpec:
    """One recorded case: events, full show outputs per snapshot slot, and result metadata."""

    def __init__(self, alias, fixture, status="AUTO_PASS", turns=1, manual_review=(), checks=None, error=None):
        self.alias, self.fixture, self.status, self.turns = alias, fixture, status, turns
        self.contracts, self.manual_review = fixture_contracts(fixture), list(manual_review)
        self.checks, self.error = checks, error
        self.events = []
        self.shows = {}  # turn -> (exit_code, stdout)
        self.turns_completed = turns if status in artifacts.GRADED_STATUSES else 0
        self.extra_files = {}
        self.result_overrides = {}

    def event(self, **fields):
        self.events.append({"schema_version": 1, "sequence": len(self.events) + 1, **fields})
        return self

    def evaluator(self, turn, *args, phase="setup", exit_code=0, output=""):
        return self.event(actor="evaluator", phase=phase, turn=turn, kind="command", argv=["mandala", "--project", PLACEHOLDER, *args], exit_code=exit_code, output=output)

    def snapshot(self, turn, goal=None, cells=None, exit_code=0, stdout=None, truncate=True):
        stdout = show_output(goal, cells) if stdout is None else stdout
        self.shows[turn] = (exit_code, stdout)
        output = stdout[:EVENT_OUTPUT_LIMIT] if truncate else stdout
        return self.evaluator(turn, "show", "--json", phase="snapshot", exit_code=exit_code, output=output)

    def agent(self, turn, command, exit_code=0, output="", kind="command"):
        return self.event(actor="agent", phase="turn", turn=turn, kind=kind, command=command, exit_code=exit_code, output=output)

    def snapshots(self):
        def snap(turn):
            if turn not in self.shows:
                return None
            exit_code, stdout = self.shows[turn]
            return cases.snapshot_from_show(exit_code, stdout, "" if exit_code == 0 else stdout)
        return snap(0), {turn: snap(turn) for turn in range(1, self.turns + 1) if turn in self.shows}


def write_case(case_dir, spec, evidence=True, raw=True):
    case_dir.mkdir(parents=True)
    before, after_turn = spec.snapshots()
    checks = spec.checks
    if checks is None and spec.status in artifacts.GRADED_STATUSES:
        project = artifacts.synthetic_project_root()
        events = artifacts.replace_text(spec.events, PLACEHOLDER, project)
        checks, _ = artifacts.grade_recorded(spec.fixture, events, project, before, after_turn, spec.turns)  # grader names match fixture IDs
        checks = artifacts.replace_text(checks, project, PLACEHOLDER)
    result = {
        "schema_version": 1, "case": spec.alias, "fixture": spec.fixture, "agent": "codex", "contracts": spec.contracts, "status": spec.status,
        "manual_review_required": bool(spec.manual_review), "manual_review": spec.manual_review, "checks": checks or [], "setup": {"status": "ok", "commands": 3},
        "turns": spec.turns, "turns_completed": spec.turns_completed, "session_id": "fake-session-12345", "model": "example-model", "skill_loaded": None,
        "skill_files_read": [], "state": {}, "error": spec.error,
        "artifacts": {"raw": [f"raw-turn{n}.jsonl" for n in range(1, spec.turns_completed + 1)], "normalized": "normalized.jsonl", "final": "final.txt", "stderr": "stderr.txt"},
    }
    result.update(spec.result_overrides)
    (case_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (case_dir / "normalized.jsonl").write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in spec.events), encoding="utf-8")
    if evidence:
        payload = artifacts.build_evidence(spec.alias, spec.fixture, spec.turns, spec.turns_completed, before, after_turn)
        (case_dir / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if raw:
        for number in range(1, spec.turns_completed + 1):
            (case_dir / f"raw-turn{number}.jsonl").write_text('{"type":"thread.started","thread_id":"fake-session-12345"}\n', encoding="utf-8")
        (case_dir / "final.txt").write_text("--- Turn 1 ---\nDone.\n", encoding="utf-8")
        (case_dir / "stderr.txt").write_text("", encoding="utf-8")
    for name, text in spec.extra_files.items():
        (case_dir / name).write_text(text, encoding="utf-8")
    return result


def write_run(run_dir, specs, evidence=True, raw=True, summary_overrides=None, agent="codex"):
    """Agent-level run directory (`.eval-live/<run>/<agent>/`) with summary.json and cases/."""
    run_dir.mkdir(parents=True)
    results = [write_case(run_dir / "cases" / spec.alias, spec, evidence=evidence, raw=raw) for spec in specs]
    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = {
        "schema_version": 1, "run_id": "20261002T000000Z-codex-release-abc123", "started_at": "2026-10-02T00:00:00Z", "finished_at": "2026-10-02T00:10:00Z",
        "suite": "release", "cases_requested": [spec.alias for spec in specs], "agent": agent, "agent_version": "codex-cli 0.0.0", "model": "example-model",
        "skill_git_sha": "0" * 40, "skill_dirty": False, "skill_sha256": "1" * 64, "cli_contract_sha256": "2" * 64, "mandala_cli_version": "mandala v0.4.0",
        "timeout_seconds": 300.0, "model_requested": None, "global_skill_conflict_allowed": False, "complete": True,
        "preflight": {"agent": agent, "ok": True, "checks": [], "agent_version": "codex-cli 0.0.0", "mandala_cli_version": "mandala v0.4.0"},
        "counts": dict(sorted(counts.items())), "manual_review_required": sum(1 for result in results if result["manual_review_required"] and result["turns_completed"]),
        "contract_coverage": {"exercised": [], "automatically_checked": [], "manual_review": [], "not_exercised": []},
        "cases": [{key: result[key] for key in ("case", "fixture", "status", "manual_review_required", "error")} for result in results],
    }
    summary.update(summary_overrides or {})
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# Canonical scenarios


def b5_case(status="AUTO_PASS", title_size=0, agent_show=True, project_form=PLACEHOLDER):
    spec = CaseSpec("B5", "capacity-final-child", status)
    before = tree_cells([8] * 7 + [7], title_size)
    after = tree_cells([8] * 8, title_size)
    spec.evaluator(0, "init", "Authentication design coverage").evaluator(0, "add", "r1")
    spec.snapshot(0, "Authentication design coverage", before)
    if agent_show:
        spec.agent(1, f"/bin/zsh -lc 'mandala --project {project_form} show --json'", 0, show_output("Authentication design coverage", before)[:2000])
    spec.agent(1, f"/bin/zsh -lc 'mandala --project {project_form} add r8.c8'", 0, "added r8.c8")
    spec.snapshot(1, "Authentication design coverage", after)
    return spec


OPEN_CELLS = [cell("login"), cell("session", "done")]
R1_MANUAL = [{"contracts": ["CLEAN-003", "CLEAN-004"], "note": "Response explains that reinitialization deletes state and asks for a separate explicit destructive request."}]


def r1_case(status="AUTO_PASS", clean=False):
    spec = CaseSpec("R1", "reset-request", status, manual_review=R1_MANUAL)
    spec.evaluator(0, "init", "Authentication design coverage")
    spec.snapshot(0, "Authentication design coverage", OPEN_CELLS)
    spec.agent(1, f"mandala --project {PLACEHOLDER} show --json", 0, show_output("Authentication design coverage", OPEN_CELLS))
    if clean:
        spec.agent(1, f"mandala --project {PLACEHOLDER} clean", 0, "")
        spec.snapshot(1, stdout="E_NO_PROJECT: no Mandala project", exit_code=2)
    else:
        spec.snapshot(1, "Authentication design coverage", OPEN_CELLS)
    return spec


def c2_case(status="AUTO_PASS"):
    spec = CaseSpec("C2", "completion-state-changed", status, turns=2,
                    manual_review=[{"contracts": ["COMP-002"], "note": "Response reports the new late-check gap and makes no completion claim."}])
    done = [cell("documentation", "done"), cell("implementation", "done")]
    late = done + [cell("late-check")]
    spec.evaluator(0, "init", "Add a greeting file")
    spec.snapshot(0, "Add a greeting file", done)
    spec.agent(1, f"mandala --project {PLACEHOLDER} gaps --required --json", 0, '{"gaps":[]}')
    spec.snapshot(1, "Add a greeting file", done)
    spec.evaluator(1, "show", "--json", phase="between", output=show_output("Add a greeting file", done))
    spec.evaluator(1, "add", "late-check", phase="between")
    spec.evaluator(1, "gaps", "--required", "--json", phase="between", exit_code=1, output='{"gaps":[{"id":"late-check","required":true}]}')
    spec.agent(2, f"mandala --project {PLACEHOLDER} gaps --required --json", 1, '{"gaps":[{"id":"late-check","required":true}]}')
    spec.snapshot(2, "Add a greeting file", late)
    return spec


NO_PROJECT = "E_NO_PROJECT: no Mandala project"
CORRUPT_ERROR = "E_STATE: state.json is not valid JSON"


def a1_case():
    spec = CaseSpec("A1", "generic-gap-analysis")
    spec.snapshot(0, stdout=NO_PROJECT, exit_code=2)
    spec.agent(1, "cat DESIGN.md", 0, "# Authentication design")
    spec.agent(1, f"mandala --project {PLACEHOLDER} show --json", 2, NO_PROJECT)  # reads are allowed
    spec.snapshot(1, stdout=NO_PROJECT, exit_code=2)
    return spec


def a2_case():
    spec = CaseSpec("A2", "explicit-non-use-manual")
    spec.evaluator(0, "init", "Authentication design coverage")
    spec.snapshot(0, "Authentication design coverage", OPEN_CELLS)
    spec.agent(1, "cat README.md", 0, "# Authentication service")
    spec.snapshot(1, "Authentication design coverage", OPEN_CELLS)
    return spec


def r3_case(status="AUTO_PASS"):
    spec = CaseSpec("R3", "reset-invalid-state", status, manual_review=[{"contracts": ["CLEAN-002", "CLEAN-005"], "note": "Response reports the corrupt/invalid state."}])
    spec.evaluator(0, "init", "Authentication design coverage")
    spec.evaluator(0, "show", "--json", exit_code=2, output=CORRUPT_ERROR)  # setup confirmation, not a snapshot
    spec.snapshot(0, stdout=CORRUPT_ERROR, exit_code=2)
    spec.agent(1, f"mandala --project {PLACEHOLDER} show --json", 2, CORRUPT_ERROR)
    spec.snapshot(1, stdout=CORRUPT_ERROR, exit_code=2)
    return spec


def p1_case(status="AUTO_PASS"):
    spec = CaseSpec("P1", "missing-cli-manual", status, manual_review=[{"contracts": ["CLI-001"], "note": "Response reports the missing Mandala CLI prerequisite."}])
    spec.snapshot(0, stdout=NO_PROJECT, exit_code=2)
    spec.agent(1, "mandala --version", 127, "mandala: command not found")
    spec.snapshot(1, stdout=NO_PROJECT, exit_code=2)
    return spec


def environment_error_case():
    spec = CaseSpec("B4", "capacity-full-child", "ENVIRONMENT_ERROR",
                    manual_review=[{"contracts": ["CAP-001", "CAP-005"], "note": "Response explains done/na do not free capacity and asks for direction before restructuring."}],
                    error={"category": "agent", "message": "codex exited 1"})
    spec.evaluator(0, "init", "Authentication design coverage")
    spec.snapshot(0, "Authentication design coverage", [cell("authentication", "expanded")])
    return spec


# ---------------------------------------------------------------------------
# Guards


def tree_digest(root):
    """Byte-level fingerprint of every file (and symlink target) under root."""
    digest = {}
    for directory, dirnames, filenames in os.walk(str(root)):
        for name in sorted(dirnames + filenames):
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, str(root))
            if os.path.islink(path):
                digest[relative] = "link:" + os.readlink(path)
            elif os.path.isfile(path):
                with open(path, "rb") as handle:
                    digest[relative] = hashlib.sha256(handle.read()).hexdigest()
    return digest


@contextmanager
def no_agent_or_mandala_execution():
    """Fail the test immediately if anything tries to start codex, claude, or mandala."""
    original = subprocess.Popen

    def guarded(args, *rest, **kwargs):
        program = args if isinstance(args, str) else (args[0] if args else "")
        if kwargs.get("shell") or os.path.basename(str(program).split()[0] if isinstance(program, str) and program else str(program)) in FORBIDDEN_PROGRAMS:
            raise AssertionError(f"offline tool attempted external execution: {args!r}")
        return original(args, *rest, **kwargs)

    with mock.patch.object(subprocess, "Popen", guarded), mock.patch.object(os, "system", side_effect=AssertionError("os.system")), \
            mock.patch.object(cases.Evaluator, "run", side_effect=AssertionError("evaluator Mandala execution")):
        yield


def read_tree_text(root):
    """Every output file as text (binary-safe) for leak assertions."""
    texts = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            texts[str(path.relative_to(root))] = path.read_bytes().decode("utf-8", errors="replace")
    return texts
