from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import eval_replay
from scripts import eval_sanitize
from scripts import live_eval_artifacts as artifacts
from tests import eval_artifact_fixtures as fx


ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-test-do-not-leak"
SESSION = "fake-session-12345"
MAC_PATH = "/Users/alice/private/project"
LINUX_PATH = "/home/alice/private/project"
LEAKS = (SECRET, SESSION, MAC_PATH, LINUX_PATH, "/Users/alice", "/home/alice")


def sensitive_run(run_dir):
    """A run whose excluded artifacts and excluded fields carry fake secrets and local paths."""
    b5 = fx.b5_case()
    b5.agent(1, f"cat {MAC_PATH}/.env # OPENAI_API_KEY={SECRET}", 0, f"OPENAI_API_KEY={SECRET}")
    b5.checks = [{"id": "b5.fresh-show", "contracts": ["STATE-001"], "status": "PASS", "evidence": f"agent ran `mandala --project {LINUX_PATH} show --json` with {SECRET}"}]
    b5.extra_files = {"final.txt": f"Session {SESSION} token {SECRET} in {MAC_PATH}\n", "stderr.txt": f"{LINUX_PATH}: {SECRET}\n",
                      "raw-turn1.jsonl": json.dumps({"type": "thread.started", "thread_id": SESSION, "secret": SECRET}) + "\n"}
    b5.result_overrides = {"session_id": SESSION, "skill_files_read": [f"{MAC_PATH}/.codex/skills/mandala/SKILL.md"]}
    env = fx.environment_error_case()
    env.error = {"category": "agent", "message": f"codex exited 1 in {LINUX_PATH} with key {SECRET} session {SESSION}"}
    env.result_overrides = {"session_id": SESSION}
    r1 = fx.r1_case()
    r1.manual_review = [{"contracts": ["CLEAN-003"], "note": f"check {MAC_PATH} and {SECRET}"}]
    fx.write_run(run_dir, [b5, env, r1], summary_overrides={
        "preflight": {"agent": "codex", "ok": True, "agent_version": "codex-cli 0.0.0", "mandala_cli_version": "mandala v0.4.0",
                      "checks": [{"check": "global-skill-isolation", "ok": True, "detail": f"user-level skill {MAC_PATH}/.codex/skills/mandala matches; {SECRET}"}]},
        "model_requested": SECRET, "agent_version": f"codex-cli 0.0.0 ({LINUX_PATH}/bin/codex)",
    })
    workdir = run_dir / "workdirs" / "B5" / ".mandala"
    workdir.mkdir(parents=True)
    (workdir / "state.json").write_text(json.dumps({"secret": SECRET}), encoding="utf-8")
    sessions = run_dir / "workdirs" / "B5" / ".codex"
    sessions.mkdir()
    (sessions / "session.jsonl").write_text(SESSION + SECRET, encoding="utf-8")


class SanitizerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "run" / "codex"
        sensitive_run(self.source)

    def tearDown(self):
        self.directory.cleanup()

    def sanitize(self, source=None, output=None):
        output = output or self.root / "share"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = eval_sanitize.main([str(source or self.source), "--output-dir", str(output)])
        self.last_error = errors.getvalue()
        return code, output

    def test_whitelisted_layout_only(self):
        code, output = self.sanitize()
        self.assertEqual(code, 0)
        files = sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())
        self.assertEqual(files, ["cases/B4/result.json", "cases/B5/result.json", "cases/R1/result.json", "coverage.json", "coverage.md", "manifest.json", "report.md", "summary.json"])
        for excluded in ("raw-turn", "normalized", "evidence", "final.txt", "stderr", "workdirs", "state.json", "session.jsonl"):
            self.assertFalse(any(excluded in name for name in files), excluded)

    def test_no_excluded_string_anywhere_in_output(self):
        _, output = self.sanitize()
        texts = fx.read_tree_text(output)
        for name, text in texts.items():
            for leak in LEAKS + (str(self.root), str(self.source)):
                self.assertNotIn(leak, text, f"{leak!r} leaked into {name}")

    def test_reduced_case_result(self):
        _, output = self.sanitize()
        b5 = json.loads((output / "cases" / "B5" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(set(b5), {"schema_version", "artifact_type", "part", "case", "fixture", "status", "manual_review_required", "manual_review", "contracts", "checks", "error"})
        self.assertEqual(b5["checks"], [{"id": "b5.fresh-show", "contracts": ["STATE-001"], "status": "PASS"}])
        self.assertEqual((b5["case"], b5["fixture"], b5["status"]), ("B5", "capacity-final-child", "AUTO_PASS"))
        self.assertEqual(b5["contracts"], ["CAP-001", "CAP-002", "STATE-001"])
        b4 = json.loads((output / "cases" / "B4" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(b4["error"], {"category": "agent"})
        r1 = json.loads((output / "cases" / "R1" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(r1["manual_review"], [{"contracts": ["CLEAN-003"]}])
        self.assertTrue(r1["manual_review_required"])
        for result in (b5, b4, r1):
            self.assertNotIn("session_id", result)
            self.assertNotIn("evidence", json.dumps(result["checks"]))

    def test_summary_and_manifest(self):
        _, output = self.sanitize()
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertNotIn("preflight", summary)
        self.assertNotIn("model_requested", summary)
        self.assertEqual(summary["agent_version"], "codex-cli 0.0.0 (<home>/private/project/bin/codex)")
        self.assertEqual(summary["mandala_cli_version"], "mandala v0.4.0")
        self.assertEqual([case["case"] for case in summary["cases"]], ["B5", "B4", "R1"])
        self.assertEqual(summary["cases"][1]["error"], {"category": "agent"})
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["schema_version"], manifest["artifact_type"], manifest["replayable"]), (1, "mandala-sanitized-export", False))
        self.assertEqual(manifest["source_kind"], "live")
        self.assertFalse(manifest["redaction_policy"]["secret_scanning"])
        self.assertEqual(manifest["source_structured_artifact_hashes"]["summary.json"], artifacts.sha256_file(self.source / "summary.json"))
        self.assertEqual(manifest["source_structured_artifact_hashes"]["cases/B5/result.json"], artifacts.sha256_file(self.source / "cases" / "B5" / "result.json"))
        self.assertTrue(all(not key.startswith("/") for key in manifest["source_structured_artifact_hashes"]))
        self.assertIn("does not prove", manifest["redaction_policy"]["notice"])
        report = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("Review the bundle before sharing", report)
        self.assertNotEqual(report, (self.source / "report.md").read_text(encoding="utf-8") if (self.source / "report.md").exists() else "")

    def test_no_secret_free_claims(self):
        _, output = self.sanitize()
        text = " ".join(fx.read_tree_text(output).values()).lower()
        for phrase in ("secret-free", "safe to publish", "guaranteed sanitized"):
            self.assertNotIn(phrase, text)
        for name in ("scripts/eval_sanitize.py", "README.md", "README.ja.md", "tests/README.md", "tests/README.ja.md"):
            content = (ROOT / name).read_text(encoding="utf-8").lower()
            for phrase in ("secret-free", "safe to publish automatically", "guaranteed sanitized"):
                self.assertNotIn(phrase, content, name)

    def test_known_paths_are_replaced_with_placeholders(self):
        locations = artifacts.known_locations(Path("/srv/runs/x"))
        self.assertEqual(artifacts.redact(f"{ROOT}/a /srv/runs/x/b {os.path.expanduser('~')}/c", locations).split(), ["<repo-root>/a", "<source-run>/b", "<home>/c"])
        self.assertEqual(artifacts.redact({"k": [f"{MAC_PATH} {LINUX_PATH}"]}, []), {"k": ["<home>/private/project <home>/private/project"]})

    def test_sanitizes_replay_output(self):
        replay_dir = self.root / "replay"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            eval_replay.main([str(self.source), "--output-dir", str(replay_dir)])
        code, output = self.sanitize(replay_dir, self.root / "share-replay")
        self.assertEqual(code, 0)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["source_kind"], manifest["replayable"]), ("replay", False))
        b4 = json.loads((output / "cases" / "B4" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((b4["replay_status"], b4["error"]), ("UNREPLAYABLE", {"category": "insufficient-evidence"}))
        for name, text in fx.read_tree_text(output).items():
            for leak in LEAKS:
                self.assertNotIn(leak, text, f"{leak!r} leaked into {name}")

    def test_sanitized_bundle_is_not_replayable(self):
        _, output = self.sanitize()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = eval_replay.main([str(output), "--output-dir", str(self.root / "nope")])
        self.assertEqual(code, 2)
        self.assertIn("unsupported source artifact_type", errors.getvalue())

    def test_source_directory_unchanged(self):
        before = fx.tree_digest(self.source)
        self.sanitize()
        self.assertEqual(fx.tree_digest(self.source), before)

    def test_output_safety(self):
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "keep").write_text("keep", encoding="utf-8")
        for target in (busy, self.source, self.source / "share", self.source.parent, ROOT / "docs" / "share", ROOT / "src" / "share"):
            with self.subTest(target=target):
                self.assertEqual(self.sanitize(output=target)[0], 2)
        self.assertEqual((busy / "keep").read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.source / "share").exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            eval_sanitize.main([str(self.source)])  # --output-dir is required

    def test_symlinked_source_is_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        self.assertEqual(self.sanitize(link, self.root / "o1")[0], 2)
        self.assertIn("symlink", self.last_error)
        (self.source / "cases" / "R1" / "final.txt").unlink()
        (self.source / "cases" / "R1" / "final.txt").symlink_to(self.root / "run" / "codex" / "summary.json")
        self.assertEqual(self.sanitize(output=self.root / "o2")[0], 2)
        self.assertFalse((self.root / "o2").exists())

    def test_no_external_execution(self):
        with fx.no_agent_or_mandala_execution():
            self.assertEqual(self.sanitize()[0], 0)

    def test_inside_repository_output_must_be_under_eval_live(self):
        fake_repo = self.root / "repo"
        (fake_repo / "docs").mkdir(parents=True)
        with mock.patch.object(artifacts, "ROOT", fake_repo):
            self.assertEqual(self.sanitize(output=fake_repo / "docs" / "share")[0], 2)
            self.assertIn("must be under .eval-live/", self.last_error)
            self.assertEqual(self.sanitize(output=fake_repo / ".eval-live")[0], 2)
            code, output = self.sanitize(output=fake_repo / ".eval-live" / "exports" / "share")
        self.assertEqual(code, 0)
        self.assertTrue((output / "manifest.json").is_file())
        self.assertFalse((fake_repo / "docs" / "share").exists())

if __name__ == "__main__":
    unittest.main()
