from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agents import (
    AgentRegistry,
    ClaudeAdapter,
    ClaudeEventParser,
    CodexAdapter,
    CodexEventParser,
    create_agent_registry,
)
from core.config import Settings
from core.exceptions import AgentUnavailableError
from domain.models import AgentEvent, AgentKind, AgentRequest, EventKind, PermissionMode
from runtime.process import ProcessResult


class FakeRunner:
    def __init__(self, lines: list[str] | None = None, result: ProcessResult | None = None) -> None:
        self.lines = lines or []
        self.result = result or ProcessResult(exit_code=0, stdout="", stderr="")
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def run(self, run_id: str, argv: list[str], **kwargs: Any) -> ProcessResult:
        self.calls.append({"run_id": run_id, "argv": argv, **kwargs})
        callback: Callable[[str], Any] | None = kwargs.get("on_stdout_line")
        if callback is not None:
            for line in self.lines:
                await callback(line)
        return self.result

    async def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        codex_executable="fake-codex",
        codex_model="gpt-test",
        codex_reasoning_effort="high",
        claude_executable="fake-claude",
        claude_model="claude-test",
        claude_allowed_tools=["Read", "Glob", "Grep", "Edit", "Bash(git status)"],
        agent_env_allowlist=["PATH"],
        run_timeout_seconds=12,
        subprocess_output_limit_bytes=65_536,
    )


def _request(tmp_path: Path, **changes: Any) -> AgentRequest:
    values: dict[str, Any] = {
        "run_id": "run-123",
        "workspace": str(tmp_path),
        "prompt": "inspect the project",
        "phase": "plan",
    }
    values.update(changes)
    return AgentRequest(**values)


def test_codex_argv_uses_stable_json_exec_and_places_flags_before_resume(settings: Settings, tmp_path: Path) -> None:
    adapter = CodexAdapter(settings, FakeRunner())  # type: ignore[arg-type]
    request = _request(tmp_path, native_session_id="thread-456")

    argv = adapter.build_argv(request)

    assert argv[:3] == ["fake-codex", "exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert 'model_reasoning_effort="high"' in argv
    assert "mcp_servers={}" in argv
    assert argv[argv.index("--model") : argv.index("--model") + 2] == ["--model", "gpt-test"]
    assert argv[-3:] == ["resume", "thread-456", "-"]
    assert argv.index("--json") < argv.index("resume")
    assert argv.index("--sandbox") < argv.index("resume")

    overridden = adapter.build_argv(_request(tmp_path, model="gpt-override", reasoning_effort="max"))
    assert 'model_reasoning_effort="max"' in overridden
    assert overridden[overridden.index("--model") + 1] == "gpt-override"

    execute_argv = adapter.build_argv(_request(tmp_path, phase="execute"))
    assert execute_argv[execute_argv.index("--sandbox") + 1] == "workspace-write"

    full_access_argv = adapter.build_argv(
        _request(tmp_path, phase="execute", permission_mode=PermissionMode.FULL_ACCESS)
    )
    assert full_access_argv[full_access_argv.index("--sandbox") + 1] == "danger-full-access"


def test_registry_pins_checked_executables_to_canonical_targets(tmp_path: Path) -> None:
    codex_target = tmp_path / "codex-target"
    claude_target = tmp_path / "claude-target"
    codex_target.touch(mode=0o700)
    claude_target.touch(mode=0o700)
    codex_link = tmp_path / "codex"
    claude_link = tmp_path / "claude"
    codex_link.symlink_to(codex_target)
    claude_link.symlink_to(claude_target)
    configured = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        codex_executable=str(codex_link),
        claude_executable=str(claude_link),
    )

    registry = create_agent_registry(configured, FakeRunner())  # type: ignore[arg-type]

    codex = registry.get(AgentKind.CODEX)
    claude = registry.get(AgentKind.CLAUDE)
    assert isinstance(codex, CodexAdapter)
    assert isinstance(claude, ClaudeAdapter)
    assert codex.settings.codex_executable == str(codex_target.resolve())
    assert claude.settings.claude_executable == str(claude_target.resolve())

    replacement = tmp_path / "replacement"
    replacement.touch(mode=0o700)
    codex_link.unlink()
    codex_link.symlink_to(replacement)

    assert codex.build_argv(_request(tmp_path))[0] == str(codex_target.resolve())


