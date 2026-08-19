from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import ValidationError

from agents import create_agent_registry
from core.config import Settings
from core.logging import configure_logging
from core.preflight import configured_agent_diagnostic as _configured_agent_diagnostic
from core.security import redact_text, resolve_workspace
from domain.models import AgentKind, EventKind, RunMode, RunStatus
from main import create_app
from persistence.database import Database
from services.relay import RelayService


def main() -> None:
    raise SystemExit(run_cli())


def run_cli(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"配置错误：{redact_text(str(exc), limit=2000)}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)

    if args.command == "serve":
        preflight = asyncio.run(_doctor(settings, as_json=False))
        if preflight != 0:
            print("启动已拒绝：请先修复 doctor 报告的配置、CLI 或路径安全问题。", file=sys.stderr)
            return preflight
        uvicorn.run(
            create_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
            access_log=True,
        )
        return 0
    if args.command == "doctor":
        return asyncio.run(_doctor(settings, as_json=args.json))
    if args.command == "demo":
        return asyncio.run(
            _demo(
                settings,
                AgentKind(args.agent),
                Path(args.workspace),
                args.mode,
                args.prompt,
            )
        )
    parser.error("unknown command")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-relay", description="Relay local coding agents to Telegram and HTTP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="start Telegram polling and the loopback API")
    doctor = subparsers.add_parser(
        "doctor",
        help="check configuration, database, CLI versions, and locally reported login state",
    )
    doctor.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    demo = subparsers.add_parser("demo", help="run the relay lifecycle locally without Telegram")
    demo.add_argument("--agent", choices=[item.value for item in AgentKind], default=AgentKind.CODEX.value)
    demo.add_argument("--workspace", default=".")
    demo.add_argument("--mode", choices=["auto", *[item.value for item in RunMode]], default="auto")
    demo.add_argument("--prompt", required=True)
    return parser


async def _doctor(settings: Settings, *, as_json: bool) -> int:
    report: dict[str, Any] = {
        "workspace": {"ok": False},
        "database": {"ok": False},
        "telegram": {
            "enabled": settings.telegram_enabled,
            "allowlisted_chats": len(settings.telegram_allowed_chat_ids),
            "allowlisted_users": len(settings.telegram_allowed_user_ids),
        },
        "api": {
            "enabled": settings.api_enabled,
            "host": settings.api_host,
            "actor_id": settings.api_actor_id,
            "token_configured": bool(settings.api_token_value),
        },
        "agents": {},
    }
    try:
        workspace = resolve_workspace(settings.default_workspace, settings.workspace_roots)
        report["workspace"] = {"ok": True, "path": str(workspace)}
    except Exception as exc:
        report["workspace"] = {"ok": False, "error": redact_text(str(exc), limit=500)}

    database = Database(settings.database_path)
    try:
        await database.initialize()
        report["database"] = {"ok": await database.ping(), "path": str(settings.database_path)}
    except Exception as exc:
        report["database"] = {"ok": False, "error": redact_text(str(exc), limit=500)}
    finally:
        await database.close()

    report["agents"] = {
        "codex": await _configured_agent_diagnostic(settings, AgentKind.CODEX),
        "claude": await _configured_agent_diagnostic(settings, AgentKind.CLAUDE),
    }
    success = bool(report["workspace"]["ok"] and report["database"]["ok"])
    success = success and all(
        item["available"] and item["path_secure"] and item["compatible"] and item["login_reported"]
        for item in report["agents"].values()
    )
    success = success and (not settings.api_enabled or bool(settings.api_token_value))
    report["status"] = "ok" if success else "error"

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Agent Relay doctor: {report['status']}")
        print(f"Workspace: {'ok' if report['workspace']['ok'] else 'error'}")
        print(f"Database: {'ok' if report['database']['ok'] else 'error'}")
        for name, item in report["agents"].items():
            version = item.get("version") or "unknown"
            auth = "login reported (not live-verified)" if item["login_reported"] else "login not reported"
            compatibility = "compatible" if item["compatible"] else f"requires >= {item['minimum_version']}"
            path_status = "secure path" if item["path_secure"] else f"unsafe path: {item.get('unsafe_path')}"
            print(f"{name}: {version}, {compatibility}, {auth}, {path_status}")
        print(
            "Telegram: "
            + ("configured" if settings.telegram_enabled else "disabled")
            + f", API: {'enabled' if settings.api_enabled else 'disabled'}"
        )
    return 0 if success else 1


