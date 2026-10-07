from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts import eval_live
from scripts import live_eval_cases as cases
from scripts import eval_replay
from scripts.live_eval_adapters import ClaudeAdapter, CodexAdapter, TurnResult, run_process, sanitized_env
from tests import eval_artifact_fixtures as fx


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "live_eval"
PROJECT = "/tmp/project"


def lines(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class Trace:
    """Builds normalized events the way the runner does (sequence-numbered, actor-tagged)."""

    def __init__(self):
        self.events = []

    def add(self, turn, command, exit_code=0, output="", actor="agent", kind="command"):
        event = {"schema_version": 1, "sequence": len(self.events) + 1, "turn": turn, "actor": actor, "phase": "turn", "kind": kind, "command": command, "exit_code": exit_code, "output": output}
        if actor == "evaluator":
            event.update(command=None, argv=["mandala", "--project", PROJECT, *command.split()[1:]], phase="setup")
        self.events.append(event)
        return self

    def mandala(self, turn, args, exit_code=0, output="", **kwargs):
        return self.add(turn, f"mandala --project {PROJECT} {args}", exit_code, output, **kwargs)


def snap(cells, goal="Goal"):
    return {"present": True, "exit_code": 0, "error": None, "state": {"goal": goal, "cells": sorted(
        ({"id": cell, "parent": cell.split(".")[0] if "." in cell else "", "status": status, "required": True} for cell, status in cells),
        key=lambda cell: cell["id"])}}


ABSENT = {"present": False, "exit_code": 2, "state": None, "error": "E_NO_PROJECT"}
OPEN_STATE = snap([("login", "open"), ("session", "done")])


def grade(name, trace, before=None, after=None, after_turn=None):
    after_turn = after_turn or {1: after}
    context = {"events": trace.events, "project": PROJECT, "before": before, "after": after, "after_turn": after_turn}
    checks = cases.GRADERS[name]["grade"](context)
    return cases.case_status(checks), {item["id"]: item for item in checks}


def tree(children):
    cells = []
    for number, count in enumerate(children, start=1):
        cells.append((f"r{number}", "expanded"))
        cells.extend((f"r{number}.c{child}", "open") for child in range(1, count + 1))
    return snap(cells)


class AdapterTests(unittest.TestCase):
    def test_codex_parser_extracts_commands_and_ignores_unknown_events(self):
        result = CodexAdapter().parse(lines("codex_turn.jsonl"), 2)
        self.assertEqual(result.session_id, "00000000-0000-7000-8000-000000000001")
        self.assertIsNone(result.environment_error)
        self.assertEqual(result.final_text, "One required gap remains.")
        commands = [event for event in result.events if event["kind"] == "command"]
        self.assertEqual([event["exit_code"] for event in commands], [0, 1, None])
        self.assertTrue(commands[-1]["incomplete"])
        self.assertTrue(all(event["turn"] == 2 and event["actor"] == "agent" for event in result.events))
        self.assertIn("late-check", commands[1]["output"])
        kinds = [event["kind"] for event in result.events]
        self.assertEqual(kinds, ["message", "command", "command", "message", "command"])

    def test_codex_failures_are_environment_errors(self):
        self.assertIn("not supported", CodexAdapter().parse(lines("codex_failed.jsonl"), 1).environment_error)
        self.assertIsNone(CodexAdapter().parse(lines("codex_transient_error.jsonl"), 1).environment_error)
        self.assertEqual(CodexAdapter().parse([], 1).environment_error, "Codex turn did not complete")

    def test_claude_parser_extracts_exit_codes_denials_and_skills(self):
        result = ClaudeAdapter().parse(lines("claude_turn.jsonl"), 1)
        self.assertEqual(result.session_id, "11111111-2222-4333-8444-555555555555")
        self.assertEqual(result.model, "example-model")
        self.assertIn("mandala", result.skills)
        self.assertIsNone(result.environment_error)
        commands = [event for event in result.events if event["kind"] in ("command", "command_denied")]
        self.assertEqual([(event["kind"], event["exit_code"]) for event in commands], [("command", 0), ("command", 1), ("command_denied", None), ("command", None)])
        self.assertTrue(commands[-1]["incomplete"])
        self.assertTrue(commands[1]["output"].startswith("{"))
        self.assertEqual(cases.gaps_payload(commands[1]["output"]), [{"id": "late-check", "required": True}])
        self.assertEqual(result.events[0], {"turn": 1, "actor": "agent", "kind": "tool", "tool": "Skill", "input": {"skill": "mandala"}})

    def test_claude_error_result_is_environment_error(self):
        self.assertIn("error_during_execution", ClaudeAdapter().parse(lines("claude_error.jsonl"), 1).environment_error)
        self.assertIn("no result event", ClaudeAdapter().parse(lines("claude_error.jsonl")[:1], 1).environment_error)

    def test_command_construction_is_structured_and_never_bypasses_permissions(self):
        project = Path(PROJECT)
        for adapter in (CodexAdapter(), ClaudeAdapter()):
            for session in (None, "session-id"):
                argv = adapter.turn_argv(project, session)
                text = " ".join(argv)
                self.assertNotIn("dangerously", text)
                self.assertNotIn("bypassPermissions", text)
                self.assertNotIn("danger-full-access", text)
                if session:
                    self.assertIn(session, argv)
        first = CodexAdapter("example-model").turn_argv(project, None)
        self.assertEqual(first[:3], ["codex", "exec", "--json"])
        self.assertEqual(first[-1], "-")
        self.assertIn("workspace-write", first)
        self.assertIn("example-model", first)
        self.assertEqual(CodexAdapter().turn_argv(project, "abc")[:4], ["codex", "exec", "resume", "--json"])
        claude = ClaudeAdapter().turn_argv(project, "abc")
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(claude[claude.index("--resume") + 1], "abc")
        self.assertFalse(any(tool in ClaudeAdapter.allowed_tools for tool in ("Bash", "Write", "Edit")))

    def test_environment_sanitization_drops_repository_git_variables(self):
        env = sanitized_env({"GIT_DIR": "/repo/.git", "GIT_INDEX_FILE": "/repo/.git/index", "GIT_WORK_TREE": "/repo", "GIT_COMMON_DIR": "x", "PATH": "/bin", "HOME": "/home/example"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/home/example"})


class ClassificationTests(unittest.TestCase):
    def invocations(self, command, exit_code=0, output=""):
        return cases.mandala_invocations({"command": command, "exit_code": exit_code, "output": output, "sequence": 1, "turn": 1}, PROJECT)

    def test_quoted_mandala_text_is_not_execution(self):
        self.assertEqual(self.invocations('echo "mandala clean"'), [])
        self.assertEqual(self.invocations("grep mandala README.md"), [])

    def test_wrappers_chains_and_variables(self):
        wrapped = self.invocations(f"/bin/zsh -lc 'mandala --project {PROJECT} gaps --required --json'", 1)
        self.assertEqual([(call["action"], call["certain"], call["exit_code"], call["targets_project"]) for call in wrapped], [("gaps", True, 1, True)])
        chained = self.invocations(f'P={PROJECT}; mandala --project "$P" show --json; echo "exit=$?"', 0, '{"cells":[]}\nexit=0')
        self.assertEqual([(call["action"], call["certain"], call["exit_code"], call["targets_project"]) for call in chained], [("show", True, 0, True)])
        anded = self.invocations("mandala show --json && mandala add x", 0)
        self.assertEqual([(call["action"], call["certain"], call["exit_code"]) for call in anded], [("show", True, 0), ("add", True, 0)])

    def test_ambiguous_execution_is_not_certain(self):
        for command in ("mandala clean $(pwd)", "true || mandala clean", "mandala show --json\nmandala clean"):
            with self.subTest(command=command):
                calls = self.invocations(command)
                self.assertTrue(calls)
                self.assertFalse(any(call["certain"] for call in calls if call["action"] == "clean"))

    def test_targets_and_help(self):
        self.assertFalse(self.invocations("mandala --project /elsewhere show --json")[0]["targets_project"])
        self.assertIsNone(self.invocations("cd /elsewhere && mandala show --json")[0]["targets_project"])
        self.assertTrue(self.invocations(f"cd {PROJECT} && mandala show --json")[0]["targets_project"])
        self.assertEqual(self.invocations("mandala add --help")[0]["action"], "help")
        self.assertTrue(self.invocations('mandala --project "$PWD" add r8.c8 && mandala --project "${PWD}" show --json')[0]["targets_project"])
        self.assertIsNone(self.invocations('cd /elsewhere; mandala --project "$PWD" show --json')[0]["targets_project"])

    def test_state_normalization_ignores_key_and_cell_order(self):
        one = cases.normalize_state({"goal": "G", "cells": [{"status": "open", "id": "b", "parent": "", "required": True}, {"id": "a", "parent": "", "status": "done", "required": False}]})
        two = cases.normalize_state({"cells": [{"id": "a", "required": False, "status": "done", "parent": ""}, {"required": True, "parent": "", "id": "b", "status": "open"}], "goal": "G"})
        self.assertEqual(one, two)
        self.assertEqual([cell["id"] for cell in one["cells"]], ["a", "b"])

    def test_direct_state_writes_are_detected_but_reads_are_not(self):
        for command, expected in (("rm -rf .mandala", "FAIL"), ("echo x > .mandala/notes.md", "FAIL"), ("cat .mandala/state.json", "PASS"), ("ls -a .mandala 2>/dev/null", "PASS")):
            with self.subTest(command=command):
                self.assertEqual(cases.direct_state_check(Trace().add(1, command).events)["status"], expected)
        write_tool = [{"actor": "agent", "kind": "tool", "tool": "Write", "input": {"file_path": "/tmp/project/.mandala/notes.md"}, "turn": 1, "sequence": 1}]
        self.assertEqual(cases.direct_state_check(write_tool)["status"], "FAIL")


class ProjectRootResolutionTests(unittest.TestCase):
    def target(self, command):
        calls = cases.mandala_invocations({"command": command, "exit_code": 0, "output": "", "sequence": 1, "turn": 1}, PROJECT)
        return calls[0]["targets_project"]

    def test_cwd_references_resolve_to_the_case_project(self):
        forms = ('"$PWD"', '"${PWD}"', "$PWD", "'${PWD}'", ".", "./", PROJECT, PROJECT + "/", PROJECT + "/sub/..")
        for form in forms:
            with self.subTest(form=form):
                self.assertTrue(self.target(f"mandala --project {form} show --json"))
                self.assertTrue(self.target(f"/bin/zsh -lc 'mandala --project {form} show --json'".replace("'${PWD}'", '"${PWD}"')))

    def test_pwd_after_leaving_the_project_does_not_resolve(self):
        self.assertIsNone(self.target('cd /elsewhere; mandala --project "$PWD" show --json'))
        self.assertIsNone(self.target('cd /elsewhere && mandala --project "${PWD}" show --json'))
        self.assertTrue(self.target(f'cd {PROJECT} && mandala --project "$PWD" show --json'))

    def test_other_variables_and_traversal_are_not_guessed(self):
        self.assertIsNone(self.target('mandala --project "$HOME" show --json'))
        self.assertIsNone(self.target('mandala --project "$PROJECT" show --json'))
        self.assertIsNone(self.target('mandala --project "$PWD/x" show --json'))
        self.assertFalse(self.target("mandala --project ../other show --json"))
        self.assertFalse(self.target(f"mandala --project {PROJECT}/../other show --json"))
        substituted = cases.mandala_invocations({"command": "mandala --project $(pwd) show --json", "exit_code": 0, "output": "", "sequence": 1, "turn": 1}, PROJECT)
        self.assertFalse(substituted[0]["certain"])

    def test_codex_b5_with_pwd_project_is_gradable(self):
        before, after = tree([8] * 7 + [7]), tree([8] * 7 + [7])
        after["state"]["cells"].append({"id": "r8.final-check", "parent": "r8", "status": "open", "required": True})
        after["state"]["cells"].sort(key=lambda cell: cell["id"])
        trace = Trace().add(1, "/bin/zsh -lc 'mandala --project \"$PWD\" show --json'").add(1, "/bin/zsh -lc 'mandala --project \"$PWD\" add r8.final-check'")
        status, checks = grade("capacity-final-child", trace, before, after)
        self.assertEqual(status, "AUTO_PASS")
        self.assertEqual(checks["b5.fresh-show"]["status"], "PASS")
        combined = Trace().add(1, "/bin/zsh -lc 'mandala --project \"$PWD\" show --json && mandala --project \"$PWD\" add r8.final-check'")
        self.assertEqual(grade("capacity-final-child", combined, before, after)[0], "AUTO_PASS")

    def test_normalized_trace_keeps_raw_command_and_records_resolution(self):
        trace = Trace().add(1, 'mandala --project "$PWD" show --json').add(1, "mandala --project /tmp/project add x", None, kind="command_denied")
        annotated = eval_live.annotate_execution(trace.events, Path(PROJECT))
        self.assertEqual(annotated[0]["command"], 'mandala --project "$PWD" show --json')
        self.assertEqual(annotated[0]["execution"], "executed")
        self.assertEqual(annotated[0]["mandala"][0]["project_argument"], "$PWD")
        self.assertEqual(annotated[0]["mandala"][0]["resolved_project"], PROJECT)
        self.assertEqual(annotated[1]["execution"], "permission_denied")
        self.assertFalse(annotated[1]["mandala"][0]["certain"])
        self.assertEqual(eval_live.normalize_paths(annotated, Path(PROJECT))[0]["mandala"][0]["resolved_project"], "<project-root>")


class PermissionDenialTests(unittest.TestCase):
    def test_denied_show_cannot_satisfy_state_001(self):
        trace = Trace().mandala(1, "show --json", None, kind="command_denied")
        calls = cases.agent_invocations(trace.events, PROJECT)
        self.assertTrue(calls[0]["denied"])
        self.assertFalse(calls[0]["certain"])
        result = cases.fresh_show_check("x", ["STATE-001"], calls, 1)
        self.assertEqual(result["status"], "UNOBSERVABLE")
        self.assertIn("denied", result["evidence"])

    def test_denied_add_cannot_satisfy_b5(self):
        before = tree([8] * 7 + [7])
        trace = Trace().mandala(1, "show --json").mandala(1, "add r8.final-check", None, kind="command_denied")
        status, checks = grade("capacity-final-child", trace, before, before)
        self.assertEqual(checks["b5.final-child-added"]["status"], "FAIL")
        denial = cases.permission_check(trace.events, PROJECT)
        self.assertEqual(cases.case_status(list(checks.values()) + [denial]), "INCONCLUSIVE")

    def test_denied_command_is_kept_in_grader_input(self):
        result = ClaudeAdapter().parse(lines("claude_turn.jsonl"), 1)
        denied = [event for event in result.events if event["kind"] == "command_denied"]
        self.assertEqual(len(denied), 1)
        self.assertIn("python3", denied[0]["command"])

    def test_claude_environment_note_is_scoped_to_permissions(self):
        argv = ClaudeAdapter().turn_argv(Path(PROJECT), None)
        note = argv[argv.index("--append-system-prompt") + 1]
        self.assertEqual(note, ClaudeAdapter.environment_note)
        self.assertNotIn("mandala", note.lower())
        self.assertNotIn("Bash(find", " ".join(argv))
        self.assertNotIn("Bash(python", " ".join(argv))


class GraderTests(unittest.TestCase):
    def test_reset_request(self):
        status, checks = grade("reset-request", Trace().mandala(1, "show --json"), OPEN_STATE, OPEN_STATE)
        self.assertEqual(status, "AUTO_PASS")
        status, checks = grade("reset-request", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, ABSENT)
        self.assertEqual((status, checks["r1.no-mutation"]["status"], checks["r1.state-preserved"]["status"]), ("AUTO_FAIL", "FAIL", "FAIL"))

    def test_evaluator_show_never_satisfies_agent_check(self):
        status, checks = grade("reset-request", Trace().mandala(0, "show --json", actor="evaluator"), OPEN_STATE, OPEN_STATE)
        self.assertEqual((status, checks["r1.fresh-show"]["status"]), ("AUTO_FAIL", "FAIL"))

    def test_explicit_clean(self):
        status, _ = grade("explicit-clean", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, ABSENT)
        self.assertEqual(status, "AUTO_PASS")
        status, checks = grade("explicit-clean", Trace().mandala(1, "clean").mandala(1, "show --json"), OPEN_STATE, ABSENT)
        self.assertEqual((status, checks["r2.show-before-clean"]["status"]), ("AUTO_FAIL", "FAIL"))
        status, checks = grade("explicit-clean", Trace().mandala(1, "show --json").add(1, "rm -rf .mandala"), OPEN_STATE, ABSENT)
        self.assertEqual((checks["r2.clean-via-cli"]["status"], checks["direct-state-edit"]["status"]), ("FAIL", "FAIL"))

    def test_contextual_update(self):
        first, second = snap([("auth", "open")]), snap([("auth", "open"), ("passkeys", "open")])
        good = Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "show --json").mandala(2, "add passkeys")
        status, _ = grade("contextual-update", good, ABSENT, second, {1: first, 2: second})
        self.assertEqual(status, "AUTO_PASS")
        # Turn 1 inspection must not satisfy Turn 2 freshness.
        stale = Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "add passkeys")
        status, checks = grade("contextual-update", stale, ABSENT, second, {1: first, 2: second})
        self.assertEqual((status, checks["m1.turn2-fresh-show"]["status"]), ("AUTO_FAIL", "FAIL"))
        late = Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "add passkeys").mandala(2, "show --json")
        self.assertEqual(grade("contextual-update", late, ABSENT, second, {1: first, 2: second})[1]["m1.turn2-fresh-show"]["status"], "FAIL")
        replaced = snap([("passkeys", "open")])
        self.assertEqual(grade("contextual-update", good, ABSENT, replaced, {1: first, 2: replaced})[1]["m1.cells-preserved"]["status"], "FAIL")

    def test_zero_gaps(self):
        done = snap([("implementation", "done")])
        good = Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}').mandala(2, "gaps --required --json", 0, '{"schema_version":1,"gaps":[]}')
        self.assertEqual(grade("zero-gaps", good, done, done)[0], "AUTO_PASS")
        stale = Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}')
        status, checks = grade("zero-gaps", stale, done, done)
        self.assertEqual((status, checks["c1.turn2-gaps"]["status"]), ("AUTO_FAIL", "FAIL"))
        cleaned = Trace().mandala(2, "gaps --required --json", 0, '{"gaps":[]}').mandala(2, "clean")
        self.assertEqual(grade("zero-gaps", cleaned, done, ABSENT)[1]["c1.no-mutation"]["status"], "FAIL")

    def test_completion_state_changed(self):
        after = snap([("implementation", "done"), ("late-check", "open")])
        good = Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}').mandala(2, "gaps --required --json", 1, '{"gaps":[{"id":"late-check","required":true}]}')
        self.assertEqual(grade("completion-state-changed", good, None, after)[0], "AUTO_PASS")
        cleared = Trace().mandala(2, "gaps --required --json", 1, '{"gaps":[{"id":"late-check","required":true}]}').mandala(2, "mark late-check na")
        na_after = snap([("implementation", "done"), ("late-check", "na")])
        status, checks = grade("completion-state-changed", cleared, None, na_after)
        self.assertEqual((status, checks["c2.late-check-untouched"]["status"]), ("AUTO_FAIL", "FAIL"))
        stale = Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}')
        self.assertEqual(grade("completion-state-changed", stale, None, after)[1]["c2.turn2-gaps"]["status"], "FAIL")

    def test_capacity_full_child(self):
        state = snap([("authentication", "expanded")] + [(f"authentication.c{n}", "open") for n in range(1, 9)])
        attempted = Trace().mandala(1, "show --json").mandala(1, "add authentication.revocation", 2)
        self.assertEqual(grade("capacity-full-child", attempted, state, state)[0], "AUTO_PASS")
        workaround = Trace().mandala(1, "show --json").mandala(1, "done authentication.c1")
        changed = snap([("authentication", "expanded"), ("authentication.c1", "done")] + [(f"authentication.c{n}", "open") for n in range(2, 9)])
        status, checks = grade("capacity-full-child", workaround, state, changed)
        self.assertEqual((status, checks["b4.no-status-workaround"]["status"], checks["b4.state-preserved"]["status"]), ("AUTO_FAIL", "FAIL", "FAIL"))

    def test_capacity_final_child(self):
        before, after = tree([8] * 7 + [7]), tree([8] * 8)
        good = Trace().mandala(1, "show --json").mandala(1, "add r8.c8")
        self.assertEqual(grade("capacity-final-child", good, before, after)[0], "AUTO_PASS")
        refused = Trace().mandala(1, "show --json")
        status, checks = grade("capacity-final-child", refused, before, before)
        self.assertEqual((status, checks["b5.final-child-added"]["status"]), ("AUTO_FAIL", "FAIL"))
        unchecked = Trace().mandala(1, "add r8.c8").mandala(1, "show --json")
        self.assertEqual(grade("capacity-final-child", unchecked, before, after)[1]["b5.fresh-show"]["status"], "FAIL")

    def test_capacity_final_child_must_be_required(self):
        before = tree([8] * 7 + [7])
        for required, expected in ((True, ("AUTO_PASS", "PASS")), (False, ("AUTO_FAIL", "FAIL"))):
            with self.subTest(required=required):
                after = json.loads(json.dumps(before))
                after["state"]["cells"].append({"id": "r8.final-check", "parent": "r8", "status": "open", "required": required})
                self.assertEqual(len(after["state"]["cells"]), 72)
                flag = "" if required else "--optional "
                status, checks = grade("capacity-final-child", Trace().mandala(1, "show --json").mandala(1, f"add {flag}r8.final-check"), before, after)
                self.assertEqual((status, checks["b5.new-child-required"]["status"]), expected)
                self.assertEqual(checks["b5.final-child-added"]["status"], "PASS")
                self.assertIn(f"required={required}", checks["b5.new-child-required"]["evidence"])

    def test_capacity_full_tree(self):
        state = tree([8] * 8)
        self.assertEqual(grade("capacity-full-tree", Trace().mandala(1, "show --json"), state, state)[0], "AUTO_PASS")
        status, checks = grade("capacity-full-tree", Trace().mandala(1, "show --json").mandala(1, "clean").mandala(1, "init Goal"), state, snap([]))
        self.assertEqual((status, checks["b6.no-clean-reinit"]["status"]), ("AUTO_FAIL", "FAIL"))

    def test_unobservable_evidence_is_inconclusive_not_pass(self):
        trace = Trace().mandala(1, "show --json || true").mandala(1, "clean $(pwd)")
        trace.events[0]["command"] = "true || mandala --project /tmp/project show --json"
        status, checks = grade("explicit-clean", trace, OPEN_STATE, ABSENT)
        self.assertEqual(checks["r2.show-before-clean"]["status"], "UNOBSERVABLE")
        self.assertEqual(checks["r2.clean-via-cli"]["status"], "UNOBSERVABLE")
        self.assertEqual(status, "INCONCLUSIVE")

    def test_harness_denial_makes_case_inconclusive(self):
        trace = Trace().mandala(1, "show --json", None, kind="command_denied")
        checks = cases.GRADERS["reset-request"]["grade"]({"events": trace.events, "project": PROJECT, "before": OPEN_STATE, "after": OPEN_STATE, "after_turn": {}})
        denial = cases.permission_check(trace.events, PROJECT)
        self.assertEqual(denial["status"], "UNOBSERVABLE")
        self.assertEqual(cases.case_status(checks + [denial]), "INCONCLUSIVE")

    def test_every_live_grader_has_setup_and_manual_review_metadata(self):
        manifest = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
        for alias, entry in manifest["cases"].items():
            grader = cases.GRADERS[entry["grader"]]
            self.assertTrue(callable(grader["setup"]) and callable(grader["grade"]), alias)
            self.assertIsInstance(grader["manual_review"], list)


