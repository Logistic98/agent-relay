from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from transports.telegram_api import TelegramAPI, TelegramAPIError


@pytest.mark.asyncio
async def test_send_message_returns_message_id_without_parse_mode() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = TelegramAPI("test-token", http)
        assert await api.send_message("1", "hello") == 42

    assert "sendMessage" in seen["url"]
    assert "parse_mode" not in seen["body"]


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"ok": False, "description": "slow down", "parameters": {"retry_after": 2}},
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = TelegramAPI("test-token", http, sleep=sleep)
        assert await api.get_updates(None, 1) == []

    assert calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_api_error_redacts_secret_description_and_token_url() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "token=super-secret-value"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = TelegramAPI("bot-token-must-not-leak", http)
        with pytest.raises(TelegramAPIError) as caught:
            await api.send_message("1", "hello")

    assert "super-secret-value" not in str(caught.value)
    assert "bot-token-must-not-leak" not in str(caught.value)


@pytest.mark.asyncio
async def test_get_updates_requests_messages_and_callbacks() -> None:
    request_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_url
        request_url = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = TelegramAPI("token", http)
        assert await api.get_updates(9, 20) == [{"update_id": 1}]

    assert "offset=9" in request_url
    assert "callback_query" in request_url


@pytest.mark.asyncio
async def test_command_menu_keeps_only_common_user_actions() -> None:
    body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await TelegramAPI("token", http).set_commands()

    commands = [item["command"] for item in body["commands"]]
    assert commands == ["home", "new", "projects", "sessions", "agent", "stop", "help"]


@pytest.mark.asyncio
async def test_menu_button_opens_telegram_workbench() -> None:
    body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await TelegramAPI("token", http).set_menu_button("https://relay.example/app")

    assert body["menu_button"] == {
        "type": "web_app",
        "text": "打开工作台",
        "web_app": {"url": "https://relay.example/app"},
    }
