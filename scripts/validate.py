#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import re
import sys
from typing import Final

if __package__:
    from .package_safety import require_real_directory_path
else:
    from package_safety import require_real_directory_path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (ROOT / "src" / "mandala", ROOT / "dist" / "mandala")
EXPECTED = {Path("SKILL.md"), Path("references/cli-contract.md")}
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LOCAL_PATH = re.compile(rb"(?:/(?:Users|home)/[^\s`'\"<>/]+/|[A-Za-z]:[\\/]+Users[\\/]+[^\s`'\"<>\\/]+[\\/])")
SAFETY_CONTRACT = {
    "explicit-use": "Mutate state only when the user clearly asks to use Mandala",
    "generic-request": "General planning, review, gap analysis, or task management alone must not start Mandala state.",
    "explicit-non-use": "If the user explicitly asks not to use Mandala, do not mutate Mandala state.",
    "read-before-write": "Before the first Mandala mutation in each new user turn, run `mandala --project <project-root> show --json` and inspect the current state.",
    "no-cached-state-inspection": "A state inspection from an earlier turn does not satisfy this requirement.",
    "cli-owned-state": "Never directly edit `.mandala/state.json` or create notes, plans, ledgers, logs, scratch files, or agent metadata under `.mandala/`.",
    "no-automatic-clean": "`clean` requires a separate, explicit destructive request to remove Mandala state; do not run it at task completion.",
    "reset-is-not-delete": "A request to reset, reinitialize, or start Mandala over does not by itself authorize `clean` or deletion of existing state.",
    "reset-preserves-valid-state": "If valid state exists, preserve it without `clean` or `init`, explain that reinitialization requires deleting existing state, and ask for a separate explicit destructive request before deletion.",
    "backup-is-not-delete": "A backup does not authorize deletion.",
    "reset-preserves-invalid-state": "If state is invalid or corrupt, report the problem without automatically deleting or repairing it.",
    "missing-cli": "Do not create a custom or fake `.mandala/` state implementation.",
    "exit-one": "Exit `1` from `status` or `gaps` means unresolved required gaps, not a crash.",
    "zero-gap": "Zero required gaps means **only that currently declared required leaf cells are resolved**.",
    "completion-gate": "For active Mandala tracking, run `mandala --project <project-root> gaps --required --json` in the same user turn immediately before any completion claim, including a claim that no declared required gaps remain, and inspect both its JSON payload and exit code.",
    "no-cached-gap-result": "A previous turn's gap result, including zero gaps, does not satisfy this completion gate.",
    "no-gap-clearing-na": "Never use `na` merely to eliminate a gap.",
    "capacity-status-counts": "`done` and `na` do not free structural capacity; resolved and optional cells still count toward root, child, and total-cell limits.",
    "no-capacity-status-workaround": "Never propose or perform status changes to existing cells (`done` or `na`),",
    "no-capacity-clean-workaround": "`clean` or reinitialization,",
    "no-capacity-coverage-replacement": "or removal or replacement of unrelated declared coverage merely to make room for another cell.",
    "capacity-restructure-direction": "When no legal slot is available for the requested cell, report the structural limit, preserve existing state, and ask the user for clear direction before restructuring declared coverage.",
    "separate-verification": "Mandala gap status is separate from test results, behavior verification, and task-specific inspection.",
}
REQUIRED_SCENARIOS = {
    "generic-gap-analysis", "explicit-tracking", "contextual-update", "zero-gaps",
    "reset-request", "gaps-exit-one-manual", "missing-cli-manual",
    "explicit-non-use-manual", "generic-task-management", "explicit-clean",
    "completion-state-changed",
    "capacity-full-child", "capacity-full-tree", "capacity-final-child",
}
TURN_SCENARIOS = {"contextual-update", "zero-gaps", "completion-state-changed"}
CLI_BASELINE: Final = "Mandala CLI v0.3.0"
CLI_CHECK: Final = "mandala --version"
VERSION_OUTPUT: Final = "mandala v0.3.0"
PINNED_INSTALL = "go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0"
OBSOLETE_VERSION: Final = re.compile(
    r"Do not (?:assume|rely on)[^\n.]*--version|no public[^\n.]*--version"
    r"|There is no required[^\n.]*--version|--version[^\n。]*(?:必須確認に使いません|前提にしません)",
    re.IGNORECASE,
)


