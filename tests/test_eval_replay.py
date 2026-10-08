from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from scripts import eval_replay
from scripts import live_eval_artifacts as artifacts
from tests import eval_artifact_fixtures as fx


ROOT = Path(__file__).resolve().parents[1]


def replay(source, *args, output=None):
    """Run eval_replay.main quietly; returns (exit code, output dir)."""
    output = output or Path(tempfile.mkdtemp()) / "replay"
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
        code = eval_replay.main([str(source), "--output-dir", str(output), *args])
    replay.last_error = errors.getvalue()
    return code, output


def case_result(output, alias):
    return json.loads((output / "cases" / alias / "result.json").read_text(encoding="utf-8"))


class ReplayTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def run_dir(self, specs, name="run", **kwargs):
        path = self.root / name / "codex"
        fx.write_run(path, specs, **kwargs)
        return path

    def replay(self, source, *args, name="out"):
        return replay(source, *args, output=self.root / name)


class EvidenceReplayTests(ReplayTestCase):
    def test_evidence_json_happy_path(self):
        source = self.run_dir([fx.b5_case(), fx.r1_case(), fx.c2_case()])
        code, output = self.replay(source)
        self.assertEqual(code, 0)
        for alias in ("B5", "R1", "C2"):
            record = case_result(output, alias)
            self.assertEqual((record["replay_status"], record["graded_status"], record["status_changed"]), ("REPLAYED", "AUTO_PASS", False), alias)
            self.assertEqual(record["snapshot_source"], "evidence.json")
            self.assertFalse(record["legacy_snapshot_reconstruction"])
            self.assertEqual(record["artifact_type"], "mandala-eval-replay-case")
            self.assertEqual(record["schema_version"], 1)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["artifact_type"], "mandala-eval-replay-summary")
        self.assertEqual(summary["counts"]["replayed"], 3)
        self.assertTrue((output / "report.md").is_file())
        coverage = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(coverage["artifact_type"], "mandala-contract-coverage")
        self.assertTrue((output / "coverage.md").is_file())

    def test_evidence_is_authoritative_over_truncated_event_output(self):
        source = self.run_dir([fx.b5_case(title_size=400)])
        events = [json.loads(line) for line in (source / "cases" / "B5" / "normalized.jsonl").read_text(encoding="utf-8").splitlines()]
        snapshots = [event for event in events if event.get("phase") == "snapshot"]
        self.assertTrue(all(len(event["output"]) == fx.EVENT_OUTPUT_LIMIT for event in snapshots))
        self.assertGreater(len(fx.show_output("Goal", fx.tree_cells([8] * 8, 400))), fx.EVENT_OUTPUT_LIMIT)
        code, output = self.replay(source)
        self.assertEqual((code, case_result(output, "B5")["graded_status"]), (0, "AUTO_PASS"))
        (source / "cases" / "B5" / "evidence.json").unlink()
        code, output = self.replay(source, name="legacy")
        record = case_result(output, "B5")
        self.assertEqual((code, record["replay_status"], record["graded_status"]), (2, "UNREPLAYABLE", None))
        self.assertIn("invalid or truncated JSON", record["error"]["message"])

    def test_malformed_evidence_is_unreplayable(self):
        source = self.run_dir([fx.b5_case()])
        path = source / "cases" / "B5" / "evidence.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["after_turn"]["1"] = None
        path.write_text(json.dumps(evidence), encoding="utf-8")
        code, output = self.replay(source)
        self.assertEqual((code, case_result(output, "B5")["replay_status"]), (2, "UNREPLAYABLE"))
        self.assertIn("unavailable", case_result(output, "B5")["error"]["message"])


