from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.exceptions import (
    ApprovalExpiredError,
    ConversationNotFoundError,
    InvalidStateError,
    RelayError,
    RunConflictError,
    RunNotFoundError,
    WorkspaceDeniedError,
)
from domain.models import AgentKind, Conversation, PermissionMode, Run, RunMode
from services.relay import RelayService
from webapp.auth import TelegramWebIdentity, require_telegram_web_auth

router = APIRouter()
Identity = Annotated[TelegramWebIdentity, Depends(require_telegram_web_auth)]
_STATIC_ROOT = Path(__file__).with_name("static")
_MAX_DISCOVERED_FILES = 24
_MAX_SCANNED_FILES = 10_000
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_TEXT_BYTES = 2 * 1024 * 1024
_SKIP_DIRECTORIES = {".git", ".hg", ".svn", ".venv", "node_modules", "dist", "build", "target"}
_SENSITIVE_NAMES = {"credentials", "credentials.json", "id_rsa", "id_ed25519", "secrets.json"}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore", ".jks"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".rst",
    ".py",
    ".pyi",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".csv",
    ".tsv",
    ".sql",
    ".sh",
    ".zsh",
    ".bash",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".swift",
    ".properties",
    ".gradle",
    ".dockerfile",
}
_MARKDOWN_TARGET_RE = re.compile(r"\[[^\]]*\]\(([^)\n]+)\)")
_QUOTED_PATH_RE = re.compile(r"[`'\"]([^`'\"\n]+\.[A-Za-z0-9]{1,12})[`'\"]")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w])(/(?:[^\s`'\"()\[\]{}]+/)*[^\s`'\"()\[\]{}]+)")
_RELATIVE_PATH_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_. -]+)*\.[A-Za-z0-9]{1,12})")
_CODEX_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
)
_CLAUDE_MODELS = (
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)


class ConversationCreatePayload(BaseModel):
    workspace: str | None = None
    agent: AgentKind = AgentKind.CODEX


class AgentPayload(BaseModel):
    agent: AgentKind


class MessagePayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL
    model: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None


class DecisionPayload(BaseModel):
    decision: Literal["approve", "reject"]


def _service(request: Request) -> RelayService:
    return cast(RelayService, request.app.state.relay_service)


