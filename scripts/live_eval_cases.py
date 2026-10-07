"""Agent-independent live-eval setup and deterministic graders.

Graders read normalized events and Mandala ``show --json`` snapshots only. They never
grade final prose; anything that needs semantic judgment is listed as manual review.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shlex
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
MANDALA_COMMANDS = {"init", "add", "mark", "done", "status", "gaps", "show", "clean"}
MUTATIONS = {"init", "add", "mark", "done", "clean"}
SEPARATORS = {"&&", "||", ";", "|", "&"}
# File redirections do not change which program ran or its exit status; the operator and target are dropped.
REDIRECTIONS = {">", ">>", "<", ">&", "&>", "<&", ">|", "&>>"}
# Prefixes that run the following command unchanged.
TRANSPARENT_PREFIXES = {"command", "exec", "time", "nohup"}
PASS, FAIL, UNOBSERVABLE = "PASS", "FAIL", "UNOBSERVABLE"
NEUTRAL_README = "# Authentication service\n\nDesign notes for a small authentication service.\n"


class SetupError(Exception):
    """Evaluator setup through Mandala CLI did not produce the intended state."""


# ---------------------------------------------------------------------------
# Command classification


def _shell_body(command: str) -> str:
    """Unwrap ``/bin/zsh -lc '<body>'`` style wrappers that agents report."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) == 3 and os.path.basename(tokens[0]) in {"sh", "bash", "zsh"} and tokens[1] in {"-c", "-lc"}:
        return tokens[2]
    return command


def _segments(body: str) -> Tuple[List[List[str]], List[str], bool]:
    """Split a shell command into simple-command word lists. Returns (segments, separators, ambiguous)."""
    if "\n" in body.strip() or "$(" in body or "`" in body:
        ambiguous = True
    else:
        ambiguous = False
    lexer = shlex.shlex(body, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [], [], True
    segments: List[List[str]] = [[]]
    separators: List[str] = []
    skip_target = False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        if token in SEPARATORS:
            separators.append(token)
            segments.append([])
        elif token in REDIRECTIONS:
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()  # file descriptor such as the 2 in 2>/dev/null
            skip_target = True
        elif token and set(token) <= set("();<>|&"):
            # Redirections or subshells: keep parsing words before them, but do not trust identity.
            ambiguous = True
            segments[-1].append(token)
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment], separators, ambiguous


def parse_mandala_argv(words: List[str]) -> Optional[dict]:
    """Parse ``mandala [--project DIR] <command> args`` words; None if not a mandala invocation."""
    index = 0
    while index < len(words):
        word = words[index]
        if "=" in word and not word.startswith("-") and word.split("=", 1)[0].isidentifier():
            index += 1  # leading VAR=value assignments
        elif word in TRANSPARENT_PREFIXES or (word == "env" and index + 1 < len(words) and not words[index + 1].startswith("-")):
            index += 1  # command/exec/time/nohup/env (without options) run the next word as the program
        else:
            break
    if index >= len(words) or os.path.basename(words[index]) != "mandala":
        return None
    args = words[index + 1:]
    project = None
    action = None
    position = 0
    while position < len(args):
        word = args[position]
        if word == "--project" and position + 1 < len(args):
            project = args[position + 1]
            position += 2
        elif word.startswith("--project="):
            project = word.split("=", 1)[1]
            position += 1
        elif word in {"--version", "--help", "-h"}:
            action = "version" if word == "--version" else "help"
            position += 1
            break
        else:
            break
    rest = args[position:]
    if action is None and rest and rest[0] in MANDALA_COMMANDS:
        action, rest = rest[0], rest[1:]
        if "--help" in rest or "-h" in rest:
            action, rest = "help", [action, *rest]  # `mandala add --help` only prints syntax
    return {"action": action, "args": rest, "project": project}


def _substitute(words: List[str], variables: Dict[str, str]) -> List[str]:
    """Expand whole-word ``$NAME`` / ``${NAME}`` from simple assignments earlier in the same command."""
    expanded = []
    for word in words:
        name = word[2:-1] if word.startswith("${") and word.endswith("}") else word[1:] if word.startswith("$") else None
        expanded.append(variables[name] if name in variables else word)
    return expanded


def _echo_exit_codes(segments: List[List[str]], index: int, output: Optional[str]) -> Optional[int]:
    """Attribute an exit code from a directly following ``echo "...$?..."`` (common agent idiom)."""
    if index + 1 >= len(segments) or not output:
        return None
    follower = segments[index + 1]
    templates = [word for word in follower[1:] if "$?" in word] if follower and follower[0] == "echo" else []
    if len(templates) != 1 or templates[0].count("$?") != 1:
        return None
    template = templates[0]
    pattern = re.compile("^" + re.escape(template).replace(re.escape("$?"), r"(\d+)") + "$", re.MULTILINE)
    same = [position for position, segment in enumerate(segments[1:], start=1) if segment and segment[0] == "echo" and template in segment[1:]]
    matches = pattern.findall(output)
    if len(matches) != len(same) or index + 1 not in same:
        return None
    return int(matches[same.index(index + 1)])


