"""Agent CLI adapters for the live-eval harness.

Each adapter builds argv for one user turn and converts the agent's native structured
events into normalized events. Graders never see native events. Flags were taken from
the installed CLIs' help (codex-cli 0.155.1, Claude Code 2.1.285):

Codex
  codex exec --json --ignore-user-config --skip-git-repo-check -s workspace-write -C <project> -
  codex exec resume --json --ignore-user-config --skip-git-repo-check -c sandbox_mode="workspace-write" <thread-id> -
  JSONL: thread.started{thread_id}, item.started/item.completed{item{type: command_execution
  {command, exit_code, aggregated_output} | agent_message{text} | error}}, turn.completed, turn.failed, error.

Claude Code
  claude -p --output-format stream-json --verbose --setting-sources project --strict-mcp-config
         --permission-mode dontAsk --append-system-prompt <permission-policy note> --allowedTools "Bash(mandala *)" <read-only Bash helpers> Read Glob Grep Skill
         [--resume <id>]
  stream-json: system/init{session_id, model, skills}, assistant{message.content[tool_use|text]},
  user{message.content[tool_result{tool_use_id, is_error, content}]}, system/permission_denied, result.

Neither adapter enables a permission-bypass mode. Prompts are sent on stdin, never through a shell.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import signal
import subprocess
from typing import Dict, List, Optional, Tuple

EXIT_CODE_PREFIX = re.compile(r"^(?:Error: )?Exit code (\d+)\b")


def sanitized_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Inherit the environment (agents need their normal auth) minus repository-context GIT_* variables."""
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if not key.startswith("GIT_")}


class TurnResult:
    def __init__(self) -> None:
        self.raw_lines: List[str] = []
        self.stderr = ""
        self.returncode: Optional[int] = None
        self.timed_out = False
        self.events: List[dict] = []
        self.session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.final_text: Optional[str] = None
        self.environment_error: Optional[str] = None
        self.skills: Optional[List[str]] = None


def run_process(argv: List[str], prompt: str, cwd: Path, env: Dict[str, str], timeout: float) -> Tuple[Optional[int], str, str, bool]:
    """Run one agent turn with the prompt on stdin; kill the whole process group on timeout."""
    process = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        return (*_stop_group(process), True)
    except KeyboardInterrupt:
        _stop_group(process)  # never leave an agent running against the project after an interruption
        raise


