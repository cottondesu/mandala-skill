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

    def test_agent_skills_metadata_accepts_current_and_boundary_values(self):
        metadata = validate.frontmatter((SOURCE / "SKILL.md").read_text(encoding="utf-8"))
        for package in (SOURCE, GENERATED):
            validate.validate_agent_skills_metadata(metadata, package.name)
        for name in ("a", "a1", "mandala-skill", "a-b-c", "a" * 64):
            validate.validate_agent_skills_metadata({"name": name, "description": "d"}, name)
        validate.validate_agent_skills_metadata({"name": "mandala-skill-2", "description": "x" * 1024}, "mandala-skill-2")

    def test_agent_skills_metadata_rejects_invalid_names(self):
        for name in ("Mandala", "MANDALA", "mandala_skill", "mandala.skill", "-mandala", "mandala-", "mandala--skill", "a" * 65, "", "mandala skill", "mandalá", "mandal\u0430"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Skill name must"):
                    validate.validate_agent_skills_metadata({"name": name, "description": "d"}, name)

    def test_agent_skills_metadata_rejects_invalid_descriptions(self):
        validate.validate_agent_skills_metadata({"name": "mandala", "description": "\u00e9" * 1024}, "mandala")
        for description in ("", "   ", "x" * 1025):
            with self.subTest(length=len(description)):
                with self.assertRaisesRegex(ValueError, "Skill description must be 1-1024"):
                    validate.validate_agent_skills_metadata({"name": "mandala", "description": description}, "mandala")

    def test_package_name_must_match_directory(self):
        for directory_name in ("other", "mandala-skill"):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "dist" / directory_name
                shutil.copytree(SOURCE, package)
                with self.assertRaisesRegex(ValueError, "Skill name must match its directory"):
                    validate.package_files(package, root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "copy" / "mandala"
            shutil.copytree(SOURCE, package)
            self.assertEqual(validate.package_files(package, root), validate.package_files(SOURCE))

    def test_package_rejects_invalid_metadata_in_copy(self):
        replacements = (
            ("name: mandala", "name: Mandala", "Skill name must"),
            ("name: mandala", "name: mandala_skill", "Skill name must"),
            ("name: mandala", "name: mandala--skill", "Skill name must"),
        )
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        description = validate.frontmatter(skill)["description"]
        replacements += ((f"description: {description}", "description: " + "x" * 1025, "Skill description must be 1-1024"),)
        for old, new, message in replacements:
            with self.subTest(change=new[:40]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "src" / "mandala"
                shutil.copytree(SOURCE, package)
                (package / "SKILL.md").write_text(skill.replace(old, new, 1), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    validate.package_files(package, root)

    def test_activation_fixtures_pass(self):
        cases = json.loads((ROOT / "tests" / "evals" / "activation.json").read_text(encoding="utf-8"))
        validate.validate_activation_metadata(cases)
        self.assertTrue({case["category"] for case in cases} >= validate.REQUIRED_ACTIVATION_CATEGORIES)

    def test_activation_fixtures_reject_invalid_schema(self):
        cases = json.loads((ROOT / "tests" / "evals" / "activation.json").read_text(encoding="utf-8"))
        first = cases[0]
        invalid = (
            (cases + [dict(first)], "duplicate activation case id"),
            ([{**first, "locale": "fr"}] + cases[1:], "invalid activation locale"),
            ([{**first, "locale": "EN"}] + cases[1:], "invalid activation locale"),
            ([{**first, "should_activate": "true"}] + cases[1:], "should_activate must be boolean"),
            ([{**first, "should_activate": 1}] + cases[1:], "should_activate must be boolean"),
            ([{**first, "id": ""}] + cases[1:], "invalid activation case schema"),
            ([{**first, "prompt": " "}] + cases[1:], "invalid activation case schema"),
            ([{key: value for key, value in first.items() if key != "category"}] + cases[1:], "invalid activation case schema"),
            ([{**first, "expected": ["load the Skill"]}] + cases[1:], "invalid activation case schema"),
            ({"cases": cases}, "activation fixture must be a list"),
        )
        for changed, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate.validate_activation_metadata(changed)

    def test_activation_fixtures_require_coverage(self):
        cases = json.loads((ROOT / "tests" / "evals" / "activation.json").read_text(encoding="utf-8"))
        for category in validate.REQUIRED_ACTIVATION_CATEGORIES:
            with self.subTest(category=category):
                with self.assertRaisesRegex(ValueError, "missing activation categories"):
                    validate.validate_activation_metadata([case for case in cases if case["category"] != category])
        for should_activate, label in ((True, "positive"), (False, "negative")):
            with self.subTest(only=label):
                with self.assertRaisesRegex(ValueError, "activation"):
                    validate.validate_activation_metadata([case for case in cases if case["should_activate"] is should_activate])
                relabeled = [{**case, "should_activate": should_activate} for case in cases]
                with self.assertRaisesRegex(ValueError, f"activation fixtures need {'negative' if should_activate else 'positive'} cases"):
                    validate.validate_activation_metadata(relabeled)
        for outcome in (True, False):
            for locale in sorted(validate.ACTIVATION_LOCALES):
                with self.subTest(outcome=outcome, locale=locale):
                    changed = [case for case in cases if (case["should_activate"], case["locale"]) != (outcome, locale)]
                    with self.assertRaisesRegex(ValueError, "activation locale/outcome coverage incomplete"):
                        validate.validate_activation_metadata(changed)

    def test_contract_catalog_is_stable_and_complete(self):
        catalog = json.loads((ROOT / "tests" / "evals" / "contracts.json").read_text(encoding="utf-8"))
        contracts = validate.validate_contract_catalog(catalog)
        self.assertEqual(len(contracts), 23)
        self.assertEqual(contracts["AUTH-001"]["slug"], "explicit-use")
        self.assertEqual(contracts["VERIFY-001"]["slug"], "separate-verification")
        self.assertEqual(validate.SAFETY_CONTRACT, {cid: item["clause"] for cid, item in contracts.items()})

    def test_contract_catalog_rejects_invalid_entries(self):
        catalog = json.loads((ROOT / "tests" / "evals" / "contracts.json").read_text(encoding="utf-8"))
        entries = catalog["contracts"]
        first = entries[0]
        invalid = (
            ({**catalog, "contracts": entries + [dict(first, slug="another")]}, "duplicate safety contract ID"),
            ({**catalog, "contracts": entries[:-1] + [dict(entries[-1], slug=first["slug"])]}, "duplicate safety contract slug"),
            ({**catalog, "contracts": [dict(first, id="auth-1")] + entries[1:]}, "invalid safety contract ID"),
            ({**catalog, "contracts": [dict(first, id="AUTH-0001")] + entries[1:]}, "invalid safety contract ID"),
            ({**catalog, "contracts": [dict(first, area="misc")] + entries[1:]}, "unknown safety contract area"),
            ({**catalog, "contracts": [dict(first, clause="")] + entries[1:]}, "invalid safety contract entry"),
            ({**catalog, "contracts": [dict(first, note="extra")] + entries[1:]}, "invalid safety contract entry"),
            ({**catalog, "contracts": entries[1:]}, "expected 23 safety contracts"),
            ({**catalog, "schema_version": 2}, "schema_version 1"),
            ({**catalog, "contracts": []}, "non-empty contracts list"),
        )
        for changed, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate.validate_contract_catalog(changed)

    def test_contract_missing_from_active_prose_is_reported_by_id(self):
        skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        clause = validate.SAFETY_CONTRACT["COMP-002"]
        with self.assertRaisesRegex(ValueError, r"COMP-002 \(completion-gate\)"):
            validate.validate_safety_contract(skill.replace(clause, "", 1))

    def test_fixture_contract_references_use_stable_ids(self):
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        self.assertTrue(all(validate.CONTRACT_ID.fullmatch(item) for case in cases for item in case["contracts"]))
        replace = lambda old, new: [{**case, "contracts": [new if item == old else item for item in case["contracts"]]} for case in cases]
        with self.assertRaisesRegex(ValueError, "legacy contract slug"):
            validate.validate_eval_metadata(replace("STATE-001", "read-before-write"))
        with self.assertRaisesRegex(ValueError, "unknown contract ID"):
            validate.validate_eval_metadata(replace("STATE-001", "STATE-999"))
        with self.assertRaisesRegex(ValueError, r"eval contract coverage incomplete: \['VERIFY-001'\]"):
            validate.validate_eval_metadata([{**case, "contracts": [item for item in case["contracts"] if item != "VERIFY-001"] or ["STATE-001"]} for case in cases])

    def test_live_suite_manifest_validation(self):
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
        validate.validate_live_suites(manifest, cases)
        self.assertEqual(manifest["suites"]["release"], ["R1", "R2", "M1", "C1", "C2", "B4", "B5", "B6"])
        def changed(**updates):
            result = json.loads(json.dumps(manifest))
            for path, value in updates.items():
                section, key = path.split("__")
                result[section][key] = value
            return result
        invalid = (
            (changed(cases__R1={"fixture": "missing-fixture", "grader": "reset-request"}), "unknown live fixture ID"),
            (changed(cases__R1={"fixture": "reset-request", "grader": "no-such-grader"}), "unknown live grader"),
            (changed(cases__R1={"fixture": "reset-request", "grader": "reset-request", "prompt": "copied"}), "invalid live case entry"),
            (changed(cases__R1={"fixture": "explicit-clean", "grader": "explicit-clean"}), "live case R1 must use fixture reset-request"),
            (changed(suites__focused=["R1", "R1", "R2", "M1", "C1", "C2"]), "duplicate live alias in suite"),
            (changed(suites__release=["R1", "R2", "M1", "C1", "C2", "B4", "B5"]), r"missing \['B6'\]"),
            (changed(suites__capacity=["B4", "B5"]), "live suite capacity must contain"),
            (changed(suites__extra=["Z9"]), "unknown live alias"),
        )
        for manifest_change, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate.validate_live_suites(manifest_change, cases)

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
            ("CAP-001", "`done`"),
            ("CAP-001", "`na`"),
            ("CAP-002", "`done`"),
            ("CAP-002", "`na`"),
            ("CAP-003", "`clean`"),
            ("CAP-003", "reinitialization"),
            ("CAP-004", "removal"),
            ("CAP-004", "replacement"),
            ("CAP-004", "merely to make room for another cell"),
            ("CAP-005", "report the structural limit"),
            ("CAP-005", "preserve existing state"),
            ("CAP-005", "ask the user for clear direction"),
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
        clause = validate.SAFETY_CONTRACT["AUTH-001"]
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

    def test_field_usage_version_contracts_example(self):
        example = json.loads(validate.FIELD_USAGE_PATH.read_text(encoding="utf-8"))
        validate.validate_field_usage_example(example)
        self.assertEqual((example["artifact_type"], example["source"], example["goal"]), ("mandala-field-usage-example", "sanitized-real-world-usage", "Verify release version contracts"))
        self.assertNotIn("contracts", example)
        self.assertIn("task_contracts", example)
        facts = {item["label"]: (item["value"], item.get("verification")) for item in example["task_contracts"]}
        self.assertEqual(facts, {
            "Gem": ("0.3.0", None), "CLI": ("0.3.0", None), "JSON schema": ("3", ["success-path", "failure-path"]),
            "config schema": ("1", ["version-2-rejected"]), "Ruby": (">= 3.3", None), "Prism": (">= 1.9, < 2", None), "dependencies added": ("none", None),
        })

    def test_field_usage_example_is_separate_from_safety_evaluation(self):
        example = json.loads(validate.FIELD_USAGE_PATH.read_text(encoding="utf-8"))
        task_ids = {item["id"] for item in example["task_contracts"]}
        contracts = validate.load_contracts()
        self.assertEqual(len(contracts), 23)
        self.assertFalse(task_ids & set(contracts))
        cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 23)
        self.assertNotIn(example["id"], {case["id"] for case in cases})
        self.assertEqual(len(json.loads((ROOT / "tests" / "evals" / "activation.json").read_text(encoding="utf-8"))), 11)
        manifest = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["cases"]), 8)
        self.assertEqual(len(manifest["suites"]["release"]), 8)
        self.assertNotIn(example["id"], {entry["fixture"] for entry in manifest["cases"].values()})
        for name in ("contracts.json", "cases.json", "live_suites.json", "activation.json"):
            self.assertNotIn("version_contracts", (ROOT / "tests" / "evals" / name).read_text(encoding="utf-8"))
            self.assertNotIn("release-version-contracts", (ROOT / "tests" / "evals" / name).read_text(encoding="utf-8"))
        # Coverage rows come only from the safety catalog; task contract IDs never appear there.
        from scripts import eval_coverage, live_eval_artifacts
        from tests import eval_artifact_fixtures as fx
        with tempfile.TemporaryDirectory() as directory:
            fx.write_run(Path(directory) / "codex", [fx.b5_case()])
            coverage = eval_coverage.build_coverage(live_eval_artifacts.Source(str(Path(directory) / "codex")))
        self.assertEqual({row["id"] for row in coverage["contracts"]}, set(contracts))
        self.assertFalse(task_ids & {row["id"] for row in coverage["contracts"]})

    def test_field_usage_example_validation_rejects_invalid_shapes(self):
        example = json.loads(validate.FIELD_USAGE_PATH.read_text(encoding="utf-8"))
        items = example["task_contracts"]
        bad = [
            ({**example, "schema_version": 2}, "schema_version 1"),
            ({**example, "artifact_type": "mandala-field-usage"}, "artifact_type"),
            ({**example, "id": " "}, "non-empty id"),
            ({**example, "source": "real-world"}, "source must be"),
            ({**example, "goal": ""}, "non-empty id and goal"),
            ({**example, "task_contracts": []}, "non-empty task_contracts"),
            ({**example, "contracts": items}, "task_contracts, not contracts"),
            ({**example, "task_contracts": items + [items[0]]}, "duplicate task contract ID"),
            ({**example, "task_contracts": [{**items[0], "label": ""}]}, "invalid task contract"),
            ({**example, "task_contracts": [{**items[0], "value": ""}]}, "invalid task contract"),
            ({**example, "task_contracts": [{**items[0], "verification": []}]}, "non-empty string list"),
            ({**example, "task_contracts": [{**items[0], "verification": ["ok", 3]}]}, "non-empty string list"),
            ({**example, "task_contracts": [{**items[0], "verification": "success-path"}]}, "non-empty string list"),
        ]
        for value, message in bad:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate.validate_field_usage_example(value)

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
