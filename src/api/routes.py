from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from api.auth import require_api_auth
from api.schemas import (
    ApprovalResponse,
    ConversationCreate,
    ConversationResponse,
    DecisionRequest,
    EventResponse,
    RunCreate,
    RunResponse,
    SwitchAgentRequest,
)
from core.exceptions import (
    ApprovalExpiredError,
    ConversationNotFoundError,
    InvalidStateError,
    RelayError,
    RunConflictError,
    RunNotFoundError,
    WorkspaceDeniedError,
)
from domain.models import TERMINAL_RUN_STATUSES
from services.relay import RelayService

router = APIRouter()
Actor = Annotated[str, Depends(require_api_auth)]


def _service(request: Request) -> RelayService:
    return cast(RelayService, request.app.state.relay_service)


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    report = await _service(request).readiness()
    return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503)


@router.get("/v1/agents")
async def agents(request: Request, actor: Actor) -> dict[str, object]:
    del actor
    return await _service(request).agent_capabilities()


@router.post("/v1/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(payload: ConversationCreate, request: Request, actor: Actor) -> ConversationResponse:
    conversation = await _translate(
        _service(request).create_conversation(
            owner_type="api",
            owner_id=actor,
            workspace=payload.workspace,
            agent=payload.agent,
            title=payload.title,
        )
    )
    return ConversationResponse.from_domain(conversation)


@router.get("/v1/conversations", response_model=list[ConversationResponse])
async def list_conversations(request: Request, actor: Actor) -> list[ConversationResponse]:
    items = await _service(request).list_conversations("api", actor)
    return [ConversationResponse.from_domain(item) for item in items]


@router.post("/v1/conversations/{conversation_id}/agent", response_model=ConversationResponse)
async def switch_agent(
    conversation_id: str,
    payload: SwitchAgentRequest,
    request: Request,
    actor: Actor,
) -> ConversationResponse:
    item = await _translate(
        _service(request).switch_agent(
            conversation_id,
            payload.agent,
            owner_type="api",
            owner_id=actor,
        )
    )
    return ConversationResponse.from_domain(item)


@router.post(
    "/v1/conversations/{conversation_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(conversation_id: str, payload: RunCreate, request: Request, actor: Actor) -> RunResponse:
    run = await _translate(
        _service(request).submit_run(
            conversation_id,
            payload.prompt,
            payload.mode,
            owner_type="api",
            owner_id=actor,
            initiator_id=actor,
            permission_mode=payload.permission_mode,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )
    )
    return RunResponse.from_domain(run)


@router.get("/v1/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, request: Request, actor: Actor) -> RunResponse:
    run = await _translate(_service(request).get_owned_run(run_id, actor, owner_type="api", owner_id=actor))
    return RunResponse.from_domain(run)


@router.get("/v1/runs/{run_id}/events", response_model=list[EventResponse])
async def get_events(
    run_id: str,
    request: Request,
    actor: Actor,
    after: int = Query(default=0, ge=0),
) -> list[EventResponse]:
    await _translate(_service(request).get_owned_run(run_id, actor, owner_type="api", owner_id=actor))
    events = await _service(request).list_events(run_id, after_seq=after)
    return [EventResponse.from_domain(item) for item in events]


@router.get("/v1/runs/{run_id}/events/stream")
async def stream_events(
    run_id: str,
    request: Request,
    actor: Actor,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    await _translate(_service(request).get_owned_run(run_id, actor, owner_type="api", owner_id=actor))

    async def stream() -> AsyncIterator[str]:
        seq = after
        while not await request.is_disconnected():
            events = await _service(request).wait_for_events(run_id, seq, timeout=15)
            for event in events:
                seq = max(seq, event.seq)
                payload = EventResponse.from_domain(event).model_dump(mode="json")
                yield f"id: {event.seq}\nevent: {event.kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            run = await _service(request).get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                remaining = await _service(request).list_events(run_id, after_seq=seq, limit=1)
                if not remaining:
                    yield "event: end\ndata: {}\n\n"
                    return
            if not events:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/runs/{run_id}/decision", response_model=ApprovalResponse)
async def decide_run(
    run_id: str,
    payload: DecisionRequest,
    request: Request,
    actor: Actor,
) -> ApprovalResponse:
    if payload.decision == "approve":
        approval = await _translate(_service(request).approve_run(run_id, actor, owner_type="api", owner_id=actor))
    else:
        approval = await _translate(_service(request).reject_run(run_id, actor, owner_type="api", owner_id=actor))
    return ApprovalResponse.from_domain(approval)


@router.post("/v1/runs/{run_id}/cancel", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: str, request: Request, actor: Actor) -> RunResponse:
    run = await _translate(_service(request).cancel_run(run_id, actor, owner_type="api", owner_id=actor))
    return RunResponse.from_domain(run)


async def _translate(awaitable):  # type: ignore[no-untyped-def]
    try:
        return await awaitable
    except (ConversationNotFoundError, RunNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkspaceDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (RunConflictError, InvalidStateError, ApprovalExpiredError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RelayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.CancelledError:
        raise