def validate_cli_baseline(text: str, name: str) -> None:
    if CLI_BASELINE not in text or CLI_CHECK not in text or VERSION_OUTPUT not in text:
        raise ValueError(f"missing CLI baseline/version check: {name}")
    if "v0.2.0" in text or OBSOLETE_VERSION.search(text):
        raise ValueError(f"obsolete CLI baseline/version guidance: {name}")


def active_instruction_text(markdown: str) -> str:
    lines = markdown.splitlines()
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError("unterminated YAML frontmatter") from exc
        lines = lines[end + 1:]
    without_comments = re.sub(r"<!--.*?(?:-->|\Z)", "\n", "\n".join(lines), flags=re.DOTALL)
    active = []
    fence = ""
    for line in without_comments.splitlines():
        if fence:
            closing = rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*"
            if re.fullmatch(closing, line):
                fence = ""
            continue
        opening = FENCE_OPEN.fullmatch(line)
        if opening:
            marker, info = opening.groups()
            if marker[0] == "~" or "`" not in info:
                fence = marker
                continue
        active.append(line)
    return "\n".join(active)


def validate_safety_contract(skill: str) -> None:
    active_prose = active_instruction_text(skill)
    for name, clause in SAFETY_CONTRACT.items():
        if clause not in active_prose:
            raise ValueError(f"missing safety contract: {name}")


def validate_eval_metadata(cases: list[dict]) -> None:
    ids = set()
    covered = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"] or not isinstance(case.get("prompt"), str) or not case["prompt"] or not isinstance(case.get("expected"), list) or not case["expected"] or not all(isinstance(item, str) and item for item in case["expected"]):
            raise ValueError("invalid eval case schema")
        contracts = case.get("contracts")
        if not isinstance(contracts, list) or not contracts or not all(isinstance(item, str) and item in SAFETY_CONTRACT for item in contracts):
            raise ValueError(f"invalid eval contract metadata: {case['id']}")
        if "setup" in case and (not isinstance(case["setup"], str) or not case["setup"]):
            raise ValueError(f"invalid eval setup: {case['id']}")
        if "turns" in case or case["id"] in TURN_SCENARIOS:
            turns = case.get("turns")
            if not isinstance(turns, list) or len(turns) < 2 or not all(
                isinstance(turn, dict) and isinstance(turn.get("prompt"), str) and turn["prompt"]
                and isinstance(turn.get("expected"), list) and turn["expected"]
                and all(isinstance(item, str) and item for item in turn["expected"])
                for turn in turns
            ):
                raise ValueError(f"invalid eval turn sequence: {case['id']}")
            if turns[-1]["prompt"] != case["prompt"]:
                raise ValueError(f"eval final turn prompt mismatch: {case['id']}")
        if "between_turns" in case or case["id"] == "completion-state-changed":
            if not isinstance(case.get("between_turns"), str) or not case["between_turns"] or "turns" not in case:
                raise ValueError(f"invalid eval between-turn setup: {case['id']}")
        ids.add(case["id"])
        covered.update(contracts)
    if len(ids) != len(cases):
        raise ValueError("duplicate eval case id")
    if not REQUIRED_SCENARIOS <= ids:
        raise ValueError(f"missing manual evaluation scenarios: {sorted(REQUIRED_SCENARIOS - ids)}")
    if covered != set(SAFETY_CONTRACT):
        raise ValueError(f"eval contract coverage incomplete: {sorted(set(SAFETY_CONTRACT) - covered)}")


