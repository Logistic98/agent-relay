from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from agents.base import EventCallback
from agents.common import OutputAccumulator, is_execute_phase, request_prompt, safe_structure, safe_text
from core.config import Settings
from core.exceptions import AgentUnavailableError
from core.security import sanitized_subprocess_env
from domain.models import AgentEvent, AgentKind, AgentRequest, AgentResult, EventKind, PermissionMode
from runtime.process import ProcessRunner

_CLAUDE_SESSION_NAMESPACE = uuid.UUID("fcb157a2-829f-5e13-9c7f-847bd129e5e5")


@dataclass(slots=True)
class _StreamingTool:
    tool_call_id: str
    name: str
    input_fragments: list[str] = field(default_factory=list)


class ClaudeEventParser:
    def __init__(self, output_limit_bytes: int = 2_000_000) -> None:
        self.native_session_id: str | None = None
        self.output = OutputAccumulator(output_limit_bytes)
        self._malformed_reported = False
        self._reported_error = False
        self._saw_streamed_text = False
        self._has_output = False
        self._thinking = False
        self._streaming_tools: dict[int, _StreamingTool] = {}
        self._started_tools: set[str] = set()
        self._completed_tools: set[str] = set()

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

        session_id = message.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.native_session_id = session_id
        message_type = message.get("type")
        if message_type == "system" and message.get("subtype") == "init":
            return [
                AgentEvent(
                    EventKind.AGENT_STARTED,
                    {
                        "agent": AgentKind.CLAUDE.value,
                        "session_id": self.native_session_id,
                        "model": safe_text(message.get("model", ""), limit=256),
                        "permission_mode": safe_text(message.get("permissionMode", ""), limit=128),
                    },
                )
            ]
        if message_type == "stream_event":
            event = message.get("event")
            return self._stream_event(event) if isinstance(event, dict) else self._malformed_event()
        if message_type == "assistant":
            return self._assistant_event(message)
        if message_type == "user":
            return self._tool_results(message)
        if message_type == "tool_use":
            return self._direct_tool_use(message)
        if message_type == "tool_result":
            return self._direct_tool_result(message)
        if message_type == "result":
            return self._result_event(message)
        if isinstance(message.get("usage"), dict):
            return [AgentEvent(EventKind.USAGE, safe_structure(message["usage"]))]
        return []

    def _stream_event(self, event: dict[str, Any]) -> list[AgentEvent]:
        event_type = event.get("type")
        if event_type == "content_block_start":
            block = event.get("content_block")
            index = event.get("index")
            if isinstance(index, int) and isinstance(block, dict) and block.get("type") == "tool_use":
                tool_id = safe_text(block.get("id", ""), limit=256)
                self._streaming_tools[index] = _StreamingTool(
                    tool_call_id=tool_id,
                    name=safe_text(block.get("name", ""), limit=256),
                )
            return []
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return []
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if not isinstance(text, str) or not text:
                    return []
                text = safe_text(text, limit=200_000)
                self._saw_streamed_text = True
                self._has_output = True
                self._thinking = False
                self.output.append(text)
                return [AgentEvent(EventKind.OUTPUT_DELTA, {"text": text})]
            if delta_type == "thinking_delta":
                if self._thinking:
                    return []
                self._thinking = True
                return [AgentEvent(EventKind.AGENT_STATUS, {"status": "thinking"})]
            if delta_type == "input_json_delta":
                index = event.get("index")
                partial_json = delta.get("partial_json")
                tool = self._streaming_tools.get(index) if isinstance(index, int) else None
                if tool is not None and isinstance(partial_json, str):
                    current_size = sum(len(part) for part in tool.input_fragments)
                    if current_size < 32_000:
                        tool.input_fragments.append(partial_json[: 32_000 - current_size])
                return []
            return []
        if event_type == "content_block_stop":
            index = event.get("index")
            tool = self._streaming_tools.pop(index, None) if isinstance(index, int) else None
            return self._start_streaming_tool(tool) if tool is not None else []
        if event_type == "message_delta" and isinstance(event.get("usage"), dict):
            return [AgentEvent(EventKind.USAGE, safe_structure(event["usage"]))]
        return []

    def _start_streaming_tool(self, tool: _StreamingTool) -> list[AgentEvent]:
        if not tool.tool_call_id or tool.tool_call_id in self._started_tools:
            return []
        self._started_tools.add(tool.tool_call_id)
        tool_input: Any = {}
        if tool.input_fragments:
            try:
                tool_input = json.loads("".join(tool.input_fragments))
            except json.JSONDecodeError:
                tool_input = "[malformed tool input omitted]"
        return [
            AgentEvent(
                EventKind.TOOL_STARTED,
                {
                    "tool": tool.name,
                    "tool_call_id": tool.tool_call_id,
                    "input": safe_structure(tool_input),
                },
            )
        ]

    def _assistant_event(self, message: dict[str, Any]) -> list[AgentEvent]:
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else message.get("content")
        if not isinstance(content, list):
            return []
        events: list[AgentEvent] = []
        for block in content[:100]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_id = safe_text(block.get("id", ""), limit=256)
                if tool_id and tool_id not in self._started_tools:
                    self._started_tools.add(tool_id)
                    events.append(
                        AgentEvent(
                            EventKind.TOOL_STARTED,
                            {
                                "tool": safe_text(block.get("name", ""), limit=256),
                                "tool_call_id": tool_id,
                                "input": safe_structure(block.get("input", {})),
                            },
                        )
                    )
            elif block.get("type") == "text" and not self._saw_streamed_text:
                text = block.get("text")
                if isinstance(text, str) and text:
                    text = safe_text(text, limit=200_000)
                    self._has_output = True
                    self.output.append(text)
                    events.append(AgentEvent(EventKind.OUTPUT_DELTA, {"text": text}))
        return events

    def _tool_results(self, message: dict[str, Any]) -> list[AgentEvent]:
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else message.get("content")
        if not isinstance(content, list):
            return []
        events: list[AgentEvent] = []
        for block in content[:100]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.extend(self._direct_tool_result(block))
        return events

    def _direct_tool_use(self, message: dict[str, Any]) -> list[AgentEvent]:
        tool_id = safe_text(message.get("id") or message.get("tool_use_id") or "", limit=256)
        if not tool_id or tool_id in self._started_tools:
            return []
        self._started_tools.add(tool_id)
        return [
            AgentEvent(
                EventKind.TOOL_STARTED,
                {
                    "tool": safe_text(message.get("name", ""), limit=256),
                    "tool_call_id": tool_id,
                    "input": safe_structure(message.get("input", {})),
                },
            )
        ]

    def _direct_tool_result(self, message: dict[str, Any]) -> list[AgentEvent]:
        tool_id = safe_text(message.get("tool_use_id") or message.get("id") or "", limit=256)
        if not tool_id or tool_id in self._completed_tools:
            return []
        self._completed_tools.add(tool_id)
        return [
            AgentEvent(
                EventKind.TOOL_COMPLETED,
                {
                    "tool_call_id": tool_id,
                    "is_error": bool(message.get("is_error", False)),
                    "result": safe_structure(message.get("content") or message.get("result") or ""),
                },
            )
        ]

    def _result_event(self, message: dict[str, Any]) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        result_text = message.get("result")
        if not self._has_output and isinstance(result_text, str) and result_text:
            result_text = safe_text(result_text, limit=200_000)
            self._has_output = True
            self.output.append(result_text)
            events.append(AgentEvent(EventKind.OUTPUT_DELTA, {"text": result_text}))
        usage = message.get("usage")
        if isinstance(usage, dict):
            payload = safe_structure(usage)
            if message.get("total_cost_usd") is not None:
                payload["total_cost_usd"] = safe_structure(message["total_cost_usd"])
            events.append(AgentEvent(EventKind.USAGE, payload))
        if message.get("is_error"):
            self._reported_error = True
            events.append(
                AgentEvent(
                    EventKind.AGENT_STATUS,
                    {
                        "status": "error",
                        "message": safe_text(result_text or message.get("subtype") or "Claude execution failed"),
                    },
                )
            )
        return events

    def _malformed_event(self) -> list[AgentEvent]:
        if self._malformed_reported:
            return []
        self._malformed_reported = True
        return [
            AgentEvent(
                EventKind.AGENT_STATUS,
                {"status": "warning", "message": "Ignored malformed JSON event from Claude CLI"},
            )
        ]


