"""Shared helpers for offline live-eval artifact tools (replay, coverage, sanitize).

Recorded artifact directories are untrusted local input: symlinks are rejected, files are
only read, and nothing here executes a recorded command. Git is the only subprocess and is
used solely to record the current repository revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

if __package__:
    from . import live_eval_cases as cases_mod
else:
    import live_eval_cases as cases_mod

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / ".eval-live"
PLACEHOLDER = "<project-root>"
SCHEMA_VERSION = 1

EVIDENCE_TYPE = "mandala-live-evidence"
REPLAY_SUMMARY_TYPE = "mandala-eval-replay-summary"
REPLAY_CASE_TYPE = "mandala-eval-replay-case"
COVERAGE_TYPE = "mandala-contract-coverage"
SANITIZED_TYPE = "mandala-sanitized-export"
FIELD_USAGE_TYPE = "mandala-field-usage-example"
PROFILE_REPORT_TYPE = "mandala-coverage-profile-report"

GRADED_STATUSES = ("AUTO_PASS", "AUTO_FAIL", "INCONCLUSIVE")
LIVE_STATUSES = GRADED_STATUSES + ("ENVIRONMENT_ERROR", "UNSUPPORTED", "NOT_RUN")
CHECK_STATUSES = (cases_mod.PASS, cases_mod.FAIL, cases_mod.UNOBSERVABLE)
ALIAS = re.compile(r"[A-Z][0-9]+")
# Fields every live agent run summary.json has; an unrelated summary.json is not guessed to be one.
LIVE_SUMMARY_KEYS = ("run_id", "agent", "suite", "skill_sha256")
# Fixed, repository-independent stand-in for <project-root> during in-memory replay grading.
SYNTHETIC_PROJECT_ROOT = "/__mandala_replay__/project-root"


class ArtifactError(ValueError):
    """Malformed, unsupported, or unsafe artifact input (tools exit 2)."""


# ---------------------------------------------------------------------------
# Small utilities


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(kind: str, agent: Optional[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{kind}-{agent or 'unknown'}-{secrets.token_hex(3)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(read_bytes(path))


def dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value) -> None:
    path.write_text(dump_json(value), encoding="utf-8")


def git_revision() -> Tuple[Optional[str], Optional[bool]]:
    """(HEAD SHA, dirty) of this repository; (None, None) when Git is unavailable."""
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if head.returncode != 0 or status.returncode != 0:
        return None, None
    return head.stdout.strip() or None, bool(status.stdout.strip())


def is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


# ---------------------------------------------------------------------------
# Source directory safety


def source_root(path: str) -> Path:
    """Validate a recorded artifact directory: it must be a real directory, not a symlink."""
    candidate = Path(os.path.abspath(path))
    try:
        mode = os.lstat(str(candidate)).st_mode
    except OSError:
        raise ArtifactError(f"source directory not found: {path}") from None
    if stat.S_ISLNK(mode):
        raise ArtifactError("source directory is a symlink; pass the real directory")
    if not stat.S_ISDIR(mode):
        raise ArtifactError("source path is not a directory")
    return candidate.resolve()


def source_file(root: Path, *parts: str, required: bool = True) -> Optional[Path]:
    """Path of root/parts after checking that no component is a symlink and the leaf is a regular file."""
    current = root
    label = "/".join(parts)
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(str(current)).st_mode
        except FileNotFoundError:
            if required:
                raise ArtifactError(f"missing source artifact: {label}") from None
            return None
        if stat.S_ISLNK(mode):
            raise ArtifactError(f"symlink in source artifact path: {'/'.join(parts[:index + 1])}")
        last = index == len(parts) - 1
        if (last and not stat.S_ISREG(mode)) or (not last and not stat.S_ISDIR(mode)):
            raise ArtifactError(f"unexpected file type in source artifact path: {'/'.join(parts[:index + 1])}")
    return current


def reject_symlinks(root: Path, relative: str) -> None:
    """Reject any symlink anywhere under root/relative (the tree is never followed through links)."""
    top = root / relative
    try:
        mode = os.lstat(str(top)).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ArtifactError(f"symlink in source artifacts: {relative}")
    for directory, dirnames, filenames in os.walk(str(top)):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(directory, name)):
                shown = os.path.relpath(os.path.join(directory, name), str(root))
                raise ArtifactError(f"symlink in source artifacts: {shown}")


def read_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def load_json(path: Path, label: str):
    try:
        return json.loads(read_bytes(path).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArtifactError(f"{label} is not valid JSON: {exc}") from None


def load_jsonl(path: Path, label: str) -> List[dict]:
    events = []
    try:
        text = read_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactError(f"{label} is not UTF-8") from None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise ArtifactError(f"{label} line {number} is not valid JSON") from None
        if not isinstance(event, dict):
            raise ArtifactError(f"{label} line {number} is not an object")
        events.append(event)
    return events


def require_schema(value, label: str, artifact_type: Optional[str] = None) -> None:
    """Supported schema check: version 1 only; never guess at other versions."""
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} is not a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError(f"{label} has unsupported schema_version {value.get('schema_version')!r} (supported: {SCHEMA_VERSION})")
    if artifact_type is not None and value.get("artifact_type") != artifact_type:
        raise ArtifactError(f"{label} has artifact_type {value.get('artifact_type')!r}, expected {artifact_type!r}")


# ---------------------------------------------------------------------------
# Run kinds


class Source:
    """A validated live agent run directory or replay output directory."""

    def __init__(self, path: str, allowed: Tuple[str, ...] = ("live", "replay")):
        self.root = source_root(path)
        summary_path = source_file(self.root, "summary.json", required=False)
        if summary_path is None:
            agents = sorted(child.name for child in self.root.iterdir() if not child.is_symlink() and child.is_dir() and (child / "summary.json").is_file())
            if agents:
                raise ArtifactError(f"source contains agent run directories ({', '.join(agents)}); pass exactly one agent-level directory such as <run>/{agents[0]}")
            raise ArtifactError("source has no summary.json; pass an agent-level live run directory or a replay output directory")
        self.summary_bytes = read_bytes(summary_path)
        try:
            summary = json.loads(self.summary_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ArtifactError("summary.json is not valid JSON") from None
        if not isinstance(summary, dict):
            raise ArtifactError("summary.json is not a JSON object")
        artifact_type = summary.get("artifact_type")
        if artifact_type is None:
            kind = "live"
            require_schema(summary, "summary.json")
            missing = [key for key in LIVE_SUMMARY_KEYS if not isinstance(summary.get(key), str) or not summary[key]]
            if missing:
                raise ArtifactError(f"summary.json is not a live agent run summary (missing {', '.join(missing)})")
        elif artifact_type == REPLAY_SUMMARY_TYPE:
            kind = "replay"
            require_schema(summary, "summary.json", REPLAY_SUMMARY_TYPE)
        else:
            raise ArtifactError(f"unsupported source artifact_type {artifact_type!r}")
        if kind not in allowed:
            raise ArtifactError(f"source is a {kind} directory; this tool accepts: {', '.join(allowed)}")
        if not isinstance(summary.get("cases"), list):
            raise ArtifactError("summary.json has no cases list")
        for entry in summary["cases"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("case"), str) or not ALIAS.fullmatch(entry["case"]):
                raise ArtifactError(f"summary.json has an invalid case entry: {entry!r:.80}")
        if len({entry["case"] for entry in summary["cases"]}) != len(summary["cases"]):
            raise ArtifactError("summary.json lists a case more than once")
        reject_symlinks(self.root, "cases")
        self.kind = kind
        self.summary = summary

    @property
    def aliases(self) -> List[str]:
        return [entry["case"] for entry in self.summary["cases"]]

    @property
    def run_id(self) -> Optional[str]:
        return self.summary.get("replay_id") if self.kind == "replay" else self.summary.get("run_id")

    @property
    def agent(self) -> Optional[str]:
        return (self.summary.get("source") or {}).get("agent") if self.kind == "replay" else self.summary.get("agent")

    @property
    def suite(self) -> Optional[str]:
        return (self.summary.get("source") or {}).get("suite") if self.kind == "replay" else self.summary.get("suite")

    def case_entry(self, alias: str) -> dict:
        return next(entry for entry in self.summary["cases"] if entry["case"] == alias)

    def case_file(self, alias: str, name: str, required: bool = False) -> Optional[Path]:
        return source_file(self.root, "cases", alias, name, required=required)

    def case_result(self, alias: str) -> Optional[dict]:
        """Validated case result.json, or None when the case has no result artifact."""
        path = self.case_file(alias, "result.json")
        if path is None:
            return None
        result = load_json(path, f"cases/{alias}/result.json")
        if self.kind == "replay":
            require_schema(result, f"cases/{alias}/result.json", REPLAY_CASE_TYPE)
        else:
            require_schema(result, f"cases/{alias}/result.json")
            if "artifact_type" in result:
                raise ArtifactError(f"cases/{alias}/result.json has unexpected artifact_type {result['artifact_type']!r}")
        if result.get("case") != alias:
            raise ArtifactError(f"cases/{alias}/result.json records case {result.get('case')!r}")
        return result


# ---------------------------------------------------------------------------
# Output directory safety


def prepare_output_dir(path: Path, sources: List[Path]) -> Path:
    """Create or accept an empty output directory that cannot overlap any source.

    Inside this repository only `.eval-live/` may hold outputs (never src/, dist/, or .mandala/).
    Existing directories are never cleaned.
    """
    target = Path(os.path.abspath(str(path))).resolve()
    for source in sources:
        resolved = source.resolve()
        if target == resolved:
            raise ArtifactError("output directory is the source directory")
        if is_within(resolved, target):
            raise ArtifactError("output directory is an ancestor of the source directory")
        if is_within(target, resolved):
            raise ArtifactError("output directory is inside the source directory")
    root = ROOT.resolve()
    if ".mandala" in target.parts:
        raise ArtifactError("output directory not allowed under .mandala/")
    if is_within(target, root) and not is_within(target, root / ".eval-live"):
        raise ArtifactError("output directory inside the repository must be under .eval-live/")
    if target == root / ".eval-live":
        raise ArtifactError("output directory must be a subdirectory of .eval-live/")
    if os.path.lexists(str(target)):
        if os.path.islink(str(target)) or not target.is_dir():
            raise ArtifactError("output path exists and is not a directory")
        if any(target.iterdir()):
            raise ArtifactError("output directory exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Snapshots and grading


def build_evidence(alias: str, fixture_id: str, turns_expected: int, turns_completed: int, before: Optional[dict], after_turn: Dict[int, dict]) -> dict:
    """Full normalized snapshots for replay; unavailable slots are null and missing turns are not invented."""
    return {
        "schema_version": SCHEMA_VERSION, "artifact_type": EVIDENCE_TYPE, "case": alias, "fixture": fixture_id,
        "turns_expected": turns_expected, "turns_completed": turns_completed, "before": before,
        "after_turn": {str(number): after_turn.get(number) for number in range(1, turns_expected + 1)},
    }


def valid_snapshot(snapshot) -> bool:
    """Shape of cases_mod.snapshot_from_show() output."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"present", "exit_code", "state", "error"}:
        return False
    if snapshot["present"] not in (True, False, None) or not isinstance(snapshot["exit_code"], int) or isinstance(snapshot["exit_code"], bool):
        return False
    state = snapshot["state"]
    if state is None:
        return snapshot["present"] is not True
    if snapshot["present"] is not True or not isinstance(state, dict) or set(state) != {"goal", "cells"} or not isinstance(state["cells"], list):
        return False
    return all(isinstance(cell, dict) and set(cell) == {"id", "parent", "status", "required"} and isinstance(cell["id"], str) for cell in state["cells"])