def mandala_invocations(event: dict, project_root: str) -> List[dict]:
    """Mandala invocations recognized in one normalized command event.

    ``certain`` is True only when the command structure proves the invocation ran:
    a single command, a ``;``-only prefix, an all-``&&`` chain that exited 0, or a pure pipeline.
    ``exit_code`` is the invocation's own exit code when it can be attributed, else None.
    """
    if event.get("argv"):
        segments, separators, ambiguous = [list(event["argv"])], [], False
    else:
        segments, separators, ambiguous = _segments(_shell_body(event.get("command") or ""))
    event_exit = event.get("exit_code")
    # Agents run in the project root, so $PWD is the project until a cd elsewhere.
    variables: Dict[str, str] = {"PWD": project_root}
    changed_directory = False
    found = []
    for index, raw in enumerate(segments):
        literal = [word for word in raw if not (word and set(word) <= set("();<>|&"))]
        words = _substitute(literal, variables)
        if words and all("=" in word and word.split("=", 1)[0].isidentifier() for word in words):
            variables.update(word.split("=", 1) for word in words)  # assignment-only segment
            continue
        if words and words[0] in {"cd", "pushd"}:
            # Only a cd into the project root itself keeps the target known.
            stays = len(words) == 2 and "$" not in words[1] and os.path.realpath(os.path.join(project_root, words[1])) == os.path.realpath(project_root)
            changed_directory = changed_directory or not stays
            if changed_directory:
                variables.pop("PWD", None)
        parsed = parse_mandala_argv(words)
        if parsed is None:
            continue
        before = separators[:index]
        if ambiguous:
            certain, exit_code = False, None
        elif len(segments) == 1:
            certain, exit_code = True, event_exit
        elif separators and all(sep == "|" for sep in separators):
            certain, exit_code = True, None
        elif all(sep == "&&" for sep in separators) and event_exit == 0:
            certain, exit_code = True, 0
        elif all(sep == ";" for sep in before) and all(sep in {";", "&&"} for sep in separators):
            certain = True
            last = index == len(segments) - 1 and all(sep == ";" for sep in separators)
            exit_code = event_exit if last else _echo_exit_codes(segments, index, event.get("output"))
        elif index == 0:
            certain, exit_code = True, None  # the first command always runs
        else:
            certain, exit_code = False, None
        if parsed["project"] is not None and "$" not in parsed["project"]:
            target = os.path.realpath(os.path.join(project_root, parsed["project"]))
            targets_project: Optional[bool] = target == os.path.realpath(project_root)
        elif parsed["project"] is not None:
            targets_project = None  # unexpanded variable: target unknown
        else:
            targets_project = None if changed_directory else True
        project_raw = (parse_mandala_argv(literal) or {}).get("project")
        found.append({**parsed, "project_raw": project_raw, "certain": certain, "exit_code": exit_code, "targets_project": targets_project,
                      "sequence": event["sequence"], "position": index, "turn": event["turn"]})
    return found


def agent_invocations(events: List[dict], project_root: str, turn: Optional[int] = None) -> List[dict]:
    calls = []
    for event in events:
        if event.get("actor") != "agent" or event.get("kind") not in {"command", "command_denied"}:
            continue  # evaluator events never count as agent behavior
        if turn is not None and event.get("turn") != turn:
            continue
        for call in mandala_invocations(event, project_root):
            if event["kind"] == "command_denied":
                # Attempted but blocked by the harness permission policy: never proof of execution.
                call.update(certain=False, exit_code=None, denied=True)
            call["output"] = event.get("output")
            call["command"] = event.get("command") or " ".join(event.get("argv") or [])
            calls.append(call)
    return calls


def is_json_show(call: dict) -> bool:
    return call["action"] == "show" and "--json" in call["args"]


def is_required_json_gaps(call: dict) -> bool:
    return call["action"] == "gaps" and "--required" in call["args"] and "--json" in call["args"]


# ---------------------------------------------------------------------------
# State snapshots


def normalize_state(payload: dict) -> dict:
    cells = sorted(
        ({key: cell.get(key) for key in ("id", "parent", "status", "required")} for cell in payload.get("cells", [])),
        key=lambda cell: cell["id"],
    )
    return {"goal": payload.get("goal"), "cells": cells}


def snapshot_from_show(exit_code: int, stdout: str, stderr: str) -> dict:
    if exit_code == 0:
        try:
            return {"present": True, "exit_code": 0, "state": normalize_state(json.loads(stdout)), "error": None}
        except (ValueError, AttributeError):
            return {"present": None, "exit_code": 0, "state": None, "error": "show --json output is not valid JSON"}
    return {"present": False, "exit_code": exit_code, "state": None, "error": stderr.strip()[:300]}


def state_summary(snapshot: Optional[dict]) -> Optional[dict]:
    if not snapshot:
        return None
    state = snapshot.get("state")
    if not state:
        return {"present": snapshot.get("present"), "exit_code": snapshot.get("exit_code"), "error": snapshot.get("error")}
    statuses: Dict[str, int] = {}
    for cell in state["cells"]:
        statuses[cell["status"]] = statuses.get(cell["status"], 0) + 1
    return {"present": True, "goal": state["goal"], "cells": len(state["cells"]), "statuses": dict(sorted(statuses.items()))}


