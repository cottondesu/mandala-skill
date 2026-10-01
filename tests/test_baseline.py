from pathlib import Path
import json
import shutil
import tempfile
import unittest

from scripts import validate


ROOT = Path(__file__).resolve().parents[1]


class BaselineTests(unittest.TestCase):
    def test_canonical_and_generated_packages_use_release_version_check(self) -> None:
        for package in validate.PACKAGES:
            for relative in ("SKILL.md", "references/cli-contract.md"):
                with self.subTest(package=package, file=relative):
                    text = (package / relative).read_text(encoding="utf-8")
                    validate.validate_cli_baseline(text, relative)
                    self.assertIn("Mandala CLI v0.3.0", text)
                    self.assertIn("mandala --version", text)
                    self.assertIn("mandala v0.3.0", text)
                    self.assertIn("mandala --help", text)
                    self.assertIn("mandala <command> --help", text)

    def test_baseline_validation_rejects_missing_or_obsolete_guidance(self) -> None:
        valid = "Mandala CLI v0.3.0; mandala --version; mandala v0.3.0"
        validate.validate_cli_baseline(valid, "fixture")
        for token in ("Mandala CLI v0.3.0", "mandala --version", "mandala v0.3.0"):
            with self.subTest(missing=token), self.assertRaisesRegex(ValueError, "missing CLI baseline/version check"):
                validate.validate_cli_baseline(valid.replace(token, ""), "fixture")
        for obsolete in (
            "Mandala CLI v0.2.0",
            "Do not assume a public --version command.",
            "v0.3.0 has no public `--version` command.",
            "There is no required `mandala --version` check.",
            "Do not rely on a `mandala --version` command.",
            "`mandala --version` は必須確認に使いません。",
            "`mandala --version` による確認は前提にしません。",
        ):
            with self.subTest(obsolete=obsolete), self.assertRaisesRegex(ValueError, "obsolete CLI baseline/version guidance"):
                validate.validate_cli_baseline(valid + "\n" + obsolete, "fixture")

    def test_english_and_japanese_installation_commands_are_aligned(self) -> None:
        for name in ("README.md", "README.ja.md", "docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0", text)
                self.assertIn("mandala --version", text)
                self.assertIn("mandala v0.3.0", text)
                self.assertIn("mandala --help", text)
                self.assertIn("mandala <command> --help", text)

    def test_documentation_validation_rejects_stale_baseline_or_help_check(self) -> None:
        names = ("README.md", "README.ja.md", "docs/INSTALLATION.md", "docs/INSTALLATION.ja.md", "tests/README.md", "tests/README.ja.md")
        for name in names:
            changes = [("v0.3.0", "v0.2.0"), ("mandala --version", "mandala --help")]
            for before, after in changes:
                with self.subTest(file=name, replacement=after), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    for relative in names:
                        destination = root / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ROOT / relative, destination)
                    target = root / name
                    target.write_text(target.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        validate.validate_documentation(root)

    def test_evaluation_setup_uses_version_check(self) -> None:
        cases = json.loads((ROOT / "tests/evals/cases.json").read_text(encoding="utf-8"))
        new_project = next(case for case in cases if case["id"] == "new-explicit-project")
        self.assertIn("mandala --version", new_project["expected"][0])
        self.assertIn("mandala v0.3.0", new_project["expected"][0])
        missing_cli = next(case for case in cases if case["id"] == "missing-cli")
        self.assertIn("mandala --version", missing_cli["prompt"])


if __name__ == "__main__":
    unittest.main()