class LegacyReplayTests(ReplayTestCase):
    def test_legacy_reconstruction_happy_path(self):
        source = self.run_dir([fx.r1_case(), fx.b5_case(), fx.c2_case()], evidence=False)
        code, output = self.replay(source)
        self.assertEqual(code, 0)
        for alias in ("R1", "B5", "C2"):
            record = case_result(output, alias)
            self.assertEqual((record["replay_status"], record["graded_status"]), ("REPLAYED", "AUTO_PASS"), alias)
            self.assertTrue(record["legacy_snapshot_reconstruction"])
            self.assertEqual(record["snapshot_source"], "normalized.jsonl")
            self.assertNotIn("evidence.json", record["source_artifact_hashes"])

    def test_missing_snapshot_is_unreplayable(self):
        spec = fx.b5_case()
        spec.events = [event for event in spec.events if not (event.get("phase") == "snapshot" and event.get("turn") == 1)]
        code, output = self.replay(self.run_dir([spec], evidence=False))
        record = case_result(output, "B5")
        self.assertEqual((code, record["replay_status"]), (2, "UNREPLAYABLE"))
        self.assertIn("after_turn[1] snapshot is missing", record["error"]["message"])

    def test_duplicate_snapshot_is_ambiguous(self):
        spec = fx.b5_case()
        spec.evaluator(0, "show", "--json", phase="snapshot", output=fx.show_output("Other", []))
        code, output = self.replay(self.run_dir([spec], evidence=False))
        self.assertIn("ambiguous", case_result(output, "B5")["error"]["message"])
        self.assertEqual(code, 2)

    def test_invalid_or_truncated_snapshot_json_is_unreplayable(self):
        malformed = (("invalid", "not json"), ("truncated", fx.show_output("Goal", fx.tree_cells([8] * 8))[:-40]), ("wrong-shape", "[1, 2]"),
                     ("empty-list", "[]"), ("cells-not-list", '{"goal": "g", "cells": "not-a-list"}'), ("no-cells", '{"goal": "g"}'),
                     ("no-goal", '{"cells": []}'), ("cell-without-id", '{"goal": "g", "cells": [{"parent": "", "status": "open", "required": true}]}'),
                     ("cell-not-object", '{"goal": "g", "cells": ["r1"]}'))
        for label, stdout in malformed:
            with self.subTest(label):
                spec = fx.b5_case()
                for event in spec.events:
                    if event.get("phase") == "snapshot" and event["turn"] == 1:
                        event["output"] = stdout
                code, output = self.replay(self.run_dir([spec], name=label, evidence=False), name=f"out-{label}")
                record = case_result(output, "B5")
                self.assertEqual((code, record["replay_status"], record["graded_status"]), (2, "UNREPLAYABLE", None))
                self.assertIn("invalid or truncated JSON", record["error"]["message"])

    def test_project_root_placeholder_is_not_shell_redirection(self):
        source = self.run_dir([fx.b5_case()], evidence=False)
        normalized = (source / "cases" / "B5" / "normalized.jsonl").read_text(encoding="utf-8")
        self.assertIn("mandala --project <project-root> show --json", normalized)
        code, output = self.replay(source)
        record = case_result(output, "B5")
        checks = {item["id"]: item for item in record["checks"]}
        self.assertEqual((code, record["graded_status"], checks["b5.fresh-show"]["status"]), (0, "AUTO_PASS", "PASS"))
        # The synthetic absolute path never leaks into replay output, and it is never created.
        synthetic = artifacts.synthetic_project_root()
        self.assertFalse(os.path.exists(synthetic))
        for path in output.rglob("*"):
            if path.is_file():
                self.assertNotIn(synthetic, path.read_text(encoding="utf-8"), path)
        self.assertIn("<project-root>", checks["b5.fresh-show"]["evidence"])
        # Without the substitution the placeholder parses as redirections and the inspection is lost.
        events = [json.loads(line) for line in normalized.splitlines()]
        before, after_turn, _ = artifacts.legacy_snapshots(events, 1)
        checks, status = artifacts.grade_recorded("capacity-final-child", events, fx.PLACEHOLDER, before, after_turn, 1)
        self.assertEqual(status, "AUTO_FAIL")