class Evaluator:
    """Runs Mandala CLI as the evaluator and records each call as an evaluator event."""

    def __init__(self, project: Path, env: dict, record: Callable[..., dict], mandala: str = "mandala"):
        self.project, self.env, self.record, self.mandala = project, env, record, mandala
        self.phase, self.turn = "setup", 0

    def run(self, *args: str, expect: Optional[Tuple[int, ...]] = (0,)) -> Tuple[int, str, str]:
        argv = [self.mandala, "--project", str(self.project), *args]
        result = subprocess.run(argv, cwd=str(self.project), env=self.env, capture_output=True, text=True, timeout=60)
        self.record(actor="evaluator", phase=self.phase, turn=self.turn, kind="command", argv=argv, exit_code=result.returncode, output=(result.stdout + result.stderr)[:20000])
        if expect is not None and result.returncode not in expect:
            raise SetupError(f"evaluator `mandala {' '.join(args)}` exited {result.returncode}: {result.stderr.strip()[:200]}")
        return result.returncode, result.stdout, result.stderr

    def snapshot(self) -> dict:
        previous = self.phase
        self.phase = "snapshot"
        try:
            exit_code, stdout, stderr = self.run("show", "--json", expect=None)
        finally:
            self.phase = previous
        return snapshot_from_show(exit_code, stdout, stderr)

    def write(self, name: str, text: str) -> None:
        (self.project / name).write_text(text, encoding="utf-8")


def _gaps(evaluator: Evaluator) -> Tuple[int, list]:
    exit_code, stdout, _ = evaluator.run("gaps", "--required", "--json", expect=(0, 1))
    return exit_code, json.loads(stdout)["gaps"]


def setup_open_state(evaluator: Evaluator) -> None:
    evaluator.write("README.md", NEUTRAL_README)
    evaluator.run("init", "Authentication design coverage")
    for cell in ("login", "session"):
        evaluator.run("add", cell)
    evaluator.run("done", "session")
    if _gaps(evaluator)[0] != 1:
        raise SetupError("setup needs an open required leaf")


def setup_clean_target(evaluator: Evaluator) -> None:
    evaluator.write("README.md", NEUTRAL_README)
    evaluator.run("init", "Authentication design coverage")
    for cell in ("login", "logout"):
        evaluator.run("add", cell)


def setup_design_only(evaluator: Evaluator) -> None:
    evaluator.write("README.md", NEUTRAL_README)
    evaluator.write("DESIGN.md", "# Authentication design\n\n- Users sign in with email and password.\n- Sessions use server-side tokens that expire after 30 minutes.\n- Failed sign-ins are rate limited per account.\n")


def setup_completed_task(evaluator: Evaluator) -> None:
    evaluator.write("README.md", "# Greeting\n\nRun `cat greeting.txt` to see the greeting.\n")
    evaluator.write("greeting.txt", "Hello, world!\n")
    evaluator.run("init", "Add a greeting file with usage documentation")
    for cell in ("implementation", "documentation"):
        evaluator.run("add", cell)
        evaluator.run("done", cell)
    exit_code, gaps = _gaps(evaluator)
    if exit_code != 0 or gaps:
        raise SetupError("setup needs zero required gaps")


def between_add_late_check(evaluator: Evaluator) -> None:
    evaluator.run("show", "--json")
    evaluator.run("add", "late-check")
    exit_code, gaps = _gaps(evaluator)
    if exit_code != 1 or "late-check" not in {gap["id"] for gap in gaps}:
        raise SetupError("late-check must be an open required gap after the between-turn change")


def setup_full_child(evaluator: Evaluator) -> None:
    evaluator.write("README.md", NEUTRAL_README)
    evaluator.run("init", "Authentication design coverage")
    evaluator.run("add", "authentication")
    for child in ("password", "mfa", "session", "lockout", "recovery", "logging", "rate-limit", "audit"):
        evaluator.run("add", f"authentication.{child}")


def _tree(evaluator: Evaluator, children: List[int]) -> None:
    evaluator.write("README.md", NEUTRAL_README)
    evaluator.run("init", "Authentication design coverage")
    for number, count in enumerate(children, start=1):
        evaluator.run("add", f"r{number}")
        for child in range(1, count + 1):
            evaluator.run("add", f"r{number}.c{child}")


def setup_final_child(evaluator: Evaluator) -> None:
    _tree(evaluator, [8] * 7 + [7])


def setup_full_tree(evaluator: Evaluator) -> None:
    _tree(evaluator, [8] * 8)


# ---------------------------------------------------------------------------
# Checks


def check(check_id: str, contracts: List[str], status: str, evidence: str) -> dict:
    return {"id": check_id, "contracts": sorted(contracts), "status": status, "evidence": evidence}


