from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from scripts import release_check


ROOT = Path(__file__).resolve().parents[1]


# Drop inherited GIT_DIR, GIT_INDEX_FILE, etc. (set inside Git hooks) so index writes stay in the temporary repository.
ISOLATED_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=ISOLATED_ENV)


class TemporaryRepository:
    """A throwaway Git repository; tests never touch the real index."""

    def __enter__(self) -> Path:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        git(root, "init", "-q")
        shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
        (root / "README.md").write_text("Install to ~/.codex/skills/ or ~/.claude/skills/.\n", encoding="utf-8")
        for relative in release_check.GENERATED_FILES:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text("generated\n", encoding="utf-8")
        git(root, "add", ".gitignore", "README.md")
        return root

    def __exit__(self, *exc_info: object) -> None:
        self.directory.cleanup()


class ReleaseCheckTests(unittest.TestCase):
    def test_current_repository_policy_passes(self) -> None:
        paths = release_check.tracked_files(ROOT)
        self.assertIn("src/mandala/SKILL.md", paths)
        self.assertEqual(release_check.validate_tracked_paths(paths), [])
        self.assertEqual(release_check.validate_generated_ignored(ROOT, paths), [])
        self.assertEqual(release_check.validate_distribution_policy(ROOT, paths), [])

    def test_clean_and_dirty_valid_repositories_pass(self) -> None:
        with TemporaryRepository() as root:
            self.assertEqual(release_check.release_errors(root), [])
            (root / "README.md").write_text("Uncommitted but valid change.\n", encoding="utf-8")
            (root / "notes.txt").write_text("untracked\n", encoding="utf-8")
            self.assertEqual(release_check.release_errors(root), [])
            (root / "staged.md").write_text("Staged valid change.\n", encoding="utf-8")
            git(root, "add", "README.md", "staged.md")
            self.assertEqual(release_check.release_errors(root), [])

    def test_tracked_artifacts_are_rejected(self) -> None:
        cases = {
            "dist/mandala/SKILL.md": "tracked generated artifact",
            "scripts/__pycache__/validate.cpython-312.pyc": "tracked Python cache",
            "src/__pycache__/x.pyc": "tracked Python cache",
            "scripts/validate.pyc": "tracked Python cache",
            "foo/bar.pyo": "tracked Python cache",
            ".DS_Store": "tracked .DS_Store",
            "foo/.DS_Store": "tracked .DS_Store",
            ".omx/state.json": "tracked local tool state",
            ".eval-live/run/codex/summary.json": "tracked live-eval artifact",
            "nested/.eval-live/report.md": "tracked live-eval artifact",
            "nested/.omx/config": "tracked local tool state",
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                errors = release_check.validate_tracked_paths(["README.md", path])
                self.assertEqual(len(errors), 1)
                self.assertIn(f"{message}: {path}", errors[0])

    def test_similar_legitimate_names_are_allowed(self) -> None:
        names = ["documentation/dist-example.md", "distribution.md", "my__pycache__notes.md", "notes.pyc.txt", "docs/.DS_Store.md", "omx/readme.md", "docs/eval-live.md", "eval-live/notes.md"]
        self.assertEqual(release_check.validate_tracked_paths(names), [])

    def test_git_failures_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaisesRegex(ValueError, "git ls-files failed"):
                release_check.release_errors(missing)
            self.assertEqual(len(release_check.run_git_check(missing)), 2)
            self.assertIn("git check-ignore failed", release_check.validate_generated_ignored(missing, [".gitignore"]))

    def test_binary_empty_and_deleted_tracked_files_are_handled(self) -> None:
        with TemporaryRepository() as root:
            (root / "image.bin").write_bytes(b"\xff\xfe\x00binary")
            (root / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
            (root / "empty.txt").write_bytes(b"")
            (root / "japanese.md").write_text("日本語の説明\n", encoding="utf-8")
            (root / "removed.md").write_text("tracked\n", encoding="utf-8")
            git(root, "add", "image.bin", "latin1.txt", "empty.txt", "japanese.md", "removed.md")
            self.assertEqual(release_check.release_errors(root), [])
            (root / "removed.md").unlink()
            self.assertEqual(release_check.release_errors(root), [])

    def test_non_utf8_whitespace_error_is_reported(self) -> None:
        with TemporaryRepository() as root:
            (root / "latin1.txt").write_bytes("caf\xe9 \n".encode("latin-1"))
            git(root, "add", "latin1.txt")
            errors = release_check.release_errors(root)
            self.assertTrue(any(error.startswith("git diff --cached --check failed") for error in errors), errors)

    def test_force_added_artifacts_are_rejected_from_index(self) -> None:
        for relative in ("dist/mandala/SKILL.md", "scripts/__pycache__/validate.cpython-312.pyc"):
            with self.subTest(path=relative), TemporaryRepository() as root:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(b"artifact")
                git(root, "add", "-f", relative)
                errors = release_check.release_errors(root)
                self.assertTrue(any(relative in error for error in errors), errors)

    def test_ignored_local_artifacts_are_allowed(self) -> None:
        with TemporaryRepository() as root:
            (root / ".DS_Store").write_bytes(b"local")
            (root / "scripts" / "__pycache__").mkdir(parents=True)
            (root / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"cache")
            (root / ".eval-live" / "run").mkdir(parents=True)
            (root / ".eval-live" / "run" / "summary.json").write_text("{}", encoding="utf-8")
            self.assertEqual(release_check.release_errors(root), [])

    def test_generated_package_must_be_ignored(self) -> None:
        with TemporaryRepository() as root:
            paths = release_check.tracked_files(root)
            self.assertEqual(release_check.validate_generated_ignored(root, paths), [])
            (root / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
            errors = release_check.validate_generated_ignored(root, paths)
            for path in release_check.GENERATED_FILES:
                self.assertIn(f"generated package is not ignored by a tracked .gitignore: {path}", errors)
            # A machine-local exclude must not stand in for the repository rule.
            (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (root / ".git" / "info" / "exclude").write_text("/dist/\n", encoding="utf-8")
            self.assertEqual(len(release_check.validate_generated_ignored(root, paths)), 2)
            (root / ".gitignore").write_text("/dist/\n!/dist/mandala/SKILL.md\n", encoding="utf-8")
            (root / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
            self.assertEqual(release_check.validate_generated_ignored(root, paths), [])

    def test_stale_split_distribution_reference_is_rejected(self) -> None:
        for layout in ("dist/" + "codex/", "dist/" + "claude-code/"):
            with self.subTest(layout=layout), TemporaryRepository() as root:
                (root / "README.md").write_text(f"Copy {layout}mandala manually.\n", encoding="utf-8")
                errors = release_check.release_errors(root)
                self.assertIn(f"stale split-distribution reference: README.md: {layout}", errors)

    def test_whitespace_errors_are_rejected(self) -> None:
        with TemporaryRepository() as root:
            (root / "README.md").write_text("trailing space \n", encoding="utf-8")
            self.assertTrue(any(error.startswith("git diff --check failed") for error in release_check.release_errors(root)))
            git(root, "add", "README.md")
            self.assertTrue(any(error.startswith("git diff --cached --check failed") for error in release_check.release_errors(root)))


if __name__ == "__main__":
    unittest.main()
