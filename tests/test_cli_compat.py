"""Offline tests for the explicit real-CLI compatibility checker; no Mandala CLI is run here."""
from pathlib import Path
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest

from scripts import check_cli_compat as compat


VALID = '{"schema_version":1,"goal":"Implement OAuth","cells":6,"groups":1,"required":{"open":2,"done":1,"na":0},"optional":{"open":1,"done":1,"na":0},"required_gaps":2}\n'
EMPTY = '{"schema_version":1,"goal":"g","cells":0,"groups":0,"required":{"open":0,"done":0,"na":0},"optional":{"open":0,"done":0,"na":0},"required_gaps":0}\n'


class StatusJsonParsingTests(unittest.TestCase):
    def test_documented_example_parses_and_exit_one_is_a_domain_result(self) -> None:
        data = compat.parse_status_json(VALID, 1)
        self.assertEqual((data["required_gaps"], data["cells"]), (2, 6))
        self.assertEqual(compat.parse_status_json(EMPTY, 0)["cells"], 0)

    def test_exit_codes_other_than_zero_or_one_are_errors(self) -> None:
        for code in (2, None, 3, -1):
            with self.subTest(code=code), self.assertRaisesRegex(compat.CompatError, "is an error"):
                compat.parse_status_json(VALID, code)

    def test_exit_code_must_agree_with_required_gaps(self) -> None:
        with self.assertRaisesRegex(compat.CompatError, "disagrees"):
            compat.parse_status_json(VALID, 0)
        with self.assertRaisesRegex(compat.CompatError, "disagrees"):
            compat.parse_status_json(EMPTY, 1)

    def test_every_required_field_is_needed(self) -> None:
        for field in compat.STATUS_FIELDS:
            data = json.loads(VALID)
            del data[field]
            with self.subTest(field=field), self.assertRaisesRegex(compat.CompatError, "fields/order"):
                compat.parse_status_json(json.dumps(data, separators=(",", ":")) + "\n", 1)
        for group in ("required", "optional"):
            for field in compat.COUNT_FIELDS:
                data = json.loads(VALID)
                del data[group][field]
                with self.subTest(group=group, field=field), self.assertRaisesRegex(compat.CompatError, "must have exactly"):
                    compat.parse_status_json(json.dumps(data, separators=(",", ":")) + "\n", 1)

    def test_field_order_and_extra_fields_are_rejected(self) -> None:
        data = json.loads(VALID)
        reordered = {"goal": data["goal"], **{k: v for k, v in data.items() if k != "goal"}}
        with self.assertRaisesRegex(compat.CompatError, "fields/order"):
            compat.parse_status_json(json.dumps(reordered, separators=(",", ":")) + "\n", 1)
        with self.assertRaisesRegex(compat.CompatError, "fields/order"):
            compat.parse_status_json(VALID[:-2] + ',"gaps":[]}\n', 1)
        with self.assertRaisesRegex(compat.CompatError, "duplicate JSON keys"):
            compat.parse_status_json(VALID[:-2] + ',"cells":6}\n', 1)

    def test_booleans_strings_and_negatives_are_not_counts(self) -> None:
        for before, after in (
            ('"schema_version":1', '"schema_version":true'),
            ('"schema_version":1', '"schema_version":"1"'),
            ('"schema_version":1', '"schema_version":2'),
            ('"groups":1', '"groups":true'),
            ('"groups":1', '"groups":1.0'),
            ('"na":0}', '"na":false}'),
            ('"done":1,"na":0},"required_gaps"', '"done":-1,"na":0},"required_gaps"'),
            ('"goal":"Implement OAuth"', '"goal":null'),
        ):
            with self.subTest(after=after), self.assertRaises(compat.CompatError):
                compat.parse_status_json(VALID.replace(before, after, 1), 1)

    def test_count_invariants_are_enforced(self) -> None:
        with self.assertRaisesRegex(compat.CompatError, "required_gaps must equal required.open"):
            compat.parse_status_json(VALID.replace('"required_gaps":2', '"required_gaps":1'), 1)
        with self.assertRaisesRegex(compat.CompatError, "cells must equal groups"):
            compat.parse_status_json(VALID.replace('"cells":6', '"cells":7'), 1)

    def test_output_must_be_compact_single_line_with_one_newline(self) -> None:
        pretty = json.dumps(json.loads(VALID), indent=2) + "\n"
        for text in (VALID[:-1], VALID + "\n", pretty, VALID.replace(",", ", ", 1), "\n"):
            with self.subTest(text=text[:30]), self.assertRaises(compat.CompatError):
                compat.parse_status_json(text, 1)
        with self.assertRaisesRegex(compat.CompatError, "not valid JSON"):
            compat.parse_status_json("{notjson}\n", 1)

    def test_special_goal_strings_keep_compactness(self) -> None:
        goal = 'say "hi" \\ 日本語 <tag>'
        text = VALID.replace('"Implement OAuth"', json.dumps(goal, ensure_ascii=False))
        self.assertEqual(compat.parse_status_json(text, 1)["goal"], goal)
        self.assertTrue(compat.is_compact('{"a":"x y\\" z"}'))
        self.assertFalse(compat.is_compact('{"a": 1}'))