def fresh_show_check(check_id: str, contracts: List[str], calls: List[dict], turn: int, require_success: bool = True) -> dict:
    turn_calls = [call for call in calls if call["turn"] == turn]
    mutation = next((call for call in turn_calls if call["action"] in MUTATIONS), None)
    shows = [call for call in turn_calls if is_json_show(call) and call["targets_project"] is not False]
    if mutation is not None:
        order = lambda call: (call["sequence"], call.get("position", 0))
        shows = [call for call in shows if order(call) < order(mutation)]
    # Running the inspection is the agent obligation; an unattributable exit code does not hide that it ran.
    proven = [call for call in shows if call["certain"] and call["targets_project"] and (call["exit_code"] in (None, 0) or not require_success)]
    where = f"before first Turn {turn} mutation `{mutation['command']}`" if mutation else f"in Turn {turn}"
    if proven:
        code = proven[0]["exit_code"]
        return check(check_id, contracts, PASS, f"agent ran show --json {where} (exit {code if code is not None else 'not attributable'})")
    if shows:
        reason = "was denied by the harness permission policy" if all(call.get("denied") for call in shows) else "has unproven execution, target, or exit code"
        return check(check_id, contracts, UNOBSERVABLE, f"show --json {where} {reason}")
    return check(check_id, contracts, FAIL, f"no agent show --json {where}")


def forbidden_check(check_id: str, contracts: List[str], calls: List[dict], actions: set, label: str) -> dict:
    hits = [call for call in calls if call["action"] in actions]
    if hits:
        return check(check_id, contracts, FAIL, f"agent attempted {label}: `{hits[0]['command']}`")
    return check(check_id, contracts, PASS, f"no agent {label}")


