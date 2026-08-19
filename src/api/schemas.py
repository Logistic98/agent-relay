from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from domain.models import AgentKind, Approval, Conversation, PermissionMode, Run, RunEvent, RunMode


class ConversationCreate(BaseModel):
    workspace: str
    agent: AgentKind = AgentKind.CODEX
    title: str | None = Field(default=None, max_length=120)


class ConversationResponse(BaseModel):
    id: str
    workspace: str
    active_agent: AgentKind
    title: str
    owner_id: str
    created_at: float
    updated_at: float

    @classmethod
    def from_domain(cls, item: Conversation) -> ConversationResponse:
        return cls(**{name: getattr(item, name) for name in cls.model_fields})


class SwitchAgentRequest(BaseModel):
    agent: AgentKind


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1)
    mode: RunMode = RunMode.RUN
    permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL
    model: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None


class RunResponse(BaseModel):
    id: str
    conversation_id: str
    agent: AgentKind
    mode: RunMode
    permission_mode: PermissionMode
    model: str | None
    reasoning_effort: str | None
    status: str
    native_session_id: str | None
    plan: str | None
    result: str | None
    error: str | None
    exit_code: int | None
    created_at: float
    started_at: float | None
    completed_at: float | None

    @classmethod
    def from_domain(cls, item: Run) -> RunResponse:
        return cls(**{name: getattr(item, name) for name in cls.model_fields})


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ApprovalResponse(BaseModel):
    id: str
    run_id: str
    status: str
    requested_at: float
    expires_at: float
    decided_at: float | None
    decided_by: str | None

    @classmethod
    def from_domain(cls, item: Approval) -> ApprovalResponse:
        return cls(**{name: getattr(item, name) for name in cls.model_fields})


class EventResponse(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: float

    @classmethod
    def from_domain(cls, item: RunEvent) -> EventResponse:
        return cls(seq=item.seq, kind=item.kind, payload=item.payload, created_at=item.created_at)
