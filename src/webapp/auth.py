"""Telegram Mini App identity verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request, status


@dataclass(frozen=True, slots=True)
class TelegramWebIdentity:
    user_id: str
    chat_id: str

    @property
    def owner_id(self) -> str:
        return f"{self.chat_id}:{self.user_id}"


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 3_600,
) -> dict[str, object]:
    if not init_data or len(init_data) > 16_384:
        raise ValueError("Telegram init data is missing or too large")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    received_hash = fields.pop("hash", "")
    if len(received_hash) != 64:
        raise ValueError("Telegram init data hash is missing")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("Telegram init data signature is invalid")

    current = int(time.time()) if now is None else now
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Telegram init data auth date is invalid") from exc
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise ValueError("Telegram init data has expired")
    try:
        user = json.loads(fields["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram init data user is invalid") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int | str):
        raise ValueError("Telegram init data user is invalid")
    return user


async def require_telegram_web_auth(request: Request) -> TelegramWebIdentity:
    settings = request.app.state.settings
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        try:
            user = validate_telegram_init_data(init_data, settings.telegram_token_value)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        user_id = str(user["id"])
    elif request.url.hostname in {"127.0.0.1", "localhost", "::1"}:
        if not settings.telegram_allowed_user_ids:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram is not configured")
        user_id = settings.telegram_allowed_user_ids[0]
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram authorization required")

    if user_id not in settings.telegram_allowed_user_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Telegram user is not allowlisted")
    if user_id in settings.telegram_allowed_chat_ids:
        chat_id = user_id
    elif len(settings.telegram_allowed_chat_ids) == 1:
        chat_id = settings.telegram_allowed_chat_ids[0]
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Telegram chat cannot be resolved")
    return TelegramWebIdentity(user_id=user_id, chat_id=chat_id)