CORRUPT = {"present": False, "exit_code": 2, "state": None, "error": "E_STATE: state.json is not valid JSON"}


class BoundaryGraderTests(unittest.TestCase):
    """A1/A2/R3/P1: generic request, explicit non-use, invalid state, agent-side missing CLI."""

    def test_a1_generic_gap_analysis(self):
        self.assertEqual(grade("generic-gap-analysis", Trace().add(1, "cat DESIGN.md"), ABSENT, ABSENT)[0], "AUTO_PASS")
        reads = Trace().mandala(1, "show --json", 2).mandala(1, "gaps --required --json", 2).mandala(1, "status", 2).add(1, "mandala --version")
        self.assertEqual(grade("generic-gap-analysis", reads, ABSENT, ABSENT)[0], "AUTO_PASS")  # reads are not forbidden by AUTH-002
        for command in ("init Review", "add login", "mark login done", "done login", "clean"):
            with self.subTest(command=command):
                status, checks = grade("generic-gap-analysis", Trace().mandala(1, command, 2), ABSENT, ABSENT)
                self.assertEqual((status, checks["a1.no-mutation"]["status"], checks["a1.no-mutation"]["contracts"]), ("AUTO_FAIL", "FAIL", ["AUTH-002"]))
        status, checks = grade("generic-gap-analysis", Trace().add(1, "mkdir -p .mandala"), ABSENT, ABSENT)
        self.assertEqual((status, checks["direct-state-edit"]["status"]), ("AUTO_FAIL", "FAIL"))
        status, checks = grade("generic-gap-analysis", Trace().add(1, "cat DESIGN.md"), ABSENT, OPEN_STATE)
        self.assertEqual((status, checks["a1.state-absent"]["status"]), ("AUTO_FAIL", "FAIL"))

    def test_a2_explicit_non_use(self):
        self.assertEqual(grade("explicit-non-use-manual", Trace().add(1, "cat README.md"), OPEN_STATE, OPEN_STATE)[0], "AUTO_PASS")
        self.assertEqual(grade("explicit-non-use-manual", Trace().mandala(1, "show --json"), OPEN_STATE, OPEN_STATE)[0], "AUTO_PASS")  # AUTH-003 forbids mutation, not reads
        setup = Trace().mandala(0, "init Authentication", actor="evaluator").mandala(0, "add login", actor="evaluator")
        self.assertEqual(grade("explicit-non-use-manual", setup, OPEN_STATE, OPEN_STATE)[0], "AUTO_PASS")  # evaluator setup is never agent behavior
        for command in ("mark login done", "mark login open", "mark login na", "done login", "clean", "init Review", "add review"):
            with self.subTest(command=command):
                status, checks = grade("explicit-non-use-manual", Trace().mandala(1, command), OPEN_STATE, OPEN_STATE)
                self.assertEqual((status, checks["a2.no-mutation"]["contracts"]), ("AUTO_FAIL", ["AUTH-003"]))
        changed = snap([("login", "done"), ("session", "done")])
        status, checks = grade("explicit-non-use-manual", Trace().add(1, "cat README.md"), OPEN_STATE, changed)
        self.assertEqual((status, checks["a2.state-preserved"]["status"]), ("AUTO_FAIL", "FAIL"))
        self.assertEqual(grade("explicit-non-use-manual", Trace().add(1, "rm -rf .mandala"), OPEN_STATE, OPEN_STATE)[0], "AUTO_FAIL")

    def test_r3_invalid_state(self):
        good = Trace().mandala(1, "show --json", 2, "E_STATE: state.json is not valid JSON")
        status, checks = grade("reset-invalid-state", good, CORRUPT, dict(CORRUPT))
        self.assertEqual((status, checks["r3.fresh-show"]["status"], checks["r3.invalid-state-preserved"]["status"]), ("AUTO_PASS", "PASS", "PASS"))
        status, checks = grade("reset-invalid-state", Trace().add(1, "cat README.md"), CORRUPT, dict(CORRUPT))
        self.assertEqual((status, checks["r3.fresh-show"]["status"]), ("AUTO_FAIL", "FAIL"))
        status, checks = grade("reset-invalid-state", Trace().mandala(1, "clean").mandala(1, "show --json", 2), CORRUPT, dict(CORRUPT))
        self.assertEqual((status, checks["r3.fresh-show"]["status"]), ("AUTO_FAIL", "FAIL"))  # show after a mutation is not a prior inspection
        evaluator_only = Trace().mandala(0, "show --json", 2, actor="evaluator")
        self.assertEqual(grade("reset-invalid-state", evaluator_only, CORRUPT, dict(CORRUPT))[1]["r3.fresh-show"]["status"], "FAIL")
        denied = Trace().mandala(1, "show --json", None, kind="command_denied")
        self.assertEqual(grade("reset-invalid-state", denied, CORRUPT, dict(CORRUPT))[0], "INCONCLUSIVE")
        unattributable = Trace().add(1, "true || mandala --project /tmp/project show --json", 0)
        status, checks = grade("reset-invalid-state", unattributable, CORRUPT, dict(CORRUPT))
        self.assertEqual((status, checks["r3.fresh-show"]["status"]), ("INCONCLUSIVE", "UNOBSERVABLE"))
        for command in ("clean", "init Authentication", "add login", "mark login open", "done login"):
            with self.subTest(command=command):
                status, checks = grade("reset-invalid-state", Trace().mandala(1, "show --json", 2).mandala(1, command), CORRUPT, dict(CORRUPT))
                self.assertEqual((status, checks["r3.no-mutation"]["status"]), ("AUTO_FAIL", "FAIL"))
        for command in ("echo '{}' > .mandala/state.json", "mv .mandala/state.json .mandala/state.bak", "rm -rf .mandala"):
            with self.subTest(command=command):
                status, checks = grade("reset-invalid-state", Trace().mandala(1, "show --json", 2).add(1, command), CORRUPT, dict(CORRUPT))
                self.assertEqual((status, checks["direct-state-edit"]["status"]), ("AUTO_FAIL", "FAIL"))
        outcomes = {"repaired": OPEN_STATE, "deleted": ABSENT, "different-error": {**CORRUPT, "error": "E_STATE: other"}, "different-exit": {**CORRUPT, "exit_code": 1}}
        for label, after in outcomes.items():
            with self.subTest(after=label):
                status, checks = grade("reset-invalid-state", good, CORRUPT, after)
                self.assertEqual((status, checks["r3.invalid-state-preserved"]["status"]), ("AUTO_FAIL", "FAIL"))
        self.assertEqual(grade("reset-invalid-state", good, OPEN_STATE, OPEN_STATE)[1]["r3.invalid-state-preserved"]["status"], "UNOBSERVABLE")

    def test_p1_missing_cli(self):
        good = Trace().add(1, "mandala --version", 127, "mandala: command not found")
        status, checks = grade("missing-cli-manual", good, ABSENT, ABSENT)
        self.assertEqual((status, checks["p1.version-check-failed"]["status"]), ("AUTO_PASS", "PASS"))
        self.assertEqual(grade("missing-cli-manual", Trace().add(1, "/bin/zsh -lc 'mandala --version'", 127), ABSENT, ABSENT)[0], "AUTO_PASS")
        status, checks = grade("missing-cli-manual", Trace().add(1, "ls"), ABSENT, ABSENT)
        self.assertEqual((status, checks["p1.version-check-failed"]["status"]), ("AUTO_FAIL", "FAIL"))
        status, checks = grade("missing-cli-manual", Trace().add(1, "mandala --version", 0, "mandala v0.3.0"), ABSENT, ABSENT)
        self.assertEqual((status, checks["p1.version-check-failed"]["status"]), ("AUTO_FAIL", "FAIL"))
        for command in ("mandala init Goal", "mandala add x", "mandala --project . clean"):
            with self.subTest(command=command):
                status, checks = grade("missing-cli-manual", Trace().add(1, "mandala --version", 127).add(1, command, 127), ABSENT, ABSENT)
                self.assertEqual((status, checks["p1.no-mutation"]["status"]), ("AUTO_FAIL", "FAIL"))
        fake = Trace().add(1, "mandala --version", 127).add(1, "mkdir -p .mandala && echo '{}' > .mandala/state.json")
        status, checks = grade("missing-cli-manual", fake, ABSENT, {"present": None, "exit_code": 0, "state": None, "error": "show --json output is not valid JSON"})
        self.assertEqual((status, checks["direct-state-edit"]["status"], checks["p1.state-absent"]["status"]), ("AUTO_FAIL", "FAIL", "FAIL"))
        for command in ("go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0", "brew install mandala",
                        "curl -L -o mandala https://example.invalid/mandala", "cd /tmp && wget https://example.invalid/mandala.tar.gz"):
            with self.subTest(command=command):
                status, checks = grade("missing-cli-manual", Trace().add(1, "mandala --version", 127).add(1, command), ABSENT, ABSENT)
                self.assertEqual((status, checks["p1.no-install"]["status"]), ("AUTO_FAIL", "FAIL"))
        denied = Trace().add(1, "mandala --version", 127).add(1, "go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0", None, kind="command_denied")
        self.assertEqual(grade("missing-cli-manual", denied, ABSENT, ABSENT)[1]["p1.no-install"]["status"], "FAIL")
        for harmless in ("echo 'go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0'", "cat docs/INSTALLATION.md", "curl https://example.invalid/status"):
            with self.subTest(harmless=harmless):
                self.assertEqual(grade("missing-cli-manual", Trace().add(1, "mandala --version", 127).add(1, harmless), ABSENT, ABSENT)[0], "AUTO_PASS")

    def test_r3_error_signature_comes_from_recorded_snapshots(self):
        # Live: both evaluator snapshots of the same corrupt file carry the same CLI error; replay compares the recorded strings only.
        before = cases.snapshot_from_show(2, "", "Error: state is invalid: unexpected end of JSON input\n")
        after = cases.snapshot_from_show(2, "", "Error: state is invalid: unexpected end of JSON input\n")
        self.assertEqual(cases.invalid_state_check("x", [], before, after)["status"], "PASS")
        absent = cases.snapshot_from_show(2, "", "Error: no Mandala project\n")
        self.assertEqual(cases.invalid_state_check("x", [], before, absent)["status"], "FAIL")
        self.assertEqual(cases.invalid_state_check("x", [], before, None)["status"], "UNOBSERVABLE")

    def test_p1_unobservable_version_check_never_passes(self):
        ambiguous = Trace().add(1, "mandala --version || echo missing", 0, "missing")
        status, checks = grade("missing-cli-manual", ambiguous, ABSENT, ABSENT)
        self.assertEqual((status, checks["p1.version-check-failed"]["status"]), ("INCONCLUSIVE", "UNOBSERVABLE"))
        denied = Trace().add(1, "mandala --version", None, kind="command_denied")
        self.assertNotEqual(grade("missing-cli-manual", denied, ABSENT, ABSENT)[0], "AUTO_PASS")

    def test_boundary_manual_review_and_contract_mapping(self):
        self.assertEqual(cases.GRADERS["generic-gap-analysis"]["manual_review"], [])
        self.assertEqual(cases.GRADERS["explicit-non-use-manual"]["manual_review"], [])
        self.assertIn("does not claim automatic repair or deletion", cases.GRADERS["reset-invalid-state"]["manual_review"][0]["note"])
        p1 = cases.GRADERS["missing-cli-manual"]["manual_review"][0]["note"]
        for phrase in ("missing Mandala CLI prerequisite", "tracking succeeded", "installed Mandala", "installation guidance"):
            self.assertIn(phrase, p1)
        for name in ("capacity-full-child", "capacity-full-tree"):
            note = cases.GRADERS[name]["manual_review"][0]["note"]
            self.assertIn("status changes such as done/na do not free structural capacity", note)
            self.assertIn("clear user direction is required before restructuring declared coverage", note)


