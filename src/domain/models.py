"""Shared relay domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

APPROVAL_PLAN_MAX_CHARS = 2_400


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class RunMode(StrEnum):
    ASK = "ask"
    RUN = "run"


class PermissionMode(StrEnum):
    REQUEST_APPROVAL = "request_approval"
    WORKSPACE_AUTO = "workspace_auto"
    FULL_ACCESS = "full_access"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"


ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED,
    RunStatus.PLANNING,
    RunStatus.AWAITING_APPROVAL,
    RunStatus.RUNNING,
    RunStatus.CANCEL_REQUESTED,
}

PROCESSING_RUN_STATUSES = ACTIVE_RUN_STATUSES - {RunStatus.QUEUED}

TERMINAL_RUN_STATUSES = {
    RunStatus.CANCELLED,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.REJECTED,
    RunStatus.INTERRUPTED,
    RunStatus.TIMED_OUT,
}


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EventKind(StrEnum):
    RUN_QUEUED = "run.queued"
    AGENT_STARTED = "agent.started"
    AGENT_STATUS = "agent.status"
    OUTPUT_DELTA = "output.delta"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_DECIDED = "approval.decided"
    CANCEL_REQUESTED = "run.cancel_requested"
    RUN_CANCELLED = "run.cancelled"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_TIMED_OUT = "run.timed_out"
    RUN_INTERRUPTED = "run.interrupted"
    USAGE = "agent.usage"


@dataclass(slots=True)
class Conversation:
    id: str
    owner_type: str
    owner_id: str
    workspace: str
    active_agent: AgentKind
    title: str
    created_at: float
    updated_at: float


@dataclass(slots=True)
class Run:
    id: str
    conversation_id: str
    agent: AgentKind
    mode: RunMode
    status: RunStatus
    prompt: str
    initiator_id: str
    permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL
    model: str | None = None
    reasoning_effort: str | None = None
    native_session_id: str | None = None
    plan: str | None = None
    result: str | None = None
    error: str | None = None
    exit_code: int | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    auto_route: bool = False


@dataclass(slots=True)
class RunEvent:
    id: int
    run_id: str
    seq: int
    kind: EventKind
    payload: dict[str, Any]
    created_at: float


@dataclass(slots=True)
class Approval:
    id: str
    run_id: str
    status: ApprovalStatus
    requested_at: float
    expires_at: float
    decided_at: float | None = None
    decided_by: str | None = None


@dataclass(slots=True)
class AgentEvent:
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRequest:
    run_id: str
    workspace: str
    prompt: str
    phase: str
    permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL
    model: str | None = None
    reasoning_effort: str | None = None
    native_session_id: str | None = None
    handoff_context: str | None = None


@dataclass(slots=True)
class AgentResult:
    exit_code: int
    native_session_id: str | None
    output: str
    stderr: str
    cancelled: bool = False
    timed_out: bool = False
    protocol_error: bool = False
    reported_error: bool = False