def validate_documentation(root: Path) -> None:
    for name in ("INSTALLATION.md", "INSTALLATION.ja.md"):
        guide = (root / "docs" / name).read_text(encoding="utf-8")
        if PINNED_INSTALL not in guide or "go install github.com/cottondesu/mandala/cmd/mandala@latest" in guide:
            raise ValueError(f"installation guide does not pin the tested CLI baseline: {name}")
        validate_cli_baseline(guide, name)
        if f"{PINNED_INSTALL}\n{CLI_CHECK}" not in guide:
            raise ValueError(f"installation guide does not verify the installed version: {name}")
    for name, manual in (("README.md", "tests/README.md"), ("README.ja.md", "tests/README.ja.md")):
        if manual not in (root / name).read_text(encoding="utf-8"):
            raise ValueError(f"README does not link the manual behavioral evaluation: {name}")
    for name in ("README.md", "README.ja.md"):
        guide = (root / name).read_text(encoding="utf-8")
        validate_cli_baseline(guide, name)
        if PINNED_INSTALL not in guide:
            raise ValueError(f"README omits pinned CLI installation: {name}")
    for name in ("README.md", "README.ja.md"):
        guide = (root / "tests" / name).read_text(encoding="utf-8")
        validate_cli_baseline(guide, f"tests/{name}")
        if not all(re.search(rf"^\| {number} \|", guide, re.MULTILINE) for number in range(1, 11)):
            raise ValueError(f"manual evaluation guide omits a required scenario: {name}")
        if not all(re.search(rf"^\| {scenario} \|", guide, re.MULTILINE) for scenario in ("R1", "R2", "M1", "C1", "C2")):
            raise ValueError(f"manual evaluation guide omits a focused freshness scenario: {name}")
        if not all(re.search(rf"^\| {scenario} \|", guide, re.MULTILINE) for scenario in ("B4", "B6", "B5")):
            raise ValueError(f"manual evaluation guide omits a capacity-workaround scenario: {name}")


def frontmatter(skill: str) -> dict[str, str]:
    lines = skill.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated YAML frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not re.fullmatch(r"(?:name|description): [^\r\n]+", line):
            raise ValueError("frontmatter must contain simple name and description scalars")
        key, value = line.split(": ", 1)
        if key in values:
            raise ValueError(f"duplicate frontmatter key: {key}")
        values[key] = value.strip()
    if set(values) != {"name", "description"}:
        raise ValueError("frontmatter needs only name and description")
    return values


def package_files(package: Path, root: Path = ROOT) -> dict[Path, bytes]:
    require_real_directory_path(root, package)
    files: dict[Path, bytes] = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink in package: {path}")
        if path.is_file():
            relative = path.relative_to(package)
            if path.stat().st_mode & 0o111:
                raise ValueError(f"executable file in package: {path}")
            files[relative] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f"non-regular entry in package: {path}")
    if set(files) != EXPECTED:
        raise ValueError(f"unexpected package files in {package}: {sorted(map(str, files))}")
    if sum(path.name == "SKILL.md" for path in files) != 1:
        raise ValueError(f"exactly one SKILL.md required: {package}")
    for relative, content in files.items():
        if b"\x00" in content or LOCAL_PATH.search(content):
            raise ValueError(f"binary data or machine-specific user-home path: {package / relative}")
        text = content.decode("utf-8")
        for target in LINK.findall(text):
            if "://" in target or target.startswith("#"):
                continue
            destination = (package / relative.parent / target.split("#", 1)[0]).resolve()
            if package.resolve() not in destination.parents or not destination.is_file():
                raise ValueError(f"broken or escaping reference: {package / relative}: {target}")
    skill = files[Path("SKILL.md")].decode("utf-8")
    metadata = frontmatter(skill)
    if metadata["name"] != "mandala" or not metadata["description"] or len(metadata["description"]) > 1024:
        raise ValueError(f"invalid Skill identity: {package}")
    if len(skill.splitlines()) >= 500:
        raise ValueError(f"SKILL.md must be under 500 lines: {package}")
    validate_safety_contract(skill)
    validate_cli_baseline(skill, str(package / "SKILL.md"))
    validate_cli_baseline(files[Path("references/cli-contract.md")].decode("utf-8"), str(package / "references/cli-contract.md"))
    return files


def main() -> None:
    for package in PACKAGES:
        require_real_directory_path(ROOT, package, allow_missing=True)
    snapshots = [package_files(package) for package in PACKAGES]
    if snapshots[0] != snapshots[1]:
        raise ValueError("generated package differs from canonical source")
    cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("eval fixture must be a list")
    validate_eval_metadata(cases)
    validate_documentation(ROOT)
    print(f"Mandala Skill packages valid and current; {len(cases)} behavioral evaluation fixtures validated; live agent evaluation is manual")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
