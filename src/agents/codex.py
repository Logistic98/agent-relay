from __future__ import annotations

import json
from typing import Any

from agents.base import EventCallback
from agents.common import OutputAccumulator, is_execute_phase, request_prompt, safe_structure, safe_text
from core.config import Settings
from core.exceptions import AgentUnavailableError
from core.security import sanitized_subprocess_env
from domain.models import AgentEvent, AgentKind, AgentRequest, AgentResult, EventKind, PermissionMode
from runtime.process import ProcessRunner


class CodexEventParser:
    def __init__(self, output_limit_bytes: int = 2_000_000) -> None:
        self.native_session_id: str | None = None
        self.output = OutputAccumulator(output_limit_bytes)
        self._malformed_reported = False
        self._reported_error = False

    @property
    def protocol_error(self) -> bool:
        return self._malformed_reported

    @property
    def reported_error(self) -> bool:
        return self._reported_error

    def feed(self, line: str) -> list[AgentEvent]:
        if not line.strip():
            return []
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return self._malformed_event()
        if not isinstance(message, dict):
            return self._malformed_event()

        event_type = message.get("type")
        if event_type == "thread.started":
            session_id = message.get("thread_id")
            if isinstance(session_id, str) and session_id:
                self.native_session_id = session_id
            return [
                AgentEvent(
                    EventKind.AGENT_STARTED,
                    {"agent": AgentKind.CODEX.value, "session_id": self.native_session_id},
                )
            ]
        if event_type == "turn.started":
            return [AgentEvent(EventKind.AGENT_STATUS, {"status": "working"})]
        if event_type in {"item.started", "item.completed"}:
            return self._item_event(message, completed=event_type == "item.completed")
        if event_type == "turn.completed":
            usage = message.get("usage")
            return [AgentEvent(EventKind.USAGE, safe_structure(usage))] if isinstance(usage, dict) else []
        if event_type in {"turn.failed", "error"}:
            self._reported_error = True
            error = message.get("error") or message.get("message")
            if isinstance(error, dict):
                error = error.get("message") or error.get("code") or error
            return [
                AgentEvent(
                    EventKind.AGENT_STATUS,
                    {"status": "error", "message": safe_text(error or "Codex execution failed")},
                )
            ]
        return []

    def _item_event(self, message: dict[str, Any], *, completed: bool) -> list[AgentEvent]:
        item = message.get("item")
        if not isinstance(item, dict):
            return self._malformed_event()
        item_type = item.get("type")
        item_id = safe_text(item.get("id", ""), limit=256)

        if item_type == "agent_message":
            if not completed:
                return []
            text = item.get("text")
            if not isinstance(text, str) or not text:
                return []
            text = safe_text(text, limit=200_000)
            self.output.append(text)
            return [AgentEvent(EventKind.OUTPUT_DELTA, {"text": text})]

        if item_type == "reasoning":
            # Codex reasoning text is intentionally never forwarded or retained.
            status = "thinking" if not completed else "working"
            return [AgentEvent(EventKind.AGENT_STATUS, {"status": status})]

        if item_type == "command_execution":
            payload: dict[str, Any] = {
                "tool": "command_execution",
                "tool_call_id": item_id,
                "command": safe_text(item.get("command", "")),
                "status": safe_text(item.get("status", ""), limit=128),
            }
            if completed:
                payload["exit_code"] = item.get("exit_code")
                output = item.get("aggregated_output") or item.get("output")
                if output:
                    payload["output"] = safe_text(output)
            return [AgentEvent(EventKind.TOOL_COMPLETED if completed else EventKind.TOOL_STARTED, payload)]

        if item_type == "file_change":
            payload = {
                "tool": "file_change",
                "tool_call_id": item_id,
                "changes": safe_structure(item.get("changes", [])),
                "status": safe_text(item.get("status", ""), limit=128),
            }
            return [AgentEvent(EventKind.TOOL_COMPLETED if completed else EventKind.TOOL_STARTED, payload)]

        if item_type in {"mcp", "mcp_tool_call"}:
            payload = {
                "tool": "mcp",
                "tool_call_id": item_id,
                "server": safe_text(item.get("server", ""), limit=256),
                "name": safe_text(item.get("tool") or item.get("name") or "", limit=256),
                "arguments": safe_structure(item.get("arguments") or item.get("input") or {}),
                "status": safe_text(item.get("status", ""), limit=128),
            }
            if completed:
                if item.get("result") is not None:
                    payload["result"] = safe_structure(item["result"])
                if item.get("error") is not None:
                    payload["error"] = safe_structure(item["error"])
            return [AgentEvent(EventKind.TOOL_COMPLETED if completed else EventKind.TOOL_STARTED, payload)]

        if item_type in {"web_search", "todo_list"}:
            payload = {
                "tool": safe_text(item_type, limit=128),
                "tool_call_id": item_id,
                "details": safe_structure({key: value for key, value in item.items() if key not in {"type", "id"}}),
            }
            return [AgentEvent(EventKind.TOOL_COMPLETED if completed else EventKind.TOOL_STARTED, payload)]
        return []

    def _malformed_event(self) -> list[AgentEvent]:
        if self._malformed_reported:
            return []
        self._malformed_reported = True
        return [
            AgentEvent(
                EventKind.AGENT_STATUS,
                {"status": "warning", "message": "Ignored malformed JSON event from Codex CLI"},
            )
        ]


