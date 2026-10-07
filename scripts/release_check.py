#!/usr/bin/env python3
"""Local release-candidate hygiene for a Git checkout.

Offline and read-only: it inspects the Git index and working tree, never requires a
clean tree, and never commits, tags, pushes, publishes, or runs agents.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Final


ROOT = Path(__file__).resolve().parents[1]
GENERATED_FILES: Final = ("dist/mandala/SKILL.md", "dist/mandala/references/cli-contract.md")
# Built by concatenation so this tracked file does not match its own check.
STALE_LAYOUTS: Final = tuple("dist/" + name + "/" for name in ("codex", "claude-code"))


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # Decode explicitly: diff output may quote non-UTF-8 file content, and locale must not matter.
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", errors="replace"
    )


def tracked_files(root: Path) -> list[str]:
    result = run_git(root, "ls-files", "-z")
    if result.returncode != 0:
        raise ValueError("git ls-files failed; run inside a Git checkout")
    return [path for path in result.stdout.split("\0") if path]


def validate_tracked_paths(paths: list[str]) -> list[str]:
    errors = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if "dist" in parts:
            errors.append(f"tracked generated artifact: {path}")
        elif "__pycache__" in parts or path.endswith((".pyc", ".pyo")):
            errors.append(f"tracked Python cache: {path}")
        elif ".omx" in parts:
            errors.append(f"tracked local tool state: {path}")
        elif ".eval-live" in parts:
            errors.append(f"tracked live-eval artifact: {path}")
        elif parts and parts[-1] == ".DS_Store":
            errors.append(f"tracked .DS_Store: {path}")
    return errors


def validate_generated_ignored(root: Path, paths: list[str]) -> list[str]:
    errors = [f"generated package file missing; run make build: {path}" for path in GENERATED_FILES if not (root / path).is_file()]
    result = run_git(root, "check-ignore", "--no-index", "--verbose", "--", *GENERATED_FILES)
    if result.returncode not in (0, 1):
        return errors + ["git check-ignore failed"]
    # Only a tracked .gitignore counts; .git/info/exclude and global excludes are local to one machine.
    ignored = set()
    for line in result.stdout.splitlines():
        match, _, path = line.partition("\t")
        source, _, pattern = match.split(":", 2) if match.count(":") >= 2 else ("", "", "")
        if source in paths and PurePosixPath(source).name == ".gitignore" and not pattern.startswith("!"):
            ignored.add(path)
    errors.extend(f"generated package is not ignored by a tracked .gitignore: {path}" for path in GENERATED_FILES if path not in ignored)
    return errors


def validate_distribution_policy(root: Path, paths: list[str]) -> list[str]:
    errors = []
    for path in paths:
        # Scan the working-tree candidate. Deleted paths, gitlinks, and non-UTF-8 files are skipped.
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            continue
        for layout in STALE_LAYOUTS:
            if layout in text:
                errors.append(f"stale split-distribution reference: {path}: {layout}")
    return errors


def run_git_check(root: Path) -> list[str]:
    errors = []
    for args, label in ((("diff", "--check"), "git diff --check"), (("diff", "--cached", "--check"), "git diff --cached --check")):
        result = run_git(root, *args)
        if result.returncode != 0:
            detail = (result.stdout or result.stderr).strip()
            errors.append(f"{label} failed" + (f":\n{detail}" if detail else ""))
    return errors


def release_errors(root: Path) -> list[str]:
    paths = tracked_files(root)
    return (
        run_git_check(root)
        + validate_tracked_paths(paths)
        + validate_generated_ignored(root, paths)
        + validate_distribution_policy(root, paths)
    )


def main() -> int:
    try:
        errors = release_errors(ROOT)
    except (ValueError, OSError) as exc:
        errors = [str(exc)]
    for error in errors:
        print(f"release check failed: {error}", file=sys.stderr)
    if errors:
        return 1
    print("Release hygiene checks passed; nothing was committed, tagged, pushed, or published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
