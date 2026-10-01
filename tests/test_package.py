from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import validate


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "mandala"
GENERATED = ROOT / "dist" / "mandala"


class PackageTests(unittest.TestCase):
    def test_packages_match_and_validate(self):
        source = validate.package_files(SOURCE)
        self.assertEqual(source, validate.package_files(GENERATED))
        self.assertEqual(set(source), validate.EXPECTED)

    def test_frontmatter_and_reference(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        metadata = validate.frontmatter(skill)
        self.assertEqual(metadata["name"], "mandala")
        self.assertIn("coverage gaps", metadata["description"])
        self.assertLessEqual(len(metadata["description"]), 1024)
        self.assertLess(len(skill.splitlines()), 500)
        self.assertIn("(references/cli-contract.md)", skill)
        self.assertTrue((SOURCE / "references" / "cli-contract.md").is_file())

    def test_safety_contract_and_removed_clause_regressions(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        validate.validate_safety_contract(skill)
        for name, clause in validate.SAFETY_CONTRACT.items():
            with self.subTest(contract=name):
                with self.assertRaisesRegex(ValueError, name):
                    validate.validate_safety_contract(skill.replace(clause, ""))

    def test_safety_contract_ignores_fences_and_comments(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        wrappers = (
            "\n```text\n{}\n````\n",
            "\n   ~~~~text\n{}\n   ~~~~~\n",
            "\n<!--\n{}\n-->\n",
            "\n<!-- {} -->\n",
        )
        for name, clause in validate.SAFETY_CONTRACT.items():
            without_active_clause = skill.replace(clause, "", 1)
            for wrapper in wrappers:
                with self.subTest(contract=name, wrapper=wrapper):
                    with self.assertRaisesRegex(ValueError, name):
                        validate.validate_safety_contract(without_active_clause + wrapper.format(clause))

    def test_capacity_contract_rejects_individual_safety_removals(self) -> None:
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        removals = (
            ("capacity-status-counts", "`done`"),
            ("capacity-status-counts", "`na`"),
            ("no-capacity-status-workaround", "`done`"),
            ("no-capacity-status-workaround", "`na`"),
            ("no-capacity-clean-workaround", "`clean`"),
            ("no-capacity-clean-workaround", "reinitialization"),
            ("no-capacity-coverage-replacement", "removal"),
            ("no-capacity-coverage-replacement", "replacement"),
            ("no-capacity-coverage-replacement", "merely to make room for another cell"),
            ("capacity-restructure-direction", "report the structural limit"),
            ("capacity-restructure-direction", "preserve existing state"),
            ("capacity-restructure-direction", "ask the user for clear direction"),
        )
        for name, removed in removals:
            with self.subTest(contract=name, removed=removed):
                clause = validate.SAFETY_CONTRACT[name]
                changed = skill.replace(clause, clause.replace(removed, "", 1), 1)
                with self.assertRaisesRegex(ValueError, name):
                    validate.validate_safety_contract(changed)

    def test_safety_contract_ignores_frontmatter(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        for name, clause in validate.SAFETY_CONTRACT.items():
            with self.subTest(contract=name):
                without_active_clause = skill.replace(clause, "", 1)
                description = without_active_clause.splitlines()[2]
                only_in_frontmatter = without_active_clause.replace(description, description + " " + clause, 1)
                with self.assertRaisesRegex(ValueError, name):
                    validate.validate_safety_contract(only_in_frontmatter)

    def test_safety_contract_allows_unrelated_code_and_blockquotes(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        examples = "\n```sh\nmandala --help\n```\n~~~text\nmandala gaps --json\n~~~\n"
        validate.validate_safety_contract(skill + examples)
        clause = validate.SAFETY_CONTRACT["explicit-use"]
        validate.validate_safety_contract(skill.replace(clause, "", 1) + "\n> " + clause + "\n")

    def test_generated_package_has_no_symlinks_or_user_home_paths(self):
        for package in (SOURCE, GENERATED):
            for path in package.rglob("*"):
                self.assertFalse(path.is_symlink(), path)
                if path.is_file():
                    self.assertIsNone(validate.LOCAL_PATH.search(path.read_bytes()))

    def test_eval_fixture_structure_and_coverage_metadata(self):
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        validate.validate_eval_metadata(cases)
        with self.assertRaisesRegex(ValueError, "missing manual evaluation scenarios"):
            validate.validate_eval_metadata([case for case in cases if case["id"] != "explicit-clean"])
        with self.assertRaisesRegex(ValueError, "invalid eval contract metadata"):
            validate.validate_eval_metadata([{**case, "contracts": []} if case["id"] == "explicit-clean" else case for case in cases])

    def test_documented_cli_baseline_and_manual_evaluation(self):
        validate.validate_documentation(ROOT)
        for name in ("INSTALLATION.md", "INSTALLATION.ja.md"):
            guide = (ROOT / "docs" / name).read_text(encoding="utf-8")
            self.assertIn(validate.PINNED_INSTALL, guide)
            self.assertNotIn("go install github.com/cottondesu/mandala/cmd/mandala@latest", guide)
        for name in ("README.md", "README.ja.md"):
            self.assertIn("v0.3.0", (ROOT / name).read_text(encoding="utf-8"))

    def test_capacity_fixtures_and_manual_rows_are_required(self) -> None:
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        for scenario in ("capacity-full-child", "capacity-full-tree", "capacity-final-child"):
            with self.subTest(fixture=scenario):
                with self.assertRaisesRegex(ValueError, "missing manual evaluation scenarios"):
                    validate.validate_eval_metadata([case for case in cases if case["id"] != scenario])
        for name in ("README.md", "README.ja.md"):
            for scenario in ("B4", "B6", "B5"):
                with self.subTest(guide=name, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    shutil.copytree(ROOT / "docs", root / "docs")
                    shutil.copytree(ROOT / "tests", root / "tests")
                    for readme in ("README.md", "README.ja.md"):
                        shutil.copy2(ROOT / readme, root / readme)
                    guide = root / "tests" / name
                    guide.write_text(guide.read_text(encoding="utf-8").replace(f"| {scenario} |", "| removed |", 1), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "capacity-workaround scenario"):
                        validate.validate_documentation(root)

    def test_eval_freshness_scenarios_require_turn_boundaries(self):
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        for scenario in validate.TURN_SCENARIOS:
            for bad_turns in (None, [], [{"prompt": "全部終わった？", "expected": ["Run gaps"]}], [{"prompt": "Start"}, {"prompt": "Finish"}]):
                with self.subTest(scenario=scenario, turns=bad_turns):
                    changed = [{**case, "turns": bad_turns} if case["id"] == scenario else case for case in cases]
                    with self.assertRaisesRegex(ValueError, "invalid eval turn sequence"):
                        validate.validate_eval_metadata(changed)
        changed = [{**case, "prompt": "Different final prompt"} if case["id"] == "zero-gaps" else case for case in cases]
        with self.assertRaisesRegex(ValueError, "eval final turn prompt mismatch"):
            validate.validate_eval_metadata(changed)
        changed = [{**case, "between_turns": ""} if case["id"] == "completion-state-changed" else case for case in cases]
        with self.assertRaisesRegex(ValueError, "invalid eval between-turn setup"):
            validate.validate_eval_metadata(changed)
        with self.assertRaisesRegex(ValueError, "missing manual evaluation scenarios"):
            validate.validate_eval_metadata([case for case in cases if case["id"] != "completion-state-changed"])

    def test_ci_permissions_are_read_only(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_build_reproducible_and_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(SOURCE, root / "src" / "mandala")
            (root / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts" / "build.py", root / "scripts" / "build.py")
            shutil.copy2(ROOT / "scripts" / "package_safety.py", root / "scripts" / "package_safety.py")
            unrelated = root / "dist" / "keep.txt"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("preserve me", encoding="utf-8")
            command = [sys.executable, str(root / "scripts" / "build.py")]
            subprocess.run(command, check=True, capture_output=True)
            first = validate.package_files(root / "dist" / "mandala", root)
            self.assertEqual(first, validate.package_files(SOURCE))
            self.assertEqual({path.name for path in (root / "dist").iterdir()}, {"mandala", "keep.txt"})
            stale = root / "dist" / "mandala" / "stale.md"
            stale.write_text("old generated file", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected package files"):
                validate.package_files(root / "dist" / "mandala", root)
            subprocess.run(command, check=True, capture_output=True)
            self.assertFalse(stale.exists())
            self.assertEqual(first, validate.package_files(root / "dist" / "mandala", root))
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve me")

    def test_build_rejects_source_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(SOURCE, root / "src" / "mandala")
            (root / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts" / "build.py", root / "scripts" / "build.py")
            shutil.copy2(ROOT / "scripts" / "package_safety.py", root / "scripts" / "package_safety.py")
            (root / "src" / "mandala" / "linked.md").symlink_to("SKILL.md")
            result = subprocess.run([sys.executable, str(root / "scripts" / "build.py")], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source symlink", result.stderr)
            self.assertFalse((root / "dist").exists())

    def test_build_and_validation_reject_symlinked_distribution_paths(self):
        for relative in ("dist", "dist/mandala"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "repo"
                shutil.copytree(SOURCE, root / "src" / "mandala")
                shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
                link = root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                outside = base / "outside"
                outside.mkdir()
                destination = outside / "linked"
                destination.mkdir()
                marker = destination / "marker.txt"
                marker.write_text("preserve me", encoding="utf-8")
                link.symlink_to(destination, target_is_directory=True)
                build = subprocess.run([sys.executable, str(root / "scripts" / "build.py")], capture_output=True, text=True)
                self.assertNotEqual(build.returncode, 0)
                self.assertIn("symlink in package path", build.stderr)
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")
                check = subprocess.run([sys.executable, str(root / "scripts" / "validate.py")], capture_output=True, text=True)
                self.assertNotEqual(check.returncode, 0)
                self.assertIn("symlink in package path", check.stderr)

    def test_validation_rejects_user_home_paths_and_allows_generic_paths(self):
        rejected = ("/Users/alice/work", "/home/alice/work", "C:\\Users\\alice\\work", "C:/Users/alice/work", "D:\\Users\\alice\\work", "D:/Users/alice/work")
        for local_path in rejected:
            with self.subTest(path=local_path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "src" / "mandala"
                shutil.copytree(SOURCE, package)
                (package / "SKILL.md").write_text((package / "SKILL.md").read_text(encoding="utf-8") + "\n" + local_path + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "machine-specific user-home path"):
                    validate.package_files(package, root)
        for generic_path in ("/path/to/project", "C:\\path\\to\\repo"):
            with self.subTest(path=generic_path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "src" / "mandala"
                shutil.copytree(SOURCE, package)
                (package / "SKILL.md").write_text((package / "SKILL.md").read_text(encoding="utf-8") + "\n" + generic_path + "\n", encoding="utf-8")
                validate.package_files(package, root)


if __name__ == "__main__":
    unittest.main()
