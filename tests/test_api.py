from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agents.base import AgentRegistry, EventCallback
from core.config import Settings
from domain.models import AgentEvent, AgentKind, AgentRequest, AgentResult, EventKind
from main import create_app
from persistence.database import Database
from services.relay import RelayService

TEST_TOKEN = "test-bearer-token-0123456789abcdef"


class ImmediateAdapter:
    def __init__(self, kind: AgentKind) -> None:
        self.kind = kind

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult:
        output = "planned" if request.phase == "plan" else "done"
        await on_event(AgentEvent(EventKind.OUTPUT_DELTA, {"text": output}))
        return AgentResult(0, f"{self.kind.value}-session", output, "")

    async def cancel(self, run_id: str) -> bool:
        del run_id
        return True


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        database_path=tmp_path / "api.db",
        api_bearer_token=TEST_TOKEN,
    )
    service = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([ImmediateAdapter(AgentKind.CODEX), ImmediateAdapter(AgentKind.CLAUDE)]),
    )
    return create_app(settings, relay_service=service), service


@pytest.mark.asyncio
async def test_direct_asgi_startup_cannot_bypass_agent_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        database_path=tmp_path / "direct-asgi.db",
        api_enabled=False,
    )

    async def failed_preflight(settings: Settings) -> Settings:
        del settings
        raise RuntimeError("agent preflight failed")

    monkeypatch.setattr("main.require_agent_preflight", failed_preflight)
    app = create_app(settings)

    with pytest.raises(RuntimeError, match="agent preflight failed"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_health_is_public_but_v1_requires_bearer(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/v1/conversations")).status_code == 401
        wrong = await client.get("/v1/conversations", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 403


@pytest.mark.asyncio
async def test_readiness_fails_when_enabled_api_has_no_token(tmp_path: Path) -> None:
    settings = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        database_path=tmp_path / "missing-token.db",
        codex_executable="/bin/true",
        claude_executable="/bin/true",
        api_enabled=True,
    )
    service = RelayService(
        settings,
        Database(settings.database_path),
        AgentRegistry([ImmediateAdapter(AgentKind.CODEX), ImmediateAdapter(AgentKind.CLAUDE)]),
    )
    app = create_app(settings, relay_service=service)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        ready = await client.get("/health/ready")
        agents = await client.get("/v1/agents")

        assert ready.status_code == 503
        assert ready.json()["api"] == "missing_token"
        assert agents.status_code == 503


@pytest.mark.asyncio
async def test_api_creates_conversation_and_read_only_run(tmp_path: Path) -> None:
    app, service = _app(tmp_path)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/v1/conversations",
            headers=headers,
            json={"workspace": str(tmp_path), "agent": "codex", "title": "demo"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        response = await client.post(
            f"/v1/conversations/{conversation_id}/runs",
            headers=headers,
            json={"prompt": "inspect", "mode": "ask"},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        await service.wait_for_terminal(run_id)

        run_response = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert run_response.json()["status"] == "completed"
        assert run_response.json()["result"] == "done"
        events = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
        assert any(item["kind"] == "output.delta" for item in events.json())
        stream = await client.get(f"/v1/runs/{run_id}/events/stream?after=0", headers=headers)
        assert "event: output.delta" in stream.text
        assert "event: end" in stream.text

        same_principal = await client.get(
            f"/v1/runs/{run_id}",
            headers={"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": "mallory"},
        )
        assert same_principal.status_code == 200


@pytest.mark.asyncio
async def test_api_ignores_client_supplied_actor_identity(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    alice = {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": "alice"}
    mallory = {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": "mallory"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        conversation = await client.post(
            "/v1/conversations",
            headers=alice,
            json={"workspace": str(tmp_path), "agent": "codex"},
        )
        response = await client.post(
            f"/v1/conversations/{conversation.json()['id']}/runs",
            headers=mallory,
            json={"prompt": "inspect", "mode": "ask"},
        )

        assert conversation.json()["owner_id"] == "api"
        assert response.status_code == 202


@pytest.mark.asyncio
async def test_api_plan_requires_explicit_decision(tmp_path: Path) -> None:
    app, service = _app(tmp_path)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": "alice"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        conversation = await client.post(
            "/v1/conversations",
            headers=headers,
            json={"workspace": str(tmp_path), "agent": "claude"},
        )
        run_response = await client.post(
            f"/v1/conversations/{conversation.json()['id']}/runs",
            headers=headers,
            json={"prompt": "change", "mode": "run"},
        )
        run_id = run_response.json()["id"]
        waiting = await service.wait_for_terminal(run_id)
        assert waiting.status == "awaiting_approval"

        decision = await client.post(
            f"/v1/runs/{run_id}/decision",
            headers=headers,
            json={"decision": "approve"},
        )
        assert decision.status_code == 200
        completed = await service.wait_for_terminal(run_id)
        assert completed.status == "completed"