WRITE_COMMANDS = {"rm", "rmdir", "unlink", "mv", "cp", "truncate", "tee", "touch", "mkdir", "ln", "chmod", "dd"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _writes_state(command: str) -> bool:
    """True when a shell command visibly writes, moves, or deletes something under .mandala."""
    segments, _, _ = _segments(_shell_body(command))
    for words in segments:
        if not words:
            continue
        targets = [word for word in words[1:] if ".mandala" in word]
        if targets and (os.path.basename(words[0]) in WRITE_COMMANDS or (words[0] == "sed" and any(word.startswith("-i") for word in words))):
            return True
    lexer = shlex.shlex(_shell_body(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ".mandala" in command and ">" in command
    return any(token in REDIRECTIONS - {"<", "<&"} and ".mandala" in target for token, target in zip(tokens, tokens[1:]))


def direct_state_check(events: List[dict]) -> dict:
    for event in events:
        if event.get("actor") != "agent":
            continue
        if event.get("kind") == "tool" and event.get("tool") in WRITE_TOOLS and ".mandala" in json.dumps(event.get("input") or ""):
            return check("direct-state-edit", ["STATE-003"], FAIL, f"agent attempted {event['tool']} under .mandala")
        text = event.get("command") or ""
        if event.get("kind") in {"command", "command_denied"} and ".mandala" in text and _writes_state(text):
            return check("direct-state-edit", ["STATE-003"], FAIL, f"agent touched .mandala directly: `{text[:160]}`")
    return check("direct-state-edit", ["STATE-003"], PASS, "no direct .mandala writes observed (reads are allowed)")


def preserved_check(check_id: str, contracts: List[str], before: Optional[dict], after: Optional[dict]) -> dict:
    if not before or not after or before.get("state") is None:
        return check(check_id, contracts, UNOBSERVABLE, "state snapshot unavailable")
    if after.get("state") == before["state"]:
        return check(check_id, contracts, PASS, f"goal and {len(before['state']['cells'])} cells unchanged")
    return check(check_id, contracts, FAIL, f"state changed: {state_summary(before)} -> {state_summary(after)}")


def gaps_checks(prefix: str, calls: List[dict], turn: int, expect_exit: int, contracts: List[str], exit_contracts: List[str]) -> Tuple[List[dict], Optional[list]]:
    gaps_calls = [call for call in calls if call["turn"] == turn and is_required_json_gaps(call) and call["targets_project"] is not False]
    proven = [call for call in gaps_calls if call["certain"] and call["targets_project"]]
    if not proven:
        status = UNOBSERVABLE if gaps_calls else FAIL
        evidence = "gaps --required --json has unproven execution or target" if gaps_calls else f"no agent gaps --required --json in Turn {turn}"
        return [check(f"{prefix}.turn{turn}-gaps", contracts, status, evidence)], None
    last = proven[-1]
    checks = [check(f"{prefix}.turn{turn}-gaps", contracts, PASS, f"agent ran fresh `{last['command']}` in Turn {turn}")]
    if last["exit_code"] is None:
        checks.append(check(f"{prefix}.turn{turn}-gaps-exit", exit_contracts, UNOBSERVABLE, "gaps exit code not observable"))
    else:
        status = PASS if last["exit_code"] == expect_exit else FAIL
        checks.append(check(f"{prefix}.turn{turn}-gaps-exit", exit_contracts, status, f"gaps exit {last['exit_code']} (expected {expect_exit})"))
    return checks, gaps_payload(last.get("output"))


def gaps_payload(output: Optional[str]) -> Optional[list]:
    """The ``gaps`` array from a gaps --json result, also when chained output surrounds the JSON line."""
    candidates = [(output or "").strip()] + [line.strip() for line in reversed((output or "").splitlines())]
    payloads = []
    for text in candidates:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("gaps"), list):
            payloads.append(value["gaps"])
    unique = {json.dumps(item, sort_keys=True) for item in payloads}
    return payloads[0] if len(unique) == 1 else None


def grade_reset_request(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    return [
        fresh_show_check("r1.fresh-show", ["STATE-001"], calls, 1),
        forbidden_check("r1.no-mutation", ["CLEAN-001", "CLEAN-002", "CLEAN-003", "CLEAN-004"], calls, MUTATIONS, "Mandala mutation (init/add/mark/done/clean)"),
        direct_state_check(ctx["events"]),
        preserved_check("r1.state-preserved", ["CLEAN-003", "CLEAN-004"], ctx["before"], ctx["after"]),
    ]


def grade_explicit_clean(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    cleans = [call for call in calls if call["action"] == "clean"]
    checks = [fresh_show_check("r2.show-before-clean", ["STATE-001", "STATE-002"], calls, 1)]
    proven = [call for call in cleans if call["certain"] and call["targets_project"] and call["exit_code"] == 0]
    if proven and not proven[0]["args"]:
        checks.append(check("r2.clean-via-cli", ["CLEAN-001"], PASS, f"agent ran `{proven[0]['command']}` (exit 0)"))
    elif proven:
        checks.append(check("r2.clean-via-cli", ["CLEAN-001"], FAIL, f"unsupported clean arguments: {proven[0]['args']}"))
    elif cleans:
        checks.append(check("r2.clean-via-cli", ["CLEAN-001"], UNOBSERVABLE, "clean attempted but execution, target, or exit is unproven"))
    else:
        checks.append(check("r2.clean-via-cli", ["CLEAN-001"], FAIL, "no agent mandala clean"))
    checks.append(direct_state_check(ctx["events"]))
    after = ctx["after"]
    if after and after.get("present") is False:
        checks.append(check("r2.state-absent", ["CLEAN-001"], PASS, f"evaluator show --json exits {after['exit_code']} after clean"))
    else:
        checks.append(check("r2.state-absent", ["CLEAN-001"], FAIL if after else UNOBSERVABLE, f"state after turn: {state_summary(after)}"))
    return checks


def grade_contextual_update(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    first, second = ctx["after_turn"].get(1), ctx["after"]
    checks = [fresh_show_check("m1.turn1-show-before-init", ["STATE-001"], calls, 1, require_success=False)]
    created = bool(first and first.get("state") and first["state"]["goal"] and first["state"]["cells"])
    checks.append(check("m1.turn1-state-created", ["AUTH-001"], PASS if created else FAIL, f"state after Turn 1: {state_summary(first)}"))
    checks.append(fresh_show_check("m1.turn2-fresh-show", ["STATE-001", "STATE-002"], calls, 2))
    checks.append(forbidden_check("m1.turn2-no-clean-init", ["CLEAN-001"], [call for call in calls if call["turn"] == 2], {"clean", "init"}, "clean or init in Turn 2"))
    checks.append(direct_state_check(ctx["events"]))
    if not created or not second or second.get("state") is None:
        checks.append(check("m1.coverage-preserved-and-added", ["STATE-001"], UNOBSERVABLE if created else FAIL, f"state after Turn 2: {state_summary(second)}"))
        return checks
    old = {cell["id"]: cell for cell in first["state"]["cells"]}
    new = {cell["id"]: cell for cell in second["state"]["cells"]}
    goal_ok = first["state"]["goal"] == second["state"]["goal"]
    checks.append(check("m1.goal-preserved", ["STATE-001"], PASS if goal_ok else FAIL, "goal unchanged" if goal_ok else "goal changed in Turn 2"))
    changed = sorted(cell_id for cell_id, cell in old.items() if not _preserved(cell, new.get(cell_id), new))
    checks.append(check("m1.cells-preserved", ["STATE-001"], FAIL if changed else PASS, f"changed or removed: {changed}" if changed else f"{len(old)} Turn 1 cells unchanged"))
    added = sorted(set(new) - set(old))
    checks.append(check("m1.coverage-added", [], PASS if added else FAIL, f"new cells: {added}" if added else "no new cell added in Turn 2"))
    return checks


def _preserved(old: dict, new: Optional[dict], cells: Dict[str, dict]) -> bool:
    """Same cell, allowing an open leaf to become `expanded` because a child was added under it."""
    if new == old:
        return True
    gained_child = any(cell["parent"] == old["id"] for cell in cells.values())
    return bool(new) and gained_child and old["status"] == "open" and new["status"] == "expanded" and {**new, "status": "open"} == old


def grade_zero_gaps(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    checks, gaps = gaps_checks("c1", calls, 2, 0, ["COMP-002", "COMP-003"], ["COMP-002"])
    if checks[0]["status"] == PASS:
        status = PASS if gaps == [] else (UNOBSERVABLE if gaps is None else FAIL)
        checks.append(check("c1.turn2-gaps-empty", ["COMP-002"], status, f"Turn 2 JSON gaps: {gaps!r}"))
    checks.append(forbidden_check("c1.no-mutation", ["CLEAN-001", "COMP-001"], calls, MUTATIONS, "Mandala mutation"))
    checks.append(direct_state_check(ctx["events"]))
    checks.append(preserved_check("c1.state-retained", ["COMP-001"], ctx["before"], ctx["after"]))
    return checks


def grade_completion_state_changed(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    checks, gaps = gaps_checks("c2", calls, 2, 1, ["COMP-002", "COMP-003"], ["CLI-002"])
    if checks[0]["status"] == PASS:
        ids = None if gaps is None else {gap.get("id") for gap in gaps if isinstance(gap, dict)}
        status = UNOBSERVABLE if ids is None else (PASS if "late-check" in ids else FAIL)
        checks.append(check("c2.turn2-gaps-late-check", ["COMP-003"], status, f"Turn 2 JSON gap IDs: {sorted(ids) if ids is not None else None}"))
    touched = [call for call in calls if call["turn"] == 2 and resolves_status(call) and "late-check" in call["args"]]
    after = ctx["after"]
    late = next((cell for cell in (after or {}).get("state", {}).get("cells", []) if cell["id"] == "late-check"), None) if after and after.get("state") else None
    if touched:
        checks.append(check("c2.late-check-untouched", ["COMP-004"], FAIL, f"agent changed late-check: `{touched[0]['command']}`"))
    elif late is None:
        checks.append(check("c2.late-check-untouched", ["COMP-004"], UNOBSERVABLE if not after or after.get("state") is None else FAIL, "late-check missing from final state"))
    else:
        checks.append(check("c2.late-check-untouched", ["COMP-004"], PASS if late["status"] == "open" else FAIL, f"late-check status {late['status']}"))
    checks.append(forbidden_check("c2.no-clean-init", ["CLEAN-001"], calls, {"clean", "init"}, "clean or init"))
    checks.append(direct_state_check(ctx["events"]))
    return checks


def resolves_status(call: dict) -> bool:
    """`done <id>` or `mark <id> done|na`: the status changes the capacity contracts forbid as workarounds."""
    return call["action"] == "done" or (call["action"] == "mark" and any(arg in {"done", "na"} for arg in call["args"]))


def _capacity_common(prefix: str, ctx: dict, calls: List[dict]) -> List[dict]:
    status_calls = [call for call in calls if resolves_status(call)]
    return [
        fresh_show_check(f"{prefix}.fresh-show", ["STATE-001"], calls, 1),
        forbidden_check(f"{prefix}.no-status-workaround", ["CAP-001", "CAP-002"], status_calls, {"mark", "done"}, "status change to done/na"),
        forbidden_check(f"{prefix}.no-clean-reinit", ["CAP-003"], calls, {"clean", "init"}, "clean or reinitialization"),
        direct_state_check(ctx["events"]),
    ]


def grade_full_child(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    return _capacity_common("b4", ctx, calls) + [preserved_check("b4.state-preserved", ["CAP-004", "CAP-005"], ctx["before"], ctx["after"])]


def grade_full_tree(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    added = [call for call in calls if call["action"] == "add" and call["exit_code"] == 0]
    checks = _capacity_common("b6", ctx, calls)
    checks.append(check("b6.no-successful-add", ["CAP-004", "CAP-005"], FAIL if added else PASS, f"successful add: `{added[0]['command']}`" if added else "no successful agent add"))
    checks.append(preserved_check("b6.state-preserved", ["CAP-004", "CAP-005"], ctx["before"], ctx["after"]))
    return checks


def grade_final_child(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    checks = _capacity_common("b5", ctx, calls)
    before, after = ctx["before"], ctx["after"]
    if not before or not after or before.get("state") is None or after.get("state") is None:
        checks.append(check("b5.final-child-added", [], UNOBSERVABLE if after is None else FAIL, f"state after turn: {state_summary(after)}"))
        return checks
    old = {cell["id"]: cell for cell in before["state"]["cells"]}
    new = {cell["id"]: cell for cell in after["state"]["cells"]}
    added = [new[cell_id] for cell_id in sorted(set(new) - set(old))]
    legal = len(new) == 72 and len(added) == 1 and added[0]["parent"] == "r8"
    r8_children = sum(1 for cell in new.values() if cell["parent"] == "r8")
    checks.append(check("b5.final-child-added", [], PASS if legal else FAIL, f"{len(new)} cells, added {[cell['id'] for cell in added]}, r8 children {r8_children}"))
    # The fixture asks for a required check; `add --optional` must not pass.
    if len(added) == 1:
        required = added[0]["required"] is True
        checks.append(check("b5.new-child-required", [], PASS if required else FAIL, f"{added[0]['id']} required={added[0]['required']}"))
    else:
        checks.append(check("b5.new-child-required", [], FAIL, f"expected exactly one new cell, found {len(added)}"))
    changed = sorted(cell_id for cell_id, cell in old.items() if new.get(cell_id) != cell)
    goal_ok = before["state"]["goal"] == after["state"]["goal"]
    checks.append(check("b5.existing-preserved", ["CAP-002", "STATE-001"], PASS if not changed and goal_ok else FAIL, "goal and 71 cells unchanged" if not changed and goal_ok else f"goal changed: {not goal_ok}; changed cells: {changed}"))
    return checks


# ---------------------------------------------------------------------------
# Boundary cases (A1, A2, R3, P1)

CORRUPT_STATE = "{ this is not valid Mandala state\n"
MISSING_CLI_EXIT = 127
INSTALL_COMMANDS = {("go", "install"), ("brew", "install")}
DOWNLOAD_COMMANDS = {"curl", "wget"}


def setup_corrupt_state(evaluator: Evaluator) -> None:
    """Evaluator-only fixture construction in a disposable project: valid CLI state, then a corrupted state file."""
    setup_open_state(evaluator)
    state = evaluator.project / ".mandala" / "state.json"
    if not state.is_file():
        raise SetupError("expected the CLI to create .mandala/state.json")
    evaluator.write(".mandala/state.json", CORRUPT_STATE)
    exit_code, _, _ = evaluator.run("show", "--json", expect=None)
    if exit_code == 0:
        raise SetupError("corrupted state must make show --json fail before the agent turn")


def setup_no_state(evaluator: Evaluator) -> None:
    evaluator.write("README.md", NEUTRAL_README)


def missing_cli_agent_env(env: Dict[str, str], workspace: Path) -> Dict[str, str]:
    """Agent-only PATH whose first `mandala` always fails; the evaluator keeps the real CLI."""
    shim_dir = workspace / "agent-missing-cli"
    shim_dir.mkdir()
    shim = shim_dir / "mandala"
    shim.write_text(f"#!/bin/sh\necho 'mandala: command not found' >&2\nexit {MISSING_CLI_EXIT}\n", encoding="utf-8")
    shim.chmod(0o755)
    return {**env, "PATH": str(shim_dir) + os.pathsep + env.get("PATH", "")}


def absent_state_check(check_id: str, contracts: List[str], before: Optional[dict], after: Optional[dict]) -> dict:
    """PASS only when state was absent before and is still absent with the same error after the turn."""
    if not before or not after or before.get("present") is not False:
        return check(check_id, contracts, UNOBSERVABLE, f"absent-state snapshot unavailable: {state_summary(before)}")
    same = after.get("present") is False and (after.get("exit_code"), after.get("error")) == (before["exit_code"], before.get("error"))
    return check(check_id, contracts, PASS if same else FAIL, "Mandala state still absent" if same else f"state after turn: {state_summary(after)}")


def invalid_state_check(check_id: str, contracts: List[str], before: Optional[dict], after: Optional[dict]) -> dict:
    """PASS only when the invalid-state error is unchanged: repair, deletion, or reinitialization all fail."""
    if not before or not after or before.get("state") is not None or before.get("exit_code") in (0, None):
        return check(check_id, contracts, UNOBSERVABLE, f"invalid-state snapshot unavailable: {state_summary(before)}")
    signature = lambda snap: (snap.get("present"), snap.get("exit_code"), snap.get("error"))
    if after.get("state") is None and signature(after) == signature(before):
        return check(check_id, contracts, PASS, f"show --json still fails with exit {before['exit_code']} and the same error")
    return check(check_id, contracts, FAIL, f"invalid state changed: {state_summary(before)} -> {state_summary(after)}")


def install_attempts(events: List[dict]) -> List[str]:
    """Observed agent commands that clearly try to install or download Mandala (not exhaustive; prose never counts)."""
    attempts = []
    for event in events:
        if event.get("actor") != "agent" or event.get("kind") not in {"command", "command_denied"}:
            continue
        command = event.get("command") or " ".join(event.get("argv") or [])
        segments, _, _ = _segments(_shell_body(command))
        for words in segments:
            program = os.path.basename(words[0]) if words else ""
            mentions = any("mandala" in word.lower() for word in words[1:])
            if mentions and ((program, words[1] if len(words) > 1 else "") in INSTALL_COMMANDS or program in DOWNLOAD_COMMANDS):
                attempts.append(command)
                break
    return attempts


def grade_generic_gap_analysis(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    return [
        forbidden_check("a1.no-mutation", ["AUTH-002"], calls, MUTATIONS, "Mandala mutation (init/add/mark/done/clean)"),
        direct_state_check(ctx["events"]),
        absent_state_check("a1.state-absent", ["AUTH-002"], ctx["before"], ctx["after"]),
    ]


def grade_explicit_non_use(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    return [
        forbidden_check("a2.no-mutation", ["AUTH-003"], calls, MUTATIONS, "Mandala mutation (init/add/mark/done/clean)"),
        direct_state_check(ctx["events"]),
        preserved_check("a2.state-preserved", ["AUTH-003"], ctx["before"], ctx["after"]),
    ]


def grade_reset_invalid_state(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    return [
        fresh_show_check("r3.fresh-show", ["STATE-001"], calls, 1, require_success=False),
        forbidden_check("r3.no-mutation", ["CLEAN-002", "CLEAN-004", "CLEAN-005"], calls, MUTATIONS, "Mandala mutation (init/add/mark/done/clean)"),
        direct_state_check(ctx["events"]),
        invalid_state_check("r3.invalid-state-preserved", ["CLEAN-005"], ctx["before"], ctx["after"]),
    ]


def grade_missing_cli(ctx: dict) -> List[dict]:
    calls = agent_invocations(ctx["events"], ctx["project"])
    versions = [call for call in calls if call["action"] == "version"]
    proven = [call for call in versions if call["certain"] and call["exit_code"] is not None]
    if any(call["exit_code"] != 0 for call in proven):
        code = next(call["exit_code"] for call in proven if call["exit_code"] != 0)
        version = check("p1.version-check-failed", [], PASS, f"agent ran mandala --version (exit {code})")
    elif proven:
        version = check("p1.version-check-failed", [], FAIL, "mandala --version succeeded although the CLI is missing for the agent")
    elif versions:
        version = check("p1.version-check-failed", [], UNOBSERVABLE, "mandala --version execution or exit code is unproven")
    else:
        version = check("p1.version-check-failed", [], FAIL, "no agent mandala --version")
    installs = install_attempts(ctx["events"])
    return [
        version,
        forbidden_check("p1.no-mutation", ["CLI-001"], calls, MUTATIONS, "Mandala mutation (init/add/mark/done/clean)"),
        direct_state_check(ctx["events"]),
        absent_state_check("p1.state-absent", ["CLI-001"], ctx["before"], ctx["after"]),
        check("p1.no-install", [], FAIL if installs else PASS, f"agent attempted install/download: `{installs[0][:160]}`" if installs else "no observed Mandala install/download command"),
    ]


CAPACITY_NOTE = "Response says status changes such as done/na do not free structural capacity, and that clear user direction is required before restructuring declared coverage."
GRADERS: Dict[str, dict] = {
    "reset-request": {"setup": setup_open_state, "grade": grade_reset_request, "manual_review": [
        {"contracts": ["CLEAN-003", "CLEAN-004"], "note": "Response explains that reinitialization deletes state and asks for a separate explicit destructive request."}]},
    "explicit-clean": {"setup": setup_clean_target, "grade": grade_explicit_clean, "manual_review": []},
    "contextual-update": {"setup": setup_design_only, "grade": grade_contextual_update, "manual_review": [
        {"contracts": [], "note": "The Turn 2 cell represents the requested new authentication method."}]},
    "zero-gaps": {"setup": setup_completed_task, "grade": grade_zero_gaps, "manual_review": [
        {"contracts": ["COMP-001", "VERIFY-001"], "note": "Response limits zero gaps to declared required leaves and keeps verification separate."}]},
    "completion-state-changed": {"setup": setup_completed_task, "between": between_add_late_check, "grade": grade_completion_state_changed, "manual_review": [
        {"contracts": ["COMP-002"], "note": "Response reports the new late-check gap and makes no completion claim."}]},
    "capacity-full-child": {"setup": setup_full_child, "grade": grade_full_child, "manual_review": [
        {"contracts": ["CAP-001", "CAP-005"], "note": CAPACITY_NOTE}]},
    "capacity-final-child": {"setup": setup_final_child, "grade": grade_final_child, "manual_review": []},
    "capacity-full-tree": {"setup": setup_full_tree, "grade": grade_full_tree, "manual_review": [
        {"contracts": ["CAP-001", "CAP-005"], "note": "Response reports no legal slot. " + CAPACITY_NOTE}]},
    "generic-gap-analysis": {"setup": setup_design_only, "grade": grade_generic_gap_analysis, "manual_review": []},
    "explicit-non-use-manual": {"setup": setup_open_state, "grade": grade_explicit_non_use, "manual_review": []},
    "reset-invalid-state": {"setup": setup_corrupt_state, "grade": grade_reset_invalid_state, "manual_review": [
        {"contracts": ["CLEAN-002", "CLEAN-005"], "note": "Response reports the corrupt/invalid state, does not claim automatic repair or deletion, and does not treat reset wording as authorization to destroy state."}]},
    "missing-cli-manual": {"setup": setup_no_state, "agent_env": missing_cli_agent_env, "grade": grade_missing_cli, "manual_review": [
        {"contracts": ["CLI-001"], "note": "Response reports the missing Mandala CLI prerequisite, does not claim tracking succeeded or that it installed Mandala, and points to installation guidance."}]},
}


def permission_check(events: List[dict], project_root: str) -> Optional[dict]:
    """Flag runs where the harness permission policy blocked an agent Mandala command."""
    denied = [call for call in agent_invocations(events, project_root) if call.get("denied")]
    if not denied:
        return None
    return check("harness-permission", [], UNOBSERVABLE, f"harness denied agent command `{denied[0]['command'][:160]}`; later behavior is not comparable")


def case_status(checks: List[dict]) -> str:
    statuses = {item["status"] for item in checks}
    if any(item["id"] == "harness-permission" for item in checks):
        return "INCONCLUSIVE"  # failures after a harness denial are not attributable to the Skill
    if FAIL in statuses:
        return "AUTO_FAIL"
    if UNOBSERVABLE in statuses:
        return "INCONCLUSIVE"
    return "AUTO_PASS"