def test_codex_parser_maps_items_usage_and_never_exposes_reasoning() -> None:
    parser = CodexEventParser()
    lines = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "r1", "type": "reasoning", "text": "private chain"}},
        {"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "private chain"}},
        {
            "type": "item.started",
            "item": {"id": "c1", "type": "command_execution", "command": "git status", "status": "in_progress"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "git status",
                "status": "completed",
                "aggregated_output": "clean",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "f1",
                "type": "file_change",
                "changes": [{"path": "README.md", "kind": "update"}],
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "m1",
                "type": "mcp_tool_call",
                "server": "docs",
                "tool": "search",
                "arguments": {"q": "api"},
                "result": {"ok": True},
            },
        },
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "Done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
    ]
    events = [event for line in lines for event in parser.feed(json.dumps(line))]

    assert parser.native_session_id == "thread-1"
    assert parser.output.text == "Done"
    assert [event.kind for event in events].count(EventKind.TOOL_STARTED) == 1
    assert [event.kind for event in events].count(EventKind.TOOL_COMPLETED) == 3
    assert any(event.kind == EventKind.USAGE and event.payload["input_tokens"] == 10 for event in events)
    serialized = json.dumps([event.payload for event in events])
    assert "private chain" not in serialized
    assert "reasoning" not in serialized


@pytest.mark.asyncio
async def test_codex_adapter_streams_events_uses_stdin_and_returns_result(settings: Settings, tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
            "broken-json with sk-proj-should-not-be-reflected",
            json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "ok"}}),
        ],
        ProcessResult(exit_code=0, stdout="raw", stderr="warning"),
    )
    adapter = CodexAdapter(settings, runner)  # type: ignore[arg-type]
    events: list[AgentEvent] = []

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    result = await adapter.run(
        _request(tmp_path, handoff_context="previous context"),
        collect,
    )

    assert result.output == "ok"
    assert result.native_session_id == "thread-2"
    assert result.protocol_error
    assert runner.calls[0]["stdin"].endswith("<current_request>\ninspect the project\n</current_request>")
    assert "inspect the project" not in runner.calls[0]["argv"]
    warnings = [event for event in events if event.payload.get("status") == "warning"]
    assert len(warnings) == 1
    assert "sk-proj" not in json.dumps(warnings[0].payload)
    assert await adapter.cancel("run-123")
    assert runner.cancelled == ["run-123"]


