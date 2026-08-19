from pathlib import Path

import pytest
from pydantic import ValidationError

from cli import run_cli
from core.config import Settings
from core.preflight import agent_diagnostic as _agent_diagnostic
from core.preflight import version_tuple as _version_tuple
from core.security import first_unsafe_path as _first_unsafe_executable_path


def test_version_tuple_parses_both_cli_version_formats() -> None:
    assert _version_tuple("codex-cli 0.148.0") == (0, 148, 0)
    assert _version_tuple("2.1.235 (Claude Code)") == (2, 1, 235)
    assert _version_tuple("unknown") is None


def test_enabled_api_rejects_short_or_placeholder_tokens(tmp_path: Path) -> None:
    common = {"default_workspace": tmp_path, "workspace_roots": [tmp_path]}
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(**common, api_bearer_token="too-short")
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(**common, api_bearer_token="replace-with-a-generated-random-token")


def test_executable_path_rejects_non_sticky_world_writable_directory(tmp_path: Path) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir(mode=0o700)
    executable = executable_dir / "agent"
    executable.touch(mode=0o700)

    assert _first_unsafe_executable_path(executable) is None

    executable_dir.chmod(0o777)
    assert _first_unsafe_executable_path(executable) == executable_dir


def test_executable_path_rejects_world_writable_sticky_regular_file(tmp_path: Path) -> None:
    executable = tmp_path / "agent"
    executable.touch(mode=0o700)
    executable.chmod(0o1707)

    assert _first_unsafe_executable_path(executable) == executable


@pytest.mark.asyncio
async def test_agent_diagnostic_never_executes_from_unsafe_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir(mode=0o777)
    executable_dir.chmod(0o777)
    executable = executable_dir / "agent"
    executable.touch(mode=0o700)
    executed = False

    async def unexpected_command(argv: list[str]) -> tuple[int, str]:
        del argv
        nonlocal executed
        executed = True
        return 0, "agent 1.2.3"

    monkeypatch.setattr("core.preflight.shutil.which", lambda _command, path=None: str(executable))
    monkeypatch.setattr("core.preflight.short_command", unexpected_command)

    report = await _agent_diagnostic("agent", ["--version"], ["auth"], minimum_version=(1, 0, 0))

    assert not executed
    assert report["available"]
    assert not report["path_secure"]
    assert report["unsafe_path"] == str(executable_dir)
    assert not report["compatible"]
    assert not report["login_reported"]


@pytest.mark.asyncio
async def test_agent_diagnostic_rejects_relative_executable_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "agent"
    executable.touch(mode=0o700)
    monkeypatch.chdir(tmp_path)
    executed = False

    async def unexpected_command(argv: list[str]) -> tuple[int, str]:
        del argv
        nonlocal executed
        executed = True
        return 0, "agent 1.2.3"

    monkeypatch.setattr("core.preflight.short_command", unexpected_command)

    report = await _agent_diagnostic("./agent", ["--version"], ["auth"], minimum_version=(1, 0, 0))

    assert not executed
    assert not report["path_secure"]
    assert report["path_error"] == "resolved executable path is not absolute"


@pytest.mark.asyncio
async def test_agent_diagnostic_rejects_bare_name_from_sticky_shared_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared-bin"
    shared.mkdir()
    shared.chmod(0o1777)
    executable = shared / "agent"
    executable.touch(mode=0o700)
    monkeypatch.setenv("PATH", f"{shared}:/usr/bin:/bin")
    executed = False

    async def unexpected_command(argv: list[str]) -> tuple[int, str]:
        del argv
        nonlocal executed
        executed = True
        return 0, "agent 1.2.3"

    monkeypatch.setattr("core.preflight.short_command", unexpected_command)

    report = await _agent_diagnostic("agent", ["--version"], ["auth"], minimum_version=(1, 0, 0))

    assert not executed
    assert not report["path_secure"]
    assert report["unsafe_path"] == str(shared)
    assert report["path_error"] == "executable is reachable only through an unsafe PATH entry"


def test_serve_cannot_bypass_failed_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        database_path=tmp_path / "relay.db",
        api_enabled=False,
        telegram_enabled=False,
    )
    server_started = False

    async def failed_doctor(settings: Settings, *, as_json: bool) -> int:
        del settings, as_json
        return 1

    def unexpected_server(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal server_started
        server_started = True

    monkeypatch.setattr("cli.Settings", lambda: settings)
    monkeypatch.setattr("cli._doctor", failed_doctor)
    monkeypatch.setattr("cli.uvicorn.run", unexpected_server)

    assert run_cli(["serve"]) == 1
    assert not server_started
