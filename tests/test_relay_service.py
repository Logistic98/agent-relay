from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.base import AgentRegistry, EventCallback
from core.config import Settings
from core.exceptions import ConversationNotFoundError, RunNotFoundError
from domain.models import (
    APPROVAL_PLAN_MAX_CHARS,
    AgentEvent,
    AgentKind,
    AgentRequest,
    AgentResult,
    EventKind,
    PermissionMode,
    Run,
    RunMode,
    RunStatus,
)
from persistence.database import Database
from services.relay import RelayService


class FakeAdapter:
    def __init__(self, kind: AgentKind) -> None:
        self.kind = kind
        self.requests: list[AgentRequest] = []
        self.cancelled: list[str] = []
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.auto_output = '{"kind":"answer","content":"codex-like answer"}'

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        self.requests.append(request)
        await on_event(AgentEvent(EventKind.AGENT_STATUS, {"message": f"{request.phase} in progress"}))
        self.started.set()
        if self.block:
            await self.release.wait()
        output = {
            "ask": f"{self.kind.value} answer",
            "plan": f"{self.kind.value} plan",
            "auto": self.auto_output,
            "execute": f"{self.kind.value} executed",
        }[request.phase]
        await on_event(AgentEvent(EventKind.OUTPUT_DELTA, {"text": output}))
        return AgentResult(
            exit_code=0,
            native_session_id=request.native_session_id or f"{self.kind.value}-session",
            output=output,
            stderr="",
            cancelled=request.run_id in self.cancelled,
        )

    async def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        self.release.set()
        return True


class PreStartCancellableAdapter(FakeAdapter):
    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        self.requests.append(request)
        self.started.set()
        await asyncio.sleep(60)
        return AgentResult(0, "never", "never", "")

    async def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return False


def _settings(tmp_path: Path, **overrides) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        database_path=tmp_path / "relay.db",
        approval_timeout_seconds=overrides.pop("approval_timeout_seconds", 30),
        **overrides,
    )


async def _service(tmp_path: Path, **settings_overrides):  # type: ignore[no-untyped-def]
    codex = FakeAdapter(AgentKind.CODEX)
    claude = FakeAdapter(AgentKind.CLAUDE)
    database = Database(":memory:")
    service = RelayService(
        _settings(tmp_path, **settings_overrides),
        database,
        AgentRegistry([codex, claude]),
    )
    await service.start()
    return service, codex, claude