def snapshots_from_evidence(evidence, alias: str, fixture_id: str, turns: int) -> Tuple[Optional[dict], Optional[Dict[int, dict]], Optional[str]]:
    """(before, after_turn, reason). A reason means the evidence cannot support a replay."""
    if evidence.get("case") != alias or evidence.get("fixture") != fixture_id:
        return None, None, "evidence.json case/fixture does not match result.json"
    if evidence.get("turns_expected") != turns or evidence.get("turns_completed") != turns:
        return None, None, f"evidence.json records {evidence.get('turns_completed')!r} of {evidence.get('turns_expected')!r} turns; {turns} required"
    slots = evidence.get("after_turn")
    if not isinstance(slots, dict):
        return None, None, "evidence.json after_turn is not an object"
    if evidence.get("before") is None:
        return None, None, "evidence.json before snapshot is unavailable"
    if not valid_snapshot(evidence["before"]):
        return None, None, "evidence.json before snapshot is malformed"
    after_turn = {}
    for number in range(1, turns + 1):
        snapshot = slots.get(str(number))
        if snapshot is None:
            return None, None, f"evidence.json after_turn[{number}] snapshot is unavailable"
        if not valid_snapshot(snapshot):
            return None, None, f"evidence.json after_turn[{number}] snapshot is malformed"
        after_turn[number] = snapshot
    return evidence["before"], after_turn, None


