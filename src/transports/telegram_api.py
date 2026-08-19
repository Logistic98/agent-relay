from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from core.security import redact_text

Sleep = Callable[[float], Awaitable[None]]


class TelegramAPIError(Exception):
    def __init__(self, status_code: int, description: str) -> None:
        self.status_code = status_code
        self.description = redact_text(description, limit=300)
        super().__init__(f"Telegram Bot API HTTP {status_code}: {self.description}")


class TelegramAPI:
    """Minimal async Bot API client that never logs or exposes the token URL."""

    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        attempts: int = 3,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._http = http
        self._sleep = sleep
        self._attempts = attempts

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            body["reply_markup"] = reply_markup
        result = await self._request("POST", "sendMessage", json=body)
        return int(result["message_id"])

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        await self._request("POST", "editMessageText", json=body)

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        await self._request("POST", "answerCallbackQuery", json=body)

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": '["message","callback_query"]',
        }
        if offset is not None:
            params["offset"] = offset
        result = await self._request(
            "GET",
            "getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        return result if isinstance(result, list) else []

    async def delete_webhook(self) -> None:
        await self._request("POST", "deleteWebhook", json={"drop_pending_updates": False})

    async def set_commands(self) -> None:
        commands = [
            {"command": "home", "description": "打开主页"},
            {"command": "new", "description": "开始无项目的新对话"},
            {"command": "projects", "description": "选择项目"},
            {"command": "sessions", "description": "查看和切换历史会话"},
            {"command": "agent", "description": "切换 Codex 或 Claude"},
            {"command": "stop", "description": "停止当前任务"},
            {"command": "help", "description": "查看用法"},
        ]
        await self._request("POST", "setMyCommands", json={"commands": commands})

    async def set_menu_button(self, webapp_url: str) -> None:
        await self._request(
            "POST",
            "setChatMenuButton",
            json={
                "menu_button": {
                    "type": "web_app",
                    "text": "打开工作台",
                    "web_app": {"url": webapp_url},
                }
            },
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._http.request(
                    method,
                    f"{self._base_url}/{endpoint}",
                    json=json,
                    params=params,
                    timeout=timeout,
                )
            except httpx.TransportError as exc:
                if attempt == self._attempts:
                    raise TelegramAPIError(0, exc.__class__.__name__) from exc
                await self._sleep(float(attempt))
                continue

            payload: dict[str, Any] = {}
            try:
                decoded = response.json()
                if isinstance(decoded, dict):
                    payload = decoded
            except ValueError:
                pass

            if response.status_code == 429 and attempt < self._attempts:
                parameters = payload.get("parameters")
                retry_after = parameters.get("retry_after", attempt) if isinstance(parameters, dict) else attempt
                await self._sleep(min(float(retry_after), 30.0))
                continue
            if response.status_code >= 500 and attempt < self._attempts:
                await self._sleep(float(attempt))
                continue
            if response.is_error or payload.get("ok") is False:
                description = str(payload.get("description") or response.reason_phrase or "request failed")
                raise TelegramAPIError(response.status_code, description)
            return payload.get("result")

        raise TelegramAPIError(0, "retry attempts exhausted")
