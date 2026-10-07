from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import unittest

from scripts import eval_coverage
from scripts import live_eval_artifacts as artifacts
from scripts import validate
from tests import eval_artifact_fixtures as fx


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = validate.load_contracts()


def check(check_id, contracts, status):
    return {"id": check_id, "contracts": contracts, "status": status, "evidence": "synthetic"}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.count = 0

    def tearDown(self):
        self.directory.cleanup()

    def coverage(self, specs, **kwargs):
        self.count += 1
        path = self.root / f"run{self.count}" / "codex"
        fx.write_run(path, specs, **kwargs)
        return eval_coverage.build_coverage(artifacts.Source(str(path)))

    def spec(self, alias="R1", fixture="reset-request", status="AUTO_PASS", checks=(), manual=()):
        spec = fx.CaseSpec(alias, fixture, status, manual_review=list(manual), checks=list(checks))
        return spec

    @staticmethod
    def row(coverage, cid):
        return next(entry for entry in coverage["contracts"] if entry["id"] == cid)

    def test_each_dimension_is_separate(self):
        spec = self.spec(checks=[check("x.pass", ["STATE-001"], "PASS"), check("x.fail", ["CLEAN-001"], "FAIL"), check("x.unobs", ["CLEAN-002"], "UNOBSERVABLE")],
                         manual=[{"contracts": ["CLEAN-003"], "note": "n"}])
        coverage = self.coverage([spec])
        self.assertEqual(self.row(coverage, "STATE-001")["automatic_pass_checks"], [{"case": "R1", "check": "x.pass"}])
        self.assertEqual(self.row(coverage, "STATE-001")["automatic_fail_checks"], [])
        self.assertEqual(self.row(coverage, "CLEAN-001")["automatic_fail_checks"], [{"case": "R1", "check": "x.fail"}])
        self.assertEqual(self.row(coverage, "CLEAN-001")["automatic_pass_checks"], [])
        self.assertEqual(self.row(coverage, "CLEAN-002")["automatic_unobservable_checks"], [{"case": "R1", "check": "x.unobs"}])
        self.assertEqual(self.row(coverage, "CLEAN-003")["manual_review_required_by"], ["R1"])
        states = {cid: self.row(coverage, cid)["coverage_state"] for cid in ("STATE-001", "CLEAN-001", "CLEAN-002", "CLEAN-003", "CLEAN-004", "AUTH-001")}
        self.assertEqual(states, {"STATE-001": "AUTOMATED_OBSERVED", "CLEAN-001": "AUTOMATED_OBSERVED", "CLEAN-002": "UNOBSERVABLE_ONLY",
                                  "CLEAN-003": "MANUAL_REQUIRED_ONLY", "CLEAN-004": "FIXTURE_ONLY", "AUTH-001": "NOT_EXERCISED"})
        self.assertEqual(self.row(coverage, "CLEAN-004")["fixture_referenced_by"], ["R1"])

    def test_precedence(self):
        spec = self.spec(checks=[check("a", ["STATE-001"], "PASS"), check("b", ["CLEAN-001"], "UNOBSERVABLE"), check("c", ["CLEAN-002"], "FAIL")],
                         manual=[{"contracts": ["STATE-001", "CLEAN-001", "CLEAN-002", "CLEAN-003"], "note": "n"}])
        coverage = self.coverage([spec])
        self.assertEqual(self.row(coverage, "STATE-001")["coverage_state"], "AUTOMATED_OBSERVED")  # PASS + manual
        self.assertEqual(self.row(coverage, "CLEAN-001")["coverage_state"], "UNOBSERVABLE_ONLY")  # UNOBSERVABLE + manual
        self.assertEqual(self.row(coverage, "CLEAN-002")["coverage_state"], "AUTOMATED_OBSERVED")  # FAIL + manual
        self.assertEqual(self.row(coverage, "CLEAN-003")["coverage_state"], "MANUAL_REQUIRED_ONLY")  # manual only

    def test_environment_failure_is_scope_only(self):
        spec = fx.environment_error_case()
        spec.checks = [check("leftover", ["CAP-001"], "PASS")]
        coverage = self.coverage([spec])
        for cid in ("CAP-001", "CAP-005", "STATE-001"):
            row = self.row(coverage, cid)
            self.assertEqual(row["coverage_state"], "FIXTURE_ONLY", cid)
            self.assertEqual((row["automatic_pass_checks"], row["manual_review_required_by"]), ([], []))
            self.assertEqual(row["fixture_referenced_by"], ["B4"])
        self.assertFalse(coverage["cases"][0]["evidence_counted"])

    def test_not_run_case_without_result_contributes_nothing(self):
        path = self.root / "nr" / "codex"
        fx.write_run(path, [fx.b5_case()])
        summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        summary["cases"].append({"case": "R1", "fixture": "reset-request", "status": "NOT_RUN", "manual_review_required": False, "error": {"category": "interrupted", "message": "x"}})
        summary["complete"] = False
        (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        coverage = eval_coverage.build_coverage(artifacts.Source(str(path)))
        self.assertFalse(coverage["source"]["complete"])
        self.assertEqual(self.row(coverage, "CLEAN-002")["coverage_state"], "NOT_EXERCISED")
        self.assertEqual([case["result_artifact"] for case in coverage["cases"]], [True, False])
        summary["cases"][-1]["status"] = "AUTO_PASS"
        (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaisesRegex(artifacts.ArtifactError, "missing"):
            eval_coverage.build_coverage(artifacts.Source(str(path)))

    def test_unknown_safety_id_is_rejected(self):
        for where in ("check", "scope", "manual"):
            with self.subTest(where=where):
                spec = self.spec()
                if where == "check":
                    spec.checks = [check("x", ["NOPE-001"], "PASS")]
                elif where == "manual":
                    spec.manual_review = [{"contracts": ["NOPE-001"], "note": "n"}]
                else:
                    spec.contracts = ["NOPE-001"]
                with self.assertRaisesRegex(artifacts.ArtifactError, "unknown safety contract IDs.*NOPE-001"):
                    self.coverage([spec])

    def test_malformed_check_status_is_rejected(self):
        with self.assertRaisesRegex(artifacts.ArtifactError, "malformed check"):
            self.coverage([self.spec(checks=[check("x", ["STATE-001"], "MAYBE")])])

    def test_all_contracts_once_and_stable_order(self):
        specs = [self.spec("R1", checks=[check("z", ["STATE-001"], "PASS"), check("a", ["STATE-001"], "PASS")]),
                 self.spec("B5", "capacity-final-child", checks=[check("m", ["STATE-001"], "PASS")])]
        coverage = self.coverage(specs)
        ids = [row["id"] for row in coverage["contracts"]]
        self.assertEqual(len(ids), 23)
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(set(ids), set(CONTRACTS))
        self.assertEqual(self.row(coverage, "STATE-001")["automatic_pass_checks"], [{"case": "B5", "check": "m"}, {"case": "R1", "check": "a"}, {"case": "R1", "check": "z"}])
        self.assertEqual(sum(coverage["counts"].values()), 23)
        self.assertEqual(coverage["unknown_contract_ids"], [])
        self.assertEqual([case["case"] for case in coverage["cases"]], ["B5", "R1"])
        self.assertEqual(eval_coverage.render_coverage(coverage), eval_coverage.render_coverage(json.loads(json.dumps(coverage))))

    def test_summary_coverage_field_is_not_trusted(self):
        coverage = self.coverage([fx.b5_case()], summary_overrides={"contract_coverage": {"exercised": ["AUTH-001"], "automatically_checked": ["AUTH-001"], "manual_review": [], "not_exercised": []}})
        self.assertEqual(self.row(coverage, "AUTH-001")["coverage_state"], "NOT_EXERCISED")
        self.assertEqual(self.row(coverage, "CAP-002")["coverage_state"], "AUTOMATED_OBSERVED")

    def test_wording_never_claims_manual_verification_or_pass_rate(self):
        coverage = self.coverage([fx.r1_case()])
        text = eval_coverage.render_coverage(coverage) + json.dumps(coverage)
        self.assertIn("not evidence", text)
        self.assertIn("Manual review required is not manual review passed", text)
        self.assertIn("not a pass rate", text)
        for phrase in ("manually verified", "manual verified", "manually passed", "pass rate:"):
            self.assertNotIn(phrase, text.lower())
        self.assertNotIn("%", eval_coverage.render_coverage(coverage))


class ReleaseScopeCoverageTests(unittest.TestCase):
    def test_complete_release_scope_is_not_all_observed(self):
        manifest = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
        specs = [fx.CaseSpec(alias, manifest["cases"][alias]["fixture"], "ENVIRONMENT_ERROR") for alias in manifest["suites"]["release"]]
        with tempfile.TemporaryDirectory() as directory:
            fx.write_run(Path(directory) / "codex", specs)
            coverage = eval_coverage.build_coverage(artifacts.Source(str(Path(directory) / "codex")))
        # Every contract is in declared scope, yet nothing was observed: NOT_EXERCISED 0 is not a pass.
        self.assertEqual(coverage["counts"]["NOT_EXERCISED"], 0)
        self.assertEqual(coverage["counts"]["FIXTURE_ONLY"], 23)
        self.assertEqual(coverage["counts"]["AUTOMATED_OBSERVED"], 0)

    def test_boundary_cases_contribute_their_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            fx.write_run(Path(directory) / "codex", [fx.a1_case(), fx.a2_case(), fx.r3_case(), fx.p1_case()])
            coverage = eval_coverage.build_coverage(artifacts.Source(str(Path(directory) / "codex")))
        rows = {row["id"]: row for row in coverage["contracts"]}
        self.assertIn({"case": "A1", "check": "a1.no-mutation"}, rows["AUTH-002"]["automatic_pass_checks"])
        self.assertIn({"case": "A2", "check": "a2.state-preserved"}, rows["AUTH-003"]["automatic_pass_checks"])
        self.assertIn({"case": "R3", "check": "r3.invalid-state-preserved"}, rows["CLEAN-005"]["automatic_pass_checks"])
        self.assertIn({"case": "P1", "check": "p1.no-mutation"}, rows["CLI-001"]["automatic_pass_checks"])
        self.assertEqual(rows["CLEAN-005"]["manual_review_required_by"], ["R3"])


class CoverageCliTests(unittest.TestCase):
    def test_cli_writes_report_outside_source_and_accepts_replay_output(self):
        from scripts import eval_replay
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run" / "codex"
            fx.write_run(source, [fx.b5_case(), fx.environment_error_case()])
            before = fx.tree_digest(source)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(eval_coverage.main([str(source), "--output-dir", str(root / "cov")]), 0)
            coverage = json.loads((root / "cov" / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual((coverage["schema_version"], coverage["artifact_type"], coverage["source"]["kind"]), (1, "mandala-contract-coverage", "live"))
            self.assertTrue((root / "cov" / "coverage.md").is_file())
            self.assertEqual(fx.tree_digest(source), before)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                eval_replay.main([str(source), "--output-dir", str(root / "replay")])
                self.assertEqual(eval_coverage.main([str(root / "replay"), "--output-dir", str(root / "cov2")]), 0)
            replayed = json.loads((root / "cov2" / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(replayed["source"]["kind"], "replay")
            self.assertEqual(next(row for row in replayed["contracts"] if row["id"] == "CAP-005")["coverage_state"], "FIXTURE_ONLY")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(eval_coverage.main([str(source), "--output-dir", str(root / "cov")]), 2)  # not empty
                self.assertEqual(eval_coverage.main([str(source), "--output-dir", str(source / "cov")]), 2)  # inside source
                self.assertEqual(eval_coverage.main([str(root / "missing"), "--output-dir", str(root / "cov3")]), 2)


if __name__ == "__main__":
    unittest.main()