class OtherJsonParsingTests(unittest.TestCase):
    def test_gaps_json(self) -> None:
        self.assertEqual(compat.parse_gaps_json('{"schema_version":1,"gaps":[]}\n', 0), [])
        gaps = compat.parse_gaps_json('{"schema_version":1,"gaps":[{"id":"a","required":true},{"id":"c","required":false}]}\n', 1)
        self.assertEqual([gap["id"] for gap in gaps], ["a", "c"])
        self.assertEqual(len(compat.parse_gaps_json('{"schema_version":1,"gaps":[{"id":"c","required":false}]}\n', 0)), 1)
        for text, code in (
            ('{"schema_version":1,"gaps":[]}\n', 2),
            ('{"schema_version":1,"gaps":[{"id":"a","required":true}]}\n', 0),
            ('{"schema_version":1,"gaps":[]}\n', 1),
            ('{"schema_version":true,"gaps":[]}\n', 0),
            ('{"schema_version":1,"gaps":[{"id":"a","required":1}]}\n', 1),
        ):
            with self.subTest(text=text, code=code), self.assertRaises(compat.CompatError):
                compat.parse_gaps_json(text, code)

    def test_show_json(self) -> None:
        data = compat.parse_show_json('{"schema_version":1,"goal":"g","cells":[{"id":"a","parent":"","status":"open","required":true}]}\n', 0)
        self.assertEqual(data["goal"], "g")
        for text in ('{"schema_version":1,"goal":"g","cells":[{"id":"b","parent":"","status":"open","required":true},{"id":"a","parent":"","status":"open","required":true}]}',
                     '{"schema_version":1,"goal":"g","cells":[{"id":"a","status":"open","required":true}]}'):
            with self.subTest(text=text), self.assertRaises(compat.CompatError):
                compat.parse_show_json(text, 0)
        with self.assertRaises(compat.CompatError):
            compat.parse_show_json('{"schema_version":1,"goal":"g","cells":[]}', 2)
        for text in ('{"schema_version":1,"goal":"g","cells":{}}', '{"schema_version":1,"goal":1,"cells":[]}',
                     '{"schema_version":1,"goal":"g","cells":[{"id":1,"parent":"","status":"open","required":true}]}'):
            with self.subTest(text=text), self.assertRaises(compat.CompatError):
                compat.parse_show_json(text, 0)


class CheckerEntryPointTests(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = compat.main(list(argv))
        return code, output.getvalue()

    def test_cases_are_c01_through_c14(self) -> None:
        cases = compat.Cases(compat.Runner("unused"), Path("."))
        self.assertEqual([case_id for case_id, _ in compat.case_methods(cases)], list(compat.CASE_IDS))
        self.assertEqual(compat.CASE_IDS, tuple(f"C{number:02d}" for number in range(1, 15)))

    def test_missing_cli_is_not_run_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            code, output = self.run_main("--mandala", str(Path(directory) / "absent-mandala"), "--report", str(report))
            self.assertEqual(code, 2)
            self.assertIn("NOT_RUN: 0 PASS, 0 FAIL, 14 SKIP", output)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "NOT_RUN")
            self.assertEqual({item["status"] for item in data["results"]}, {"SKIP"})

    @unittest.skipIf(os.name == "nt", "POSIX shell shim")
    def test_other_cli_version_is_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "mandala"
            shim.write_text("#!/bin/sh\necho 'mandala v0.3.0'\n", encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
            code, output = self.run_main("--mandala", str(shim))
            self.assertEqual(code, 2)
            self.assertIn("required 'mandala v0.4.0'", output)
            self.assertNotIn("PASS C", output)

    def test_required_version_matches_validator_baseline(self) -> None:
        from scripts import validate, eval_live
        self.assertEqual(compat.REQUIRED_VERSION, validate.VERSION_OUTPUT)
        self.assertEqual(eval_live.REQUIRED_CLI, validate.VERSION_OUTPUT)


def run_main(*argv: str) -> tuple:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = compat.main(list(argv))
    return code, output.getvalue()


def fingerprint(path: Path) -> tuple:
    info = os.lstat(str(path))
    content = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino, content)