class StatusTests(ReplayTestCase):
    def test_auto_pass_to_auto_fail_is_recorded_as_changed(self):
        spec = fx.b5_case(agent_show=False)
        spec.checks = [{"id": "b5.fresh-show", "contracts": ["STATE-001"], "status": "PASS", "evidence": "recorded under an older grader"}]
        code, output = self.replay(self.run_dir([spec]))
        record = case_result(output, "B5")
        self.assertEqual((code, record["source_status"], record["graded_status"], record["status_changed"]), (1, "AUTO_PASS", "AUTO_FAIL", True))
        self.assertEqual({item["id"]: item["status"] for item in record["checks"]}["b5.fresh-show"], "FAIL")
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"]["status_changed"], 1)

    def test_source_auto_fail_remains_replayable(self):
        code, output = self.replay(self.run_dir([fx.r1_case("AUTO_FAIL", clean=True)]))
        record = case_result(output, "R1")
        self.assertEqual((code, record["replay_status"], record["graded_status"], record["status_changed"]), (1, "REPLAYED", "AUTO_FAIL", False))

    def test_environment_error_is_unreplayable_not_behavior(self):
        code, output = self.replay(self.run_dir([fx.environment_error_case()]))
        record = case_result(output, "B4")
        self.assertEqual((code, record["replay_status"], record["graded_status"], record["status_changed"]), (2, "UNREPLAYABLE", None, None))
        self.assertEqual(record["error"]["category"], "insufficient-evidence")
        self.assertEqual(record["checks"], [])

    def test_incomplete_turns_are_unreplayable(self):
        spec = fx.c2_case()
        spec.result_overrides = {"turns_completed": 1}
        code, output = self.replay(self.run_dir([spec], evidence=False))
        self.assertEqual((code, case_result(output, "C2")["error"]["category"]), (2, "turn-mismatch"))

    def test_inconclusive_replay_exits_two(self):
        spec = fx.r1_case("INCONCLUSIVE")
        spec.events = [event for event in spec.events if event["actor"] == "evaluator"]
        spec.agent(1, f"true || mandala --project {fx.PLACEHOLDER} show --json")
        code, output = self.replay(self.run_dir([spec]))
        self.assertEqual((code, case_result(output, "R1")["graded_status"]), (2, "INCONCLUSIVE"))

    def test_manual_review_metadata_preserved_but_not_graded(self):
        spec = fx.r1_case()
        spec.extra_files["final.txt"] = "--- Turn 1 ---\nI deleted everything. PASS.\n"
        code, output = self.replay(self.run_dir([spec]))
        record = case_result(output, "R1")
        self.assertEqual(code, 0)
        self.assertTrue(record["manual_review_required"])
        self.assertEqual(record["manual_review"], fx.R1_MANUAL)
        self.assertEqual(record["semantic_review"], "not replayed")
        self.assertNotIn("manual_review_result", record)
        self.assertIn("not replayed", (output / "report.md").read_text(encoding="utf-8"))


class ExitPrecedenceTests(ReplayTestCase):
    def test_unreplayable_beats_auto_fail(self):
        code, output = self.replay(self.run_dir([fx.r1_case("AUTO_FAIL", clean=True), fx.environment_error_case()]))
        self.assertEqual((case_result(output, "R1")["graded_status"], case_result(output, "B4")["replay_status"]), ("AUTO_FAIL", "UNREPLAYABLE"))
        self.assertEqual(code, 2)

    def test_inconclusive_beats_auto_fail(self):
        records = [{"replay_status": "REPLAYED", "graded_status": "AUTO_FAIL"}, {"replay_status": "REPLAYED", "graded_status": "INCONCLUSIVE"}]
        self.assertEqual(eval_replay.exit_code(records), 2)
        code, _ = self.replay(self.run_dir([fx.r1_case("AUTO_FAIL", clean=True), fx.c2_case()]))
        self.assertEqual(code, 1)  # AUTO_FAIL with every other case cleanly replayed
        self.assertEqual(eval_replay.exit_code([{"replay_status": "REPLAYED", "graded_status": "AUTO_PASS"}]), 0)