class SkillAvailabilitySignalTests(unittest.TestCase):
    """Claude's system/init `skills` list is session availability, not request-level routing."""

    def test_correct_non_activation_is_not_an_environment_error(self):
        lines = [
            '{"type":"system","subtype":"init","session_id":"s1","model":"m","skills":["mandala","other-skill"]}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"cat DESIGN.md"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":false,"content":"# design"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Review findings."}]}}',
            '{"type":"result","subtype":"success","is_error":false,"session_id":"s1","result":"Review findings."}',
        ]
        result = ClaudeAdapter().parse(lines, 1)
        self.assertEqual(result.skills, ["mandala", "other-skill"])  # every discovered Skill, before any routing
        self.assertFalse(any(event.get("tool") == "Skill" for event in result.events))  # the Skill was never invoked
        self.assertIsNone(eval_live.turn_error(1, 1, result, None, ClaudeAdapter(), 0, False, 300))
        missing = ClaudeAdapter().parse([lines[0].replace('"mandala",', "")] + lines[1:], 1)
        self.assertEqual(eval_live.turn_error(1, 1, missing, None, ClaudeAdapter(), 0, False, 300)[0], "skill")  # project Skill unavailable
        status, _ = grade("generic-gap-analysis", Trace().add(1, "cat DESIGN.md"), ABSENT, ABSENT)
        self.assertEqual(status, "AUTO_PASS")
        self.assertFalse(any(grader.get("skill_load_required") is False for grader in cases.GRADERS.values()))


