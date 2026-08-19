from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agents.base import AgentRegistry
from core.config import Settings
from core.exceptions import (
    ApprovalExpiredError,
    ConversationNotFoundError,
    InvalidStateError,
    RunNotFoundError,
)
from core.security import redact_text, resolve_workspace
from domain.models import (
    ACTIVE_RUN_STATUSES,
    APPROVAL_PLAN_MAX_CHARS,
    TERMINAL_RUN_STATUSES,
    AgentEvent,
    AgentKind,
    AgentRequest,
    AgentResult,
    Approval,
    ApprovalStatus,
    Conversation,
    EventKind,
    PermissionMode,
    Run,
    RunEvent,
    RunMode,
    RunStatus,
)
from persistence.database import Database

logger = logging.getLogger(__name__)


def _is_safe_model_name(value: str) -> bool:
    return all(character.isalnum() or character in {"-", "_", ".", ":"} for character in value)


_TERMINAL_EVENT_KINDS = {
    EventKind.RUN_CANCELLED,
    EventKind.RUN_COMPLETED,
    EventKind.RUN_FAILED,
    EventKind.RUN_TIMED_OUT,
    EventKind.RUN_INTERRUPTED,
}


class RelayService:
    """Durable two-phase orchestration shared by Telegram and HTTP transports."""

    def __init__(self, settings: Settings, database: Database, agents: AgentRegistry) -> None:
        self.settings = settings
        self.database = database
        self.agents = agents
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self._queue_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._expiry_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._telegram_transport_running = not settings.telegram_enabled
        token = settings.telegram_token_value
        self._telegram_bot_key = hashlib.sha256(token.encode()).hexdigest()[:16] if token else "disabled"

    async def start(self) -> None:
        await self.database.initialize()
        recovered = await self.database.recover_active_runs()
        for run in recovered:
            await self._emit(run.id, EventKind.RUN_INTERRUPTED, {"message": "Relay restarted during this run"})
        await self._expire_due_approvals()
        await self._schedule_queued_runs()
        self._expiry_task = asyncio.create_task(self._approval_expiry_loop(), name="approval-expiry")

    async def stop(self) -> None:
        self._stopping = True
        if self._expiry_task:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)
            self._expiry_task = None

        active_tasks = list(self._tasks.items())
        for run_id, task in active_tasks:
            run = await self.database.get_run(run_id)
            if run and run.status in {RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}:
                await self.agents.get(run.agent).cancel(run_id)
            task.cancel()
        if active_tasks:
            await asyncio.gather(*(task for _, task in active_tasks), return_exceptions=True)
        self._tasks.clear()
        await self.database.close()

    async def create_conversation(
        self,
        *,
        owner_type: str,
        owner_id: str,
        workspace: str | Path | None,
        agent: AgentKind,
        title: str | None = None,
    ) -> Conversation:
        general = workspace is None
        resolved = (
            self._prepare_general_workspace()
            if general
            else resolve_workspace(workspace, self.settings.workspace_roots)
        )
        now = time.time()
        conversation = Conversation(
            id=str(uuid.uuid4()),
            owner_type=owner_type,
            owner_id=owner_id,
            workspace=str(resolved),
            active_agent=AgentKind(agent),
            title=(title or "通用对话" if general else title or resolved.name or "workspace")[:120],
            created_at=now,
            updated_at=now,
        )
        created = await self.database.create_conversation(conversation)
        await self.database.set_active_conversation(owner_type, owner_id, created.id)
        return created

    def is_general_workspace(self, workspace: str | Path) -> bool:
        try:
            return Path(workspace).expanduser().resolve(strict=True) == self.settings.general_workspace.resolve(
                strict=True
            )
        except (OSError, RuntimeError):
            return False

    def _prepare_general_workspace(self) -> Path:
        workspace = self.settings.general_workspace
        try:
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise InvalidStateError("无法创建通用对话的隔离工作区") from exc
        return resolve_workspace(workspace, [workspace])

    async def list_conversations(self, owner_type: str, owner_id: str) -> list[Conversation]:
        return await self.database.list_conversations(owner_type=owner_type, owner_id=owner_id)

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await self._get_conversation(conversation_id)

    async def get_active_conversation(self, owner_type: str, owner_id: str) -> Conversation | None:
        return await self.database.get_active_conversation(owner_type, owner_id)

    async def set_active_conversation(self, owner_type: str, owner_id: str, conversation_id: str) -> Conversation:
        conversation = await self.database.set_active_conversation(owner_type, owner_id, conversation_id)
        if conversation is None:
            raise ConversationNotFoundError("会话不存在")
        return conversation

    async def clear_active_conversation(self, owner_type: str, owner_id: str) -> None:
        await self.database.set_active_conversation(owner_type, owner_id, None)

    async def find_owned_conversation(self, owner_type: str, owner_id: str, prefix: str) -> Conversation:
        matches = [item for item in await self.list_conversations(owner_type, owner_id) if item.id.startswith(prefix)]
        if len(matches) != 1:
            raise ConversationNotFoundError("会话短 ID 不存在或不唯一")
        return matches[0]

    async def switch_agent(
        self,
        conversation_id: str,
        agent: AgentKind,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Conversation:
        conversation = await self._get_conversation(conversation_id)
        if conversation.owner_type != owner_type or conversation.owner_id != owner_id:
            raise ConversationNotFoundError("会话不存在")
        return await self.database.switch_conversation_agent_if_idle(
            conversation_id,
            AgentKind(agent),
            owner_type=owner_type,
            owner_id=owner_id,
        )

    async def submit_run(
        self,
        conversation_id: str,
        prompt: str,
        mode: RunMode,
        *,
        owner_type: str,
        owner_id: str,
        initiator_id: str,
        auto_route: bool = False,
        permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Run:
        if self._stopping:
            raise InvalidStateError("Relay 正在停止，不能接收新任务")
        normalized = prompt.strip()
        if not normalized:
            raise InvalidStateError("任务内容不能为空")
        if len(normalized) > self.settings.max_prompt_chars:
            raise InvalidStateError(f"任务内容超过 {self.settings.max_prompt_chars} 字符限制")
        conversation = await self._get_conversation(conversation_id)
        if conversation.owner_type != owner_type or conversation.owner_id != owner_id:
            raise ConversationNotFoundError("会话不存在")
        normalized_model = model.strip() if model else None
        if normalized_model and (len(normalized_model) > 120 or not _is_safe_model_name(normalized_model)):
            raise InvalidStateError("模型名称格式无效")
        normalized_effort = reasoning_effort.strip().lower() if reasoning_effort else None
        allowed_efforts = (
            {"low", "medium", "high", "xhigh", "max", "ultra"}
            if conversation.active_agent is AgentKind.CODEX
            else {"low", "medium", "high", "xhigh", "max"}
        )
        if normalized_effort and normalized_effort not in allowed_efforts:
            raise InvalidStateError(f"{conversation.active_agent.value} 不支持该推理强度")
        if self.is_general_workspace(conversation.workspace):
            mode = RunMode.ASK
            auto_route = False
            permission_mode = PermissionMode.REQUEST_APPROVAL
        run = Run(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            agent=conversation.active_agent,
            mode=RunMode(mode),
            status=RunStatus.QUEUED,
            prompt=normalized,
            initiator_id=initiator_id,
            permission_mode=PermissionMode(permission_mode),
            model=normalized_model,
            reasoning_effort=normalized_effort,
            created_at=time.time(),
            auto_route=auto_route,
        )
        created = await self.database.create_run(run)
        await self._emit(
            created.id,
            EventKind.RUN_QUEUED,
            {
                "agent": created.agent.value,
                "mode": created.mode.value,
                "permission_mode": created.permission_mode.value,
                "model": created.model,
                "reasoning_effort": created.reasoning_effort,
            },
        )
        await self._schedule_queued_runs()
        return created

    async def approve_run(
        self,
        run_id: str,
        actor_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Approval:
        run = await self._get_run(run_id)
        await self._require_run_access(run, owner_type, owner_id, actor_id)
        try:
            decision = await self.database.decide_run_approval(run.id, ApprovalStatus.APPROVED, actor_id)
        except ApprovalExpiredError:
            await self._emit(run.id, EventKind.APPROVAL_DECIDED, {"decision": "expired"})
            raise
        if not decision:
            raise InvalidStateError("任务当前不在等待批准状态，或审批已被处理")
        _, decided = decision
        await self._emit(run.id, EventKind.APPROVAL_DECIDED, {"decision": "approved", "actor": actor_id})
        self._spawn(run.id, self._execute_write_phase(run.id))
        return decided

    async def reject_run(
        self,
        run_id: str,
        actor_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Approval:
        run = await self._get_run(run_id)
        await self._require_run_access(run, owner_type, owner_id, actor_id)
        try:
            decision = await self.database.decide_run_approval(run.id, ApprovalStatus.REJECTED, actor_id)
        except ApprovalExpiredError:
            await self._emit(run.id, EventKind.APPROVAL_DECIDED, {"decision": "expired"})
            raise
        if not decision:
            raise InvalidStateError("任务当前不在等待批准状态，或审批已被处理")
        _, decided = decision
        await self._emit(run.id, EventKind.APPROVAL_DECIDED, {"decision": "rejected", "actor": actor_id})
        await self._schedule_queued_runs()
        return decided

    async def cancel_run(
        self,
        run_id: str,
        actor_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Run:
        run = await self._get_run(run_id)
        await self._require_run_access(run, owner_type, owner_id, actor_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        await self._emit(run.id, EventKind.CANCEL_REQUESTED, {"actor": actor_id})
        while True:
            current = await self._get_run(run.id)
            if current.status in TERMINAL_RUN_STATUSES or current.status == RunStatus.CANCEL_REQUESTED:
                return current
            if current.status == RunStatus.QUEUED:
                cancelled = await self.database.transition_run_status(
                    current.id,
                    RunStatus.CANCELLED,
                    expected_statuses={RunStatus.QUEUED},
                )
                if not cancelled:
                    continue
                task = self._tasks.get(current.id)
                if task:
                    task.cancel()
                await self._emit(current.id, EventKind.RUN_CANCELLED, {"message": "Cancelled before execution"})
                await self._schedule_queued_runs()
                return cancelled
            if current.status == RunStatus.AWAITING_APPROVAL:
                cancelled_decision = await self.database.cancel_awaiting_run(current.id, actor_id)
                if not cancelled_decision:
                    continue
                cancelled, _ = cancelled_decision
                await self._emit(current.id, EventKind.RUN_CANCELLED, {"message": "Cancelled before execution"})
                await self._schedule_queued_runs()
                return cancelled
            requested = await self.database.transition_run_status(
                current.id,
                RunStatus.CANCEL_REQUESTED,
                expected_statuses={RunStatus.PLANNING, RunStatus.RUNNING},
            )
            if not requested:
                continue
            process_cancelled = await self.agents.get(current.agent).cancel(current.id)
            if not process_cancelled:
                task = self._tasks.get(current.id)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                latest = await self._get_run(current.id)
                if latest.status == RunStatus.CANCEL_REQUESTED:
                    await self._finish_cancel(latest)
            return requested

    async def get_run(self, run_id: str) -> Run:
        return await self._get_run(run_id)

    async def get_owned_run(
        self,
        run_id: str,
        actor_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Run:
        run = await self._get_run(run_id)
        await self._require_run_access(run, owner_type, owner_id, actor_id)
        return run

    async def get_active_run(self, conversation_id: str) -> Run | None:
        return await self.database.find_active_run(conversation_id)

    async def list_conversation_runs(
        self,
        conversation_id: str,
        *,
        owner_type: str,
        owner_id: str,
        limit: int = 100,
    ) -> list[Run]:
        conversation = await self._get_conversation(conversation_id)
        if conversation.owner_type != owner_type or conversation.owner_id != owner_id:
            raise ConversationNotFoundError("会话不存在")
        return await self.database.list_runs(conversation_id, limit=limit)

    async def find_owned_run(self, conversation_id: str, actor_id: str, prefix: str | None = None) -> Run:
        if prefix:
            runs = await self.database.list_runs(conversation_id, limit=100)
            matches = [item for item in runs if item.id.startswith(prefix) and item.initiator_id == actor_id]
            if len(matches) != 1:
                raise RunNotFoundError("任务短 ID 不存在或不唯一")
            return matches[0]
        run = await self.database.find_active_run(conversation_id)
        if not run:
            raise RunNotFoundError("当前会话没有进行中的任务")
        self._require_initiator(run, actor_id)
        return run

    async def get_approval(self, run_id: str) -> Approval | None:
        return await self.database.get_approval_for_run(run_id)

    async def claim_telegram_update(self, update_id: int) -> bool:
        return await self.database.claim_telegram_update(update_id, bot_key=self._telegram_bot_key)

    async def list_events(self, run_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[RunEvent]:
        return await self.database.list_events(run_id, after_seq=after_seq, limit=limit)

    async def wait_for_events(self, run_id: str, after_seq: int, *, timeout: float) -> list[RunEvent]:
        events = await self.list_events(run_id, after_seq=after_seq)
        if events:
            return events
        signal = self._signals.setdefault(run_id, asyncio.Event())
        signal.clear()
        events = await self.list_events(run_id, after_seq=after_seq)
        if events:
            return events
        try:
            await asyncio.wait_for(signal.wait(), timeout=timeout)
        except TimeoutError:
            return []
        return await self.list_events(run_id, after_seq=after_seq)

    async def wait_for_terminal(self, run_id: str, *, timeout: float = 30) -> Run:
        deadline = asyncio.get_running_loop().time() + timeout
        seq = 0
        while True:
            run = await self._get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES or run.status == RunStatus.AWAITING_APPROVAL:
                return run
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Run {run_id} did not settle")
            events = await self.wait_for_events(run_id, seq, timeout=min(remaining, 2))
            if events:
                seq = events[-1].seq

    async def readiness(self) -> dict[str, Any]:
        database_ok = await self.database.ping()
        executables = {
            AgentKind.CODEX.value: shutil.which(self.settings.codex_executable),
            AgentKind.CLAUDE.value: shutil.which(self.settings.claude_executable),
        }
        agents_ok = all(executables.values())
        telegram_ok = not self.settings.telegram_enabled or (
            bool(self.settings.telegram_token_value) and self._telegram_transport_running
        )
        api_ok = not self.settings.api_enabled or bool(self.settings.api_token_value)
        return {
            "status": (
                "ready" if database_ok and agents_ok and telegram_ok and api_ok and not self._stopping else "not_ready"
            ),
            "database": "ok" if database_ok else "error",
            "agents": {name: "available" if path else "missing" for name, path in executables.items()},
            "telegram": (
                "running"
                if self.settings.telegram_enabled and telegram_ok
                else "error"
                if self.settings.telegram_enabled
                else "disabled"
            ),
            "api": (
                "configured"
                if self.settings.api_enabled and api_ok
                else "missing_token"
                if self.settings.api_enabled
                else "disabled"
            ),
            "accepting_runs": not self._stopping,
        }

    def set_telegram_transport_running(self, running: bool) -> None:
        self._telegram_transport_running = running

    async def agent_capabilities(self) -> dict[str, object]:
        return {
            "agents": {
                "codex": {
                    "available": shutil.which(self.settings.codex_executable) is not None,
                    "streaming": "state-events",
                    "approval": "read-only-plan-then-workspace-write",
                },
                "claude": {
                    "available": shutil.which(self.settings.claude_executable) is not None,
                    "streaming": "text-delta",
                    "approval": "read-only-plan-then-scoped-tool-execution",
                },
            },
            "cancellation": "SIGINT then SIGTERM then SIGKILL for the isolated process group",
        }

    async def _execute_read_phase(self, run_id: str) -> None:
        try:
            async with self._semaphore:
                run = await self._get_run(run_id)
                if run.status == RunStatus.CANCELLED:
                    return
                if run.status != RunStatus.PLANNING:
                    return
                await self._emit(run.id, EventKind.AGENT_STARTED, {"phase": "read_only", "agent": run.agent.value})
                phase = "auto" if run.auto_route else "ask" if run.mode == RunMode.ASK else "plan"
                result = await self._invoke_agent(run, phase)
                await self._complete_read_phase(run, result)
        except asyncio.CancelledError:
            await self._handle_phase_task_cancel(run_id, "Relay stopped while the read phase was active")
            raise
        except Exception as exc:
            logger.exception("read phase failed", extra={"run_id": run_id})
            if self._stopping:
                await self._mark_interrupted_if_active(run_id, "Relay stopped while the read phase was active")
            else:
                await self._fail_if_active(run_id, exc)

    async def _execute_write_phase(self, run_id: str) -> None:
        try:
            async with self._semaphore:
                run = await self._get_run(run_id)
                if run.status == RunStatus.CANCEL_REQUESTED:
                    await self._finish_cancel(run)
                    return
                if run.status != RunStatus.RUNNING:
                    return
                await self._emit(run.id, EventKind.AGENT_STARTED, {"phase": "execute", "agent": run.agent.value})
                result = await self._invoke_agent(run, "execute")
                await self._complete_write_phase(run, result)
        except asyncio.CancelledError:
            await self._handle_phase_task_cancel(run_id, "Relay stopped while execution was active")
            raise
        except Exception as exc:
            logger.exception("write phase failed", extra={"run_id": run_id})
            if self._stopping:
                await self._mark_interrupted_if_active(run_id, "Relay stopped while execution was active")
            else:
                await self._fail_if_active(run_id, exc)

    async def _invoke_agent(self, run: Run, phase: str) -> AgentResult:
        conversation = await self._get_conversation(run.conversation_id)
        allowed_roots = (
            [self.settings.general_workspace]
            if self.is_general_workspace(conversation.workspace)
            else self.settings.workspace_roots
        )
        resolved_workspace = resolve_workspace(conversation.workspace, allowed_roots)
        if str(resolved_workspace) != conversation.workspace:
            raise InvalidStateError("工作区真实路径在会话创建后发生变化，已拒绝启动 Agent")
        native_session_id = await self.database.get_native_session(conversation.id, run.agent)
        prompt = self._phase_prompt(run, phase, general=self.is_general_workspace(conversation.workspace))
        handoff: str | None = None
        previous = [item for item in await self.database.list_runs(conversation.id, limit=3) if item.id != run.id]
        if phase != "execute" and previous and previous[0].agent != run.agent:
            handoff = await self.database.get_handoff_context(
                conversation.id,
                excluding_agent=run.agent,
                before=run.created_at,
                max_chars=self.settings.handoff_context_chars,
            )
        request = AgentRequest(
            run_id=run.id,
            workspace=str(resolved_workspace),
            prompt=prompt,
            phase=phase,
            permission_mode=run.permission_mode,
            model=run.model,
            reasoning_effort=run.reasoning_effort,
            native_session_id=native_session_id,
            handoff_context=handoff,
        )

        async def on_event(event: AgentEvent) -> None:
            await self._emit(run.id, event.kind, event.payload)

        return await self.agents.get(run.agent).run(request, on_event)

    async def _complete_read_phase(self, run: Run, result: AgentResult) -> None:
        await self._persist_agent_result(run, result)
        current = await self._get_run(run.id)
        if current.status == RunStatus.CANCEL_REQUESTED or result.cancelled:
            await self._finish_cancel(current)
            return
        if result.timed_out:
            await self._finish_timeout(current)
            return
        if result.exit_code != 0 or result.reported_error:
            await self._finish_failure(current, self._result_error(result))
            return
        raw_output = result.output.strip()
        if run.auto_route:
            if result.protocol_error:
                await self._finish_failure(current, "Agent 返回的对话格式不完整，请重新发送一次。")
                return
            routed = _parse_auto_response(raw_output)
            if routed is None:
                await self._finish_failure(current, "Agent 未能正确判断本次请求，请重新发送或使用 /run 明确要求执行。")
                return
            route, raw_output = routed
            if route == "answer":
                await self._complete_answer(run, result, raw_output, phase="auto_answer")
                return

        if run.mode == RunMode.ASK:
            await self._complete_answer(run, result, raw_output, phase="ask")
            return

        if result.protocol_error:
            await self._finish_failure(current, "Agent event protocol was malformed; plan was not approved")
            return
        if not raw_output:
            await self._finish_failure(current, "Agent returned no reviewable plan; write phase was not enabled")
            return
        if not result.native_session_id:
            await self._finish_failure(current, "Agent returned no resumable session; write phase was not enabled")
            return
        output = redact_text(raw_output)
        if len(output) > APPROVAL_PLAN_MAX_CHARS:
            await self._finish_failure(
                current,
                f"Agent plan exceeds the {APPROVAL_PLAN_MAX_CHARS}-character approval display limit; "
                "request a shorter plan",
            )
            return

        if run.permission_mode is not PermissionMode.REQUEST_APPROVAL:
            running = await self.database.transition_run_status(
                run.id,
                RunStatus.RUNNING,
                expected_statuses={RunStatus.PLANNING},
                plan=output,
                exit_code=result.exit_code,
            )
            if running:
                await self._emit(
                    run.id,
                    EventKind.AGENT_STATUS,
                    {
                        "status": "executing",
                        "message": "按当前权限模式自动开始执行",
                        "permission_mode": run.permission_mode.value,
                    },
                )
                self._spawn(run.id, self._execute_write_phase(run.id))
            return

        now = time.time()
        approval = Approval(
            id=str(uuid.uuid4()),
            run_id=run.id,
            status=ApprovalStatus.PENDING,
            requested_at=now,
            expires_at=now + self.settings.approval_timeout_seconds,
        )
        await self.database.publish_run_approval(
            run.id,
            plan=output,
            exit_code=result.exit_code,
            approval=approval,
        )
        await self._emit(
            run.id,
            EventKind.APPROVAL_REQUIRED,
            {"approval_id": approval.id, "expires_at": approval.expires_at},
        )

    async def _complete_answer(self, run: Run, result: AgentResult, raw_output: str, *, phase: str) -> None:
        output = redact_text(raw_output or "Agent completed without a textual response")
        completed = await self.database.transition_run_status(
            run.id,
            RunStatus.COMPLETED,
            expected_statuses={RunStatus.PLANNING},
            result=output,
            exit_code=result.exit_code,
        )
        if completed:
            await self._emit(run.id, EventKind.RUN_COMPLETED, {"phase": phase})

    async def _complete_write_phase(self, run: Run, result: AgentResult) -> None:
        await self._persist_agent_result(run, result)
        current = await self._get_run(run.id)
        if current.status == RunStatus.CANCEL_REQUESTED or result.cancelled:
            await self._finish_cancel(current)
            return
        if result.timed_out:
            await self._finish_timeout(current)
            return
        if result.exit_code != 0 or result.reported_error:
            await self._finish_failure(current, self._result_error(result))
            return
        output = redact_text(result.output.strip() or "Agent completed without a textual response")
        completed = await self.database.transition_run_status(
            run.id,
            RunStatus.COMPLETED,
            expected_statuses={RunStatus.RUNNING},
            result=output,
            exit_code=result.exit_code,
        )
        if completed:
            await self._emit(run.id, EventKind.RUN_COMPLETED, {"phase": "execute"})

    async def _persist_agent_result(self, run: Run, result: AgentResult) -> None:
        native_id = result.native_session_id or run.native_session_id
        await self.database.update_run(run.id, native_session_id=native_id, exit_code=result.exit_code)
        if native_id:
            await self.database.set_native_session(run.conversation_id, run.agent, native_id)

    async def _finish_cancel(self, run: Run) -> None:
        cancelled = await self.database.transition_run_status(
            run.id,
            RunStatus.CANCELLED,
            expected_statuses={RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
        )
        if cancelled:
            await self._emit(run.id, EventKind.RUN_CANCELLED, {"message": "Agent process stopped"})

    async def _finish_timeout(self, run: Run) -> None:
        timed_out = await self.database.transition_run_status(
            run.id,
            RunStatus.TIMED_OUT,
            expected_statuses={RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
            error="Agent run exceeded the configured timeout",
        )
        if timed_out:
            await self._emit(run.id, EventKind.RUN_TIMED_OUT, {"message": "Configured timeout exceeded"})

    async def _finish_failure(self, run: Run, message: str) -> None:
        failed = await self.database.transition_run_status(
            run.id,
            RunStatus.FAILED,
            expected_statuses={RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
            error=message,
        )
        if failed:
            await self._emit(run.id, EventKind.RUN_FAILED, {"message": message})

    async def _fail_if_active(self, run_id: str, exc: Exception) -> None:
        run = await self.database.get_run(run_id)
        if run and run.status in ACTIVE_RUN_STATUSES:
            if run.status == RunStatus.CANCEL_REQUESTED:
                await self._finish_cancel(run)
            else:
                await self._finish_failure(run, redact_text(f"{exc.__class__.__name__}: {exc}", limit=1000))

    async def _mark_interrupted_if_active(self, run_id: str, message: str) -> None:
        run = await self.database.get_run(run_id)
        if not run or run.status not in ACTIVE_RUN_STATUSES:
            return
        interrupted = await self.database.transition_run_status(
            run.id,
            RunStatus.INTERRUPTED,
            expected_statuses={run.status},
            error=message,
        )
        if interrupted:
            await self._emit(run.id, EventKind.RUN_INTERRUPTED, {"message": message})

    async def _handle_phase_task_cancel(self, run_id: str, shutdown_message: str) -> None:
        run = await self.database.get_run(run_id)
        if run and run.status == RunStatus.CANCEL_REQUESTED:
            await self._finish_cancel(run)
        else:
            await self._mark_interrupted_if_active(run_id, shutdown_message)

    async def _emit(self, run_id: str, kind: EventKind, payload: Mapping[str, Any]) -> RunEvent:
        safe_payload = _sanitize_payload(payload, self.settings.max_event_text_chars)
        event = await self.database.append_event(run_id, kind, safe_payload)
        signal = self._signals.setdefault(run_id, asyncio.Event())
        signal.set()
        if kind in _TERMINAL_EVENT_KINDS or (
            kind == EventKind.APPROVAL_DECIDED and safe_payload.get("decision") in {"rejected", "expired"}
        ):
            self._signals.pop(run_id, None)
        return event

    def _spawn(self, run_id: str, coroutine) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coroutine, name=f"run-{run_id[:8]}")
        self._tasks[run_id] = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(run_id) is completed:
                self._tasks.pop(run_id, None)
            if not self._stopping:
                asyncio.create_task(self._schedule_queued_runs(), name="schedule-queued-runs")

        task.add_done_callback(done)

    async def _schedule_queued_runs(self) -> None:
        if self._stopping:
            return
        async with self._queue_lock:
            available = max(0, self.settings.max_concurrent_runs - len(self._tasks))
            for _ in range(available):
                claimed = await self.database.claim_next_queued_run()
                if claimed is None:
                    return
                self._spawn(claimed.id, self._execute_read_phase(claimed.id))

    async def _approval_expiry_loop(self) -> None:
        interval = min(30.0, max(1.0, self.settings.approval_timeout_seconds / 10))
        while True:
            try:
                await self._expire_due_approvals()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("approval expiry sweep failed; scheduler will retry")
            await asyncio.sleep(interval)

    async def _expire_due_approvals(self) -> None:
        expired = await self.database.expire_approvals()
        for approval in expired:
            run = await self.database.get_run(approval.run_id)
            if not run or run.status != RunStatus.REJECTED or run.error != "Approval expired":
                continue
            await self._emit(run.id, EventKind.APPROVAL_DECIDED, {"decision": "expired"})
        if expired:
            await self._schedule_queued_runs()

    async def _get_conversation(self, conversation_id: str) -> Conversation:
        conversation = await self.database.get_conversation(conversation_id)
        if not conversation:
            raise ConversationNotFoundError("会话不存在")
        return conversation

    async def _get_run(self, run_id: str) -> Run:
        run = await self.database.get_run(run_id)
        if not run:
            raise RunNotFoundError("任务不存在")
        return run

    @staticmethod
    def _require_initiator(run: Run, actor_id: str) -> None:
        if run.initiator_id != actor_id:
            raise RunNotFoundError("任务不存在")

    async def _require_run_access(
        self,
        run: Run,
        owner_type: str,
        owner_id: str,
        actor_id: str,
    ) -> None:
        conversation = await self._get_conversation(run.conversation_id)
        if conversation.owner_type != owner_type or conversation.owner_id != owner_id:
            raise RunNotFoundError("任务不存在")
        self._require_initiator(run, actor_id)

    @staticmethod
    def _result_error(result: AgentResult) -> str:
        detail = result.stderr.strip() or result.output.strip() or f"Agent exited with code {result.exit_code}"
        return redact_text(detail, limit=2000)

    @staticmethod
    def _phase_prompt(run: Run, phase: str, *, general: bool = False) -> str:
        if phase == "ask":
            if general:
                return (
                    f"{run.prompt}\n\n"
                    "Agent Relay general conversation boundary: answer without inspecting the filesystem, running "
                    "commands, or changing local or external state. If the request depends on a codebase or requires "
                    "file changes, explain that the user needs to select a project first. Do not reveal hidden "
                    "chain-of-thought; provide only concise conclusions and useful next steps."
                )
            return (
                f"{run.prompt}\n\n"
                "Agent Relay safety boundary: answer in read-only mode. You may inspect files, but do not modify the "
                "workspace or run commands that change state. Do not reveal hidden chain-of-thought; provide only "
                "concise conclusions and evidence."
            )
        if phase == "plan":
            return (
                f"{run.prompt}\n\n"
                "Agent Relay planning phase: remain read-only. Do not modify files or run state-changing commands. "
                "Return a concrete execution plan naming the files, command categories, expected effects, validation, "
                f"and material risks in at most {APPROVAL_PLAN_MAX_CHARS} characters. This exact plan will be shown "
                "to a remote user for approval. Do not execute it yet and do not reveal hidden chain-of-thought."
            )
        if phase == "auto":
            return (
                f"{run.prompt}\n\n"
                "Agent Relay conversational routing phase: remain strictly read-only. You may inspect files and run "
                "read-only commands, but do not modify files or external state. Decide whether the user's request can "
                "be fully handled read-only, or whether satisfying it requires workspace writes or state-changing "
                "commands. Return exactly one JSON object and no Markdown fence or surrounding text. For greetings, "
                "questions, explanations, reviews, and read-only inspection use "
                '{"kind":"answer","content":"your concise user-facing answer"}. If changes or state-changing '
                'commands are required, use {"kind":"plan","content":"a concrete approval plan naming files, '
                'command categories, expected effects, validation, and material risks"}. A plan must be no longer than '
                f"{APPROVAL_PLAN_MAX_CHARS} characters. Do not claim changes were made and do not reveal hidden "
                "chain-of-thought."
            )
        if run.permission_mode is PermissionMode.REQUEST_APPROVAL:
            authority = "The remote user approved the following plan for this workspace"
            boundary = "Stay inside the configured workspace and its sandbox"
        elif run.permission_mode is PermissionMode.WORKSPACE_AUTO:
            authority = "The remote user selected automatic execution for this workspace"
            boundary = "Stay inside the configured workspace and its restricted execution boundary"
        else:
            authority = "The remote user explicitly enabled full access for this task"
            boundary = "Use access outside the workspace or network only when the request actually requires it"
        return (
            f"{authority}:\n\n"
            f"{run.plan or '(no plan text)'}\n\n"
            f"Execute the plan now. {boundary}. If completion would require a destructive action, external side "
            "effect, "
            "or material deviation from the plan that the user did not request, stop and explain instead. Run "
            "proportionate verification and report changes plus any unverified boundary."
        )


def _sanitize_payload(value: Mapping[str, Any], text_limit: int) -> dict[str, Any]:
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[nested payload truncated]"
        if isinstance(item, str):
            return redact_text(item, limit=text_limit)
        if isinstance(item, Mapping):
            return {str(key)[:100]: clean(child, depth + 1) for key, child in list(item.items())[:100]}
        if isinstance(item, list | tuple):
            return [clean(child, depth + 1) for child in item[:100]]
        if item is None or isinstance(item, bool | int | float):
            return item
        return redact_text(str(item), limit=500)

    return {str(key)[:100]: clean(item) for key, item in value.items()}


def _parse_auto_response(raw_output: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"kind", "content"}:
        return None
    kind = payload.get("kind")
    content = payload.get("content")
    if kind not in {"answer", "plan"} or not isinstance(content, str) or not content.strip():
        return None
    return kind, content.strip()
