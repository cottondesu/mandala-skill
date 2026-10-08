from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import copy
import io
import json
import re
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from scripts import eval_coverage
from scripts import eval_profile
from scripts import eval_replay
from scripts import eval_sanitize
from scripts import live_eval_artifacts as artifacts
from scripts import validate
from tests import eval_artifact_fixtures as fx


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
CATALOG = json.loads(validate.PROFILE_PATH.read_text(encoding="utf-8"))
RELEASE = ["R1", "R2", "M1", "C1", "C2", "B4", "B5", "B6", "A1", "A2", "R3", "P1"]
PROFILE_IDS = ["tracked-design-review", "capacity-constrained-expansion", "reset-recovery"]
APPLICABILITY = {
    "tracked-design-review": {"CORE": ["M1", "C1", "C2"], "CONDITIONAL": ["R1", "R2", "B4", "B5", "B6", "A1", "A2", "R3", "P1"], "NOT_APPLICABLE": []},
    "capacity-constrained-expansion": {"CORE": ["M1", "B4", "B5", "B6"], "CONDITIONAL": ["C1", "C2", "P1"], "NOT_APPLICABLE": ["R1", "R2", "A1", "A2", "R3"]},
    "reset-recovery": {"CORE": ["R1", "R2", "R3"], "CONDITIONAL": ["P1"], "NOT_APPLICABLE": ["M1", "C1", "C2", "B4", "B5", "B6", "A1", "A2"]},
}
CONTRACT_COUNTS = {
    "tracked-design-review": {"CORE": 9, "CONDITIONAL": 14, "NOT_APPLICABLE": 0},
    "capacity-constrained-expansion": {"CORE": 8, "CONDITIONAL": 8, "NOT_APPLICABLE": 7},
    "reset-recovery": {"CORE": 7, "CONDITIONAL": 2, "NOT_APPLICABLE": 14},
}


def profiles():
    return validate.validate_profile_catalog(copy.deepcopy(CATALOG), MANIFEST, CASES)


def check(check_id, contracts, status):
    return {"id": check_id, "contracts": contracts, "status": status, "evidence": "synthetic"}


def fixture_of(alias):
    return MANIFEST["cases"][alias]["fixture"]


def passing_spec(alias, status="PASS", **kwargs):
    """Synthetic case whose recorded checks observe every declared contract with one status."""
    contracts = fx.fixture_contracts(fixture_of(alias))
    return fx.CaseSpec(alias, fixture_of(alias), checks=[check(f"{alias.lower()}.synthetic", contracts, status)], **kwargs)