class SourceKindTests(ReplayTestCase):
    def test_source_kind_matrix(self):
        from scripts import eval_coverage, eval_sanitize
        live = self.run_dir([fx.b5_case()])
        _, replayed = self.replay(live)
        sanitized = self.root / "sanitized"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(eval_sanitize.main([str(live), "--output-dir", str(sanitized)]), 0)
        arbitrary = self.root / "arbitrary"
        arbitrary.mkdir()
        (arbitrary / "summary.json").write_text(json.dumps({"schema_version": 1, "cases": []}), encoding="utf-8")
        parent = self.root / "run"  # holds the codex/ agent directory
        expected = {  # source -> (replay, coverage, sanitize) exit codes
            "live": (live, 0, 0, 0), "replay": (replayed, 2, 0, 0), "sanitized": (sanitized, 2, 2, 2),
            "arbitrary": (arbitrary, 2, 2, 2), "parent": (parent, 2, 2, 2),
        }
        for label, (source, replay_code, coverage_code, sanitize_code) in expected.items():
            with self.subTest(source=label), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(eval_replay.main([str(source), "--output-dir", str(self.root / f"r-{label}")]), replay_code)
                self.assertEqual(eval_coverage.main([str(source), "--output-dir", str(self.root / f"c-{label}")]), coverage_code)
                self.assertEqual(eval_sanitize.main([str(source), "--output-dir", str(self.root / f"s-{label}")]), sanitize_code)
        with self.assertRaisesRegex(artifacts.ArtifactError, "not a live agent run summary"):
            artifacts.Source(str(arbitrary))
        with self.assertRaisesRegex(artifacts.ArtifactError, "mandala-sanitized-export"):
            artifacts.Source(str(sanitized))