@router.get("/app", include_in_schema=False)
async def workbench() -> FileResponse:
    return FileResponse(_STATIC_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/app/api/bootstrap")
async def bootstrap(request: Request, identity: Identity) -> dict[str, object]:
    service = _service(request)
    conversations = await service.list_conversations("telegram", identity.owner_id)
    active = await service.get_active_conversation("telegram", identity.owner_id)
    projects = _discover_projects(
        service.settings.workspace_roots,
        service.settings.default_workspace,
        service.settings.general_workspace,
        conversations,
    )
    summaries = []
    for conversation in conversations[:100]:
        runs = await service.list_conversation_runs(
            conversation.id,
            owner_type="telegram",
            owner_id=identity.owner_id,
            limit=1,
        )
        summaries.append(_conversation_json(service, conversation, runs[0] if runs else None))
    return {
        "active_conversation_id": active.id if active else None,
        "conversations": summaries,
        "projects": [
            {"path": str(project), "name": project.name, "group": project.parent.name} for project in projects
        ],
        "agents": ["codex", "claude"],
        "agent_options": _agent_options(service),
    }


@router.post("/app/api/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreatePayload,
    request: Request,
    identity: Identity,
) -> dict[str, object]:
    conversation = await _translate(
        _service(request).create_conversation(
            owner_type="telegram",
            owner_id=identity.owner_id,
            workspace=payload.workspace,
            agent=payload.agent,
        )
    )
    return _conversation_json(_service(request), conversation)


@router.post("/app/api/conversations/{conversation_id}/activate")
async def activate_conversation(
    conversation_id: str,
    request: Request,
    identity: Identity,
) -> dict[str, object]:
    conversation = await _translate(
        _service(request).set_active_conversation("telegram", identity.owner_id, conversation_id)
    )
    return _conversation_json(_service(request), conversation)


@router.post("/app/api/conversations/{conversation_id}/agent")
async def update_agent(
    conversation_id: str,
    payload: AgentPayload,
    request: Request,
    identity: Identity,
) -> dict[str, object]:
    conversation = await _translate(
        _service(request).switch_agent(
            conversation_id,
            payload.agent,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    return _conversation_json(_service(request), conversation)


@router.get("/app/api/conversations/{conversation_id}/runs")
async def conversation_runs(
    conversation_id: str,
    request: Request,
    identity: Identity,
) -> list[dict[str, object]]:
    runs = await _translate(
        _service(request).list_conversation_runs(
            conversation_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
            limit=100,
        )
    )
    return [_run_json(run) for run in reversed(runs)]


@router.post("/app/api/conversations/{conversation_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    conversation_id: str,
    payload: MessagePayload,
    request: Request,
    identity: Identity,
) -> dict[str, object]:
    run = await _translate(
        _service(request).submit_run(
            conversation_id,
            payload.prompt,
            RunMode.RUN,
            owner_type="telegram",
            owner_id=identity.owner_id,
            initiator_id=identity.user_id,
            auto_route=True,
            permission_mode=payload.permission_mode,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )
    )
    return _run_json(run)


@router.get("/app/api/runs/{run_id}")
async def get_run(run_id: str, request: Request, identity: Identity) -> dict[str, object]:
    run = await _translate(
        _service(request).get_owned_run(
            run_id,
            identity.user_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    return _run_json(run)


@router.get("/app/api/runs/{run_id}/files")
async def run_files(run_id: str, request: Request, identity: Identity) -> list[dict[str, object]]:
    service = _service(request)
    run = await _translate(
        service.get_owned_run(
            run_id,
            identity.user_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    conversation = await service.get_conversation(run.conversation_id)
    if service.is_general_workspace(conversation.workspace):
        return []
    events = await service.list_events(run.id)
    return await asyncio.to_thread(_discover_run_files, Path(conversation.workspace), run, events)


@router.get("/app/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, identity: Identity) -> list[dict[str, object]]:
    service = _service(request)
    run = await _translate(
        service.get_owned_run(
            run_id,
            identity.user_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    events = await service.list_events(run.id, limit=1000)
    return [
        {
            "seq": event.seq,
            "kind": event.kind.value,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
    ]


@router.get("/app/api/runs/{run_id}/files/{file_id}")
async def run_file_content(run_id: str, file_id: str, request: Request, identity: Identity) -> FileResponse:
    service = _service(request)
    run = await _translate(
        service.get_owned_run(
            run_id,
            identity.user_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    conversation = await service.get_conversation(run.conversation_id)
    if service.is_general_workspace(conversation.workspace):
        raise HTTPException(status_code=404, detail="文件不存在")
    events = await service.list_events(run.id)
    descriptors = await asyncio.to_thread(_discover_run_files, Path(conversation.workspace), run, events)
    if not any(item["id"] == file_id for item in descriptors):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        relative = _decode_file_id(file_id)
        target = _safe_preview_file(Path(conversation.workspace), relative)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="文件不存在") from None
    size = target.stat().st_size
    kind, media_type = _preview_kind(target)
    limit = _MAX_TEXT_BYTES if kind == "text" else _MAX_FILE_BYTES
    if size > limit:
        raise HTTPException(status_code=413, detail="文件过大，无法在线预览")
    if kind == "text":
        media_type = "text/plain; charset=utf-8"
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/app/api/runs/{run_id}/decision")
async def decide_run(
    run_id: str,
    payload: DecisionPayload,
    request: Request,
    identity: Identity,
) -> dict[str, str]:
    service = _service(request)
    if payload.decision == "approve":
        approval = await _translate(
            service.approve_run(
                run_id,
                identity.user_id,
                owner_type="telegram",
                owner_id=identity.owner_id,
            )
        )
    else:
        approval = await _translate(
            service.reject_run(
                run_id,
                identity.user_id,
                owner_type="telegram",
                owner_id=identity.owner_id,
            )
        )
    return {"status": approval.status.value}


@router.post("/app/api/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: str, request: Request, identity: Identity) -> dict[str, object]:
    run = await _translate(
        _service(request).cancel_run(
            run_id,
            identity.user_id,
            owner_type="telegram",
            owner_id=identity.owner_id,
        )
    )
    return _run_json(run)


def _discover_projects(
    roots: list[Path],
    default: Path,
    general_workspace: Path,
    conversations: list[Conversation],
) -> list[Path]:
    projects: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            projects.append(resolved)

    for conversation in conversations:
        if Path(conversation.workspace).resolve() != general_workspace.expanduser().resolve():
            add(Path(conversation.workspace))
    add(default)
    for root in roots:
        try:
            children = sorted(root.expanduser().resolve(strict=True).iterdir(), key=lambda item: item.name.lower())
        except (OSError, RuntimeError):
            continue
        for child in children:
            if not child.name.startswith("."):
                add(child)
    return projects[:100]


def _conversation_json(
    service: RelayService,
    conversation: Conversation,
    latest_run: Run | None = None,
) -> dict[str, object]:
    project_selected = not service.is_general_workspace(conversation.workspace)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "workspace": conversation.workspace if project_selected else None,
        "project_name": Path(conversation.workspace).name if project_selected else "无项目",
        "project_selected": project_selected,
        "active_agent": conversation.active_agent.value,
        "updated_at": conversation.updated_at,
        "latest_run": _run_json(latest_run) if latest_run else None,
    }


def _run_json(run: Run) -> dict[str, object]:
    return {
        "id": run.id,
        "conversation_id": run.conversation_id,
        "agent": run.agent.value,
        "status": run.status.value,
        "permission_mode": run.permission_mode.value,
        "model": run.model,
        "reasoning_effort": run.reasoning_effort,
        "prompt": run.prompt,
        "plan": run.plan,
        "result": run.result,
        "error": run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


def _agent_options(service: RelayService) -> dict[str, object]:
    codex_models = list(dict.fromkeys(filter(None, (service.settings.codex_model, *_CODEX_MODELS))))
    claude_models = list(dict.fromkeys(filter(None, (service.settings.claude_model, *_CLAUDE_MODELS))))
    return {
        "codex": {
            "models": codex_models,
            "default_model": service.settings.codex_model or codex_models[0],
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "default_reasoning_effort": service.settings.codex_reasoning_effort,
        },
        "claude": {
            "models": claude_models,
            "default_model": service.settings.claude_model or claude_models[0],
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "default_reasoning_effort": "high",
        },
    }


def _discover_run_files(workspace: Path, run: Run, events: list[object]) -> list[dict[str, object]]:
    root = workspace.expanduser().resolve(strict=True)
    candidates = _extract_file_candidates(run.result or "")
    if not candidates:
        candidates = _extract_file_candidates(run.plan or "")
        for event in events:
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict) or not _is_file_write_event(payload):
                continue
            candidates.extend(_extract_file_candidates(json.dumps(payload, ensure_ascii=False)))

    if not candidates and run.started_at is not None:
        candidates.extend(_files_modified_during_run(root, run))

    descriptors: list[dict[str, object]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = _safe_preview_file(root, candidate)
        except (ValueError, OSError):
            continue
        if path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        kind, media_type = _preview_kind(path)
        limit = _MAX_TEXT_BYTES if kind == "text" else _MAX_FILE_BYTES
        descriptors.append(
            {
                "id": _encode_file_id(relative),
                "name": path.name,
                "path": relative,
                "kind": kind,
                "media_type": media_type,
                "size": size,
                "available": size <= limit,
            }
        )
        if len(descriptors) >= _MAX_DISCOVERED_FILES:
            break
    return descriptors


def _extract_file_candidates(text: str) -> list[str | Path]:
    candidates: list[str | Path] = []
    for pattern in (_MARKDOWN_TARGET_RE, _QUOTED_PATH_RE, _ABSOLUTE_PATH_RE, _RELATIVE_PATH_RE):
        candidates.extend(match.group(1).strip().rstrip(".,;:，。；：") for match in pattern.finditer(text))
    return candidates


def _is_file_write_event(payload: dict[str, object]) -> bool:
    tool = str(payload.get("tool") or payload.get("name") or "").lower()
    return any(marker in tool for marker in ("file_change", "write", "edit", "notebook"))


def _files_modified_during_run(root: Path, run: Run) -> list[Path]:
    lower = (run.started_at or 0) - 5
    upper = (run.completed_at or time.time()) + 5
    candidates: list[Path] = []
    scanned = 0
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if name not in _SKIP_DIRECTORIES]
        for filename in filenames:
            scanned += 1
            if scanned > _MAX_SCANNED_FILES:
                break
            path = Path(current) / filename
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if lower <= modified <= upper:
                candidates.append(path)
        if scanned > _MAX_SCANNED_FILES:
            break
    return candidates


def _safe_preview_file(workspace: Path, candidate: str | Path) -> Path:
    root = workspace.expanduser().resolve(strict=True)
    raw = str(candidate).strip()
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw).expanduser()
    target = path.resolve(strict=True) if path.is_absolute() else (root / path).resolve(strict=True)
    if target == root or root not in target.parents or not target.is_file() or _is_sensitive_file(root, target):
        raise ValueError("file is outside the preview boundary")
    return target


def _is_sensitive_file(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    lowered_parts = {part.lower() for part in relative.parts}
    name = path.name.lower()
    return (
        bool(lowered_parts & {".git", ".ssh", ".gnupg", ".aws"})
        or name.startswith(".env")
        or name in _SENSITIVE_NAMES
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
    )


def _preview_kind(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if suffix in _IMAGE_SUFFIXES:
        return "image", guessed
    if suffix == ".pdf":
        return "pdf", "application/pdf"
    if suffix in _AUDIO_SUFFIXES:
        return "audio", guessed
    if suffix in _VIDEO_SUFFIXES:
        return "video", guessed
    if suffix in _TEXT_SUFFIXES or guessed.startswith("text/") or path.name.lower() in {"dockerfile", "makefile"}:
        return "text", "text/plain"
    return "file", guessed


def _encode_file_id(relative: str) -> str:
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


def _decode_file_id(value: str) -> str:
    if not value or len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid file id")
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding).decode()
    if not decoded or "\x00" in decoded:
        raise ValueError("invalid file id")
    return decoded


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