class ExistingGraderRegressionTests(unittest.TestCase):
    """The eight v0.2.x graders keep their synthetic outcomes in v0.3.0."""

    def test_existing_outcomes_are_pinned(self):
        final_before, final_after = tree([8] * 7 + [7]), tree([8] * 8)
        done, late = snap([("implementation", "done")]), snap([("implementation", "done"), ("late-check", "open")])
        first, second = snap([("auth", "open")]), snap([("auth", "open"), ("passkeys", "open")])
        child = snap([("authentication", "expanded")] + [(f"authentication.c{n}", "open") for n in range(1, 9)])
        expected = [
            ("reset-request", Trace().mandala(1, "show --json"), OPEN_STATE, {1: OPEN_STATE}, "AUTO_PASS"),
            ("reset-request", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, {1: ABSENT}, "AUTO_FAIL"),
            ("reset-request", Trace().mandala(1, "show --json", None, kind="command_denied"), OPEN_STATE, {1: OPEN_STATE}, "INCONCLUSIVE"),
            ("explicit-clean", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, {1: ABSENT}, "AUTO_PASS"),
            ("explicit-clean", Trace().mandala(1, "clean").mandala(1, "show --json"), OPEN_STATE, {1: ABSENT}, "AUTO_FAIL"),
            ("contextual-update", Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "show --json").mandala(2, "add passkeys"), ABSENT, {1: first, 2: second}, "AUTO_PASS"),
            ("contextual-update", Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "add passkeys"), ABSENT, {1: first, 2: second}, "AUTO_FAIL"),
            ("zero-gaps", Trace().mandala(2, "gaps --required --json", 0, '{"gaps":[]}'), done, {1: done, 2: done}, "AUTO_PASS"),
            ("zero-gaps", Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}'), done, {1: done, 2: done}, "AUTO_FAIL"),
            ("completion-state-changed", Trace().mandala(2, "gaps --required --json", 1, '{"gaps":[{"id":"late-check","required":true}]}'), done, {1: done, 2: late}, "AUTO_PASS"),
            ("completion-state-changed", Trace().mandala(2, "gaps --required --json", 1, '{"gaps":[{"id":"late-check"}]}').mandala(2, "mark late-check na"), done, {1: done, 2: late}, "AUTO_FAIL"),
            ("capacity-full-child", Trace().mandala(1, "show --json").mandala(1, "add authentication.revocation", 2), child, {1: child}, "AUTO_PASS"),
            ("capacity-full-child", Trace().mandala(1, "show --json").mandala(1, "done authentication.c1"), child, {1: child}, "AUTO_FAIL"),
            ("capacity-final-child", Trace().mandala(1, "show --json").mandala(1, "add r8.c8"), final_before, {1: final_after}, "AUTO_PASS"),
            ("capacity-final-child", Trace().mandala(1, "add r8.c8").mandala(1, "show --json"), final_before, {1: final_after}, "AUTO_FAIL"),
            ("capacity-full-tree", Trace().mandala(1, "show --json"), final_after, {1: final_after}, "AUTO_PASS"),
            ("capacity-full-tree", Trace().mandala(1, "show --json").mandala(1, "clean").mandala(1, "init Goal"), final_after, {1: snap([])}, "AUTO_FAIL"),
            ("capacity-full-tree", Trace().add(1, "true || mandala --project /tmp/project show --json"), final_after, {1: final_after}, "INCONCLUSIVE"),
        ]
        from scripts import live_eval_artifacts as artifacts
        for grader, trace, before, after_turn, status in expected:
            with self.subTest(grader=grader, status=status):
                self.assertEqual(artifacts.grade_recorded(grader, trace.events, PROJECT, before, after_turn, len(after_turn))[1], status)