async def _demo(
    base_settings: Settings,
    agent: AgentKind,
    workspace: Path,
    mode: str,
    prompt: str,
) -> int:
    diagnostic = await _configured_agent_diagnostic(base_settings, agent)
    if (
        not diagnostic["available"]
        or not diagnostic["path_secure"]
        or not diagnostic["compatible"]
        or not diagnostic["login_reported"]
    ):
        print(
            f"{agent.value} preflight failed: version={diagnostic.get('version') or 'missing'}, "
            f"minimum={diagnostic['minimum_version']}, login_reported={diagnostic['login_reported']}, "
            f"unsafe_path={diagnostic.get('unsafe_path')}",
            file=sys.stderr,
        )
        return 1
    resolved = resolve_workspace(workspace, [workspace])
    with tempfile.TemporaryDirectory(prefix="agent-relay-demo-") as temporary:
        settings = base_settings.model_copy(
            update={
                "default_workspace": resolved,
                "workspace_roots": [resolved],
                "database_path": Path(temporary) / "demo.db",
                "telegram_enabled": False,
                "api_enabled": False,
            }
        )
        relay = RelayService(settings, Database(settings.database_path), create_agent_registry(settings))
        await relay.start()
        try:
            conversation = await relay.create_conversation(
                owner_type="demo",
                owner_id="local",
                workspace=resolved,
                agent=agent,
                title="local demo",
            )
            run = await relay.submit_run(
                conversation.id,
                prompt,
                RunMode.RUN if mode == "auto" else RunMode(mode),
                owner_type="demo",
                owner_id="local",
                initiator_id="local",
                auto_route=mode == "auto",
            )
            seq = await _stream_console(relay, run.id, 0)
            settled = await relay.get_run(run.id)
            if settled.status == RunStatus.AWAITING_APPROVAL:
                print("\n\nPlan awaiting approval:\n")
                print(settled.plan or "(no plan text)")
                answer = await asyncio.to_thread(
                    input, "\nType APPROVE to execute this plan; anything else rejects it: "
                )
                if answer.strip() == "APPROVE":
                    await relay.approve_run(
                        run.id,
                        "local",
                        owner_type="demo",
                        owner_id="local",
                    )
                    await _stream_console(relay, run.id, seq)
                else:
                    await relay.reject_run(
                        run.id,
                        "local",
                        owner_type="demo",
                        owner_id="local",
                    )
                    print("Plan rejected; no write phase was started.")
            final = await relay.get_run(run.id)
            print(f"\nFinal status: {final.status.value}")
            return 0 if final.status in {RunStatus.COMPLETED, RunStatus.REJECTED} else 1
        finally:
            await relay.stop()


async def _stream_console(relay: RelayService, run_id: str, after_seq: int) -> int:
    seq = after_seq
    while True:
        events = await relay.wait_for_events(run_id, seq, timeout=2)
        for event in events:
            seq = event.seq
            if event.kind == EventKind.OUTPUT_DELTA:
                print(str(event.payload.get("text") or ""), end="", flush=True)
            elif event.kind == EventKind.TOOL_STARTED:
                tool = event.payload.get("tool") or event.payload.get("name") or "tool"
                detail = event.payload.get("command") or ""
                print(f"\n[tool] {tool} {detail}")
            elif event.kind == EventKind.AGENT_STATUS:
                print(f"\n[status] {event.payload.get('message') or event.payload.get('status') or 'working'}")
        run = await relay.get_run(run_id)
        if run.status in TERMINAL_OR_APPROVAL:
            return seq


TERMINAL_OR_APPROVAL = {
    RunStatus.AWAITING_APPROVAL,
    RunStatus.CANCELLED,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.REJECTED,
    RunStatus.INTERRUPTED,
    RunStatus.TIMED_OUT,
}
