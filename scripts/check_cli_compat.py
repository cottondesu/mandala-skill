#!/usr/bin/env python3
"""Explicit real-CLI compatibility check for the Mandala CLI baseline (`status --json` and existing JSON).

Runs only when invoked; never part of `make check`, `make test`, `make release-check`, or CI. It uses
an already installed CLI (PATH or `--mandala`), never installs or downloads anything, and creates every
state through the CLI in fresh temporary projects outside any Git repository. It never edits state
files; read-only snapshots compare bytes and metadata. Results are PASS, FAIL, or SKIP; a CLI that is
missing or reports another version is NOT_RUN (exit 2), never a pass. `--report` only creates a new regular
file (exclusive create, never inside `.git/` or `.mandala/`); a refused or failed report exits 3.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

REQUIRED_VERSION = "mandala v0.4.0"
STATUS_SCHEMA_VERSION = 1
STATUS_FIELDS = ("schema_version", "goal", "cells", "groups", "required", "optional", "required_gaps")
COUNT_FIELDS = ("open", "done", "na")
TIMEOUT = 30.0
REPORT_ERROR_EXIT = 3
PROTECTED_DIRECTORIES = (".git", ".mandala")


class CompatError(Exception):
    """A contract violation observed in real CLI output."""


def is_count(value: object) -> bool:
    # bool is a subclass of int; JSON true/false must never pass as a count.
    return type(value) is int and value >= 0


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise CompatError(f"duplicate JSON keys: {keys}")
    return dict(pairs)


def is_compact(text: str) -> bool:
    """True when no insignificant whitespace appears outside JSON strings."""
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in " \t\r\n":
            return False
    return True


def parse_status_json(stdout: str, exit_code: Optional[int]) -> Dict[str, Any]:
    """Validate one `status --json` result. Exit 1 is a valid domain result, not a crash."""
    if exit_code not in (0, 1):
        raise CompatError(f"status --json exit {exit_code!r} is an error, not a coverage result")
    if not stdout.endswith("\n") or stdout.count("\n") != 1:
        raise CompatError("status --json must print one line with exactly one trailing newline")
    body = stdout[:-1]
    if not is_compact(body):
        raise CompatError("status --json output is not compact JSON")
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        raise CompatError(f"status --json output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CompatError("status --json output must be a JSON object")
    if tuple(data) != STATUS_FIELDS:
        raise CompatError(f"status --json fields/order {list(data)} != {list(STATUS_FIELDS)}")
    if type(data["schema_version"]) is not int or data["schema_version"] != STATUS_SCHEMA_VERSION:
        raise CompatError(f"status --json schema_version must be integer {STATUS_SCHEMA_VERSION}")
    if not isinstance(data["goal"], str):
        raise CompatError("status --json goal must be a string")
    for field in ("cells", "groups", "required_gaps"):
        if not is_count(data[field]):
            raise CompatError(f"status --json {field} must be a non-negative integer")
    for group in ("required", "optional"):
        counts = data[group]
        if not isinstance(counts, dict) or tuple(counts) != COUNT_FIELDS:
            raise CompatError(f"status --json {group} must have exactly {list(COUNT_FIELDS)} in order")
        for field in COUNT_FIELDS:
            if not is_count(counts[field]):
                raise CompatError(f"status --json {group}.{field} must be a non-negative integer")
    if data["required_gaps"] != data["required"]["open"]:
        raise CompatError("required_gaps must equal required.open")
    leaves = sum(data[group][field] for group in ("required", "optional") for field in COUNT_FIELDS)
    if data["cells"] != data["groups"] + leaves:
        raise CompatError("cells must equal groups plus all leaf counts")
    if (exit_code == 1) != (data["required_gaps"] > 0):
        raise CompatError(f"exit {exit_code} disagrees with required_gaps {data['required_gaps']}")
    return data


def parse_gaps_json(stdout: str, exit_code: Optional[int]) -> List[Dict[str, Any]]:
    if exit_code not in (0, 1):
        raise CompatError(f"gaps --json exit {exit_code!r} is an error")
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise CompatError(f"gaps --json output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "gaps"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise CompatError("gaps --json needs exactly integer schema_version 1 and gaps")
    if not isinstance(data["gaps"], list) or not all(
        isinstance(item, dict) and set(item) == {"id", "required"} and isinstance(item["id"], str) and isinstance(item["required"], bool)
        for item in data["gaps"]
    ):
        raise CompatError("gaps --json entries need string id and boolean required")
    # Without --required the list also holds optional gaps; the exit code tracks required ones only.
    if (exit_code == 1) != any(item["required"] for item in data["gaps"]):
        raise CompatError(f"gaps exit {exit_code} disagrees with listed required gaps")
    return data["gaps"]


def parse_show_json(stdout: str, exit_code: Optional[int]) -> Dict[str, Any]:
    if exit_code != 0:
        raise CompatError(f"show --json exit {exit_code!r}")
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise CompatError(f"show --json output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or list(data) != ["schema_version", "goal", "cells"] or data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise CompatError("show --json needs schema_version, goal, cells")
    if not isinstance(data["goal"], str) or not isinstance(data["cells"], list):
        raise CompatError("show --json needs a string goal and a cells list")
    ids = []
    for cell in data["cells"]:
        if (not isinstance(cell, dict) or list(cell) != ["id", "parent", "status", "required"] or not isinstance(cell["required"], bool)
                or not all(isinstance(cell[field], str) for field in ("id", "parent", "status"))):
            raise CompatError(f"show --json cell shape changed: {cell!r}")
        ids.append(cell["id"])
    if ids != sorted(ids):
        raise CompatError("show --json cells are not ID-sorted")
    return data


class Runner:
    def __init__(self, mandala: str) -> None:
        self.mandala = mandala
        self.log: List[Dict[str, Any]] = []

    def run(self, args: List[str], cwd: Path) -> Tuple[Optional[int], str, str]:
        argv = [self.mandala, *args]
        try:
            # Argument vector, never a shell string: goals and paths are not interpolated.
            result = subprocess.run(argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, stdout, stderr = None, "", str(exc)
        self.log.append({"argv": args, "cwd": str(cwd), "exit": code, "stdout": stdout, "stderr": stderr})
        return code, stdout, stderr

    def ok(self, project: Path, *args: str) -> str:
        """Run a CLI state transition that must succeed with no stdout."""
        code, stdout, stderr = self.run(["--project", str(project), *args], project)
        if code != 0 or stdout:
            raise CompatError(f"setup {' '.join(args)!r} failed: exit {code}, stderr {stderr.strip()!r}")
        return stdout

    def status(self, project: Path, cwd: Optional[Path] = None) -> Tuple[Dict[str, Any], int, str]:
        code, stdout, stderr = self.run(["--project", str(project), "status", "--json"], cwd or project)
        data = parse_status_json(stdout, code)
        if stderr:
            raise CompatError(f"status --json wrote stderr: {stderr.strip()!r}")
        return data, code, stdout

    def required_gap_ids(self, project: Path) -> Tuple[List[str], int]:
        code, stdout, _ = self.run(["--project", str(project), "gaps", "--required", "--json"], project)
        gaps = parse_gaps_json(stdout, code)
        if not all(item["required"] for item in gaps):
            raise CompatError("gaps --required --json listed an optional gap")
        return [item["id"] for item in gaps], code


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CompatError(message)


def counts(data: Dict[str, Any]) -> Tuple[int, ...]:
    return (data["cells"], data["groups"], *(data[g][f] for g in ("required", "optional") for f in COUNT_FIELDS), data["required_gaps"])


def snapshot(project: Path) -> Dict[str, Tuple[int, int, int, bytes]]:
    """Bytes and metadata of every entry under the project (read-only)."""
    entries = {}
    for path in sorted(project.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        entries[str(path.relative_to(project))] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
    return entries


class Cases:
    def __init__(self, runner: Runner, base: Path) -> None:
        self.r = runner
        self.base = base
        self.serial = 0

    def project(self, goal: str = "Compatibility goal") -> Path:
        self.serial += 1
        path = self.base / f"p{self.serial:02d}"
        path.mkdir()
        self.r.ok(path, "init", goal)
        return path

    def c01_empty_plan(self) -> str:
        data, code, _ = self.r.status(self.project("Empty plan"))
        expect(code == 0, f"empty plan exit {code}")
        expect(data["goal"] == "Empty plan", "goal mismatch")
        expect(counts(data) == (0,) * 9, f"empty plan counts {counts(data)}")
        return "exit 0, every count 0"

    def c02_required_open(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "api")
        data, code, _ = self.r.status(project)
        expect(code == 1, f"required open exit {code}")
        expect(counts(data) == (1, 0, 1, 0, 0, 0, 0, 0, 1), f"counts {counts(data)}")
        return "exit 1 with valid JSON, required_gaps 1"

    def c03_optional_only(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "--optional", "docs")
        data, code, _ = self.r.status(project)
        expect(code == 0, f"optional-only exit {code}")
        expect(counts(data) == (1, 0, 0, 0, 0, 1, 0, 0, 0), f"counts {counts(data)}")
        return "exit 0, optional.open 1, required_gaps 0"

    def c04_mixed_statuses(self) -> str:
        project = self.project()
        for cell in ("r1", "r2", "r3"):
            self.r.ok(project, "add", cell)
        for cell in ("o1", "o2", "o3"):
            self.r.ok(project, "add", "--optional", cell)
        self.r.ok(project, "done", "r2")
        self.r.ok(project, "mark", "r3", "na")
        self.r.ok(project, "done", "o1")
        self.r.ok(project, "mark", "o2", "na")
        data, code, _ = self.r.status(project)
        expect(code == 1, f"exit {code}")
        expect(data["required"] == {"open": 1, "done": 1, "na": 1}, f"required {data['required']}")
        expect(data["optional"] == {"open": 1, "done": 1, "na": 1}, f"optional {data['optional']}")
        expect((data["cells"], data["groups"], data["required_gaps"]) == (6, 0, 1), f"counts {counts(data)}")
        return "required and optional open/done/na each 1"

    def c05_expanded_group(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "auth")
        self.r.ok(project, "add", "auth.login")
        self.r.ok(project, "add", "--optional", "auth.audit")
        self.r.ok(project, "add", "--optional", "ops")
        self.r.ok(project, "add", "ops.alerts")  # inherits optional parent
        self.r.ok(project, "add", "tests")
        self.r.ok(project, "done", "tests")
        data, code, _ = self.r.status(project)
        expect(code == 1, f"exit {code}")
        expect(counts(data) == (6, 2, 1, 1, 0, 2, 0, 0, 1), f"counts {counts(data)}")
        self.r.ok(project, "done", "auth.login")
        data, code, _ = self.r.status(project)
        expect(code == 0 and counts(data) == (6, 2, 0, 2, 0, 2, 0, 0, 0), f"after done: exit {code}, {counts(data)}")
        return "groups 2, cells == groups + leaves, children of optional parent counted optional"

    def c06_deterministic(self) -> str:
        outputs = []
        for _ in range(2):
            project = self.project("Deterministic")
            self.r.ok(project, "add", "a")
            self.r.ok(project, "add", "a.b")
            self.r.ok(project, "add", "--optional", "c")
            for _ in range(3):
                outputs.append(self.r.status(project)[2])
        expect(len(set(outputs)) == 1, f"outputs differ: {set(outputs)}")
        return "6 runs over 2 identical states byte-identical"

    def c07_read_only(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "a")
        self.r.ok(project, "add", "--optional", "b")
        before = snapshot(project)
        expect(".mandala/state.json" in before, "state.json missing from snapshot")
        for args in (["status", "--json"], ["status"], ["status", "--json=false"], ["status", "--json"]):
            self.r.run(["--project", str(project), *args], project)
        expect(snapshot(project) == before, "status changed project files or metadata")
        self.r.ok(project, "done", "a")
        before = snapshot(project)
        self.r.status(project)
        expect(snapshot(project) == before, "status changed project files or metadata at exit 0")
        return "state bytes, mode, size, mtime unchanged at exit 1 and 0"

    def c08_text_status(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "a")
        self.r.ok(project, "add", "--optional", "b")
        plain = self.r.run(["--project", str(project), "status"], project)
        disabled = self.r.run(["--project", str(project), "status", "--json=false"], project)
        expect(plain == disabled, f"status and --json=false differ: {plain!r} vs {disabled!r}")
        expect(plain[0] == 1 and plain[1].startswith("goal: ") and "required gaps: 1\n" in plain[1], f"text output changed: {plain!r}")
        data = self.r.status(project)[0]
        expect(f"required gaps: {data['required_gaps']}" in plain[1], "text and JSON gap counts differ")
        return "status == status --json=false (stdout, stderr, exit)"

    def c09_invalid_flags(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "a")
        target = ["--project", str(project)]
        for argv in (target + ["--json", "status"], ["--json", *target, "status"], target + ["status", "--bogus"],
                     target + ["status", "--json", "extra"], target + ["status", "--json=maybe"]):
            code, stdout, stderr = self.r.run(argv, project)
            expect(code == 2, f"{argv} exit {code}")
            expect(stdout == "", f"{argv} wrote stdout {stdout!r}")
            expect("E_USAGE" in stderr, f"{argv} stderr {stderr!r}")
        return "global --json, unknown flag, extra argument, bad bool: exit 2, empty stdout, E_USAGE"

    def c10_missing_project(self) -> str:
        empty = self.base / "not-a-project"
        empty.mkdir()
        regular = self.base / "regular-file"
        regular.write_text("not a directory\n", encoding="utf-8")
        cases = ((empty, "E_NO_PROJECT"), (self.base / "missing", "E_"), (regular, "E_USAGE"))
        for path, error in cases:
            code, stdout, stderr = self.r.run(["--project", str(path), "status", "--json"], self.base)
            expect(code == 2 and stdout == "" and error in stderr, f"{path.name}: exit {code}, stdout {stdout!r}, stderr {stderr!r}")
        code, stdout, stderr = self.r.run(["--project", "", "status", "--json"], self.base)
        expect(code == 2 and stdout == "" and "E_USAGE" in stderr, f"empty --project: exit {code}")
        return "absent state, missing path, file path, empty --project: exit 2, empty stdout"

    def c11_show_and_gaps(self) -> str:
        project = self.project("Show and gaps")
        code, stdout, _ = self.r.run(["--project", str(project), "show", "--json"], project)
        expect(stdout == '{"schema_version":1,"goal":"Show and gaps","cells":[]}\n' and code == 0, f"empty show {stdout!r}")
        code, stdout, _ = self.r.run(["--project", str(project), "gaps", "--required", "--json"], project)
        expect((code, stdout) == (0, '{"schema_version":1,"gaps":[]}\n'), f"empty gaps {code} {stdout!r}")
        self.r.ok(project, "add", "b")
        self.r.ok(project, "add", "a")
        self.r.ok(project, "add", "a.x")
        self.r.ok(project, "add", "--optional", "c")
        code, stdout, _ = self.r.run(["--project", str(project), "show", "--json"], project)
        show = parse_show_json(stdout, code)
        expect([cell["id"] for cell in show["cells"]] == ["a", "a.x", "b", "c"], "show order")
        expect(show["cells"][0] == {"id": "a", "parent": "", "status": "expanded", "required": True}, f"expanded parent {show['cells'][0]}")
        code, stdout, _ = self.r.run(["--project", str(project), "gaps", "--json"], project)
        all_gaps = parse_gaps_json(stdout, code)
        expect(code == 1 and [(g["id"], g["required"]) for g in all_gaps] == [("a.x", True), ("b", True), ("c", False)], f"gaps --json {stdout!r}")
        ids, code = self.r.required_gap_ids(project)
        expect(code == 1 and ids == ["a.x", "b"], f"gaps --required --json {ids} exit {code}")
        return "show --json and gaps [--required] --json shapes and exits unchanged"

    def c12_explicit_project(self) -> str:
        first = self.project("Project A")
        self.r.ok(first, "add", "a")
        second = self.project("Project B")
        nested = first / "sub"
        nested.mkdir()
        data, code, _ = self.r.status(second, cwd=first)
        expect(code == 0 and data["goal"] == "Project B" and data["cells"] == 0, f"--project B from A: {data}")
        data, code, _ = self.r.status(first, cwd=second)
        expect(code == 1 and data["goal"] == "Project A" and data["cells"] == 1, f"--project A from B: {data}")
        code, stdout, _ = self.r.run(["--project", os.path.relpath(str(second), str(first)), "status", "--json"], first)
        expect(parse_status_json(stdout, code)["goal"] == "Project B", "relative --project not resolved against cwd")
        code, stdout, stderr = self.r.run(["--project", str(nested), "status", "--json"], second)
        expect(code == 2 and stdout == "" and "E_NO_PROJECT" in stderr, f"explicit nested dir searched parents: exit {code}")
        code, stdout, _ = self.r.run(["status", "--json"], nested)
        expect(parse_status_json(stdout, code)["goal"] == "Project A", "implicit discovery from nested dir")
        return "explicit --project reads only the target; no parent search for explicit paths"

    def c13_special_goals(self) -> str:
        goals = (
            'Quote "double" and \'single\'',
            "Backslash \\ and C:\\path\\x",
            "Unicode 日本語 🚀 é ü",
            "<tag> & ampersand >",
            "  leading and trailing spaces  ",
            "Emoji ZWJ 👩‍💻 and combining é",
        )
        for goal in goals:
            project = self.project(goal)
            data, code, stdout = self.r.status(project)
            expect(code == 0 and data["goal"] == goal, f"goal round-trip failed: {goal!r} -> {data['goal']!r}")
            code, show_out, _ = self.r.run(["--project", str(project), "show", "--json"], project)
            expect(parse_show_json(show_out, code)["goal"] == goal, f"show goal mismatch for {goal!r}")
        rejected = self.base / "control"
        rejected.mkdir()
        code, stdout, _ = self.r.run(["--project", str(rejected), "init", "line\nbreak"], rejected)
        expect(code == 2 and stdout == "", "control-character goal accepted")
        return f"{len(goals)} goals round-trip through status/show JSON; control characters rejected at init"

    def c14_completion_safety(self) -> str:
        project = self.project()
        self.r.ok(project, "add", "a")
        self.r.ok(project, "add", "a.x")
        self.r.ok(project, "add", "b")
        self.r.ok(project, "add", "--optional", "c")
        data, code, _ = self.r.status(project)
        ids, gaps_code = self.r.required_gap_ids(project)
        expect(code == gaps_code == 1, f"exit codes status {code} gaps {gaps_code}")
        expect(ids == ["a.x", "b"] and len(ids) == data["required_gaps"], f"IDs {ids} vs required_gaps {data['required_gaps']}")
        expect(not any(isinstance(value, list) for value in data.values()), "status --json unexpectedly lists IDs")
        self.r.ok(project, "done", "a.x")
        self.r.ok(project, "done", "b")
        data, code, _ = self.r.status(project)
        ids, gaps_code = self.r.required_gap_ids(project)
        expect((code, gaps_code, ids, data["required_gaps"]) == (0, 0, [], 0), "zero required gaps disagree")
        self.r.ok(project, "mark", "b", "open")
        data, code, _ = self.r.status(project)
        ids, gaps_code = self.r.required_gap_ids(project)
        expect((code, gaps_code, ids, data["required_gaps"]) == (1, 1, ["b"], 1), "reopened gap not reported fresh")
        return "required-gap IDs come only from gaps --required --json; counts agree; reopen is reflected fresh"


CASE_IDS = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14")


def case_methods(cases: Cases) -> List[Tuple[str, Callable[[], str]]]:
    names = [name for name in dir(Cases) if name[:1] == "c" and name[1:3].isdigit()]
    return [(name[:3].upper(), getattr(cases, name)) for name in sorted(names)]


class ReportError(Exception):
    """The report path is unsafe or could not be created as a new file."""


def prepare_report_path(path: Path) -> Path:
    """Return a safe absolute report path, or raise ReportError.

    The report must be a new file in an existing directory outside `.git/` and `.mandala/`, judged on
    both the given path and the symlink-resolved directory. The final create is exclusive anyway.
    """
    absolute = Path(os.path.abspath(str(path)))  # normalizes `..` lexically; the final component is not resolved
    parent = absolute.parent
    if absolute.name in ("", ".", "..") or not parent.is_dir():
        raise ReportError(f"report directory does not exist (it is never created): {parent}")
    real_parent = Path(os.path.realpath(str(parent)))
    for parts in (absolute.parts, real_parent.parts + (absolute.name,)):
        protected = [part for part in parts if part in PROTECTED_DIRECTORIES]
        if protected:
            raise ReportError(f"report path is inside a protected {protected[0]}/ directory: {path}")
    if os.path.lexists(str(absolute)):
        raise ReportError(f"report path already exists and is never overwritten: {path}")
    return real_parent / absolute.name


def write_report(path: Path, text: str) -> None:
    """Create the report exclusively ("x" mode: O_CREAT|O_EXCL); an existing file or symlink is never opened."""
    try:
        with open(str(path), "x", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise ReportError(f"could not create report {path}: {exc}") from exc


def inside_git_or_mandala(path: Path) -> Optional[Path]:
    for parent in (path, *path.parents):
        if (parent / ".git").exists() or (parent / ".mandala").exists():
            return parent
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check an installed Mandala CLI against the v0.4.0 contract (explicit, local, no install).")
    parser.add_argument("--mandala", default="mandala", help="CLI executable (default: mandala on PATH); used as given, never installed")
    parser.add_argument("--report", type=Path, help="write a JSON report with every command run")
    args = parser.parse_args(argv)
    report: Optional[Path] = None
    if args.report is not None:
        try:
            report = prepare_report_path(args.report)
        except ReportError as exc:
            print(f"REPORT_ERROR: {exc}; no case was run", file=sys.stderr)
            return REPORT_ERROR_EXIT
    found = shutil.which(args.mandala)
    # Resolve once to an absolute path: cases run with other working directories and must use this same binary.
    executable = os.path.abspath(found) if found else None
    results: List[Dict[str, str]] = []
    runner = Runner(executable or args.mandala)

    def finish(verdict: str, code: int) -> int:
        summary = {status: sum(1 for item in results if item["status"] == status) for status in ("PASS", "FAIL", "SKIP")}
        counts = f"{summary['PASS']} PASS, {summary['FAIL']} FAIL, {summary['SKIP']} SKIP (required CLI {REQUIRED_VERSION})"
        # Save the report before announcing a verdict, so a failed save never leaves a verdict line behind.
        if report is not None:
            try:
                write_report(report, json.dumps({"schema_version": 1, "verdict": verdict, "required_cli": REQUIRED_VERSION,
                                                 "mandala": executable, "results": results, "commands": runner.log},
                                                ensure_ascii=False, indent=2) + "\n")
            except ReportError as exc:
                print(f"REPORT_ERROR: {exc}; no verdict is reported (unsaved case counts: {counts})")
                return REPORT_ERROR_EXIT
        print(f"{verdict}: {counts}")
        return code

    def skip_all(reason: str) -> int:
        for case_id in CASE_IDS:
            results.append({"case": case_id, "status": "SKIP", "detail": reason})
            print(f"SKIP {case_id}: {reason}")
        return finish("NOT_RUN", 2)

    if executable is None:
        return skip_all(f"{args.mandala!r} not found (install is not automatic)")
    code, stdout, stderr = runner.run(["--version"], Path.cwd())
    if code != 0 or stdout != REQUIRED_VERSION + "\n" or stderr:
        return skip_all(f"`mandala --version` printed {stdout!r} exit {code}; required {REQUIRED_VERSION!r}")
    print(f"CLI: {executable} ({REQUIRED_VERSION})")
    with tempfile.TemporaryDirectory(prefix="mandala-cli-compat-") as directory:
        base = Path(directory).resolve()
        enclosing = inside_git_or_mandala(base)
        if enclosing is not None:
            return skip_all(f"temporary directory is inside {enclosing} (Git or Mandala project); set TMPDIR elsewhere")
        cases = Cases(runner, base)
        for case_id, method in case_methods(cases):
            try:
                detail = method()
                results.append({"case": case_id, "status": "PASS", "detail": detail})
                print(f"PASS {case_id}: {detail}")
            except CompatError as exc:
                results.append({"case": case_id, "status": "FAIL", "detail": str(exc)})
                print(f"FAIL {case_id}: {exc}")
            except Exception as exc:  # an unexpected harness or environment error is a FAIL, never a PASS
                results.append({"case": case_id, "status": "FAIL", "detail": f"unexpected {type(exc).__name__}: {exc}"})
                print(f"FAIL {case_id}: unexpected {type(exc).__name__}: {exc}")
    if [item["case"] for item in results] != list(CASE_IDS):
        raise SystemExit(f"internal error: cases ran as {[item['case'] for item in results]}")
    failed = any(item["status"] == "FAIL" for item in results)
    return finish("FAIL" if failed else "PASS", 1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
