from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Any

from core.config import Settings
from core.security import first_unsafe_path, redact_text, safe_search_path, sanitized_subprocess_env
from domain.models import AgentKind

MIN_CODEX_VERSION = (0, 148, 0)
MIN_CLAUDE_VERSION = (2, 1, 163)


class UnsafeExecutablePathError(RuntimeError):
    def __init__(self, path: str | Path, reason: str) -> None:
        super().__init__(reason)
        self.path = Path(path)
        self.reason = reason


def _validated_found_executable(found: str) -> Path:
    unresolved = Path(found)
    if not unresolved.is_absolute():
        raise UnsafeExecutablePathError(unresolved, "resolved executable path is not absolute")
    candidate = Path(os.path.abspath(found))
    unsafe_path = first_unsafe_path(candidate)
    if unsafe_path is not None:
        raise UnsafeExecutablePathError(unsafe_path, "executable path contains a non-sticky world-writable entry")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise UnsafeExecutablePathError(candidate, "executable path cannot be resolved") from exc
    if not resolved.is_file():
        raise UnsafeExecutablePathError(resolved, "resolved executable is not a regular file")
    return resolved


def _find_executable(executable: str) -> str | None:
    if Path(executable).is_absolute() or os.sep in executable:
        return shutil.which(executable)
    raw_path = os.environ.get("PATH", "")
    found = shutil.which(executable, path=safe_search_path(raw_path))
    if found:
        return found
    unsafe_found = shutil.which(executable, path=raw_path)
    if unsafe_found:
        candidate = Path(unsafe_found)
        unsafe_path = first_unsafe_path(candidate) or candidate.parent
        raise UnsafeExecutablePathError(unsafe_path, "executable is reachable only through an unsafe PATH entry")
    return None


def resolve_safe_executable_path(executable: str) -> Path:
    found = _find_executable(executable)
    if not found:
        raise FileNotFoundError(executable)
    return _validated_found_executable(found)


async def agent_diagnostic(
    executable: str,
    version_args: list[str],
    auth_args: list[str],
    *,
    minimum_version: tuple[int, int, int],
) -> dict[str, Any]:
    minimum_text = ".".join(str(part) for part in minimum_version)
    try:
        found = _find_executable(executable)
    except UnsafeExecutablePathError as exc:
        return {
            "available": True,
            "path": executable,
            "path_secure": False,
            "unsafe_path": str(exc.path),
            "path_error": exc.reason,
            "version": None,
            "version_ok": False,
            "compatible": False,
            "minimum_version": minimum_text,
            "login_reported": False,
            "authentication_live_verified": False,
        }
    if not found:
        return {
            "available": False,
            "path_secure": False,
            "unsafe_path": None,
            "path_error": "executable was not found",
            "compatible": False,
            "minimum_version": minimum_text,
            "login_reported": False,
            "authentication_live_verified": False,
            "version": None,
        }
    try:
        path = _validated_found_executable(found)
    except UnsafeExecutablePathError as exc:
        return {
            "available": True,
            "path": found,
            "path_secure": False,
            "unsafe_path": str(exc.path),
            "path_error": exc.reason,
            "version": None,
            "version_ok": False,
            "compatible": False,
            "minimum_version": minimum_text,
            "login_reported": False,
            "authentication_live_verified": False,
        }

    version_code, version_output = await short_command([str(path), *version_args])
    auth_code, _ = await short_command([str(path), *auth_args])
    parsed_version = version_tuple(version_output)
    return {
        "available": True,
        "path": str(path),
        "path_secure": True,
        "unsafe_path": None,
        "path_error": None,
        "version": redact_text(
            version_output.strip().splitlines()[0] if version_output.strip() else "unknown", limit=200
        ),
        "version_ok": version_code == 0,
        "compatible": version_code == 0 and parsed_version is not None and parsed_version >= minimum_version,
        "minimum_version": minimum_text,
        "login_reported": auth_code == 0,
        "authentication_live_verified": False,
    }


async def configured_agent_diagnostic(settings: Settings, agent: AgentKind) -> dict[str, Any]:
    if agent is AgentKind.CODEX:
        return await agent_diagnostic(
            settings.codex_executable,
            ["--version"],
            [
                "-c",
                f'model_reasoning_effort="{settings.codex_reasoning_effort}"',
                "login",
                "status",
            ],
            minimum_version=MIN_CODEX_VERSION,
        )
    return await agent_diagnostic(
        settings.claude_executable,
        ["--version"],
        ["auth", "status", "--json"],
        minimum_version=MIN_CLAUDE_VERSION,
    )


async def configured_agent_diagnostics(settings: Settings) -> dict[str, dict[str, Any]]:
    codex, claude = await asyncio.gather(
        configured_agent_diagnostic(settings, AgentKind.CODEX),
        configured_agent_diagnostic(settings, AgentKind.CLAUDE),
    )
    return {AgentKind.CODEX.value: codex, AgentKind.CLAUDE.value: claude}


def diagnostics_succeeded(diagnostics: dict[str, dict[str, Any]]) -> bool:
    return all(
        item["available"] and item["path_secure"] and item["compatible"] and item["login_reported"]
        for item in diagnostics.values()
    )


async def require_agent_preflight(settings: Settings) -> Settings:
    diagnostics = await configured_agent_diagnostics(settings)
    if not diagnostics_succeeded(diagnostics):
        failures = []
        for name, item in diagnostics.items():
            failures.append(
                f"{name}: version={item.get('version') or 'unknown'}, minimum={item['minimum_version']}, "
                f"login_reported={item['login_reported']}, path_error={item.get('path_error') or 'none'}"
            )
        raise RuntimeError("Agent preflight failed; " + "; ".join(failures))
    return settings.model_copy(
        update={
            "codex_executable": diagnostics[AgentKind.CODEX.value]["path"],
            "claude_executable": diagnostics[AgentKind.CLAUDE.value]["path"],
        }
    )


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    return tuple(int(match.group(index)) for index in range(1, 4)) if match else None


async def short_command(argv: list[str]) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=sanitized_subprocess_env(["HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL"]),
            start_new_session=True,
        )
    except OSError as exc:
        return 1, exc.__class__.__name__
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return 1, "TimeoutError"
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    return process.returncode or 0, redact_text(stdout.decode(errors="replace"), limit=1000)