class CodexAdapter:
    kind = AgentKind.CODEX

    def __init__(self, settings: Settings, runner: ProcessRunner) -> None:
        self.settings = settings
        self.runner = runner

    def build_argv(self, request: AgentRequest) -> list[str]:
        sandbox = "read-only"
        if is_execute_phase(request.phase):
            sandbox = (
                "danger-full-access" if request.permission_mode is PermissionMode.FULL_ACCESS else "workspace-write"
            )
        argv = [
            self.settings.codex_executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--cd",
            request.workspace,
            "--skip-git-repo-check",
            "--config",
            f'model_reasoning_effort="{request.reasoning_effort or self.settings.codex_reasoning_effort}"',
            "--config",
            'approval_policy="never"',
            "--config",
            "mcp_servers={}",
        ]
        model = request.model or self.settings.codex_model
        if model:
            argv.extend(["--model", model])
        if request.native_session_id:
            # Exec-level flags must precede the resume subcommand.
            argv.extend(["resume", request.native_session_id, "-"])
        else:
            argv.append("-")
        return argv

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        parser = CodexEventParser(self.settings.subprocess_output_limit_bytes)

        async def consume(line: str) -> None:
            for event in parser.feed(line):
                await on_event(event)

        try:
            result = await self.runner.run(
                request.run_id,
                self.build_argv(request),
                cwd=request.workspace,
                stdin=request_prompt(request.prompt, request.handoff_context),
                env=sanitized_subprocess_env(self.settings.agent_env_allowlist),
                timeout=self.settings.run_timeout_seconds,
                on_stdout_line=consume,
            )
        except FileNotFoundError as exc:
            raise AgentUnavailableError(f"Codex executable not found: {self.settings.codex_executable}") from exc

        if result.output_truncated:
            await on_event(
                AgentEvent(
                    EventKind.AGENT_STATUS,
                    {"status": "warning", "message": "Local CLI diagnostic capture reached its configured limit"},
                )
            )
        return AgentResult(
            exit_code=result.exit_code,
            native_session_id=parser.native_session_id or request.native_session_id,
            output=parser.output.text,
            stderr=result.stderr,
            cancelled=result.cancelled,
            timed_out=result.timed_out,
            protocol_error=parser.protocol_error or result.output_truncated,
            reported_error=parser.reported_error,
        )

    async def cancel(self, run_id: str) -> bool:
        return await self.runner.cancel(run_id)