def legacy_snapshots(events: List[dict], turns: int) -> Tuple[Optional[dict], Optional[Dict[int, dict]], Optional[str]]:
    """Rebuild before/after_turn from evaluator snapshot events in normalized.jsonl.

    Only actor=evaluator, phase=snapshot, kind=command events count. Each slot (turn 0 = before,
    turn N = after Turn N) needs exactly one event whose output parses completely; truncated or
    invalid JSON, a missing slot, or a duplicate is a reason, never a guess.
    """
    slots: Dict[int, List[dict]] = {}
    for event in events:
        if event.get("actor") == "evaluator" and event.get("phase") == "snapshot" and event.get("kind") == "command":
            slots.setdefault(event.get("turn"), []).append(event)
    unexpected = sorted(str(turn) for turn in slots if not (isinstance(turn, int) and 0 <= turn <= turns))
    if unexpected:
        return None, None, f"legacy snapshot events for unexpected turns: {unexpected}"
    rebuilt: Dict[int, dict] = {}
    for turn in range(0, turns + 1):
        name = "before" if turn == 0 else f"after_turn[{turn}]"
        found = slots.get(turn, [])
        if not found:
            return None, None, f"legacy {name} snapshot is missing from normalized.jsonl"
        if len(found) > 1:
            return None, None, f"legacy {name} snapshot is ambiguous ({len(found)} evaluator snapshots)"
        event = found[0]
        exit_code, output = event.get("exit_code"), event.get("output")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not isinstance(output, str):
            return None, None, f"legacy {name} snapshot has no exit code or output"
        if exit_code == 0 and not _show_payload_shape(output):
            return None, None, f"legacy {name} snapshot output is invalid or truncated JSON"
        try:
            snapshot = cases_mod.snapshot_from_show(0, output, "") if exit_code == 0 else cases_mod.snapshot_from_show(exit_code, "", output)
        except (TypeError, KeyError):
            snapshot = None
        if snapshot is None or not valid_snapshot(snapshot) or (exit_code == 0 and snapshot["state"] is None):
            return None, None, f"legacy {name} snapshot output is invalid or truncated JSON"
        rebuilt[turn] = snapshot
    return rebuilt[0], {turn: rebuilt[turn] for turn in range(1, turns + 1)}, None


