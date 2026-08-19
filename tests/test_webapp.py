from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from agents.base import AgentRegistry, EventCallback
from core.config import Settings
from domain.models import (
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
from main import create_app
from persistence.database import Database
from services.relay import RelayService
from webapp.auth import validate_telegram_init_data
from webapp.routes import _discover_run_files


class WebAdapter:
    def __init__(self, kind: AgentKind) -> None:
        self.kind = kind
        self.requests: list[AgentRequest] = []

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        self.requests.append(request)
        await on_event(AgentEvent(EventKind.AGENT_STATUS, {"status": "thinking"}))
        await on_event(
            AgentEvent(
                EventKind.TOOL_STARTED, {"tool": "Read", "tool_call_id": "tool-1", "input": {"path": "README.md"}}
            )
        )
        output = '{"kind":"answer","content":"工作台回答"}' if request.phase == "auto" else f"{self.kind.value} result"
        await on_event(AgentEvent(EventKind.OUTPUT_DELTA, {"text": output}))
        return AgentResult(0, f"{self.kind.value}-session", output, "")

    async def cancel(self, run_id: str) -> bool:
        del run_id
        return True


class BlockingWebAdapter(WebAdapter):
    def __init__(self, kind: AgentKind) -> None:
        super().__init__(kind)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        self.requests.append(request)
        await on_event(AgentEvent(EventKind.AGENT_STATUS, {"status": "thinking"}))
        self.started.set()
        await self.release.wait()
        return AgentResult(0, f"{self.kind.value}-session", '{"kind":"answer","content":"后台完成"}', "")

    async def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        self.release.set()
        return True


def _signed_init_data(token: str, user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_telegram_init_data_signature_and_expiry() -> None:
    token = "123456789:test-token"
    payload = _signed_init_data(token, 42, 1_000)
    assert validate_telegram_init_data(payload, token, now=1_100)["id"] == 42

    with pytest.raises(ValueError, match="signature"):
        validate_telegram_init_data(payload, token + "-wrong", now=1_100)
    with pytest.raises(ValueError, match="expired"):
        validate_telegram_init_data(payload, token, now=5_000)


def test_file_discovery_prefers_files_declared_in_final_answer(tmp_path: Path) -> None:
    image = tmp_path / "result.png"
    document = tmp_path / "result.txt"
    old_image = tmp_path / "old.png"
    old_document = tmp_path / "old.txt"
    image.write_bytes(b"png")
    document.write_text("result")
    old_image.write_bytes(b"old")
    old_document.write_text("old")
    now = time.time()
    for path in (image, document, old_image, old_document):
        os.utime(path, (now, now))
    run = Run(
        id="run-1",
        conversation_id="conversation-1",
        agent=AgentKind.CODEX,
        mode=RunMode.ASK,
        status=RunStatus.COMPLETED,
        prompt="生成一张图和一个文本文件",
        initiator_id="42",
        permission_mode=PermissionMode.FULL_ACCESS,
        result=f"已生成 [result.png]({image}) 和 [result.txt]({document})。",
        created_at=now - 10,
        started_at=now - 5,
        completed_at=now + 1,
    )

    files = _discover_run_files(tmp_path, run, [])

    assert [file["name"] for file in files] == ["result.png", "result.txt"]


@pytest.mark.asyncio
async def test_workbench_local_flow_lists_projects_and_runs_conversation(tmp_path: Path) -> None:
    project = tmp_path / "sample-project"
    project.mkdir()
    settings = Settings(
        default_workspace=project,
        workspace_roots=[tmp_path],
        general_workspace=tmp_path / "general-workspace",
        database_path=tmp_path / "webapp.db",
        telegram_allowed_chat_ids=["42"],
        telegram_allowed_user_ids=["42"],
        telegram_bot_token="123456789:test-token",
        api_enabled=False,
    )
    codex = WebAdapter(AgentKind.CODEX)
    service = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([codex, WebAdapter(AgentKind.CLAUDE)]),
    )
    app = create_app(settings, relay_service=service)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client,
    ):
        page = await client.get("/app")
        assert page.status_code == 200
        assert "Agent Relay" in page.text
        script = await client.get("/app/assets/app.js")
        stylesheet = await client.get("/app/assets/app.css")
        assert "visualViewport" in script.text
        assert "workspace_auto" in script.text
        assert "full_access" in script.text
        assert "继续发送，消息会排队处理" in script.text
        assert "!hasText || hasActiveRun" not in script.text
        assert "hasAnyActiveRun" in script.text
        assert "运行中 · " in script.text
        assert "执行过程" in script.text
        assert "modelSelect" in script.text
        assert 'new Option("默认模型"' not in script.text
        assert "renderMarkdown(content" in script.text
        assert ".innerHTML" not in script.text
        assert "processPanel(run, true)" not in script.text
        assert "expandedProcessRuns: new Set()" in script.text
        assert "state.expandedProcessRuns.add(run.id)" in script.text
        assert "state.expandedProcessRuns.delete(run.id)" in script.text
        assert "runPresentationSignature(state.runs)" in script.text
        assert "refreshExpandedProcessPanels()" in script.text
        assert "timeline.dataset.eventSignature === signature" in script.text
        assert "const bootstrapChanged = JSON.stringify(state.bootstrap)" in script.text
        assert "conversationContextSignature(state.activeConversation)" in script.text
        assert "file-image-preview" in script.text
        assert "--app-height" in script.text
        assert "var(--app-height" in stylesheet.text
        assert "tg-viewport-stable-height" not in stylesheet.text

        initial = await client.get("/app/api/bootstrap")
        assert initial.status_code == 200
        assert any(item["path"] == str(project) for item in initial.json()["projects"])
        assert "gpt-5.6-sol" in initial.json()["agent_options"]["codex"]["models"]
        assert initial.json()["agent_options"]["codex"]["default_model"] == "gpt-5.6-sol"
        claude_options = initial.json()["agent_options"]["claude"]
        assert claude_options["default_model"] == "claude-sonnet-5"
        assert claude_options["models"] == [
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]
        assert "xhigh" in initial.json()["agent_options"]["claude"]["reasoning_efforts"]

        general = await client.post(
            "/app/api/conversations",
            json={"workspace": None, "agent": "codex"},
        )
        assert general.status_code == 201
        assert general.json()["workspace"] is None
        assert general.json()["project_name"] == "无项目"
        assert general.json()["project_selected"] is False
        general_run = await client.post(
            f"/app/api/conversations/{general.json()['id']}/runs",
            json={"prompt": "解释 Python 的上下文管理器", "permission_mode": "full_access"},
        )
        general_completed = await service.wait_for_terminal(general_run.json()["id"])
        assert general_completed.mode.value == "ask"
        assert general_completed.auto_route is False
        assert general_completed.permission_mode.value == "request_approval"
        assert codex.requests[-1].phase == "ask"
        assert codex.requests[-1].workspace == str(settings.general_workspace.resolve())

        created = await client.post(
            "/app/api/conversations",
            json={"workspace": str(project), "agent": "codex"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        submitted = await client.post(
            f"/app/api/conversations/{conversation_id}/runs",
            json={"prompt": "介绍这个项目", "model": "gpt-5.6-terra", "reasoning_effort": "xhigh"},
        )
        run_id = submitted.json()["id"]
        completed = await service.wait_for_terminal(run_id)
        assert completed.result == "工作台回答"
        assert completed.model == "gpt-5.6-terra"
        assert completed.reasoning_effort == "xhigh"
        assert codex.requests[-1].model == "gpt-5.6-terra"
        assert codex.requests[-1].reasoning_effort == "xhigh"
        run_events = await client.get(f"/app/api/runs/{run_id}/events")
        assert run_events.status_code == 200
        assert any(event["kind"] == "tool.started" for event in run_events.json())
        assert any(event["kind"] == "output.delta" for event in run_events.json())

        (project / "preview.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        (project / "notes.py").write_text("print('preview')\n")
        (project / "artifact.bin").write_bytes(b"binary-preview")
        (project / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (project / "sample.mp3").write_bytes(b"ID3")
        (project / "sample.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        (project / "too-large.txt").write_bytes(b"")
        with (project / "too-large.txt").open("r+b") as large_file:
            large_file.truncate(2 * 1024 * 1024 + 1)
        (project / ".env").write_text("SECRET=must-not-be-listed\n")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        (project / "outside-link.txt").symlink_to(outside)
        unreferenced = project / "unreferenced.txt"
        unreferenced.write_text("not part of this run")
        os.utime(unreferenced, (1, 1))

        files_response = await client.get(f"/app/api/runs/{run_id}/files")
        assert files_response.status_code == 200
        files = {item["name"]: item for item in files_response.json()}
        assert files["preview.png"]["kind"] == "image"
        assert files["notes.py"]["kind"] == "text"
        assert files["artifact.bin"]["kind"] == "file"
        assert files["report.pdf"]["kind"] == "pdf"
        assert files["sample.mp3"]["kind"] == "audio"
        assert files["sample.mp4"]["kind"] == "video"
        assert files["too-large.txt"]["available"] is False
        assert ".env" not in files
        assert "outside-link.txt" not in files

        image_content = await client.get(f"/app/api/runs/{run_id}/files/{files['preview.png']['id']}")
        assert image_content.status_code == 200
        assert image_content.headers["content-type"] == "image/png"
        assert image_content.content.startswith(b"\x89PNG")

        text_content = await client.get(f"/app/api/runs/{run_id}/files/{files['notes.py']['id']}")
        assert text_content.status_code == 200
        assert text_content.text == "print('preview')\n"
        forged = base64.urlsafe_b64encode(b"../outside.txt").decode().rstrip("=")
        assert (await client.get(f"/app/api/runs/{run_id}/files/{forged}")).status_code == 404
        unreferenced_id = base64.urlsafe_b64encode(b"unreferenced.txt").decode().rstrip("=")
        assert (await client.get(f"/app/api/runs/{run_id}/files/{unreferenced_id}")).status_code == 404
        too_large = await client.get(f"/app/api/runs/{run_id}/files/{files['too-large.txt']['id']}")
        assert too_large.status_code == 413

        full_access = await client.post(
            f"/app/api/conversations/{conversation_id}/runs",
            json={"prompt": "继续介绍", "permission_mode": "full_access"},
        )
        full_access_completed = await service.wait_for_terminal(full_access.json()["id"])
        assert full_access_completed.permission_mode.value == "full_access"
        assert full_access.json()["permission_mode"] == "full_access"

        runs = await client.get(f"/app/api/conversations/{conversation_id}/runs")
        assert runs.json()[0]["prompt"] == "介绍这个项目"
        assert runs.json()[0]["result"] == "工作台回答"
        assert runs.json()[1]["permission_mode"] == "full_access"


@pytest.mark.asyncio
async def test_workbench_can_switch_conversations_while_run_continues(tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    settings = Settings(
        default_workspace=project_a,
        workspace_roots=[tmp_path],
        general_workspace=tmp_path / "general-workspace",
        database_path=tmp_path / "switching.db",
        telegram_allowed_chat_ids=["42"],
        telegram_allowed_user_ids=["42"],
        api_enabled=False,
    )
    codex = BlockingWebAdapter(AgentKind.CODEX)
    service = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([codex, WebAdapter(AgentKind.CLAUDE)]),
    )
    app = create_app(settings, relay_service=service)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client,
        ):
            conversation_a = await client.post(
                "/app/api/conversations",
                json={"workspace": str(project_a), "agent": "codex"},
            )
            conversation_b = await client.post(
                "/app/api/conversations",
                json={"workspace": str(project_b), "agent": "codex"},
            )
            conversation_a_id = conversation_a.json()["id"]
            conversation_b_id = conversation_b.json()["id"]
            assert (await client.post(f"/app/api/conversations/{conversation_a_id}/activate")).status_code == 200

            submitted = await client.post(
                f"/app/api/conversations/{conversation_a_id}/runs",
                json={"prompt": "只读分析项目结构"},
            )
            await asyncio.wait_for(codex.started.wait(), timeout=1)
            switched = await client.post(f"/app/api/conversations/{conversation_b_id}/activate")

            assert switched.status_code == 200
            assert switched.json()["id"] == conversation_b_id
            assert (await client.get("/app/api/bootstrap")).json()["active_conversation_id"] == conversation_b_id
            running = await client.get(f"/app/api/runs/{submitted.json()['id']}")
            assert running.json()["status"] == "planning"
            assert codex.cancelled == []

            codex.release.set()
            completed = await service.wait_for_terminal(submitted.json()["id"])
            assert completed.result == "后台完成"
    finally:
        codex.release.set()


@pytest.mark.asyncio
async def test_public_workbench_api_requires_signed_telegram_identity(tmp_path: Path) -> None:
    settings = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        general_workspace=tmp_path / "general-workspace",
        database_path=tmp_path / "public-webapp.db",
        telegram_allowed_chat_ids=["42"],
        telegram_allowed_user_ids=["42"],
        telegram_bot_token="123456789:test-token",
        api_enabled=False,
    )
    service = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([WebAdapter(AgentKind.CODEX), WebAdapter(AgentKind.CLAUDE)]),
    )
    app = create_app(settings, relay_service=service)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://relay.example") as client,
    ):
        assert (await client.get("/app/api/bootstrap")).status_code == 401
        signed = _signed_init_data(settings.telegram_token_value, 42, int(time.time()))
        response = await client.get(
            "/app/api/bootstrap",
            headers={"X-Telegram-Init-Data": signed},
        )
        assert response.status_code == 200