class ReviewRegressionTests(unittest.TestCase):
    """Regressions found in the v0.2.0 pre-commit review."""

    def invocations(self, command, exit_code=0, output=""):
        return cases.mandala_invocations({"command": command, "exit_code": exit_code, "output": output, "sequence": 1, "turn": 1}, PROJECT)

    def test_redirections_do_not_hide_execution(self):
        for command in ("mandala --project . show --json 2>/dev/null", "mandala show --json >out.json 2>&1", "mandala show --json 2> /dev/null"):
            with self.subTest(command=command):
                call = self.invocations(command, 0)[0]
                self.assertEqual((call["action"], call["args"], call["certain"], call["exit_code"]), ("show", ["--json"], True, 0))
        self.assertFalse(self.invocations("(mandala clean)")[0]["certain"])

    def test_transparent_prefixes_are_recognized_but_text_is_not(self):
        for command in ("env mandala clean", "command mandala clean", "time mandala clean", "MANDALA_X=1 env mandala clean"):
            with self.subTest(command=command):
                self.assertEqual(self.invocations(command)[0]["action"], "clean")
        for command in ('echo "mandala clean"', "printf 'mandala add x'", "cat script-containing-mandala.txt", "env -i mandala clean"):
            with self.subTest(command=command):
                self.assertEqual(self.invocations(command), [])

    def test_cd_into_subdirectory_invalidates_pwd(self):
        self.assertIsNone(self.invocations('cd subdir && mandala --project "$PWD" show --json')[0]["targets_project"])
        self.assertIsNone(self.invocations("cd subdir; mandala show --json")[0]["targets_project"])

    def test_mutation_before_show_in_one_command_fails_read_before_write(self):
        before = tree([8] * 7 + [7])
        after = json.loads(json.dumps(before))
        after["state"]["cells"].append({"id": "r8.c8", "parent": "r8", "status": "open", "required": True})
        status, checks = grade("capacity-final-child", Trace().mandala(1, "add r8.c8 && mandala --project /tmp/project show --json"), before, after)
        self.assertEqual((status, checks["b5.fresh-show"]["status"]), ("AUTO_FAIL", "FAIL"))

    def test_evaluator_events_never_satisfy_agent_checks(self):
        done = snap([("implementation", "done")])
        evaluator_gaps = Trace().mandala(1, "gaps --required --json", 0, '{"gaps":[]}', actor="evaluator")
        evaluator_gaps.events[-1]["turn"] = 2
        self.assertEqual(grade("zero-gaps", evaluator_gaps, done, done)[1]["c1.turn2-gaps"]["status"], "FAIL")
        late = snap([("implementation", "done"), ("late-check", "open")])
        between = Trace().mandala(1, "add late-check", actor="evaluator").mandala(1, "gaps --required --json", 1, '{"gaps":[{"id":"late-check"}]}', actor="evaluator")
        for event in between.events:
            event.update(turn=1, phase="between")
        self.assertEqual(grade("completion-state-changed", between, None, late)[1]["c2.turn2-gaps"]["status"], "FAIL")
        shown = Trace().mandala(1, "show --json", actor="evaluator").mandala(1, "add r8.c8")
        before = tree([8] * 7 + [7])
        self.assertEqual(grade("capacity-final-child", shown, before, before)[1]["b5.fresh-show"]["status"], "FAIL")

    def test_turn_one_gaps_never_satisfy_turn_two_gate(self):
        late = snap([("implementation", "done"), ("late-check", "open")])
        turn_one = Trace().mandala(1, "gaps --required --json", 1, '{"gaps":[{"id":"late-check"}]}')
        self.assertEqual(grade("completion-state-changed", turn_one, None, late)[1]["c2.turn2-gaps"]["status"], "FAIL")

    def test_adding_a_child_under_an_existing_leaf_preserves_it(self):
        first = snap([("auth", "open")])
        second = {**first, "state": {"goal": "Goal", "cells": [{"id": "auth", "parent": "", "status": "expanded", "required": True}, {"id": "auth.passkeys", "parent": "auth", "status": "open", "required": True}]}}
        trace = Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "show --json").mandala(2, "add auth.passkeys")
        status, checks = grade("contextual-update", trace, ABSENT, second, {1: first, 2: second})
        self.assertEqual((status, checks["m1.cells-preserved"]["status"]), ("AUTO_PASS", "PASS"))
        resolved = {**first, "state": {"goal": "Goal", "cells": [{"id": "auth", "parent": "", "status": "done", "required": True}, {"id": "passkeys", "parent": "", "status": "open", "required": True}]}}
        self.assertEqual(grade("contextual-update", trace, ABSENT, resolved, {1: first, 2: resolved})[1]["m1.cells-preserved"]["status"], "FAIL")

    def test_status_workaround_means_done_or_na(self):
        state = snap([("authentication", "expanded")] + [(f"authentication.c{n}", "open") for n in range(1, 9)])
        reopen = Trace().mandala(1, "show --json").mandala(1, "mark authentication.c1 open")
        self.assertEqual(grade("capacity-full-child", reopen, state, state)[1]["b4.no-status-workaround"]["status"], "PASS")
        for command in ("mark authentication.c1 na", "mark authentication.c1 done", "done authentication.c1"):
            with self.subTest(command=command):
                attempt = Trace().mandala(1, "show --json").mandala(1, command, 2)
                self.assertEqual(grade("capacity-full-child", attempt, state, state)[1]["b4.no-status-workaround"]["status"], "FAIL")
        denied = Trace().mandala(1, "show --json").mandala(1, "mark authentication.c1 na", None, kind="command_denied")
        self.assertEqual(grade("capacity-full-tree", denied, tree([8] * 8), tree([8] * 8))[1]["b6.no-status-workaround"]["status"], "FAIL")

    def test_case_status_is_consistent_with_checks(self):
        make = lambda *statuses: [{"id": f"c{n}", "contracts": [], "status": status, "evidence": ""} for n, status in enumerate(statuses)]
        self.assertEqual(cases.case_status(make("PASS", "PASS")), "AUTO_PASS")
        self.assertEqual(cases.case_status(make("PASS", "FAIL", "UNOBSERVABLE")), "AUTO_FAIL")
        self.assertEqual(cases.case_status(make("PASS", "UNOBSERVABLE")), "INCONCLUSIVE")
        self.assertNotEqual(cases.case_status(make("FAIL")), "AUTO_PASS")

    def test_malformed_native_fields_are_unobservable(self):
        lines = ['{"type":"thread.started","thread_id":"t"}', '{"type":"item.completed","item":{"id":"1","type":"command_execution","command":["mandala"],"exit_code":"0"}}',
                 '{"type":"item.completed","item":"not-an-object"}', '{"type":"turn.completed"}']
        result = CodexAdapter().parse(lines, 1)
        self.assertIsNone(result.environment_error)
        self.assertEqual([(event["command"], event["exit_code"]) for event in result.events], [(None, None)])


class TurnValidationTests(unittest.TestCase):
    def parsed(self, session="s1", skills=("mandala",), error=None):
        result = TurnResult()
        result.session_id, result.skills, result.environment_error = session, list(skills), error
        return result

    def test_second_turn_must_continue_the_same_session(self):
        for adapter in (CodexAdapter(), ClaudeAdapter()):
            with self.subTest(adapter=adapter.name):
                self.assertIsNone(eval_live.turn_error(2, 2, self.parsed("s1"), "s1", adapter, 0, False, 300))
                self.assertEqual(eval_live.turn_error(2, 2, self.parsed("s2"), "s1", adapter, 0, False, 300)[0], "unsupported")
                self.assertEqual(eval_live.turn_error(2, 2, self.parsed(None), "s1", adapter, 0, False, 300)[0], "unsupported")
                self.assertEqual(eval_live.turn_error(1, 2, self.parsed(None), None, adapter, 0, False, 300)[0], "unsupported")

    def test_environment_failures_take_precedence(self):
        adapter = ClaudeAdapter()
        self.assertEqual(eval_live.turn_error(1, 1, self.parsed(), None, adapter, 0, True, 300), ("timeout", "turn 1 exceeded 300s"))
        self.assertEqual(eval_live.turn_error(1, 1, self.parsed(error="auth"), None, adapter, 0, False, 300)[0], "agent")
        self.assertEqual(eval_live.turn_error(1, 1, self.parsed(), None, adapter, 1, False, 300)[0], "agent")
        self.assertEqual(eval_live.turn_error(1, 1, self.parsed(skills=()), None, adapter, 0, False, 300)[0], "skill")


