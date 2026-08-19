from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agents import create_agent_registry
from api.routes import router
from core.config import Settings, get_settings
from core.preflight import require_agent_preflight
from persistence.database import Database
from services.relay import RelayService
from transports.telegram import TelegramBot
from transports.telegram_api import TelegramAPI
from webapp.routes import _STATIC_ROOT
from webapp.routes import router as webapp_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    relay_service: RelayService | None = None,
) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        relay = relay_service
        if relay is None:
            runtime_settings = await require_agent_preflight(configured)
            database = Database(runtime_settings.database_path)
            relay = RelayService(runtime_settings, database, create_agent_registry(runtime_settings))
        app.state.relay_service = relay
        await relay.start()

        telegram_task: asyncio.Task[None] | None = None
        stop_event = asyncio.Event()
        http: httpx.AsyncClient | None = None
        if configured.telegram_enabled:
            http = httpx.AsyncClient(timeout=configured.telegram_poll_timeout_seconds + 10)
            telegram_api = TelegramAPI(configured.telegram_token_value, http)
            telegram = TelegramBot(configured, telegram_api, relay)
            telegram_task = asyncio.create_task(
                _supervise_telegram(telegram, relay, stop_event),
                name="telegram-polling",
            )

        try:
            yield
        finally:
            stop_event.set()
            if telegram_task:
                telegram_task.cancel()
                await asyncio.gather(telegram_task, return_exceptions=True)
            if http:
                await http.aclose()
            await relay.stop()

    app = FastAPI(
        title=configured.app_name,
        version="0.1.0",
        docs_url="/docs" if configured.api_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = configured
    app.include_router(router)
    app.include_router(webapp_router)
    app.mount("/app/assets", StaticFiles(directory=_STATIC_ROOT), name="telegram-app-assets")
    return app


app = create_app()


async def _supervise_telegram(
    telegram: TelegramBot,
    relay: RelayService,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        relay.set_telegram_transport_running(True)
        try:
            await telegram.run(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram transport crashed; retrying")
        finally:
            relay.set_telegram_transport_running(False)
        if stop_event.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=5)