class MappingTests(ReplayTestCase):
    def test_unknown_case_alias_is_unreplayable(self):
        spec = fx.b5_case()
        spec.alias = "Z9"
        code, output = self.replay(self.run_dir([spec], evidence=False))
        record = case_result(output, "Z9")
        self.assertEqual((code, record["replay_status"], record["error"]["category"]), (2, "UNREPLAYABLE", "unknown-case"))

    def test_fixture_mapping_mismatch_is_unreplayable(self):
        spec = fx.b5_case()
        spec.result_overrides = {"fixture": "capacity-full-tree"}
        code, output = self.replay(self.run_dir([spec], evidence=False))
        record = case_result(output, "B5")
        self.assertEqual((code, record["error"]["category"], record["graded_status"]), (2, "fixture-mismatch", None))

    def test_unsupported_schema_exits_two_without_output(self):
        for target, change in (("result.json", {"schema_version": 2}), ("summary.json", {"schema_version": 2}), ("evidence.json", {"schema_version": 2}),
                               ("normalized.jsonl", None)):
            with self.subTest(target=target):
                source = self.run_dir([fx.b5_case()], name=target)
                path = source / ("summary.json" if target == "summary.json" else f"cases/B5/{target}")
                if change is None:
                    path.write_text(path.read_text(encoding="utf-8").replace('"schema_version": 1', '"schema_version": 2', 1), encoding="utf-8")
                else:
                    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **change}), encoding="utf-8")
                code, output = self.replay(source, name=f"out-{target}")
                self.assertEqual(code, 2)
                self.assertIn("unsupported schema_version", replay.last_error)
                self.assertFalse(output.exists())

    def test_case_and_suite_selection(self):
        source = self.run_dir([fx.r1_case(), fx.b5_case()])
        code, output = self.replay(source, "--case", "B5")
        self.assertEqual((code, json.loads((output / "summary.json").read_text())["counts"]["cases"]), (0, 1))
        code, output = self.replay(source, "--case", "R1", "--case", "B5", name="two")
        self.assertEqual(sorted(path.name for path in (output / "cases").iterdir()), ["B5", "R1"])
        code, output = self.replay(source, "--suite", "capacity", name="suite")
        records = {alias: case_result(output, alias) for alias in ("B4", "B5", "B6")}
        self.assertEqual(records["B5"]["graded_status"], "AUTO_PASS")
        self.assertEqual(records["B4"]["error"]["category"], "not-recorded")
        self.assertEqual(code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            eval_replay.main([str(source), "--case", "B5", "--suite", "release"])
        self.assertEqual(raised.exception.code, 2)

    def test_multiple_agent_directories_are_not_guessed(self):
        fx.write_run(self.root / "run" / "codex", [fx.b5_case()])
        fx.write_run(self.root / "run" / "claude", [fx.b5_case()], agent="claude")
        code, output = self.replay(self.root / "run")
        self.assertEqual(code, 2)
        self.assertIn("pass exactly one agent-level directory", replay.last_error)
        self.assertFalse(output.exists())

    def test_replay_output_is_not_a_replay_source(self):
        _, output = self.replay(self.run_dir([fx.b5_case()]))
        code, _ = self.replay(output, name="again")
        self.assertEqual(code, 2)
        self.assertIn("accepts: live", replay.last_error)


class ProvenanceTests(ReplayTestCase):
    def test_provenance_hashes_and_versions(self):
        source = self.run_dir([fx.b5_case()])
        code, output = self.replay(source)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(summary["source"]["source_summary_sha256"], artifacts.sha256_file(source / "summary.json"))
        self.assertEqual(summary["source"]["skill_sha256"], "1" * 64)
        self.assertEqual(summary["current"]["skill_sha256"], artifacts.sha256_file(ROOT / "src" / "mandala" / "SKILL.md"))
        self.assertFalse(summary["current"]["skill_hashes_match"])  # provenance, not a failure
        for name in ("scripts/live_eval_cases.py", "tests/evals/cases.json", "tests/evals/live_suites.json", "tests/evals/contracts.json"):
            self.assertEqual(summary["current"]["files"][name], artifacts.sha256_file(ROOT / name))
        self.assertIn("dirty", summary["current"])
        hashes = case_result(output, "B5")["source_artifact_hashes"]
        for name in ("result.json", "normalized.jsonl", "evidence.json"):
            self.assertEqual(hashes[name], artifacts.sha256_file(source / "cases" / "B5" / name))
        text = "".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(ROOT), text)

    def test_dirty_repository_points_to_file_hashes(self):
        source = self.run_dir([fx.b5_case()])
        with mock.patch.object(artifacts, "git_revision", return_value=("d" * 40, True)):
            code, output = self.replay(source)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((code, summary["current"]["git_sha"], summary["current"]["dirty"]), (0, "d" * 40, True))
        self.assertIn("the Git SHA alone does not identify the graders", (output / "report.md").read_text(encoding="utf-8"))

    def test_synthetic_project_root_is_fixed_and_outside_inputs(self):
        source = self.run_dir([fx.b5_case()], evidence=False)
        synthetic = Path(artifacts.synthetic_project_root())
        self.assertTrue(synthetic.is_absolute())
        self.assertEqual(artifacts.synthetic_project_root(), str(synthetic))
        self.assertFalse(synthetic.exists())
        self.assertFalse(artifacts.is_within(synthetic, ROOT) or artifacts.is_within(synthetic, source))
        self.assertNotIn(str(synthetic), (source / "cases" / "B5" / "normalized.jsonl").read_text(encoding="utf-8"))

    def test_source_artifacts_remain_byte_identical(self):
        source = self.run_dir([fx.b5_case(), fx.r1_case("AUTO_FAIL", clean=True), fx.environment_error_case()])
        before = fx.tree_digest(source)
        self.replay(source)
        self.replay(source, "--case", "B5", name="second")
        self.assertEqual(fx.tree_digest(source), before)