def test_claude_argv_uses_plan_or_dont_ask_settings_and_predictable_session(settings: Settings, tmp_path: Path) -> None:
    adapter = ClaudeAdapter(settings, FakeRunner())  # type: ignore[arg-type]
    request = _request(tmp_path)

    plan_argv = adapter.build_argv(request)

    assert plan_argv[:3] == ["fake-claude", "-p", "--verbose"]
    assert plan_argv[plan_argv.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in plan_argv
    assert plan_argv[plan_argv.index("--permission-mode") + 1] == "plan"
    assert "--settings" not in plan_argv
    session_id = plan_argv[plan_argv.index("--session-id") + 1]
    assert uuid.UUID(session_id).version == 5
    assert session_id == adapter.session_id_for_run(request.run_id)

    overridden = adapter.build_argv(_request(tmp_path, model="claude-opus-5", reasoning_effort="xhigh"))
    assert overridden[overridden.index("--model") + 1] == "claude-opus-5"
    assert overridden[overridden.index("--effort") + 1] == "xhigh"

    execute_argv = adapter.build_argv(_request(tmp_path, phase="execute"))
    assert execute_argv[execute_argv.index("--permission-mode") + 1] == "dontAsk"
    settings_payload = json.loads(execute_argv[execute_argv.index("--settings") + 1])
    assert settings_payload == {"permissions": {"allow": settings.claude_allowed_tools}}

    full_access_argv = adapter.build_argv(
        _request(tmp_path, phase="execute", permission_mode=PermissionMode.FULL_ACCESS)
    )
    assert full_access_argv[full_access_argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in full_access_argv
    assert "--settings" not in full_access_argv

    resume_argv = adapter.build_argv(_request(tmp_path, native_session_id="session-9"))
    assert resume_argv[-2:] == ["--resume", "session-9"]
    assert "--session-id" not in resume_argv


def test_claude_parser_filters_thinking_and_avoids_full_message_duplicates() -> None:
    parser = ClaudeEventParser()
    messages = [
        {"type": "system", "subtype": "init", "session_id": "session-1", "model": "sonnet"},
        {
            "type": "stream_event",
            "session_id": "session-1",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "secret thought"},
            },
        },
        {
            "type": "stream_event",
            "session_id": "session-1",
            "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
        },
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {"content": [{"type": "text", "text": "Hello"}]},
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"command":"git status"}'},
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 2}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "git status"}}]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "clean"}]},
        },
        {
            "type": "result",
            "session_id": "session-1",
            "is_error": False,
            "result": "Hello",
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    ]
    events = [event for message in messages for event in parser.feed(json.dumps(message))]

    assert parser.native_session_id == "session-1"
    assert parser.output.text == "Hello"
    assert [event.kind for event in events].count(EventKind.OUTPUT_DELTA) == 1
    assert [event.kind for event in events].count(EventKind.TOOL_STARTED) == 1
    assert [event.kind for event in events].count(EventKind.TOOL_COMPLETED) == 1
    tool_start = next(event for event in events if event.kind == EventKind.TOOL_STARTED)
    assert tool_start.payload["input"] == {"command": "git status"}
    serialized = json.dumps([event.payload for event in events])
    assert "secret thought" not in serialized
    assert "thinking_delta" not in serialized


@pytest.mark.asyncio
async def test_claude_adapter_returns_predictable_session_when_init_is_missing(
    settings: Settings, tmp_path: Path
) -> None:
    runner = FakeRunner(
        [
            "not-json",
            "also-not-json",
            json.dumps({"type": "result", "is_error": False, "result": "complete", "usage": {"output_tokens": 1}}),
        ]
    )
    adapter = ClaudeAdapter(settings, runner)  # type: ignore[arg-type]
    events: list[AgentEvent] = []

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    result = await adapter.run(_request(tmp_path), collect)

    assert result.output == "complete"
    assert result.native_session_id == adapter.session_id_for_run("run-123")
    assert result.protocol_error
    assert len([event for event in events if event.payload.get("status") == "warning"]) == 1
    assert "inspect the project" not in runner.calls[0]["argv"]
    assert runner.calls[0]["stdin"] == "inspect the project"


def test_registry_routes_both_agents_and_rejects_missing(settings: Settings) -> None:
    runner = FakeRunner()
    codex = CodexAdapter(settings, runner)  # type: ignore[arg-type]
    claude = ClaudeAdapter(settings, runner)  # type: ignore[arg-type]
    registry = AgentRegistry([codex, claude])

    assert registry.get(AgentKind.CODEX) is codex
    assert registry.get(AgentKind.CLAUDE) is claude
    assert registry.kinds == (AgentKind.CODEX, AgentKind.CLAUDE)
    with pytest.raises(ValueError):
        registry.register(codex)

    empty = AgentRegistry()
    with pytest.raises(AgentUnavailableError):
        empty.get(AgentKind.CODEX)


@pytest.mark.parametrize("adapter_type", [CodexAdapter, ClaudeAdapter])
def test_adapters_fail_closed_on_unknown_phase(adapter_type: type[Any], settings: Settings, tmp_path: Path) -> None:
    adapter = adapter_type(settings, FakeRunner())
    with pytest.raises(ValueError, match="unsupported agent phase"):
        adapter.build_argv(_request(tmp_path, phase="typo"))