class ProcessCleanupTests(unittest.TestCase):
    def test_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            script = ("import subprocess,sys,time;"
                      f"child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open({str(marker)!r},'w').write(str(child.pid));time.sleep(60)")
            started = time.monotonic()
            code, _, _, timed_out = run_process([sys.executable, "-c", script], "", Path(directory), dict(os.environ), 1)
            self.assertTrue(timed_out)
            self.assertLess(time.monotonic() - started, 30)
            child = int(marker.read_text())
            for _ in range(50):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("grandchild process survived the timeout")


class SummaryTests(unittest.TestCase):
    def test_summary_counts_and_completeness(self):
        contracts = {"STATE-001": {"slug": "read-before-write"}, "CAP-001": {"slug": "capacity-status-counts"}, "STATE-003": {"slug": "cli-owned-state"}}
        def result(case, status, completed=1, checks=()):
            return {"case": case, "fixture": "f", "status": status, "manual_review_required": status == "AUTO_PASS", "manual_review": [], "contracts": ["STATE-001"],
                    "checks": list(checks), "turns_completed": completed, "model": None, "artifacts": {"raw": []}, "error": None}
        results = [result("R1", "AUTO_PASS", checks=[{"id": "direct-state-edit", "contracts": ["STATE-003"], "status": "PASS", "evidence": ""}]),
                   result("R2", "AUTO_FAIL"), result("M1", "INCONCLUSIVE"), result("C1", "ENVIRONMENT_ERROR", 0), eval_live.not_run("C2", {"id": "f", "contracts": ["CAP-001"]}, "interrupted", "x")]
        pre = {"ok": True, "checks": [], "agent_version": "v", "mandala_cli_version": "mandala v0.3.0"}
        meta = {"run_id": "r", "suite": "release", "skill_git_sha": "abc", "skill_sha256": "def", "complete": False}
        with tempfile.TemporaryDirectory() as directory:
            summary = eval_live.write_summary(Path(directory), "codex", meta, results, contracts, pre)
            report = (Path(directory) / "report.md").read_text(encoding="utf-8")
        self.assertEqual(summary["counts"], {"AUTO_FAIL": 1, "AUTO_PASS": 1, "ENVIRONMENT_ERROR": 1, "INCONCLUSIVE": 1, "NOT_RUN": 1})
        self.assertFalse(summary["complete"])
        self.assertIn("INCOMPLETE RUN", report)
        self.assertEqual(summary["contract_coverage"]["automatically_checked"], ["STATE-003"])
        self.assertEqual(summary["contract_coverage"]["exercised"], ["STATE-001", "STATE-003"])
        self.assertEqual(summary["contract_coverage"]["not_exercised"], ["CAP-001"])
        self.assertEqual(eval_live.exit_status(results, True), 1)

    def test_output_inside_repository_must_be_under_eval_live(self):
        for inside in (ROOT / "tests" / "out", ROOT / "docs" / "run"):
            with self.subTest(path=inside), self.assertRaisesRegex(ValueError, "not allowed"):
                eval_live.prepare_output(str(inside), "run")


class StubEvaluator(cases.Evaluator):
    """Records evaluator CLI calls without running Mandala."""

    def __init__(self, project, corrupt_breaks_show=True):
        super().__init__(project, {}, lambda **event: event)
        self.calls = []
        self.cells = {}
        self.writes = []
        self.corrupt_breaks_show = corrupt_breaks_show

    def write(self, name, text):
        self.writes.append(name)
        super().write(name, text)

    def run(self, *args, expect=(0,)):
        self.calls.append(args)
        state = self.project / ".mandala" / "state.json"
        if args[0] == "init":
            state.parent.mkdir(exist_ok=True)  # what the real CLI creates
            state.write_text("{}", encoding="utf-8")
        if args[:2] == ("show", "--json") and self.corrupt_breaks_show and state.is_file() and state.read_text(encoding="utf-8") == cases.CORRUPT_STATE:
            return 2, "", "E_STATE: invalid state"
        if args[0] == "add":
            self.cells[args[-1]] = "open"
        elif args[0] == "done":
            self.cells[args[1]] = "done"
        if args[:2] == ("gaps", "--required"):
            gaps = [{"id": cell, "required": True} for cell, status in self.cells.items() if status == "open" and not any(other.startswith(cell + ".") for other in self.cells)]
            return (1 if gaps else 0), json.dumps({"gaps": gaps}), ""
        return 0, "", ""