class ReportSafetyTests(unittest.TestCase):
    """`--report` only ever creates a new regular file; nothing existing is overwritten."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.absent = str(self.root / "absent-mandala")  # missing CLI: fast NOT_RUN path that still writes a report

    def tearDown(self) -> None:
        self.directory.cleanup()

    def assert_rejected(self, report: Path, protected: Path = None) -> None:
        target = protected if protected is not None else report
        before = fingerprint(target) if os.path.lexists(str(target)) else None
        code, output = run_main("--mandala", self.absent, "--report", str(report))
        self.assertEqual(code, compat.REPORT_ERROR_EXIT, output)
        self.assertIn("REPORT_ERROR", output)
        self.assertNotIn("PASS:", output)
        if before is not None:
            self.assertEqual(fingerprint(target), before)
        else:
            self.assertFalse(os.path.lexists(str(target)))

    def test_new_report_file_is_created_with_existing_schema(self) -> None:
        report = self.root / "report.json"
        code, output = run_main("--mandala", self.absent, "--report", str(report))
        self.assertEqual(code, 2)
        self.assertIn("NOT_RUN", output)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["schema_version", "verdict", "required_cli", "mandala", "results", "commands"])
        self.assertEqual((data["schema_version"], data["verdict"], data["required_cli"]), (1, "NOT_RUN", "mandala v0.4.0"))
        self.assertEqual([item["case"] for item in data["results"]], list(compat.CASE_IDS))
        self.assertTrue(report.is_file() and not report.is_symlink())

    def test_relative_report_path_is_created_in_the_current_directory(self) -> None:
        previous = os.getcwd()
        os.chdir(str(self.root))
        try:
            code, _ = run_main("--mandala", self.absent, "--report", "relative-report.json")
        finally:
            os.chdir(previous)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.root / "relative-report.json").read_text(encoding="utf-8"))["verdict"], "NOT_RUN")

    def test_existing_report_is_never_overwritten(self) -> None:
        report = self.root / "report.json"
        report.write_text('{"keep": true}\n', encoding="utf-8")
        self.assert_rejected(report)
        self.assertEqual(report.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_mandala_state_and_protected_directories_are_rejected(self) -> None:
        project = self.root / "project"
        (project / ".mandala").mkdir(parents=True)
        state = project / ".mandala" / "state.json"
        state.write_text('{"schema_version":1,"goal":"g","cells":[]}\n', encoding="utf-8")
        (project / ".git").mkdir()
        self.assert_rejected(state)
        self.assert_rejected(project / ".mandala" / "new-report.json")
        self.assert_rejected(project / ".git" / "new-report.json")
        self.assert_rejected(project / ".git" / "config")
        relative = Path(os.path.relpath(str(project / ".mandala" / "rel.json")))
        self.assert_rejected(relative, project / ".mandala" / "rel.json")
        self.assert_rejected(project / "sub" / ".." / ".mandala" / "dotdot.json")
        self.assertEqual(sorted(path.name for path in (project / ".mandala").iterdir()), ["state.json"])
        self.assertEqual(list((project / ".git").iterdir()), [])

    def test_directory_and_non_regular_targets_are_rejected(self) -> None:
        directory = self.root / "a-directory"
        directory.mkdir()
        self.assert_rejected(directory)
        self.assertTrue(directory.is_dir())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_cannot_redirect_the_report(self) -> None:
        victim = self.root / "victim.json"
        victim.write_text("precious\n", encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(victim)
        self.assert_rejected(link, victim)
        self.assertTrue(link.is_symlink())
        dangling = self.root / "dangling.json"
        dangling.symlink_to(self.root / "would-be-created.json")
        self.assert_rejected(dangling)
        self.assertFalse((self.root / "would-be-created.json").exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_parent_directory_symlink_into_protected_area_is_rejected(self) -> None:
        project = self.root / "project"
        (project / ".mandala").mkdir(parents=True)
        (project / ".git").mkdir()
        alias = self.root / "innocent-dir"
        alias.symlink_to(project / ".mandala", target_is_directory=True)
        self.assert_rejected(alias / "report.json", project / ".mandala" / "report.json")
        git_alias = self.root / "also-innocent"
        git_alias.symlink_to(project / ".git", target_is_directory=True)
        self.assert_rejected(git_alias / "report.json", project / ".git" / "report.json")
        self.assertEqual(list((project / ".mandala").iterdir()), [])

    def test_missing_parent_directory_is_an_error_and_not_created(self) -> None:
        report = self.root / "missing" / "nested" / "report.json"
        self.assert_rejected(report)
        self.assertFalse((self.root / "missing").exists())

    def test_report_created_concurrently_is_not_overwritten_and_not_pass(self) -> None:
        report = self.root / "race.json"
        original = compat.Runner.run

        def racing_run(runner, args, cwd):  # the file appears after validation, before the final write
            if not report.exists():
                report.write_text("raced\n", encoding="utf-8")
            return original(runner, args, cwd)

        shim = self.root / "mandala"
        shim.write_text("#!/bin/sh\necho 'mandala v0.3.0'\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        compat.Runner.run = racing_run
        try:
            code, output = run_main("--mandala", str(shim), "--report", str(report))
        finally:
            compat.Runner.run = original
        self.assertEqual(code, compat.REPORT_ERROR_EXIT, output)
        self.assertIn("REPORT_ERROR", output)
        self.assertEqual(report.read_text(encoding="utf-8"), "raced\n")

    @unittest.skipIf(os.name == "nt", "POSIX shell shim")
    def test_report_failure_after_passing_cases_never_prints_pass(self) -> None:
        report = self.root / "late.json"
        shim = self.root / "mandala"
        shim.write_text("#!/bin/sh\necho 'mandala v0.4.0'\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

        def passing_cases(cases):  # every case passes; one of them makes the report path appear mid-run
            def make(case_id):
                def method():
                    if case_id == "C07":
                        report.write_text("raced\n", encoding="utf-8")
                    return "ok"
                return method
            return [(case_id, make(case_id)) for case_id in compat.CASE_IDS]

        original = compat.case_methods
        compat.case_methods = passing_cases
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = compat.main(["--mandala", str(shim), "--report", str(report)])
        finally:
            compat.case_methods = original
        self.assertEqual(code, compat.REPORT_ERROR_EXIT)
        self.assertFalse(any(line.startswith("PASS:") for line in stdout.getvalue().splitlines()), stdout.getvalue())
        self.assertIn("REPORT_ERROR", stdout.getvalue())
        self.assertEqual(report.read_text(encoding="utf-8"), "raced\n")


@unittest.skipIf(os.name == "nt", "POSIX shell shim")
class CliPathResolutionTests(unittest.TestCase):
    """The resolved CLI is made absolute once, so case working directories cannot lose it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.log = self.root / "invocations.log"
        self.previous_cwd = os.getcwd()
        self.previous_path = os.environ.get("PATH", "")

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        os.environ["PATH"] = self.previous_path
        self.directory.cleanup()

    def shim(self, relative: str, version: str = "mandala v0.4.0") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Answers --version; records its own path for every other call (cases then FAIL: it is not a real CLI).
        path.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo '{version}'; exit 0; fi\n"
                        f"printf '%s\\n' \"$0\" >> '{self.log}'\nexit 2\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def run_from_root(self, mandala: str) -> tuple:
        os.chdir(str(self.root))
        report = self.root / "report.json"
        code, output = run_main("--mandala", mandala, "--report", str(report))
        return code, output, json.loads(report.read_text(encoding="utf-8"))

    def assert_same_absolute_binary(self, expected: Path, code: int, output: str, data: dict) -> None:
        self.assertEqual(code, 1, output)  # the shim reached every case and failed them; not NOT_RUN, not PASS
        self.assertEqual(data["mandala"], str(expected))
        self.assertTrue(os.path.isabs(data["mandala"]))
        self.assertTrue(all(command["exit"] is not None for command in data["commands"]), "a case lost the CLI")
        calls = self.log.read_text(encoding="utf-8").split()
        self.assertGreaterEqual(len(calls), len(compat.CASE_IDS))
        self.assertEqual(set(calls), {str(expected)})

    def test_dot_slash_relative_path(self) -> None:
        expected = self.shim("mandala")
        self.assert_same_absolute_binary(expected, *self.run_from_root("./mandala"))

    def test_nested_relative_path(self) -> None:
        expected = self.shim("bin/mandala")
        self.assert_same_absolute_binary(expected, *self.run_from_root("./bin/mandala"))

    def test_absolute_path(self) -> None:
        expected = self.shim("abs/mandala")
        self.assert_same_absolute_binary(expected, *self.run_from_root(str(expected)))

    def test_path_lookup_by_name(self) -> None:
        expected = self.shim("onpath/mandala")
        os.environ["PATH"] = str(self.root / "onpath") + os.pathsep + self.previous_path
        self.assert_same_absolute_binary(expected, *self.run_from_root("mandala"))

    def test_relative_directory_on_path(self) -> None:
        expected = self.shim("relbin/mandala")
        os.environ["PATH"] = "relbin" + os.pathsep + self.previous_path
        self.assert_same_absolute_binary(expected, *self.run_from_root("mandala"))

    def test_missing_executable_stays_not_run(self) -> None:
        code, output, data = self.run_from_root("./no-such-mandala")
        self.assertEqual((code, data["verdict"]), (2, "NOT_RUN"))
        self.assertEqual({item["status"] for item in data["results"]}, {"SKIP"})

    def test_other_version_stays_not_run(self) -> None:
        self.shim("old/mandala", version="mandala v0.3.0")
        code, output, data = self.run_from_root("./old/mandala")
        self.assertEqual((code, data["verdict"]), (2, "NOT_RUN"))
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