class CatalogTests(unittest.TestCase):
    def assertRejected(self, catalog, pattern, manifest=None):
        with self.assertRaisesRegex(ValueError, pattern):
            validate.validate_profile_catalog(catalog, MANIFEST if manifest is None else manifest, CASES)

    def mutate(self, change):
        catalog = copy.deepcopy(CATALOG)
        change(catalog)
        return catalog

    def test_current_catalog_is_valid(self):
        loaded = profiles()
        self.assertEqual(sorted(loaded), sorted(PROFILE_IDS))
        self.assertEqual(CATALOG["schema_version"], 1)
        self.assertEqual(set(CATALOG), {"schema_version", "profiles"})
        self.assertEqual([profile["id"] for profile in CATALOG["profiles"]], PROFILE_IDS)

    def test_release_suite_order_is_pinned(self):
        self.assertEqual(MANIFEST["suites"]["release"], RELEASE)
        for profile in CATALOG["profiles"]:
            self.assertEqual([entry["case"] for entry in profile["cases"]], RELEASE, profile["id"])

    def test_initial_case_applicability(self):
        for profile in CATALOG["profiles"]:
            with self.subTest(profile=profile["id"]):
                actual = {value: [entry["case"] for entry in profile["cases"] if entry["applicability"] == value] for value in validate.APPLICABILITY}
                expected = {value: [alias for alias in RELEASE if alias in APPLICABILITY[profile["id"]][value]] for value in validate.APPLICABILITY}
                self.assertEqual(actual, expected)
                for entry in profile["cases"]:
                    self.assertEqual(entry["condition"] is None, entry["applicability"] != "CONDITIONAL", entry["case"])

    def test_derived_contract_counts(self):
        contracts = validate.load_contracts()
        for pid, profile in profiles().items():
            with self.subTest(profile=pid):
                derived = validate.derive_contract_applicability(profile, MANIFEST, CASES, contracts)
                counts = {value: sum(1 for row in derived.values() if row["applicability"] == value) for value in validate.APPLICABILITY}
                self.assertEqual(counts, CONTRACT_COUNTS[pid])
                self.assertEqual({pid: tuple(counts.values())}, {pid: validate.PROFILE_CONTRACT_COUNTS[pid]})
                for cid, row in derived.items():
                    if row["applicability"] == "NOT_APPLICABLE":
                        self.assertEqual((row["core_cases"], row["conditional_cases"]), ([], []))
        # Counts are derived; profiles.json never stores safety contract IDs or counts.
        text = validate.PROFILE_PATH.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b[A-Z]+-[0-9]{3}\b", text))
        self.assertNotIn("contracts", text)
        self.assertNotIn("task_contracts", text)

    def test_derivation_ignores_task_contracts(self):
        example = json.loads(validate.FIELD_USAGE_PATH.read_text(encoding="utf-8"))
        task_ids = {item["id"] for item in example["task_contracts"]}
        contracts = validate.load_contracts()
        for profile in profiles().values():
            self.assertFalse(task_ids & set(validate.derive_contract_applicability(profile, MANIFEST, CASES, contracts)))

    def test_schema_and_top_level(self):
        self.assertRejected(self.mutate(lambda c: c.update(schema_version=2)), "unsupported schema_version 2")
        self.assertRejected(self.mutate(lambda c: c.update(extra=1)), "exactly schema_version and profiles")
        self.assertRejected(self.mutate(lambda c: c.pop("schema_version")), "exactly schema_version and profiles")
        self.assertRejected(self.mutate(lambda c: c.update(profiles=[])), "non-empty profiles list")
        self.assertRejected(self.mutate(lambda c: c.update(profiles={})), "non-empty profiles list")
        self.assertRejected([], "exactly schema_version and profiles")

    def test_profile_identity(self):
        self.assertRejected(self.mutate(lambda c: c["profiles"].append(copy.deepcopy(c["profiles"][0]))), "duplicate profile ID")
        self.assertRejected(self.mutate(lambda c: c["profiles"].pop()), "must contain exactly")
        self.assertRejected(self.mutate(lambda c: c["profiles"][0].update(id="other-profile")), "must contain exactly")
        self.assertRejected(self.mutate(lambda c: c["profiles"][0].update(id="Bad ID")), "invalid profile ID")
        for field in ("title", "description"):
            for value in ("", "   ", None):
                with self.subTest(field=field, value=value):
                    self.assertRejected(self.mutate(lambda c: c["profiles"][1].update({field: value})), f"non-empty {field}")
        self.assertRejected(self.mutate(lambda c: c["profiles"][0].update(extra=1)), "needs exactly")
        self.assertRejected(self.mutate(lambda c: c["profiles"][0].pop("title")), "needs exactly")
        self.assertRejected(self.mutate(lambda c: c["profiles"][0].update(cases={})), "cases must be a list")

    def test_case_set_and_order(self):
        def first(c):
            return c["profiles"][0]["cases"]
        self.assertRejected(self.mutate(lambda c: first(c).pop()), "does not classify release cases: \\['P1'\\]")
        self.assertRejected(self.mutate(lambda c: first(c).append(copy.deepcopy(first(c)[0]))), "more than once: \\['R1'\\]")
        self.assertRejected(self.mutate(lambda c: first(c).__setitem__(11, dict(first(c)[11], case="R1"))), "more than once")
        self.assertRejected(self.mutate(lambda c: first(c).append(dict(first(c)[0], case="Z9"))), "unknown or non-release case aliases: \\['Z9'\\]")
        self.assertRejected(self.mutate(lambda c: first(c).__setitem__(0, dict(first(c)[0], case="Z9"))), "unknown or non-release")
        self.assertRejected(self.mutate(lambda c: first(c).reverse()), "release-suite order")
        self.assertRejected(self.mutate(lambda c: first(c).__setitem__(0, dict(first(c)[0], extra=1))), "case entry needs exactly")
        self.assertRejected(self.mutate(lambda c: first(c)[0].pop("rationale")), "case entry needs exactly")
        self.assertRejected(self.mutate(lambda c: first(c).__setitem__(0, "R1")), "case entry needs exactly")

    def test_case_fields(self):
        def edit(index, **fields):
            return self.mutate(lambda c: c["profiles"][0]["cases"][index].update(fields))
        core, conditional = 2, 0  # tracked-design-review: M1 CORE, R1 CONDITIONAL
        self.assertRejected(edit(core, applicability="REQUIRED"), "invalid applicability")
        self.assertRejected(edit(core, applicability="core"), "invalid applicability")
        for value in (None, "", "  "):
            self.assertRejected(edit(conditional, condition=value), "needs a non-empty condition")
        self.assertRejected(edit(core, condition="sometimes"), "CORE case M1 must have a null condition")
        self.assertRejected(self.mutate(lambda c: c["profiles"][2]["cases"][2].update(condition="x")), "NOT_APPLICABLE case M1 must have a null condition")
        for value in ("", "  ", None, " padded "):
            self.assertRejected(edit(core, rationale=value), "trimmed rationale")

    def test_profile_needs_core_case(self):
        def demote(c):
            for entry in c["profiles"][2]["cases"]:
                if entry["applicability"] == "CORE":
                    entry.update(applicability="NOT_APPLICABLE")
        self.assertRejected(self.mutate(demote), "at least one CORE case")

    def test_derived_counts_regression(self):
        def promote(c):
            c["profiles"][2]["cases"][2].update(applicability="CORE")
        self.assertRejected(self.mutate(promote), "reset-recovery derived contract applicability")

    def test_future_release_alias_requires_classification(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["cases"]["Z9"] = {"fixture": "explicit-tracking", "grader": "contextual-update"}
        manifest["suites"]["release"].append("Z9")
        self.assertRejected(copy.deepcopy(CATALOG), "does not classify release cases: \\['Z9'\\]", manifest)

    def test_validator_main_validates_profiles(self):
        broken = self.mutate(lambda c: c["profiles"][0]["cases"].pop())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with mock.patch.object(validate, "PROFILE_PATH", path), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "does not classify release cases"):
                    validate.main()
        output = io.StringIO()
        with redirect_stdout(output):
            validate.main()
        self.assertIn("3 real-world coverage profiles", output.getvalue())
        self.assertNotIn("verified profile", output.getvalue())


class ProfileReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.count = 0
        self.profiles = profiles()

    def tearDown(self):
        self.directory.cleanup()

    def run_dir(self, specs, **kwargs):
        self.count += 1
        path = self.root / f"run{self.count}" / "codex"
        fx.write_run(path, specs, **kwargs)
        return path

    def report(self, path, profile_id):
        return eval_profile.build_profile_report(artifacts.Source(str(path)), self.profiles[profile_id], MANIFEST, CASES)

    @staticmethod
    def contract(report, cid):
        return next(row for row in report["contracts"] if row["id"] == cid)

    @staticmethod
    def case(report, alias):
        return next(row for row in report["cases"] if row["case"] == alias)

    def full_release(self):
        return self.run_dir([passing_spec(alias) for alias in RELEASE])

    def test_complete_release_counts_for_every_profile(self):
        path = self.full_release()
        for pid in PROFILE_IDS:
            with self.subTest(profile=pid):
                report = self.report(path, pid)
                self.assertEqual((report["schema_version"], report["artifact_type"]), (1, "mandala-coverage-profile-report"))
                self.assertEqual(report["contract_applicability_counts"], CONTRACT_COUNTS[pid])
                self.assertEqual(report["case_applicability_counts"], {value: len(APPLICABILITY[pid][value]) for value in validate.APPLICABILITY})
                self.assertEqual(report["core_evidence_counts"]["AUTOMATED_OBSERVED"], CONTRACT_COUNTS[pid]["CORE"])
                self.assertEqual(report["conditional_evidence_counts"]["AUTOMATED_OBSERVED"], CONTRACT_COUNTS[pid]["CONDITIONAL"])
                self.assertEqual(list(report["core_evidence_counts"]), list(eval_coverage.STATES))
                self.assertEqual(list(report["conditional_evidence_counts"]), list(eval_coverage.STATES))
                self.assertEqual([row["case"] for row in report["cases"]], RELEASE)
                ids = [row["id"] for row in report["contracts"]]
                self.assertEqual(ids, sorted(ids))
                self.assertEqual(len(ids), 23)
                self.assertEqual(report["unprofiled_source_cases"], [])
                self.assertEqual(set(report), {"schema_version", "artifact_type", "generated_at", "profile", "source", "semantics", "case_applicability_counts",
                                               "contract_applicability_counts", "core_evidence_counts", "conditional_evidence_counts", "unprofiled_source_cases", "cases", "contracts"})
                self.assertEqual(set(report["source"]), {"kind", "id", "agent", "suite", "complete"})
                self.assertEqual(set(report["profile"]), {"id", "title", "description"})
                for row in report["cases"]:
                    self.assertEqual(set(row), {"case", "fixture", "applicability", "condition", "rationale", "source_present", "source_status", "result_artifact", "evidence_counted"})
                    self.assertEqual(row["evidence_counted"], row["applicability"] != "NOT_APPLICABLE")
                for row in report["contracts"]:
                    self.assertEqual(set(row), {"id", "slug", "area", "applicability", "core_cases", "conditional_cases", "core_evidence", "conditional_evidence"})
                    for bucket in (row["core_evidence"], row["conditional_evidence"]):
                        self.assertEqual(set(bucket), {"coverage_state", "fixture_referenced_by", "automatic_pass_checks", "automatic_fail_checks",
                                                       "automatic_unobservable_checks", "manual_review_required_by"})

    def test_conditional_evidence_cannot_promote_missing_core(self):
        # STATE-001 is CORE in capacity-constrained-expansion via M1/B4/B5/B6; none present. P1 (CONDITIONAL) is present,
        # and a stray P1 check observes STATE-001 and AUTH-001 (CORE via M1 only).
        spec = passing_spec("P1")
        spec.checks.append(check("p1.stray", ["STATE-001", "AUTH-001", "STATE-003"], "PASS"))
        report = self.report(self.run_dir([spec]), "capacity-constrained-expansion")
        for cid in ("AUTH-001", "STATE-001"):
            row = self.contract(report, cid)
            self.assertEqual(row["applicability"], "CORE")
            self.assertEqual(row["core_evidence"]["coverage_state"], "NOT_EXERCISED")
            self.assertEqual(row["core_evidence"]["automatic_pass_checks"], [])
            self.assertEqual(row["conditional_cases"], [])
            self.assertEqual(row["conditional_evidence"]["coverage_state"], "NOT_EXERCISED")
        self.assertEqual(self.case(report, "M1")["source_status"], "NOT_PRESENT")
        self.assertEqual(report["core_evidence_counts"]["AUTOMATED_OBSERVED"], 0)
        self.assertEqual(report["core_evidence_counts"]["NOT_EXERCISED"], 8)
        # The conditional observation is still visible in its own bucket.
        row = self.contract(report, "STATE-003")
        self.assertEqual(row["applicability"], "CONDITIONAL")
        self.assertEqual(row["conditional_evidence"]["coverage_state"], "AUTOMATED_OBSERVED")
        self.assertIn({"case": "P1", "check": "p1.stray"}, row["conditional_evidence"]["automatic_pass_checks"])

    def test_core_contract_with_both_case_kinds_keeps_buckets_separate(self):
        # tracked-design-review: STATE-001 CORE via M1, CONDITIONAL via R1/R2/B4/B5/B6. Only R1 present.
        report = self.report(self.run_dir([passing_spec("R1")]), "tracked-design-review")
        row = self.contract(report, "STATE-001")
        self.assertEqual((row["applicability"], row["core_cases"]), ("CORE", ["M1"]))
        self.assertEqual(row["conditional_cases"], ["R1", "R2", "B4", "B5", "B6"])
        self.assertEqual(row["core_evidence"]["coverage_state"], "NOT_EXERCISED")
        self.assertEqual(row["core_evidence"]["fixture_referenced_by"], [])
        self.assertEqual(row["conditional_evidence"]["coverage_state"], "AUTOMATED_OBSERVED")
        self.assertEqual(row["conditional_evidence"]["fixture_referenced_by"], ["R1"])

    def test_not_applicable_evidence_is_ignored(self):
        # reset-recovery: M1, B4 are N/A; their checks observe STATE-001 (CORE via R1/R2) and AUTH-001 (N/A).
        m1 = passing_spec("M1")
        m1.checks.append(check("m1.stray", ["CLEAN-001"], "PASS"))
        report = self.report(self.run_dir([m1, passing_spec("B4")]), "reset-recovery")
        self.assertEqual(self.contract(report, "STATE-001")["core_evidence"]["coverage_state"], "NOT_EXERCISED")
        self.assertEqual(self.contract(report, "CLEAN-001")["core_evidence"]["coverage_state"], "NOT_EXERCISED")
        for cid in ("AUTH-001", "CAP-001"):
            row = self.contract(report, cid)
            self.assertEqual(row["applicability"], "NOT_APPLICABLE")
            for bucket in (row["core_evidence"], row["conditional_evidence"]):
                self.assertEqual(bucket["coverage_state"], "NOT_EXERCISED")
                self.assertEqual([bucket[field] for field in ("fixture_referenced_by", "automatic_pass_checks", "automatic_fail_checks",
                                                              "automatic_unobservable_checks", "manual_review_required_by")], [[]] * 5)
        self.assertEqual((self.case(report, "M1")["source_status"], self.case(report, "M1")["evidence_counted"]), ("AUTO_PASS", False))
        self.assertEqual(report["core_evidence_counts"]["AUTOMATED_OBSERVED"], 0)

    def test_scope_ignores_recorded_checks(self):
        # Recorded checks naming contracts outside a profile's scope never change applicability.
        m1 = passing_spec("M1")
        m1.checks.append(check("m1.stray", ["CLEAN-005", "AUTH-002"], "PASS"))
        report = self.report(self.run_dir([m1]), "capacity-constrained-expansion")
        self.assertEqual(report["contract_applicability_counts"], CONTRACT_COUNTS["capacity-constrained-expansion"])
        self.assertEqual(self.contract(report, "CLEAN-005")["applicability"], "NOT_APPLICABLE")
        self.assertEqual(self.contract(report, "CLEAN-005")["core_evidence"]["coverage_state"], "NOT_EXERCISED")

    def test_fail_unobservable_manual_and_fixture_only_are_preserved(self):
        r1 = fx.CaseSpec("R1", "reset-request", "AUTO_FAIL", checks=[check("r1.fail", ["CLEAN-001"], "FAIL"), check("r1.unobs", ["CLEAN-002"], "UNOBSERVABLE")],
                         manual_review=[{"contracts": ["CLEAN-003"], "note": "n"}])
        report = self.report(self.run_dir([r1]), "reset-recovery")
        fail = self.contract(report, "CLEAN-001")["core_evidence"]
        self.assertEqual(fail["coverage_state"], "AUTOMATED_OBSERVED")
        self.assertEqual(fail["automatic_fail_checks"], [{"case": "R1", "check": "r1.fail"}])
        self.assertEqual(fail["automatic_pass_checks"], [])
        self.assertEqual(self.contract(report, "CLEAN-002")["core_evidence"]["coverage_state"], "UNOBSERVABLE_ONLY")
        manual = self.contract(report, "CLEAN-003")["core_evidence"]
        self.assertEqual((manual["coverage_state"], manual["manual_review_required_by"]), ("MANUAL_REQUIRED_ONLY", ["R1"]))
        self.assertEqual(self.contract(report, "CLEAN-004")["core_evidence"]["coverage_state"], "FIXTURE_ONLY")
        self.assertEqual(self.case(report, "R1")["source_status"], "AUTO_FAIL")
        self.assertIn("r1.fail", eval_profile.render_profile(report))

    def test_environment_and_not_run_contribute_no_observed_evidence(self):
        env = fx.environment_error_case()  # B4, ENVIRONMENT_ERROR
        env.checks = [check("leftover", ["CAP-001"], "PASS")]
        path = self.run_dir([env])
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        summary["cases"].append({"case": "B6", "fixture": "capacity-full-tree", "status": "NOT_RUN", "manual_review_required": False, "error": None})
        summary["complete"] = False
        (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        report = self.report(path, "capacity-constrained-expansion")
        self.assertFalse(report["source"]["complete"])
        row = self.contract(report, "CAP-001")["core_evidence"]
        self.assertEqual((row["coverage_state"], row["fixture_referenced_by"], row["automatic_pass_checks"], row["manual_review_required_by"]), ("FIXTURE_ONLY", ["B4"], [], []))
        b4, b6, b5 = self.case(report, "B4"), self.case(report, "B6"), self.case(report, "B5")
        self.assertEqual((b4["source_status"], b4["result_artifact"], b4["evidence_counted"]), ("ENVIRONMENT_ERROR", True, False))
        self.assertEqual((b6["source_present"], b6["source_status"], b6["result_artifact"], b6["evidence_counted"]), (True, "NOT_RUN", False, False))
        self.assertEqual((b5["source_present"], b5["source_status"], b5["result_artifact"]), (False, "NOT_PRESENT", False))
        self.assertEqual(report["core_evidence_counts"]["AUTOMATED_OBSERVED"], 0)

    def test_missing_profile_cases_are_not_present(self):
        report = self.report(self.run_dir([fx.b5_case()]), "tracked-design-review")
        absent = [row["case"] for row in report["cases"] if row["source_status"] == "NOT_PRESENT"]
        self.assertEqual(absent, [alias for alias in RELEASE if alias != "B5"])
        for row in report["cases"]:
            if row["case"] != "B5":
                self.assertEqual((row["source_present"], row["result_artifact"], row["evidence_counted"]), (False, False, False))

    def test_fixture_mismatch_is_rejected(self):
        spec = passing_spec("M1")
        spec.fixture = "explicit-tracking"
        path = self.run_dir([spec])
        with self.assertRaisesRegex(artifacts.ArtifactError, "M1 records fixture 'explicit-tracking'"):
            self.report(path, "tracked-design-review")
        # A summary entry that disagrees with the result is also a mismatch.
        path = self.run_dir([passing_spec("M1")])
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        summary["cases"][0]["fixture"] = "zero-gaps"
        (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaisesRegex(artifacts.ArtifactError, "M1 records fixture 'zero-gaps'"):
            self.report(path, "tracked-design-review")

    def test_unprofiled_source_cases_are_listed_and_ignored(self):
        stray = fx.CaseSpec("Q7", "contextual-update", checks=[check("q7.x", ["AUTH-001", "STATE-001"], "PASS")])
        report = self.report(self.run_dir([stray, passing_spec("C1")]), "tracked-design-review")
        self.assertEqual(report["unprofiled_source_cases"], ["Q7"])
        self.assertNotIn("Q7", [row["case"] for row in report["cases"]])
        self.assertEqual(self.contract(report, "AUTH-001")["core_evidence"]["coverage_state"], "NOT_EXERCISED")
        self.assertNotIn("Q7", json.dumps(report["contracts"]))
        self.assertIn("Unprofiled source cases", eval_profile.render_profile(report))

    def test_unprofiled_cases_are_never_read(self):
        # A newer source may carry an unprofiled alias that declares a contract this catalog does not know.
        stray = fx.CaseSpec("Q7", "contextual-update", checks=[check("q7.x", ["NEW-001"], "PASS")])
        stray.contracts = ["NEW-001"]
        path = self.run_dir([stray, passing_spec("R1")])
        with self.assertRaisesRegex(artifacts.ArtifactError, "unknown safety contract IDs"):
            eval_coverage.build_coverage(artifacts.Source(str(path)))  # whole-source coverage is unchanged
        report = self.report(path, "reset-recovery")
        self.assertEqual(report["unprofiled_source_cases"], ["Q7"])
        self.assertEqual(self.contract(report, "CLEAN-001")["core_evidence"]["coverage_state"], "AUTOMATED_OBSERVED")
        # Unknown contract IDs in a profiled case remain a structural error.
        bad = passing_spec("R1")
        bad.checks.append(check("r1.x", ["NEW-001"], "PASS"))
        with self.assertRaisesRegex(artifacts.ArtifactError, "unknown safety contract IDs"):
            self.report(self.run_dir([bad]), "reset-recovery")

    def test_markdown_escapes_untrusted_strings(self):
        spec = passing_spec("R1")
        spec.checks.append(check("r1.a|b\nc", ["CLEAN-001"], "PASS"))
        path = self.run_dir([spec], summary_overrides={"suite": "rel|ease\n# injected", "run_id": "id`x"})
        text = eval_profile.render_profile(self.report(path, "reset-recovery"))
        self.assertNotIn("\n# injected", text)
        self.assertIn("rel\\|ease # injected", text)
        self.assertIn("r1.a\\|b c", text)
        for line in text.splitlines():
            if line.startswith("| CLEAN-001"):
                self.assertEqual(line.replace("\\|", "").count("|"), 8)

    def test_summary_coverage_is_not_trusted(self):
        path = self.run_dir([fx.b5_case()], summary_overrides={"contract_coverage": {"exercised": ["AUTH-001"], "automatically_checked": ["AUTH-001"], "manual_review": [], "not_exercised": []}})
        report = self.report(path, "capacity-constrained-expansion")
        self.assertEqual(self.contract(report, "AUTH-001")["core_evidence"]["coverage_state"], "NOT_EXERCISED")
        self.assertEqual(self.contract(report, "CAP-002")["core_evidence"]["coverage_state"], "AUTOMATED_OBSERVED")

    def test_replay_source_and_historical_v02_layout(self):
        source = self.run_dir([fx.b5_case(), fx.r1_case(), fx.r3_case()], evidence=False)  # v0.2.x: no evidence.json
        replay = self.root / "replay"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(eval_replay.main([str(source), "--output-dir", str(replay)]), 0)
        report = eval_profile.build_profile_report(artifacts.Source(str(replay)), self.profiles["reset-recovery"], MANIFEST, CASES)
        self.assertEqual(report["source"]["kind"], "replay")
        self.assertEqual(report["contract_applicability_counts"], CONTRACT_COUNTS["reset-recovery"])
        self.assertEqual(self.case(report, "R2")["source_status"], "NOT_PRESENT")
        self.assertEqual(self.case(report, "B5")["applicability"], "NOT_APPLICABLE")
        self.assertFalse(self.case(report, "B5")["evidence_counted"])
        self.assertTrue(self.case(report, "R1")["source_present"])

    def test_sanitized_and_other_sources_are_rejected(self):
        source = self.run_dir([fx.b5_case()])
        sanitized = self.root / "sanitized"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(eval_sanitize.main([str(source), "--output-dir", str(sanitized)]), 0)
        with self.assertRaisesRegex(artifacts.ArtifactError, "unsupported source artifact_type"):
            artifacts.Source(str(sanitized))
        other = self.root / "other"
        other.mkdir()
        (other / "summary.json").write_text(json.dumps({"schema_version": 1, "cases": []}), encoding="utf-8")
        with self.assertRaisesRegex(artifacts.ArtifactError, "not a live agent run summary"):
            artifacts.Source(str(other))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(eval_profile.main([str(sanitized), "--profile", "reset-recovery", "--output-dir", str(self.root / "p1")]), 2)
            self.assertEqual(eval_profile.main([str(ROOT), "--profile", "reset-recovery", "--output-dir", str(self.root / "p2")]), 2)
            self.assertEqual(eval_profile.main([str(source.parent), "--profile", "reset-recovery", "--output-dir", str(self.root / "p3")]), 2)

    def test_render_is_deterministic_and_avoids_score_claims(self):
        for pid in PROFILE_IDS:
            report = self.report(self.full_release(), pid)
            text = eval_profile.render_profile(report)
            self.assertEqual(text, eval_profile.render_profile(json.loads(json.dumps(report))))
            self.assertTrue(text.startswith(f"# Coverage profile: {report['profile']['title']}"))
            head = "\n".join(text.splitlines()[:4])
            self.assertIn("This is a curated applicability view over recorded evaluation evidence.", head)
            self.assertIn("It is not a pass rate, safety score, release gate, or proof of verification.", head)
            self.assertIn("CORE is workflow applicability, not success", text)
            self.assertIn("CONDITIONAL evidence is shown separately and does not satisfy missing CORE evidence", text)
            self.assertIn("NOT_APPLICABLE applies only to this profile", text)
            self.assertIn("Manual review required is not manual review passed", text)
            self.assertIn("Automated FAIL is observed evidence, not success", text)
            self.assertIn("A NOT_EXERCISED count of 0 is not success", text)
            combined = (text + json.dumps(report)).lower()
            self.assertNotIn("%", text)
            for phrase in ("fully verified", "profile passed", "profile is safe", "verified safe", "safe to publish", "secret-free", "sanitized",
                           "pass rate:", "score:", "percentage", "grade", "manually verified", "manually passed"):
                self.assertNotIn(phrase, combined, phrase)
            self.assertNotIn("percent", combined)


class ProfileCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = eval_profile.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_list_profiles(self):
        before = sorted(p.name for p in artifacts.OUTPUT_ROOT.iterdir()) if artifacts.OUTPUT_ROOT.is_dir() else None
        with fx.no_agent_or_mandala_execution():
            code, out, _ = self.run_main("--list-profiles")
        self.assertEqual(code, 0)
        self.assertEqual([line.split("\t")[0] for line in out.splitlines()], sorted(PROFILE_IDS))
        self.assertIn("Tracked design review", out)
        after = sorted(p.name for p in artifacts.OUTPUT_ROOT.iterdir()) if artifacts.OUTPUT_ROOT.is_dir() else None
        self.assertEqual(before, after)
        for extra in (["src"], ["--profile", "reset-recovery"], ["--output-dir", str(self.root / "x")]):
            with self.subTest(extra=extra):
                code, _, err = self.run_main("--list-profiles", *extra)
                self.assertEqual(code, 2)
                self.assertIn("--list-profiles takes no source", err)
        self.assertEqual(self.run_main()[0], 2)
        self.assertEqual(self.run_main(str(self.root))[0], 2)

    def test_unknown_profile_and_malformed_catalog(self):
        source = self.root / "run" / "codex"
        fx.write_run(source, [fx.b5_case()])
        code, _, err = self.run_main(str(source), "--profile", "nope", "--output-dir", str(self.root / "out"))
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)
        self.assertFalse((self.root / "out").exists())
        broken = self.root / "profiles.json"
        broken.write_text("{", encoding="utf-8")
        with mock.patch.object(validate, "PROFILE_PATH", broken):
            self.assertEqual(self.run_main("--list-profiles")[0], 2)

    def test_cli_writes_report_without_touching_source_or_executing_anything(self):
        source = self.root / "run" / "codex"
        specs = [passing_spec(alias) for alias in RELEASE]
        specs[0].extra_files = {"final.txt": "--- Turn 1 ---\nsecret-final-response\n"}
        fx.write_run(source, specs)
        digest = fx.tree_digest(source)

        def no_network(*args, **kwargs):
            raise AssertionError("network access attempted")
        with fx.no_agent_or_mandala_execution(), mock.patch.object(socket, "socket", side_effect=no_network), \
                mock.patch.object(socket, "create_connection", side_effect=no_network):
            for pid in PROFILE_IDS:
                code, out, err = self.run_main(str(source), "--profile", pid, "--output-dir", str(self.root / pid))
                self.assertEqual(code, 0, err)
                self.assertNotIn("%", out)
        self.assertEqual(fx.tree_digest(source), digest)
        for pid in PROFILE_IDS:
            files = sorted(path.name for path in (self.root / pid).iterdir())
            self.assertEqual(files, ["profile.json", "profile.md"])
            report = json.loads((self.root / pid / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(report["contract_applicability_counts"], CONTRACT_COUNTS[pid])
            self.assertEqual(report["profile"]["id"], pid)
            texts = "".join(fx.read_tree_text(self.root / pid).values())
            for leaked in ("secret-final-response", "fake-session-12345", "/bin/zsh", "mandala --project", "thread.started", "preflight", str(source)):
                self.assertNotIn(leaked, texts)

    def test_unsafe_outputs_are_rejected(self):
        source = self.root / "run" / "codex"
        fx.write_run(source, [fx.b5_case()])
        digest = fx.tree_digest(source)
        nonempty = self.root / "full"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("x", encoding="utf-8")
        for target, message in ((source, "is the source"), (source / "out", "inside the source"), (self.root / "run", "ancestor"),
                                (nonempty, "not empty"), (ROOT / "tests" / "profile-out", "under .eval-live"), (ROOT / "src" / "profile-out", "under .eval-live"),
                                (ROOT / "docs" / "profile-out", "under .eval-live"), (ROOT / "dist" / "profile-out", "under .eval-live"),
                                (ROOT / ".eval-live" / ".mandala" / "x", ".mandala")):
            with self.subTest(target=target):
                code, _, err = self.run_main(str(source), "--profile", "reset-recovery", "--output-dir", str(target))
                self.assertEqual(code, 2)
                self.assertIn(message, err)
        self.assertEqual((nonempty / "keep.txt").read_text(encoding="utf-8"), "x")
        self.assertEqual(fx.tree_digest(source), digest)
        for path in ("tests/profile-out", "src/profile-out", "docs/profile-out", "dist/profile-out"):
            self.assertFalse((ROOT / path).exists())

    def test_default_output_is_fresh_directory_under_eval_live_profiles(self):
        source = self.root / "run" / "codex"
        fx.write_run(source, [fx.b5_case()])
        with mock.patch.object(artifacts, "OUTPUT_ROOT", self.root / "eval-live"):
            code, out, err = self.run_main(str(source), "--profile", "capacity-constrained-expansion")
            again = self.run_main(str(source), "--profile", "capacity-constrained-expansion")
        self.assertEqual((code, again[0]), (0, 0), err)
        created = sorted((self.root / "eval-live" / "profiles").iterdir())
        self.assertEqual(len(created), 2)
        self.assertTrue(all((path / "profile.json").is_file() for path in created))

    def test_default_output_never_uses_untrusted_agent_path(self):
        source = self.root / "run" / "codex"
        fx.write_run(source, [fx.b5_case()], agent="../../../../escape")
        with mock.patch.object(artifacts, "OUTPUT_ROOT", self.root / "eval-live"):
            code, _, err = self.run_main(str(source), "--profile", "reset-recovery")
        self.assertEqual(code, 0, err)
        created = list((self.root / "eval-live" / "profiles").iterdir())
        self.assertEqual(len(created), 1)
        self.assertIn("-profile-unknown-", created[0].name)
        self.assertFalse(any("escape" in path.name for path in self.root.rglob("*")))
        self.assertEqual(eval_profile.default_output_agent("claude"), "claude")
        for agent in (None, "", "..", "a/b", "Codex", "x" * 65):
            self.assertEqual(eval_profile.default_output_agent(agent), "unknown", agent)

    def test_symlinked_source_is_rejected(self):
        source = self.root / "run" / "codex"
        fx.write_run(source, [fx.b5_case()])
        link = self.root / "link"
        link.symlink_to(source, target_is_directory=True)
        code, _, err = self.run_main(str(link), "--profile", "reset-recovery", "--output-dir", str(self.root / "o1"))
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        inner = self.root / "run2" / "codex"
        fx.write_run(inner, [fx.b5_case()])
        shutil.move(str(inner / "cases" / "B5" / "result.json"), str(self.root / "elsewhere.json"))
        (inner / "cases" / "B5" / "result.json").symlink_to(self.root / "elsewhere.json")
        code, _, err = self.run_main(str(inner), "--profile", "reset-recovery", "--output-dir", str(self.root / "o2"))
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)


if __name__ == "__main__":
    unittest.main()