def _stop_group(process: subprocess.Popen) -> Tuple[Optional[int], str, str]:
    """SIGTERM, then SIGKILL, the agent's whole process group (it runs in its own session)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            stdout, stderr = process.communicate(timeout=10)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            continue
    process.wait()
    return process.returncode, "", ""


def _json_lines(lines: List[str]) -> List[Tuple[str, Optional[dict]]]:
    parsed = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        parsed.append((line, value if isinstance(value, dict) else None))
    return parsed


class CodexAdapter:
    name = "codex"
    executable = "codex"
    required_help = {"exec": ("--json", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "workspace-write", "resume"), "exec resume": ("--json", "SESSION_ID", "--config")}
    # Codex JSONL has no observable skill-load or model field.
    skill_load_observable = False

    def __init__(self, model: Optional[str] = None):
        self.model = model

    def help_commands(self) -> Dict[str, List[str]]:
        return {"exec": [self.executable, "exec", "--help"], "exec resume": [self.executable, "exec", "resume", "--help"]}

    def skill_dir(self, project: Path) -> Path:
        return project / ".codex" / "skills" / "mandala"

    def global_skill_dirs(self, env: Dict[str, str]) -> List[Path]:
        home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
        return [home / "skills" / "mandala"]

    def turn_argv(self, project: Path, session_id: Optional[str]) -> List[str]:
        model = ["-m", self.model] if self.model else []
        common = ["--json", "--ignore-user-config", "--skip-git-repo-check"]
        if session_id is None:
            return [self.executable, "exec", *common, "-s", "workspace-write", "-C", str(project), *model, "-"]
        return [self.executable, "exec", "resume", *common, "-c", 'sandbox_mode="workspace-write"', *model, session_id, "-"]

    def parse(self, lines: List[str], turn: int) -> TurnResult:
        result = TurnResult()
        result.raw_lines = lines
        started: Dict[str, dict] = {}
        completed = set()
        turn_done = False
        last_error: Optional[str] = None
        for _, event in _json_lines(lines):
            if event is None:
                continue
            kind = event.get("type")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if kind == "thread.started":
                result.session_id = event.get("thread_id")
            elif kind == "turn.completed":
                turn_done = True
            elif kind == "turn.failed":
                error = event.get("error")
                result.environment_error = "turn.failed: " + str(error.get("message") if isinstance(error, dict) else error)[:300]
            elif kind == "error":
                last_error = "error: " + str(event.get("message"))[:300]
            elif kind in ("item.started", "item.completed") and item.get("type") == "command_execution":
                item_id = str(item.get("id"))
                if kind == "item.started":
                    started[item_id] = item
                    continue
                completed.add(item_id)
                command = item.get("command")
                exit_code = item.get("exit_code")
                result.events.append({
                    "turn": turn, "actor": "agent", "kind": "command",
                    "command": command if isinstance(command, str) else None,
                    "exit_code": exit_code if isinstance(exit_code, int) else None,
                    "output": item.get("aggregated_output") if isinstance(item.get("aggregated_output"), str) else None,
                })
            elif kind == "item.completed" and item.get("type") == "agent_message":
                result.final_text = item.get("text") if isinstance(item.get("text"), str) else result.final_text
                result.events.append({"turn": turn, "actor": "agent", "kind": "message", "text": result.final_text})
            # Unknown event and item types are preserved only in the raw trace.
        for item_id, item in started.items():
            if item_id not in completed:  # started but never completed: execution evidence incomplete
                result.events.append({"turn": turn, "actor": "agent", "kind": "command", "command": item.get("command"), "exit_code": None, "output": None, "incomplete": True})
        if not turn_done and not result.environment_error:
            # Transient top-level errors (for example reconnects) only matter when the turn never completed.
            result.environment_error = last_error or "Codex turn did not complete"
        result.model = self.model
        return result


class ClaudeAdapter:
    name = "claude"
    executable = "claude"
    required_help = {"main": ("--print", "--output-format", "stream-json", "--verbose", "--resume", "--permission-mode", "dontAsk", "--allowedTools", "--setting-sources", "--strict-mcp-config", "--append-system-prompt")}
    # Mandala plus read-only shell helpers that agents commonly chain with it; nothing that writes files.
    allowed_tools = ["Bash(mandala *)", "Bash(echo *)", "Bash(pwd)", "Bash(ls *)", "Bash(cat *)", "Bash(head *)",
                     "Bash(tail *)", "Bash(wc *)", "Bash(jq *)", "Read", "Glob", "Grep", "Skill"]
    # system/init lists the loaded skills, so project-local loading is verified per turn.
    skill_load_observable = True
    # dontAsk never approves commands with shell variables, command substitution, or pipes into
    # interpreters, even under Bash(mandala *). Saying so up front avoids denials that would make a
    # case INCONCLUSIVE. It describes the harness environment only, not Mandala behavior.
    environment_note = (
        "This non-interactive session runs under a restricted permission policy. Shell commands are "
        "approved only with literal arguments: do not use shell variables such as $PWD, command "
        "substitution, or pipes into interpreters. The current working directory is the project root."
    )

    def __init__(self, model: Optional[str] = None):
        self.model = model

    def help_commands(self) -> Dict[str, List[str]]:
        return {"main": [self.executable, "--help"]}

    def skill_dir(self, project: Path) -> Path:
        return project / ".claude" / "skills" / "mandala"

    def global_skill_dirs(self, env: Dict[str, str]) -> List[Path]:
        return []  # --setting-sources project excludes user-level skills (verified via system/init skills)

    def turn_argv(self, project: Path, session_id: Optional[str]) -> List[str]:
        argv = [self.executable, "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project",
                "--strict-mcp-config", "--permission-mode", "dontAsk", "--append-system-prompt", self.environment_note,
                "--allowedTools", *self.allowed_tools]
        if self.model:
            argv += ["--model", self.model]
        if session_id is not None:
            argv += ["--resume", session_id]
        return argv

    def parse(self, lines: List[str], turn: int) -> TurnResult:
        result = TurnResult()
        result.raw_lines = lines
        pending: Dict[str, dict] = {}
        denied = set()
        finished = False
        for _, event in _json_lines(lines):
            if event is None:
                continue
            kind, subtype = event.get("type"), event.get("subtype")
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            if kind == "system" and subtype == "init":
                result.session_id = event.get("session_id")
                result.model = event.get("model") if isinstance(event.get("model"), str) else None
                skills = event.get("skills")
                result.skills = [str(skill) for skill in skills] if isinstance(skills, list) else None
            elif kind == "system" and subtype == "permission_denied":
                denied.add(str(event.get("tool_use_id")))
            elif kind == "assistant":
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                        if block.get("name") == "Bash":
                            pending[str(block.get("id"))] = {"turn": turn, "actor": "agent", "kind": "command", "command": tool_input.get("command") if isinstance(tool_input.get("command"), str) else None, "exit_code": None, "output": None, "incomplete": True}
                            result.events.append(pending[str(block.get("id"))])
                        else:
                            result.events.append({"turn": turn, "actor": "agent", "kind": "tool", "tool": block.get("name"), "input": tool_input})
                    elif block.get("type") == "text" and isinstance(block.get("text"), str):
                        result.events.append({"turn": turn, "actor": "agent", "kind": "message", "text": block["text"]})
            elif kind == "user":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    event_ref = pending.get(str(block.get("tool_use_id")))
                    if event_ref is None:
                        continue
                    text = _result_text(block.get("content"))
                    event_ref.pop("incomplete", None)
                    if str(block.get("tool_use_id")) in denied:
                        event_ref["kind"] = "command_denied"
                    elif block.get("is_error") is False:
                        event_ref["exit_code"], event_ref["output"] = 0, text
                    elif block.get("is_error") is True:
                        match = EXIT_CODE_PREFIX.match(text or "")
                        if match:
                            event_ref["exit_code"] = int(match.group(1))
                            event_ref["output"] = (text or "")[match.end():].lstrip("\n")
                        else:
                            event_ref["kind"] = "command_denied" if "ermission" in (text or "") else "command"
                            event_ref["output"] = text
            elif kind == "result":
                finished = True
                if isinstance(event.get("result"), str):
                    result.final_text = event["result"]
                if event.get("is_error") or subtype not in (None, "success"):
                    result.environment_error = f"result {subtype}: {str(event.get('result'))[:300]}"
        if not finished and not result.environment_error:
            result.environment_error = "Claude Code turn produced no result event"
        return result


def _result_text(content: object) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


ADAPTERS = {"codex": CodexAdapter, "claude": ClaudeAdapter}