@pytest.mark.asyncio
async def test_read_only_ask_completes_and_persists_native_session(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "inspect",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        completed = await service.wait_for_terminal(created.id)
        assert completed.status == RunStatus.COMPLETED
        assert completed.result == "codex answer"
        assert await service.database.get_native_session(conversation.id, AgentKind.CODEX) == "codex-session"
        assert codex.requests[0].phase == "ask"
        assert "read-only mode" in codex.requests[0].prompt
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_switching_or_leaving_conversation_does_not_interrupt_active_run(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    codex.block = True
    try:
        conversation_a = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=project_a,
            agent=AgentKind.CODEX,
        )
        conversation_b = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=project_b,
            agent=AgentKind.CODEX,
        )
        await service.set_active_conversation("api", "alice", conversation_a.id)
        created = await service.submit_run(
            conversation_a.id,
            "inspect project a",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await asyncio.wait_for(codex.started.wait(), timeout=1)

        switched = await service.set_active_conversation("api", "alice", conversation_b.id)
        assert switched.id == conversation_b.id
        assert (await service.get_run(created.id)).status == RunStatus.PLANNING
        assert codex.cancelled == []

        await service.set_active_conversation("api", "alice", conversation_a.id)
        await service.clear_active_conversation("api", "alice")
        assert await service.get_active_conversation("api", "alice") is None
        assert (await service.get_run(created.id)).status == RunStatus.PLANNING
        assert codex.cancelled == []

        codex.release.set()
        completed = await service.wait_for_terminal(created.id)
        assert completed.status == RunStatus.COMPLETED
        assert completed.result == "codex answer"
    finally:
        codex.release.set()
        await service.stop()


@pytest.mark.asyncio
async def test_mutating_run_requires_plan_approval_before_execution(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "change code",
            RunMode.RUN,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        waiting = await service.wait_for_terminal(created.id)
        assert waiting.status == RunStatus.AWAITING_APPROVAL
        assert waiting.plan == "codex plan"
        assert [request.phase for request in codex.requests] == ["plan"]

        await service.approve_run(created.id, "alice", owner_type="api", owner_id="alice")
        completed = await service.wait_for_terminal(created.id)
        assert completed.status == RunStatus.COMPLETED
        assert completed.result == "codex executed"
        assert [request.phase for request in codex.requests] == ["plan", "execute"]
        assert "approved" in codex.requests[1].prompt.lower()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_auto_route_answers_read_only_request_without_approval(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="telegram",
            owner_id="chat:user",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "你好",
            RunMode.RUN,
            owner_type="telegram",
            owner_id="chat:user",
            initiator_id="user",
            auto_route=True,
        )

        completed = await service.wait_for_terminal(created.id)

        assert completed.status == RunStatus.COMPLETED
        assert completed.result == "codex-like answer"
        assert completed.auto_route is True
        assert [request.phase for request in codex.requests] == ["auto"]
        assert "exactly one JSON object" in codex.requests[0].prompt
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_auto_route_plan_still_requires_explicit_approval(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    codex.auto_output = '{"kind":"plan","content":"修改 app.py 并运行测试"}'
    try:
        conversation = await service.create_conversation(
            owner_type="telegram",
            owner_id="chat:user",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "修改 app.py",
            RunMode.RUN,
            owner_type="telegram",
            owner_id="chat:user",
            initiator_id="user",
            auto_route=True,
        )

        waiting = await service.wait_for_terminal(created.id)
        assert waiting.status == RunStatus.AWAITING_APPROVAL
        assert waiting.plan == "修改 app.py 并运行测试"
        assert [request.phase for request in codex.requests] == ["auto"]

        await service.approve_run(created.id, "user", owner_type="telegram", owner_id="chat:user")
        completed = await service.wait_for_terminal(created.id)
        assert completed.status == RunStatus.COMPLETED
        assert [request.phase for request in codex.requests] == ["auto", "execute"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_workspace_auto_executes_plan_without_approval_and_persists_policy(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    codex.auto_output = '{"kind":"plan","content":"修改 app.py 并运行测试"}'
    try:
        conversation = await service.create_conversation(
            owner_type="telegram",
            owner_id="chat:user",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "修改 app.py",
            RunMode.RUN,
            owner_type="telegram",
            owner_id="chat:user",
            initiator_id="user",
            auto_route=True,
            permission_mode=PermissionMode.WORKSPACE_AUTO,
        )

        completed = await service.wait_for_terminal(created.id)

        assert completed.status is RunStatus.COMPLETED
        assert completed.permission_mode is PermissionMode.WORKSPACE_AUTO
        assert completed.plan == "修改 app.py 并运行测试"
        assert await service.get_approval(created.id) is None
        assert [request.phase for request in codex.requests] == ["auto", "execute"]
        assert all(request.permission_mode is PermissionMode.WORKSPACE_AUTO for request in codex.requests)
        assert "automatic execution" in codex.requests[-1].prompt
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_auto_route_malformed_output_fails_closed(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    codex.auto_output = "I already changed the files"
    try:
        conversation = await service.create_conversation(
            owner_type="telegram",
            owner_id="chat:user",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "修改 app.py",
            RunMode.RUN,
            owner_type="telegram",
            owner_id="chat:user",
            initiator_id="user",
            auto_route=True,
        )

        failed = await service.wait_for_terminal(created.id)

        assert failed.status == RunStatus.FAILED
        assert "重新发送" in (failed.error or "")
        assert [request.phase for request in codex.requests] == ["auto"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reject_is_idempotently_guarded_and_bound_to_initiator(tmp_path: Path) -> None:
    service, _, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "change",
            RunMode.RUN,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await service.wait_for_terminal(created.id)
        with pytest.raises(RunNotFoundError):
            await service.reject_run(created.id, "mallory", owner_type="api", owner_id="alice")
        await service.reject_run(created.id, "alice", owner_type="api", owner_id="alice")
        assert (await service.get_run(created.id)).status == RunStatus.REJECTED
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancel_stops_active_adapter_and_reaches_cancelled(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    codex.block = True
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        created = await service.submit_run(
            conversation.id,
            "long task",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await asyncio.wait_for(codex.started.wait(), timeout=2)
        requested = await service.cancel_run(created.id, "alice", owner_type="api", owner_id="alice")
        assert requested.status == RunStatus.CANCEL_REQUESTED
        terminal = await service.wait_for_terminal(created.id)
        assert terminal.status == RunStatus.CANCELLED
        assert codex.cancelled == [created.id]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_follow_up_messages_queue_and_run_in_order(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    codex.block = True
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        first = await service.submit_run(
            conversation.id,
            "first",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await asyncio.wait_for(codex.started.wait(), timeout=2)
        second = await service.submit_run(
            conversation.id,
            "second",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        assert (await service.get_run(second.id)).status is RunStatus.QUEUED
        assert [request.prompt.split("\n", 1)[0] for request in codex.requests] == ["first"]

        codex.release.set()
        assert (await service.wait_for_terminal(first.id)).status is RunStatus.COMPLETED
        assert (await service.wait_for_terminal(second.id)).status is RunStatus.COMPLETED
        assert [request.prompt.split("\n", 1)[0] for request in codex.requests] == ["first", "second"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_agent_switch_injects_bounded_cross_agent_handoff(tmp_path: Path) -> None:
    service, codex, claude = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        first = await service.submit_run(
            conversation.id,
            "inspect",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await service.wait_for_terminal(first.id)
        await service.switch_agent(
            conversation.id,
            AgentKind.CLAUDE,
            owner_type="api",
            owner_id="alice",
        )
        second = await service.submit_run(
            conversation.id,
            "continue",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await service.wait_for_terminal(second.id)
        assert claude.requests[0].handoff_context is not None
        assert "inspect" in claude.requests[0].handoff_context
        assert "codex answer" in claude.requests[0].handoff_context

        await service.switch_agent(
            conversation.id,
            AgentKind.CODEX,
            owner_type="api",
            owner_id="alice",
        )
        third = await service.submit_run(
            conversation.id,
            "back to codex",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await service.wait_for_terminal(third.id)
        assert codex.requests[-1].native_session_id == "codex-session"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_event_payload_is_redacted_before_persistence(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)

    async def secret_run(request: AgentRequest, on_event: EventCallback) -> AgentResult:
        await on_event(AgentEvent(EventKind.AGENT_STATUS, {"message": "token=super-secret"}))
        return AgentResult(0, "session", "ok", "")

    codex.run = secret_run  # type: ignore[method-assign]
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        run = await service.submit_run(
            conversation.id,
            "inspect",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await service.wait_for_terminal(run.id)
        events = await service.list_events(run.id)
        assert "super-secret" not in str([event.payload for event in events])
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancel_closes_race_before_adapter_process_registration(tmp_path: Path) -> None:
    adapter = PreStartCancellableAdapter(AgentKind.CODEX)
    service = RelayService(
        _settings(tmp_path),
        Database(":memory:"),
        AgentRegistry([adapter, FakeAdapter(AgentKind.CLAUDE)]),
    )
    await service.start()
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        run = await service.submit_run(
            conversation.id,
            "cancel me",
            RunMode.ASK,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2)
        await service.cancel_run(run.id, "alice", owner_type="api", owner_id="alice")
        terminal = await service.wait_for_terminal(run.id)
        assert terminal.status == RunStatus.CANCELLED
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_pending_approval_survives_service_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database_one = Database(settings.database_path)
    codex_one = FakeAdapter(AgentKind.CODEX)
    first = RelayService(settings, database_one, AgentRegistry([codex_one, FakeAdapter(AgentKind.CLAUDE)]))
    await first.start()
    conversation = await first.create_conversation(
        owner_type="api",
        owner_id="alice",
        workspace=tmp_path,
        agent=AgentKind.CODEX,
    )
    run = await first.submit_run(
        conversation.id,
        "change",
        RunMode.RUN,
        owner_type="api",
        owner_id="alice",
        initiator_id="alice",
    )
    assert (await first.wait_for_terminal(run.id)).status == RunStatus.AWAITING_APPROVAL
    await first.stop()

    codex_two = FakeAdapter(AgentKind.CODEX)
    second = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([codex_two, FakeAdapter(AgentKind.CLAUDE)]),
    )
    await second.start()
    try:
        assert (await second.get_run(run.id)).status == RunStatus.AWAITING_APPROVAL
        await second.approve_run(run.id, "alice", owner_type="api", owner_id="alice")
        assert (await second.wait_for_terminal(run.id)).status == RunStatus.COMPLETED
        assert [request.phase for request in codex_two.requests] == ["execute"]
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_plan_protocol_errors_and_oversized_plans_fail_closed(tmp_path: Path) -> None:
    service, codex, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )

        async def malformed_plan(request: AgentRequest, on_event: EventCallback) -> AgentResult:
            del request, on_event
            return AgentResult(0, "session", "looks valid", "", protocol_error=True)

        codex.run = malformed_plan  # type: ignore[method-assign]
        malformed = await service.submit_run(
            conversation.id,
            "change",
            RunMode.RUN,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        assert (await service.wait_for_terminal(malformed.id)).status is RunStatus.FAILED
        assert await service.get_approval(malformed.id) is None

        async def oversized_plan(request: AgentRequest, on_event: EventCallback) -> AgentResult:
            del request, on_event
            return AgentResult(0, "session", "x" * (APPROVAL_PLAN_MAX_CHARS + 1), "")

        codex.run = oversized_plan  # type: ignore[method-assign]
        oversized = await service.submit_run(
            conversation.id,
            "change again",
            RunMode.RUN,
            owner_type="api",
            owner_id="alice",
            initiator_id="alice",
        )
        final = await service.wait_for_terminal(oversized.id)
        assert final.status is RunStatus.FAILED
        assert "approval display limit" in (final.error or "")
        assert await service.get_approval(oversized.id) is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_run_submission_requires_full_transport_owner(tmp_path: Path) -> None:
    service, _, _ = await _service(tmp_path)
    try:
        conversation = await service.create_conversation(
            owner_type="telegram",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        with pytest.raises(ConversationNotFoundError):
            await service.submit_run(
                conversation.id,
                "inspect",
                RunMode.ASK,
                owner_type="api",
                owner_id="alice",
                initiator_id="alice",
            )
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancel_retries_when_queued_run_concurrently_enters_planning(tmp_path: Path) -> None:
    adapter = PreStartCancellableAdapter(AgentKind.CODEX)
    database = Database(":memory:")
    service = RelayService(
        _settings(tmp_path),
        database,
        AgentRegistry([adapter, FakeAdapter(AgentKind.CLAUDE)]),
    )
    await service.start()
    await service._semaphore.acquire()  # Hold the queued task before its normal transition.
    try:
        conversation = await service.create_conversation(
            owner_type="api",
            owner_id="alice",
            workspace=tmp_path,
            agent=AgentKind.CODEX,
        )
        run = Run(
            id="queued-race",
            conversation_id=conversation.id,
            agent=AgentKind.CODEX,
            mode=RunMode.ASK,
            status=RunStatus.QUEUED,
            prompt="inspect",
            initiator_id="alice",
        )
        await database.create_run(run)
        service._spawn(run.id, service._execute_read_phase(run.id))
        original_transition = database.transition_run_status
        raced = False

        async def transition_with_race(run_id: str, new_status: RunStatus, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal raced
            if not raced and new_status is RunStatus.CANCELLED:
                raced = True
                await original_transition(
                    run_id,
                    RunStatus.PLANNING,
                    expected_statuses={RunStatus.QUEUED},
                )
                return None
            return await original_transition(run_id, new_status, **kwargs)

        database.transition_run_status = transition_with_race  # type: ignore[method-assign]
        await service.cancel_run(run.id, "alice", owner_type="api", owner_id="alice")
        terminal = await service.wait_for_terminal(run.id)

        assert raced
        assert terminal.status is RunStatus.CANCELLED
    finally:
        service._semaphore.release()
        await service.stop()