def _show_payload_shape(output: str) -> bool:
    """A complete `show --json` object: a goal key and a list of cell objects with string IDs."""
    try:
        payload = json.loads(output)
    except ValueError:
        return False
    return (isinstance(payload, dict) and "goal" in payload and isinstance(payload.get("cells"), list)
            and all(isinstance(cell, dict) and isinstance(cell.get("id"), str) for cell in payload["cells"]))


def synthetic_project_root() -> str:
    """Absolute stand-in for <project-root> so recorded commands parse as they did live.

    Fixed and independent of the repository or source location; it is never created, and no command runs.
    """
    return SYNTHETIC_PROJECT_ROOT


def replace_text(value, old: str, new: str):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [replace_text(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_text(item, old, new) for key, item in value.items()}
    return value


def grade_recorded(grader_name: str, events: List[dict], project: str, before: Optional[dict], after_turn: Dict[int, dict], turns: int) -> Tuple[List[dict], str]:
    """Current deterministic grading of recorded events and snapshots: (sorted checks, status)."""
    context = {"events": events, "project": project, "before": before, "after": after_turn[turns], "after_turn": after_turn}
    checks = cases_mod.GRADERS[grader_name]["grade"](context)
    denial = cases_mod.permission_check(events, project)
    checks = sorted(checks + ([denial] if denial else []), key=lambda item: item["id"])
    return checks, cases_mod.case_status(checks)


# ---------------------------------------------------------------------------
# Redaction


def known_locations(source: Optional[Path] = None) -> List[Tuple[str, str]]:
    """(path, placeholder) pairs, longest path first, for free-text metadata reduction."""
    pairs = []
    if source is not None:
        pairs += [(str(source), "<source-run>"), (os.path.realpath(str(source)), "<source-run>")]
    pairs += [(str(ROOT), "<repo-root>"), (os.path.realpath(str(ROOT)), "<repo-root>")]
    home = os.path.expanduser("~")
    if home and home != "~" and home != "/":
        pairs += [(home, "<home>"), (os.path.realpath(home), "<home>")]
    temp = tempfile.gettempdir()
    pairs += [(temp, "<tmp>"), (os.path.realpath(temp), "<tmp>")]
    unique = {path: placeholder for path, placeholder in pairs if path and path != "/"}
    return sorted(unique.items(), key=lambda pair: len(pair[0]), reverse=True)


GENERIC_HOME = re.compile(r"(?:/Users|/home)/[^/\s\"'`<>]+|[A-Za-z]:\\Users\\[^\\\s\"'`<>]+")


def redact(value, locations: List[Tuple[str, str]]):
    """Replace known local paths (and any other user-home prefix) in retained strings."""
    if isinstance(value, str):
        for path, placeholder in locations:
            value = value.replace(path, placeholder)
        return GENERIC_HOME.sub("<home>", value)
    if isinstance(value, list):
        return [redact(item, locations) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, locations) for key, item in value.items()}
    return value