class BoundaryReplayTests(ReplayTestCase):
    """v0.3.0 boundary cases re-grade from the same events + snapshots model, offline."""

    def test_boundary_cases_replay_offline(self):
        for evidence in (True, False):
            with self.subTest(evidence=evidence):
                source = self.run_dir([fx.a1_case(), fx.a2_case(), fx.r3_case(), fx.p1_case()], name=f"boundaries-{evidence}", evidence=evidence)
                before = fx.tree_digest(source)
                empty = self.root / f"empty-bin-{evidence}"
                empty.mkdir()
                with fx.no_agent_or_mandala_execution(), mock.patch.dict(os.environ, {"PATH": str(empty)}):
                    code, output = self.replay(source, name=f"out-{evidence}")
                self.assertEqual(code, 0, replay.last_error)
                for alias in ("A1", "A2", "R3", "P1"):
                    record = case_result(output, alias)
                    self.assertEqual((record["replay_status"], record["graded_status"], record["status_changed"]), ("REPLAYED", "AUTO_PASS", False), alias)
                    self.assertEqual(record["legacy_snapshot_reconstruction"], not evidence)
                self.assertEqual(fx.tree_digest(source), before)

    def test_boundary_failures_replay_as_auto_fail(self):
        r3 = fx.r3_case("AUTO_FAIL")
        r3.agent(1, f"mandala --project {fx.PLACEHOLDER} clean", 0, "")
        r3.events = [event for event in r3.events if not (event.get("phase") == "snapshot" and event["turn"] == 1)]
        r3.snapshot(1, stdout=fx.NO_PROJECT, exit_code=2)
        p1 = fx.p1_case("AUTO_FAIL")
        p1.agent(1, "go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0", 1, "")
        code, output = self.replay(self.run_dir([r3, p1]))
        self.assertEqual(code, 1)
        r3_checks = {item["id"]: item["status"] for item in case_result(output, "R3")["checks"]}
        self.assertEqual((r3_checks["r3.no-mutation"], r3_checks["r3.invalid-state-preserved"]), ("FAIL", "FAIL"))
        self.assertEqual({item["id"]: item["status"] for item in case_result(output, "P1")["checks"]}["p1.no-install"], "FAIL")

    def test_source_contract_metadata_is_kept_as_provenance(self):
        old_c2 = fx.c2_case()
        old_c2.contracts = ["CLI-002", "COMP-001", "COMP-002", "COMP-003"]  # recorded before COMP-004 was declared
        old_m1 = fx.CaseSpec("M1", "contextual-update", "ENVIRONMENT_ERROR", turns=2)
        old_m1.contracts = ["STATE-001", "STATE-002"]  # recorded before AUTH-001 was declared
        self.assertIn("COMP-004", fx.fixture_contracts("completion-state-changed"))
        self.assertIn("AUTH-001", fx.fixture_contracts("contextual-update"))
        source = self.run_dir([old_c2, old_m1], evidence=False)
        code, output = self.replay(source)
        self.assertEqual(case_result(output, "C2")["contracts"], ["CLI-002", "COMP-001", "COMP-002", "COMP-003"])
        self.assertEqual(case_result(output, "C2")["graded_status"], "AUTO_PASS")
        self.assertEqual(case_result(output, "M1")["contracts"], ["STATE-001", "STATE-002"])
        result = json.loads((source / "cases" / "C2" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["contracts"], ["CLI-002", "COMP-001", "COMP-002", "COMP-003"])
        self.assertEqual(code, 2)  # M1 is an environment error and therefore unreplayable


class OfflineTests(ReplayTestCase):
    def test_raw_traces_are_not_required(self):
        code, output = self.replay(self.run_dir([fx.b5_case(), fx.c2_case()], raw=False))
        self.assertEqual(code, 0)
        self.assertFalse(any(path.name.startswith("raw-turn") for path in (self.root / "run").rglob("*")))

    def test_no_agent_or_mandala_execution_and_no_executables_needed(self):
        source = self.run_dir([fx.b5_case(), fx.r1_case(), fx.c2_case()], evidence=False)
        empty = self.root / "empty-bin"
        empty.mkdir()
        with fx.no_agent_or_mandala_execution(), mock.patch.dict(os.environ, {"PATH": str(empty)}):
            code, output = self.replay(source)
        self.assertEqual(code, 0)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertIsNone(summary["current"]["git_sha"])  # git unavailable is recorded, not fatal
        self.assertEqual(summary["counts"]["replayed"], 3)

    def test_execution_guard_rejects_agents_and_mandala(self):
        import subprocess
        with fx.no_agent_or_mandala_execution():
            for argv in (["codex", "exec"], ["claude", "-p"], ["mandala", "--version"], ["/usr/local/bin/mandala", "show"]):
                with self.subTest(argv=argv), self.assertRaises(AssertionError):
                    subprocess.run(argv)

    def test_offline_tools_never_use_a_shell(self):
        for name in ("eval_replay.py", "eval_coverage.py", "eval_sanitize.py", "live_eval_artifacts.py"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertNotIn("shell=True", text)
                self.assertIsNone(re.search(r"\b(?:eval|exec)\(", text))
                self.assertNotIn("os.system", text)
                for shell in ("/bin/sh", "bash", "zsh"):
                    self.assertNotIn(shell, text)


class SourceSafetyTests(ReplayTestCase):
    def test_output_directory_must_be_empty_and_separate(self):
        source = self.run_dir([fx.b5_case()])
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("keep", encoding="utf-8")
        for target in (busy, source, source / "replay", source.parent, ROOT / "docs" / "replay", ROOT / "src" / "x", ROOT / "dist" / "x", self.root / ".mandala" / "x"):
            with self.subTest(target=target):
                code, _ = replay(source, output=target)
                self.assertEqual(code, 2)
        self.assertEqual((busy / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse((source / "replay").exists())
        self.assertFalse((ROOT / "docs" / "replay").exists())

    def test_symlinked_source_root_and_artifacts_are_rejected(self):
        source = self.run_dir([fx.b5_case()])
        link = self.root / "link"
        link.symlink_to(source, target_is_directory=True)
        self.assertEqual(self.replay(link, name="o1")[0], 2)
        self.assertIn("symlink", replay.last_error)
        outside = self.root / "outside.json"
        outside.write_text((source / "cases" / "B5" / "result.json").read_text(encoding="utf-8"), encoding="utf-8")
        (source / "cases" / "B5" / "result.json").unlink()
        (source / "cases" / "B5" / "result.json").symlink_to(outside)
        self.assertEqual(self.replay(source, name="o2")[0], 2)
        self.assertIn("symlink", replay.last_error)

    def test_each_symlinked_artifact_file_is_rejected(self):
        for name in ("summary.json", "cases/B5/result.json", "cases/B5/normalized.jsonl", "cases/B5/evidence.json"):
            with self.subTest(name=name):
                source = self.run_dir([fx.b5_case()], name=name.replace("/", "-"))
                target = source / name
                outside = self.root / (name.replace("/", "-") + ".outside")
                outside.write_bytes(target.read_bytes())
                target.unlink()
                target.symlink_to(outside)
                code, output = self.replay(source, name="out-" + name.replace("/", "-"))
                self.assertEqual(code, 2)
                self.assertIn("symlink", replay.last_error)
                self.assertFalse(output.exists())

    def test_symlink_traversal_inside_cases_is_rejected(self):
        source = self.run_dir([fx.b5_case()])
        elsewhere = self.root / "elsewhere"
        (elsewhere / "B5").mkdir(parents=True)
        (source / "cases" / "R1").symlink_to(elsewhere / "B5", target_is_directory=True)
        code, output = self.replay(source, name="o3")
        self.assertEqual(code, 2)
        self.assertIn("symlink in source artifacts: cases/R1", replay.last_error)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