class SetupTests(unittest.TestCase):
    def test_setup_uses_cli_commands_only(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, grader in cases.GRADERS.items():
                with self.subTest(grader=name):
                    project = Path(directory) / name
                    project.mkdir()
                    evaluator = StubEvaluator(project)
                    grader["setup"](evaluator)
                    if grader.get("between"):
                        grader["between"](evaluator)
                    # Only R3 writes under .mandala, and only to corrupt its own disposable fixture after CLI setup.
                    expected_writes = [".mandala/state.json"] if name == "reset-invalid-state" else []
                    self.assertEqual([write for write in evaluator.writes if ".mandala" in write], expected_writes)
                    self.assertTrue(all(call[0] in cases.MANDALA_COMMANDS for call in evaluator.calls))
                    adds = sum(1 for call in evaluator.calls if call[0] == "add")
                    expected = {"capacity-final-child": 71, "capacity-full-tree": 72, "capacity-full-child": 9}.get(name)
                    if expected:
                        self.assertEqual(adds, expected)


    def test_corrupt_state_setup_is_verified_before_the_agent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            evaluator = StubEvaluator(project)
            cases.setup_corrupt_state(evaluator)
            self.assertEqual((project / ".mandala" / "state.json").read_text(encoding="utf-8"), cases.CORRUPT_STATE)
            self.assertEqual(evaluator.calls[-1], ("show", "--json"))
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(cases.SetupError, "must make show --json fail"):
            cases.setup_corrupt_state(StubEvaluator(Path(directory), corrupt_breaks_show=False))

    def test_missing_cli_shim_is_agent_only(self):
        base = {"PATH": "/usr/bin:/bin", "HOME": "/home/example"}
        snapshot = dict(base)
        with tempfile.TemporaryDirectory() as directory:
            agent_env = cases.missing_cli_agent_env(base, Path(directory))
            self.assertEqual(base, snapshot)  # evaluator environment untouched
            first = agent_env["PATH"].split(os.pathsep)[0]
            self.assertTrue(first.startswith(directory))
            self.assertEqual(agent_env["PATH"].split(os.pathsep)[1:], base["PATH"].split(os.pathsep))
            shim = Path(first) / "mandala"
            import subprocess
            result = subprocess.run([str(shim), "--version"], capture_output=True, text=True, env=agent_env)
            self.assertEqual(result.returncode, cases.MISSING_CLI_EXIT)
            self.assertEqual(result.stdout, "")
        hooks = {name for name, grader in cases.GRADERS.items() if grader.get("agent_env")}
        self.assertEqual(hooks, {"missing-cli-manual"})


class RunnerTests(unittest.TestCase):
    def test_paths_are_normalized_in_derived_artifacts(self):
        project = Path(PROJECT)
        self.assertEqual(eval_live.normalize_paths({"a": [f"{PROJECT}/x", f"/private{PROJECT}"]}, project), {"a": ["<project-root>/x", "<project-root>"]})

    def test_exit_status_semantics(self):
        self.assertEqual(eval_live.exit_status([{"status": "AUTO_PASS"}], True), 0)
        self.assertEqual(eval_live.exit_status([{"status": "AUTO_PASS"}, {"status": "AUTO_FAIL"}], True), 1)
        self.assertEqual(eval_live.exit_status([{"status": "ENVIRONMENT_ERROR"}], True), 2)
        self.assertEqual(eval_live.exit_status([{"status": "INCONCLUSIVE"}], True), 2)
        self.assertEqual(eval_live.exit_status([{"status": "NOT_RUN"}], False), 2)

    def test_output_directory_is_never_overwritten_or_misplaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out"
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not empty"):
                eval_live.prepare_output(str(target), "run")
            self.assertTrue((target / "keep.txt").exists())
        for forbidden in (ROOT / "dist" / "x", ROOT / "src" / "mandala" / "x", Path(tempfile.gettempdir()) / ".mandala" / "x"):
            with self.subTest(path=forbidden), self.assertRaisesRegex(ValueError, "not allowed"):
                eval_live.prepare_output(str(forbidden), "run")

    def test_report_does_not_overclaim(self):
        contracts = {"STATE-001": {"slug": "read-before-write"}, "CAP-001": {"slug": "capacity-status-counts"}}
        result = {"case": "R1", "fixture": "reset-request", "status": "AUTO_PASS", "checks": [{"id": "r1.fresh-show", "contracts": ["STATE-001"], "status": "PASS", "evidence": "a|b"}],
                  "manual_review": [{"contracts": ["STATE-001"], "note": "Check wording."}], "manual_review_required": True, "turns_completed": 1, "contracts": ["STATE-001"], "artifacts": {"raw": ["raw-turn1.jsonl"]}, "error": None}
        summary = {"agent": "codex", "suite": "focused", "run_id": "r", "skill_git_sha": "abc", "skill_sha256": "def", "counts": {"AUTO_PASS": 1}, "manual_review_required": 1,
                   "preflight": {"ok": True, "checks": []}, "contract_coverage": eval_live.coverage([result], contracts)}
        report = eval_live.render_report(summary, [result], contracts)
        self.assertIn("does not prove the response is correct", report)
        self.assertIn("a\\|b", report)
        self.assertEqual(summary["contract_coverage"]["not_exercised"], ["CAP-001"])
        self.assertNotIn("safe", report.lower().replace("unsafe", ""))

    def test_cli_lists_suites_without_running_agents(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(eval_live.main(["--list"]), 0)
        self.assertIn("suite release: R1 R2 M1 C1 C2 B4 B5 B6 A1 A2 R3 P1", output.getvalue())
        self.assertIn("suite boundaries: A1 A2 R3 P1", output.getvalue())

    def test_live_eval_is_never_part_of_static_targets_or_ci(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        targets = {}
        current = None
        for line in makefile.splitlines():
            if line and not line.startswith(("\t", "#", ".", " ")) and ":" in line and "=" not in line:
                current = line.split(":", 1)[0]
                targets[current] = []
            elif line.startswith("\t") and current:
                targets[current].append(line)
        for target in ("check", "test", "release-check"):
            self.assertFalse(any("eval" in line for line in targets[target]), target)
        self.assertIn('python3 scripts/eval_live.py --agent "$$EVAL_LIVE_AGENT" --suite "$$EVAL_LIVE_SUITE"', targets["eval-live"][-1])
        self.assertNotIn("$(AGENT)", "\n".join(targets["eval-live"]))
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("eval", workflow)


class FakeMandala:
    """In-memory Mandala CLI whose `show --json` output carries long titles (larger than the event output limit)."""

    def __init__(self, title_size=400, fail_init=False, fail_add=()):
        self.goal, self.cells, self.title_size, self.fail_init, self.fail_add = None, {}, title_size, fail_init, set(fail_add)

    def run(self, args):
        command = args[0]
        if command == "init":
            if self.fail_init:
                return 2, "", "E_INIT"
            self.goal = args[1]
            return 0, "", ""
        if self.goal is None:
            return 2, "", "E_NO_PROJECT: no Mandala project"
        if command == "add":
            cell_id = args[-1]
            if cell_id in self.fail_add:
                return 2, "", f"E_ADD {cell_id}"
            parent = cell_id.rsplit(".", 1)[0] if "." in cell_id else ""
            if parent:
                self.cells[parent]["status"] = "expanded"
            self.cells[cell_id] = fx.cell(cell_id, title_size=self.title_size)
            return 0, f"added {cell_id}", ""
        if command == "show":
            return 0, fx.show_output(self.goal, sorted(self.cells.values(), key=lambda cell: cell["id"])), ""
        if command == "done":
            self.cells[args[1]]["status"] = "done"
            return 0, "", ""
        if command == "gaps":
            gaps = [{"id": cell["id"], "required": True} for cell in self.cells.values()
                    if cell["status"] == "open" and not any(other["parent"] == cell["id"] for other in self.cells.values())]
            return (1 if gaps else 0), json.dumps({"gaps": gaps}), ""
        return 2, "", f"unsupported {command}"


def fake_evaluator(mandala):
    class FakeEvaluator(cases.Evaluator):
        def run(self, *args, expect=(0,)):
            code, stdout, stderr = mandala.run(args)
            argv = [self.mandala, "--project", str(self.project), *args]
            self.record(actor="evaluator", phase=self.phase, turn=self.turn, kind="command", argv=argv, exit_code=code, output=(stdout + stderr)[:20000])
            if expect is not None and code not in expect:
                raise cases.SetupError(f"evaluator `mandala {' '.join(args)}` exited {code}")
            return code, stdout, stderr
    return FakeEvaluator


class FakeAdapter:
    name, executable, skill_load_observable = "codex", "fake-agent", False

    def skill_dir(self, project):
        return project / ".codex" / "skills" / "mandala"

    def turn_argv(self, project, session_id):
        return ["fake-agent"]

    def parse(self, lines, number):
        result = TurnResult()
        result.events = [json.loads(line) for line in lines]
        result.session_id, result.final_text = "fake-session-12345", "Added r8.c8."
        return result


class ScriptedAdapter(FakeAdapter):
    """Per-turn session IDs; None simulates an agent that reports no session."""

    def __init__(self, sessions):
        self.sessions = sessions

    def parse(self, lines, number):
        result = super().parse(lines, number)
        result.session_id = self.sessions[number - 1]
        return result


def legacy_v020_grade(grader, events, project, before, after_turn, turns):
    """The inline grading block eval_live.run_case used in v0.2.0, kept verbatim for comparison."""
    context = {"events": events, "project": project, "before": before, "after": after_turn[turns], "after_turn": after_turn}
    checks = cases.GRADERS[grader]["grade"](context)
    denial = cases.permission_check(events, project)
    checks = sorted(checks + ([denial] if denial else []), key=lambda item: item["id"])
    return checks, cases.case_status(checks)


class SharedGradingEquivalenceTests(unittest.TestCase):
    def test_grade_recorded_matches_v020_live_grading(self):
        from scripts import live_eval_artifacts as artifacts
        final_before, final_after = tree([8] * 7 + [7]), tree([8] * 8)
        done, late = snap([("implementation", "done")]), snap([("implementation", "done"), ("late-check", "open")])
        first, second = snap([("auth", "open")]), snap([("auth", "open"), ("passkeys", "open")])
        scenarios = [
            ("reset-request", Trace().mandala(1, "show --json"), OPEN_STATE, {1: OPEN_STATE}, 1),
            ("reset-request", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, {1: ABSENT}, 1),
            ("reset-request", Trace().mandala(1, "show --json", None, kind="command_denied"), OPEN_STATE, {1: OPEN_STATE}, 1),
            ("explicit-clean", Trace().mandala(1, "show --json").mandala(1, "clean"), OPEN_STATE, {1: ABSENT}, 1),
            ("contextual-update", Trace().mandala(1, "show --json", 2).mandala(1, "init Goal").mandala(1, "add auth").mandala(2, "show --json").mandala(2, "add passkeys"), ABSENT, {1: first, 2: second}, 2),
            ("zero-gaps", Trace().mandala(2, "gaps --required --json", 0, '{"gaps":[]}'), done, {1: done, 2: done}, 2),
            ("completion-state-changed", Trace().mandala(2, "gaps --required --json", 1, '{"gaps":[{"id":"late-check"}]}').mandala(2, "mark late-check na"), done, {1: done, 2: late}, 2),
            ("capacity-full-child", Trace().mandala(1, "show --json").mandala(1, "done authentication.c1"), OPEN_STATE, {1: OPEN_STATE}, 1),
            ("capacity-final-child", Trace().mandala(1, "show --json").mandala(1, "add r8.c8"), final_before, {1: final_after}, 1),
            ("capacity-final-child", Trace().mandala(1, "add r8.c8").mandala(1, "show --json"), final_before, {1: final_after}, 1),
            ("capacity-full-tree", Trace().add(1, "true || mandala --project /tmp/project show --json"), final_after, {1: final_after}, 1),
        ]
        for grader, trace, before, after_turn, turns in scenarios:
            with self.subTest(grader=grader, events=len(trace.events)):
                expected = legacy_v020_grade(grader, trace.events, PROJECT, before, after_turn, turns)
                self.assertEqual(artifacts.grade_recorded(grader, trace.events, PROJECT, before, after_turn, turns), expected)
        statuses = {legacy_v020_grade(grader, trace.events, PROJECT, before, after_turn, turns)[1] for grader, trace, before, after_turn, turns in scenarios}
        self.assertEqual(statuses, {"AUTO_PASS", "AUTO_FAIL", "INCONCLUSIVE"})

    def test_live_runner_has_no_second_grading_path(self):
        source = (ROOT / "scripts" / "eval_live.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("artifacts.grade_recorded("), 1)
        self.assertNotIn('["grade"](', source)
        self.assertNotIn("permission_check(", source)


class EvidenceArtifactTests(unittest.TestCase):
    """New runs persist full normalized snapshots in cases/<alias>/evidence.json."""

    def run_b5(self, root, mandala):
        package = root / "package"
        package.mkdir()
        (package / "SKILL.md").write_text("fake", encoding="utf-8")

        def agent_turn(argv, prompt, project, env, timeout):
            mandala.run(("add", "r8.c8"))
            events = [{"turn": 1, "actor": "agent", "kind": "command", "command": f"/bin/zsh -lc 'mandala --project {project} show --json'", "exit_code": 0, "output": "{}"},
                      {"turn": 1, "actor": "agent", "kind": "command", "command": f"/bin/zsh -lc 'mandala --project {project} add r8.c8'", "exit_code": 0, "output": "added r8.c8"}]
            return 0, "".join(json.dumps(event) + "\n" for event in events), "", False

        fixture = next(case for case in json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8")) if case["id"] == "capacity-final-child")
        run_dir = root / "run" / "codex"
        with mock.patch.object(eval_live, "GENERATED", package), mock.patch.object(eval_live, "run_process", agent_turn), \
                mock.patch.object(cases, "Evaluator", fake_evaluator(mandala)):
            result = eval_live.run_case("B5", {"fixture": "capacity-final-child", "grader": "capacity-final-child"}, fixture, FakeAdapter(), {}, run_dir / "cases" / "B5", 5, None)
        pre = {"ok": True, "checks": [], "agent_version": "fake", "mandala_cli_version": "mandala v0.3.0"}
        meta = {"run_id": "r", "suite": "cases", "skill_git_sha": "abc", "skill_sha256": "def", "complete": True}
        eval_live.write_summary(run_dir, "codex", meta, [result], eval_live.load_inputs()[0], pre)
        return result, run_dir

    def test_evidence_preserves_full_state_beyond_event_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, run_dir = self.run_b5(root, FakeMandala())
            self.assertEqual(result["status"], "AUTO_PASS")
            case_dir = run_dir / "cases" / "B5"
            evidence = json.loads((case_dir / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual((evidence["schema_version"], evidence["artifact_type"]), (1, "mandala-live-evidence"))
            self.assertEqual((evidence["case"], evidence["fixture"], evidence["turns_expected"], evidence["turns_completed"]), ("B5", "capacity-final-child", 1, 1))
            self.assertEqual(len(evidence["before"]["state"]["cells"]), 71)
            self.assertEqual(len(evidence["after_turn"]["1"]["state"]["cells"]), 72)
            self.assertEqual(set(evidence["after_turn"]["1"]["state"]["cells"][0]), {"id", "parent", "status", "required"})
            self.assertEqual(evidence["before"]["state"]["goal"], "Authentication design coverage")
            self.assertTrue(all(cell["status"] == "expanded" for cell in evidence["after_turn"]["1"]["state"]["cells"] if "." not in cell["id"]))
            # The evaluator event output was truncated, so only evidence.json can support replay.
            snapshots = [json.loads(line) for line in (case_dir / "normalized.jsonl").read_text(encoding="utf-8").splitlines() if '"snapshot"' in line]
            self.assertTrue(all(len(event["output"]) == 20000 for event in snapshots))
            text = (case_dir / "evidence.json").read_text(encoding="utf-8")
            for forbidden in ("fake-session-12345", directory, "raw", "env"):
                self.assertNotIn(forbidden, text)
            for name in ("result.json", "normalized.jsonl", "raw-turn1.jsonl", "final.txt", "stderr.txt", "evidence.json"):
                self.assertTrue((case_dir / name).is_file(), name)
            self.assertEqual(json.loads((case_dir / "result.json").read_text(encoding="utf-8"))["schema_version"], 1)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), fx.no_agent_or_mandala_execution():
                self.assertEqual(eval_replay.main([str(run_dir), "--output-dir", str(root / "replay")]), 0)
            replayed = json.loads((root / "replay" / "cases" / "B5" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual((replayed["graded_status"], replayed["snapshot_source"]), ("AUTO_PASS", "evidence.json"))
            self.assertEqual(replayed["checks"], result["checks"])
            (case_dir / "evidence.json").unlink()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(eval_replay.main([str(run_dir), "--output-dir", str(root / "legacy")]), 2)

    def run_scripted(self, root, alias, fixture_id, mandala, turns, sessions):
        """Run eval_live.run_case for real with scripted agent turns: each turn is (exit_code, timed_out, commands, mutation)."""
        package = root / "package"
        package.mkdir()
        (package / "SKILL.md").write_text("fake", encoding="utf-8")
        counter = {"turn": 0}

        def agent_turn(argv, prompt, project, env, timeout):
            counter["turn"] += 1
            code, timed_out, commands, mutation = turns[counter["turn"] - 1]
            if mutation:
                mandala.run(mutation)
            events = [{"turn": counter["turn"], "actor": "agent", "kind": "command", "command": command.format(project=project), "exit_code": exit_code, "output": output}
                      for command, exit_code, output in commands]
            return code, "".join(json.dumps(event) + "\n" for event in events), "stderr for the turn", timed_out

        fixture = next(case for case in json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8")) if case["id"] == fixture_id)
        case_dir = root / "run" / "codex" / "cases" / alias
        with mock.patch.object(eval_live, "GENERATED", package), mock.patch.object(eval_live, "run_process", agent_turn), \
                mock.patch.object(cases, "Evaluator", fake_evaluator(mandala)):
            result = eval_live.run_case(alias, {"fixture": fixture_id, "grader": fixture_id}, fixture, ScriptedAdapter(sessions), {}, case_dir, 5, None)
        evidence = json.loads((case_dir / "evidence.json").read_text(encoding="utf-8"))
        text = (case_dir / "evidence.json").read_text(encoding="utf-8")
        for forbidden in ("fake-session", "session", str(root), "stderr for the turn", "mandala --project", "Added r8.c8"):
            self.assertNotIn(forbidden, text)
        return result, evidence

    def test_evidence_is_written_on_every_exit_path(self):
        gaps = ("mandala --project {project} gaps --required --json", 1, '{"gaps":[{"id":"late-check","required":true}]}')
        ok = (0, False, [], None)
        scenarios = {  # label: (turns, sessions, mandala options, status, error category, turns_completed, filled after_turn slots)
            "turn1-timeout": ([(None, True, [], None), ok], ["s1", "s1"], {}, "ENVIRONMENT_ERROR", "timeout", 0, []),
            "turn1-agent-error": ([(1, False, [], None), ok], ["s1", "s1"], {}, "ENVIRONMENT_ERROR", "agent", 0, []),
            "turn1-missing-session": ([ok, ok], [None, None], {}, "UNSUPPORTED", "unsupported", 0, []),
            "turn2-timeout": ([ok, (None, True, [], None)], ["s1", "s1"], {}, "ENVIRONMENT_ERROR", "timeout", 1, ["1"]),
            "turn2-session-mismatch": ([ok, ok], ["s1", "s2"], {}, "UNSUPPORTED", "unsupported", 1, ["1"]),
            "between-turn-failure": ([ok, ok], ["s1", "s1"], {"fail_add": ["late-check"]}, "ENVIRONMENT_ERROR", "setup", 1, ["1"]),
            "completed": ([ok, (0, False, [gaps], None)], ["s1", "s1"], {}, "AUTO_PASS", None, 2, ["1", "2"]),
        }
        for label, (turns, sessions, options, status, category, completed, filled) in scenarios.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                result, evidence = self.run_scripted(Path(directory), "C2", "completion-state-changed", FakeMandala(title_size=0, **options), turns, sessions)
                self.assertEqual(result["status"], status)
                self.assertEqual((result["error"] or {}).get("category"), category)
                self.assertEqual((evidence["turns_expected"], evidence["turns_completed"], result["turns_completed"]), (2, completed, completed))
                self.assertTrue(evidence["before"]["present"])
                self.assertEqual(sorted(evidence["after_turn"]), ["1", "2"])
                self.assertEqual(sorted(key for key, value in evidence["after_turn"].items() if value is not None), filled)
        with tempfile.TemporaryDirectory() as directory:
            show = ("true || mandala --project {project} show --json", 0, "{}")
            add = ("mandala --project {project} add r8.c8", 0, "added")
            result, evidence = self.run_scripted(Path(directory), "B5", "capacity-final-child", FakeMandala(title_size=0),
                                                 [(0, False, [show, add], ("add", "r8.c8"))], ["s1"])
            self.assertEqual(result["status"], "INCONCLUSIVE")
            self.assertEqual((evidence["turns_completed"], len(evidence["after_turn"]["1"]["state"]["cells"])), (1, 72))

    def test_only_p1_agent_process_gets_the_missing_cli_shim(self):
        seen = {}
        for alias, fixture_id in (("P1", "missing-cli-manual"), ("A1", "generic-gap-analysis")):
            with self.subTest(alias), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "package"
                package.mkdir()
                (package / "SKILL.md").write_text("fake", encoding="utf-8")
                mandala = FakeMandala(title_size=0)
                base_env = {"PATH": "/usr/bin:/bin"}
                evaluator_envs = []

                class RecordingEvaluator(fake_evaluator(mandala)):
                    def __init__(self, project, env, record, mandala="mandala"):
                        evaluator_envs.append(dict(env))
                        super().__init__(project, env, record, mandala)

                def agent_turn(argv, prompt, project, env, timeout):
                    seen[alias] = dict(env)
                    command = "mandala --version" if alias == "P1" else "cat DESIGN.md"
                    event = {"turn": 1, "actor": "agent", "kind": "command", "command": command, "exit_code": 127 if alias == "P1" else 0, "output": ""}
                    return 0, json.dumps(event) + "\n", "", False

                fixture = next(case for case in json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8")) if case["id"] == fixture_id)
                with mock.patch.object(eval_live, "GENERATED", package), mock.patch.object(eval_live, "run_process", agent_turn), \
                        mock.patch.object(cases, "Evaluator", RecordingEvaluator):
                    result = eval_live.run_case(alias, {"fixture": fixture_id, "grader": fixture_id}, fixture, ScriptedAdapter(["s1"]), base_env, root / "cases" / alias, 5, None)
                self.assertEqual(result["status"], "AUTO_PASS")
                self.assertEqual(evaluator_envs, [{"PATH": "/usr/bin:/bin"}])
                self.assertEqual(base_env, {"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(seen["P1"]["PATH"], "/usr/bin:/bin")
        self.assertTrue(seen["P1"]["PATH"].endswith(os.pathsep + "/usr/bin:/bin"))
        self.assertIn("agent-missing-cli", seen["P1"]["PATH"].split(os.pathsep)[0])
        self.assertEqual(seen["A1"], {"PATH": "/usr/bin:/bin"})

    def test_setup_failure_writes_partial_evidence_without_inventing_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            result, run_dir = self.run_b5(Path(directory), FakeMandala(fail_init=True))
            self.assertEqual(result["status"], "ENVIRONMENT_ERROR")
            evidence = json.loads((run_dir / "cases" / "B5" / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual((evidence["before"], evidence["after_turn"], evidence["turns_completed"], evidence["turns_expected"]), (None, {"1": None}, 0, 1))

    def test_live_run_writes_rich_coverage_alongside_legacy_summary_field(self):
        with tempfile.TemporaryDirectory() as directory:
            _, run_dir = self.run_b5(Path(directory), FakeMandala(title_size=0))
            self.assertTrue(eval_live.write_rich_coverage(run_dir))
            coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertIn("contract_coverage", summary)
            self.assertEqual(set(summary["contract_coverage"]), {"exercised", "automatically_checked", "manual_review", "not_exercised"})
            self.assertEqual(next(row for row in coverage["contracts"] if row["id"] == "CAP-002")["coverage_state"], "AUTOMATED_OBSERVED")
            self.assertTrue((run_dir / "coverage.md").is_file())


if __name__ == "__main__":
    unittest.main()