class ClaudeAdapter:
    kind = AgentKind.CLAUDE

    def __init__(self, settings: Settings, runner: ProcessRunner) -> None:
        self.settings = settings
        self.runner = runner

    @staticmethod
    def session_id_for_run(run_id: str) -> str:
        return str(uuid.uuid5(_CLAUDE_SESSION_NAMESPACE, f"agent-relay:claude:{run_id}"))

    def build_argv(self, request: AgentRequest) -> list[str]:
        execute = is_execute_phase(request.phase)
        permission_mode = "plan"
        if execute:
            permission_mode = (
                "bypassPermissions" if request.permission_mode is PermissionMode.FULL_ACCESS else "dontAsk"
            )
        argv = [
            self.settings.claude_executable,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--setting-sources",
            "",
            "--permission-mode",
            permission_mode,
        ]
        if execute and request.permission_mode is not PermissionMode.FULL_ACCESS:
            settings_json = json.dumps(
                {"permissions": {"allow": self.settings.claude_allowed_tools}},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            argv.extend(["--settings", settings_json])
        if execute and request.permission_mode is PermissionMode.FULL_ACCESS:
            argv.append("--dangerously-skip-permissions")
        model = request.model or self.settings.claude_model
        if model:
            argv.extend(["--model", model])
        if request.reasoning_effort:
            argv.extend(["--effort", request.reasoning_effort])
        if request.native_session_id:
            argv.extend(["--resume", request.native_session_id])
        else:
            argv.extend(["--session-id", self.session_id_for_run(request.run_id)])
        return argv

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        parser = ClaudeEventParser(self.settings.subprocess_output_limit_bytes)

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
            raise AgentUnavailableError(f"Claude executable not found: {self.settings.claude_executable}") from exc

        if result.output_truncated:
            await on_event(
                AgentEvent(
                    EventKind.AGENT_STATUS,
                    {"status": "warning", "message": "Local CLI diagnostic capture reached its configured limit"},
                )
            )
        return AgentResult(
            exit_code=result.exit_code,
            native_session_id=(
                parser.native_session_id or request.native_session_id or self.session_id_for_run(request.run_id)
            ),
            output=parser.output.text,
            stderr=result.stderr,
            cancelled=result.cancelled,
            timed_out=result.timed_out,
            protocol_error=parser.protocol_error or result.output_truncated,
            reported_error=parser.reported_error,
        )

    async def cancel(self, run_id: str) -> bool:
        return await self.runner.cancel(run_id)
