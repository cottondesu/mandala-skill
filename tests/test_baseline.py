from pathlib import Path
import json
import shutil
import tempfile
import unittest

from scripts import validate


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "references/cli-contract.md"
DOCS = ("README.md", "README.ja.md", "docs/INSTALLATION.md", "docs/INSTALLATION.ja.md", "tests/README.md", "tests/README.ja.md")


def copy_docs(root: Path) -> None:
    for relative in DOCS:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


class BaselineTests(unittest.TestCase):
    def test_baseline_constants_name_cli_v0_4_0(self) -> None:
        self.assertEqual(validate.CLI_BASELINE, "Mandala CLI v0.4.0")
        self.assertEqual(validate.CLI_CHECK, "mandala --version")
        self.assertEqual(validate.VERSION_OUTPUT, "mandala v0.4.0")
        self.assertEqual(validate.PINNED_INSTALL, "go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0")

    def test_canonical_and_generated_packages_use_release_version_check(self) -> None:
        for package in validate.PACKAGES:
            for relative in ("SKILL.md", CONTRACT):
                with self.subTest(package=package, file=relative):
                    text = (package / relative).read_text(encoding="utf-8")
                    validate.validate_cli_baseline(text, relative)
                    self.assertIn("Mandala CLI v0.4.0", text)
                    self.assertIn("mandala --version", text)
                    self.assertIn("mandala v0.4.0", text)
                    self.assertIn("mandala --help", text)
                    self.assertIn("mandala <command> --help", text)
                    self.assertNotIn("v0.3.0", text)

    def test_cli_contract_records_v0_4_0_release(self) -> None:
        text = (validate.PACKAGES[0] / CONTRACT).read_text(encoding="utf-8")
        self.assertIn("tag `v0.4.0`, release commit `2fd15fb1ae32d0e13b6d9a1ed8107eb3f3a9deb9`", text)
        self.assertIn(validate.PINNED_INSTALL, text)
        validate.validate_status_contract(text, CONTRACT)

    def test_baseline_validation_rejects_missing_or_obsolete_guidance(self) -> None:
        valid = "Mandala CLI v0.4.0; mandala --version; mandala v0.4.0"
        validate.validate_cli_baseline(valid, "fixture")
        for token in ("Mandala CLI v0.4.0", "mandala --version", "mandala v0.4.0"):
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

    def test_previous_baseline_is_rejected_as_current(self) -> None:
        valid = "Mandala CLI v0.4.0; mandala --version; mandala v0.4.0"
        for stale in (
            "Tested baseline: Mandala CLI v0.3.0",
            "release output `mandala v0.3.0`",
            "go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0",
            "Mandala CLI v0.5.0",
        ):
            with self.subTest(stale=stale), self.assertRaisesRegex(ValueError, "stale or unpinned CLI baseline"):
                validate.validate_cli_baseline(valid + "\n" + stale, "fixture")
        old = valid.replace("v0.4.0", "v0.3.0")
        with self.assertRaisesRegex(ValueError, "missing CLI baseline/version check"):
            validate.validate_cli_baseline(old, "fixture")
        # The Skill repository's own version is not a CLI baseline token.
        validate.validate_cli_baseline(valid + "\nmandala-skill v0.5.0", "fixture")

    def test_sentence_punctuation_is_not_part_of_a_version(self) -> None:
        valid = "Mandala CLI v0.4.0; mandala --version; mandala v0.4.0"
        for sentence in ("The baseline is Mandala CLI v0.4.0.", "It prints mandala v0.4.0.", "Run cmd/mandala@v0.4.0.", "基準は mandala v0.4.0。"):
            with self.subTest(sentence=sentence):
                validate.validate_cli_baseline(valid + "\n" + sentence, "fixture")
        for stale in ("Mandala v0.3.0 is the baseline.", "It prints mandala v0.3.0."):
            with self.subTest(stale=stale), self.assertRaisesRegex(ValueError, "stale or unpinned CLI baseline"):
                validate.validate_cli_baseline(valid + "\n" + stale, "fixture")

    def test_latest_is_never_the_pinned_baseline(self) -> None:
        valid = "Mandala CLI v0.4.0; mandala --version; mandala v0.4.0"
        with self.assertRaisesRegex(ValueError, "stale or unpinned CLI baseline"):
            validate.validate_cli_baseline(valid + "\ngo install github.com/cottondesu/mandala/cmd/mandala@latest", "fixture")
        for name in ("README.md", "docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                copy_docs(root)
                target = root / name
                target.write_text(target.read_text(encoding="utf-8").replace("@v0.4.0", "@latest"), encoding="utf-8")
                with self.assertRaises(ValueError):
                    validate.validate_documentation(root)

    def test_english_and_japanese_installation_commands_are_aligned(self) -> None:
        for name in ("README.md", "README.ja.md", "docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0", text)
                self.assertIn("mandala --version", text)
                self.assertIn("mandala v0.4.0", text)
                self.assertIn("mandala --help", text)
                self.assertIn("mandala <command> --help", text)
                self.assertIn("status --json", text)
                self.assertIn("gaps --required --json", text)
                self.assertNotIn("v0.3.0", text)
        for name in ("docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(consecutive=name):
                self.assertIn(f"{validate.PINNED_INSTALL}\n{validate.CLI_CHECK}\n", (ROOT / name).read_text(encoding="utf-8"))

    def test_installation_path_troubleshooting_is_preserved(self) -> None:
        for name in ("docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for token in ("GOBIN", "GOPATH", "go env GOBIN", "go env GOBIN failed", "$LASTEXITCODE -ne 0", "Test-Path", "mandala (devel)", "Go 1.26"):
                with self.subTest(file=name, token=token):
                    self.assertIn(token, text)

    def test_documentation_validation_rejects_stale_baseline_or_help_check(self) -> None:
        for name in DOCS:
            changes = [("v0.4.0", "v0.2.0"), ("v0.4.0", "v0.3.0"), ("mandala --version", "mandala --help")]
            for before, after in changes:
                with self.subTest(file=name, replacement=(before, after)), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    copy_docs(root)
                    target = root / name
                    target.write_text(target.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        validate.validate_documentation(root)

    def test_install_and_version_check_must_be_consecutive(self) -> None:
        for name in ("docs/INSTALLATION.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                copy_docs(root)
                target = root / name
                text = target.read_text(encoding="utf-8")
                target.write_text(text.replace(f"{validate.PINNED_INSTALL}\n{validate.CLI_CHECK}", f"{validate.PINNED_INSTALL}\n\n{validate.CLI_CHECK}"), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not verify the installed version"):
                    validate.validate_documentation(root)

    def test_status_json_contract_clauses_are_required(self) -> None:
        text = (validate.PACKAGES[0] / CONTRACT).read_text(encoding="utf-8")
        for clause in validate.STATUS_JSON_CONTRACT:
            with self.subTest(clause=clause[:50]), self.assertRaisesRegex(ValueError, "missing status JSON contract"):
                validate.validate_status_contract(text.replace(clause, ""), CONTRACT)
        for field in ('"schema_version":1', '"required_gaps":2', '"optional":{"open":1,"done":1,"na":0}'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "missing status JSON contract"):
                validate.validate_status_contract(text.replace(field, ""), CONTRACT)

    def test_status_json_contract_missing_from_package_is_rejected(self) -> None:
        clause = validate.STATUS_JSON_CONTRACT[-1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "mandala"
            shutil.copytree(validate.PACKAGES[0], package)
            validate.package_files(package, root)
            target = package / CONTRACT
            target.write_text(target.read_text(encoding="utf-8").replace(clause, ""), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing status JSON contract"):
                validate.package_files(package, root)

    def test_global_json_guidance_is_rejected(self) -> None:
        text = (validate.PACKAGES[0] / CONTRACT).read_text(encoding="utf-8")
        for wrong in ("Run `mandala --json status`.", "mandala --project /path/to/project --json status", "Use `--json=true status`.",
                      "mandala --json --project /path/to/project status"):
            with self.subTest(wrong=wrong):
                with self.assertRaisesRegex(ValueError, "invalid global --json guidance"):
                    validate.validate_status_contract(text + "\n" + wrong + "\n", CONTRACT)
                with self.assertRaisesRegex(ValueError, "invalid global --json guidance"):
                    validate.validate_cli_baseline(text + "\n" + wrong + "\n", CONTRACT)
        for name in ("README.md", "docs/INSTALLATION.ja.md"):
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                copy_docs(root)
                target = root / name
                target.write_text(target.read_text(encoding="utf-8").replace("status --json", "--json status", 1), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid global --json guidance"):
                    validate.validate_documentation(root)

    def test_completion_gate_substitution_is_rejected(self) -> None:
        text = (validate.PACKAGES[0] / CONTRACT).read_text(encoding="utf-8")
        for wrong in (
            "Use `status --json` instead of `gaps --required --json` before a completion claim.",
            "`status --json` satisfies the completion gate when `required_gaps` is 0.",
            "`status --json` is the completion gate.",
            "`status --json` を `gaps --required --json` の代わりに完了判定に使います。",
            "Use status --json as the completion gate.",
            "`status --json` can replace gaps for completion.",
            "`required_gaps: 0` from `status --json` proves completion.",
        ):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, "completion gate substitute"):
                validate.validate_status_contract(text + "\n" + wrong + "\n", CONTRACT)
        for allowed in (
            "`status --json` gives counts rather than per-cell detail; use gaps for IDs.",
            "`status --json` does not replace `gaps --required --json` as the completion gate.",
            "Zero `required_gaps` does not prove task completion.",
        ):
            with self.subTest(allowed=allowed):
                validate.validate_status_contract(text + "\n" + allowed + "\n", CONTRACT)
        for name, clause in validate.STATUS_DOC_CLAUSES.items():
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                copy_docs(root)
                target = root / name
                target.write_text(target.read_text(encoding="utf-8").replace(clause, ""), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "completion gate distinction"):
                    validate.validate_documentation(root)

    def test_completion_gate_negation_is_local_to_its_clause(self) -> None:
        rejected = (
            "status --json does not replace show --json for inspection, but status --json replaces gaps --required --json as the completion gate.",
            "status --json does not replace show --json; however, status --json is the completion gate.",
            "Zero required_gaps does not prove completion, but required_gaps: 0 proves completion.",
            "status --json は show --json の代わりにはなりませんが、gaps --required --json の代わりに完了判定に使えます。",
            "status --json does not replace show --json and is the completion gate.",
            "status --json is not optional: it is the completion gate.",
            "status --json never changes state, so use it instead of gaps --required --json before completion.",
            "status --json is read-only. It does not edit state, yet it replaces gaps as the completion gate.",
            "status --json does not replace show --json\nbut it serves as the completion gate",
            "Although show --json is not a gate, `status --json` satisfies the completion gate.",
            "`status --json` は読み取り専用で状態を変更しないので、`gaps --required --json` の代わりに使えます。",
            "status --json or gaps --required --json is the completion gate.",
            "Either status --json or gaps --required --json serves as the completion gate.",
            "status --json and gaps --required --json both serve as the completion gate.",
        )
        for sentence in rejected:
            with self.subTest(rejected=sentence), self.assertRaisesRegex(ValueError, "completion gate substitute"):
                validate.validate_status_guidance(sentence, "fixture")
        allowed = (
            "status --json does not replace gaps --required --json as the completion gate.",
            "status --json never replaces gaps --required --json for completion.",
            "Zero required_gaps does not prove task completion.",
            "status --json は完了判定の gaps --required --json の代わりにはなりません。",
            "status --json gives aggregate counts rather than per-cell detail; use gaps for IDs.",
            "status --json does not replace show --json, and it does not replace gaps --required --json as the completion gate.",
            "`gaps --required --json` is the completion gate; `status --json` is an optional summary.",
            "`status --json` cannot replace `gaps --required --json`, which remains the completion gate.",
            "`status --json` は `show --json` の代わりにならず、`gaps --required --json` の代わりにもなりません。",
            "`status --json` is a summary, and `gaps --required --json` is the completion gate.",
            "status --json does not replace show --json or gaps --required --json before a completion claim.",
        )
        for sentence in allowed:
            with self.subTest(allowed=sentence):
                validate.validate_status_guidance(sentence, "fixture")

    def test_unrelated_negation_does_not_excuse_a_substitute_phrase(self) -> None:
        rejected = (
            "Because status --json never writes state, it replaces gaps --required --json as the completion gate.",
            "Because status --json cannot modify state, it can replace gaps --required --json before completion.",
            "Because status --json never writes state it replaces gaps --required --json as the completion gate.",
            "Because status --json cannot modify state it can replace gaps --required --json before completion.",
            "status --json that never writes state is the completion gate.",
            "`status --json` (not `show --json`) replaces gaps --required --json.",
            "status --json never edits state and replaces gaps --required --json.",
            "`status --json` は状態を変更しない `gaps --required --json` の代わりに完了判定に使えます。",
            "`status --json` は `gaps --required --json` の代わりに使えます（状態は変更しません）。",
            "Use status --json instead of gaps --required --json because it never writes state.",
            "status --json never changes state, so rely on it in place of gaps --required --json.",
            "`status --json` は `gaps --required --json` の代わりに使えるので迷わない。",
        )
        for sentence in rejected:
            with self.subTest(rejected=sentence), self.assertRaisesRegex(ValueError, "completion gate substitute"):
                validate.validate_status_guidance(sentence, "fixture")
        allowed = (
            "Because status --json never writes state, it does not replace gaps --required --json as the completion gate.",
            "status --json never replaces gaps --required --json for completion.",
            "Because status --json never writes state it does not replace gaps --required --json as the completion gate.",
            "status --json doesn't replace gaps --required --json as the completion gate.",
            "Do not use status --json as the completion gate.",
            "status --json will never replace gaps --required --json.",
            "`status --json` は `gaps --required --json` の代わりに使えません。",
            "`status --json` は `gaps --required --json` の代わりに完了判定には使えません。",
            "Do not use status --json instead of gaps --required --json.",
            "Never use status --json in place of gaps --required --json.",
            "status --json should not be used instead of gaps --required --json.",
            "Never rely on status --json instead of gaps --required --json.",
            "Do not run status --json instead of gaps --required --json.",
            "status --json is not a substitute; do not use it in place of gaps --required --json.",
            "`status --json` は `gaps --required --json` の代わりに完了判定として使用してはいけません。",
        )
        for sentence in allowed:
            with self.subTest(allowed=sentence):
                validate.validate_status_guidance(sentence, "fixture")

    def test_final_review_scope_and_negation_regressions(self) -> None:
        # A prohibition on substitution is not guidance to substitute status for gaps.
        allowed = (
            "Avoid using status --json instead of gaps --required --json.",
            "Avoid using status --json as the completion gate.",
            "status --json includes required_gaps, and tests prove completion.",
            "status --json includes required_gaps; tests prove completion.",
            "status --json does not replace gaps --required --json as the completion gate.",
        )
        for sentence in allowed:
            with self.subTest(allowed=sentence):
                validate.validate_status_guidance(sentence, "fixture")
        rejected = (
            "status --json は gaps --required --json の代わりに使えるが書き込まない。",
            "status --json or gaps --required --json is the completion gate.",
            "status --json is a summary, and it proves completion.",
        )
        for sentence in rejected:
            with self.subTest(rejected=sentence), self.assertRaisesRegex(ValueError, "completion gate substitute"):
                validate.validate_status_guidance(sentence, "fixture")

    def test_current_documents_pass_the_completion_gate_tripwire(self) -> None:
        for name in DOCS + ("src/mandala/references/cli-contract.md", "src/mandala/SKILL.md"):
            with self.subTest(file=name):
                validate.validate_status_guidance((ROOT / name).read_text(encoding="utf-8"), name)

    def test_skill_completion_gate_clauses_cannot_be_removed(self) -> None:
        skill = (validate.PACKAGES[0] / "SKILL.md").read_text(encoding="utf-8")
        for contract_id in ("COMP-002", "COMP-003", "CLI-002", "STATE-001", "STATE-002"):
            with self.subTest(contract=contract_id), self.assertRaisesRegex(ValueError, contract_id):
                validate.validate_safety_contract(skill.replace(validate.SAFETY_CONTRACT[contract_id], ""))
        self.assertEqual(len(validate.SAFETY_CONTRACT), 23)

    def test_skill_stays_within_byte_budget_with_new_baseline(self) -> None:
        content = (validate.PACKAGES[0] / "SKILL.md").read_bytes()
        self.assertEqual(validate.SKILL_BYTE_BUDGET, 6214)
        self.assertLessEqual(len(content), validate.SKILL_BYTE_BUDGET)
        with self.assertRaisesRegex(ValueError, "over the 6214-byte budget"):
            validate.validate_skill_budget(content + b"x" * (validate.SKILL_BYTE_BUDGET - len(content) + 1), "fixture")

    def test_package_byte_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, generated = root / "src" / "mandala", root / "dist" / "mandala"
            shutil.copytree(validate.PACKAGES[0], source)
            shutil.copytree(validate.PACKAGES[0], generated)
            validate.validate_packages((source, generated), root)
            target = generated / CONTRACT
            target.write_bytes(target.read_bytes().replace(b"exit `2`", b"exit  `2`", 1))
            with self.assertRaisesRegex(ValueError, "generated package differs from canonical source"):
                validate.validate_packages((source, generated), root)

    def test_evaluation_setup_uses_version_check(self) -> None:
        cases = json.loads((ROOT / "tests/evals/cases.json").read_text(encoding="utf-8"))
        new_project = next(case for case in cases if case["id"] == "new-explicit-project")
        self.assertIn("mandala --version", new_project["expected"][0])
        self.assertIn("mandala v0.4.0", new_project["expected"][0])
        missing_cli = next(case for case in cases if case["id"] == "missing-cli")
        self.assertIn("mandala --version", missing_cli["prompt"])
        self.assertNotIn("v0.3.0", json.dumps(cases))


if __name__ == "__main__":
    unittest.main()
