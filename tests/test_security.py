from pathlib import Path

import pytest

from core.exceptions import WorkspaceDeniedError
from core.security import redact_text, resolve_workspace, sanitized_subprocess_env


def test_redact_text_masks_common_credentials() -> None:
    text = (
        "token=secret-value sk-proj-abcdefghijklmnop 123456789:abcdefghijklmnopqrstuvwx "
        "Bearer abcdefghijklmnop https://api.telegram.org/bot123456789:abcdefghijklmnopqrstuvwx/sendMessage"
    )
    redacted = redact_text(text)
    assert "secret-value" not in redacted
    assert "sk-proj" not in redacted
    assert "123456789:" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "/bot123456789:" not in redacted
    assert redacted.count("[REDACTED]") == 5


def test_redact_text_truncates() -> None:
    assert len(redact_text("x" * 100, limit=30)) == 27
    assert redact_text("short", limit=30) == "short"


def test_resolve_workspace_enforces_real_path_boundary(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    child = allowed / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()

    assert resolve_workspace(child, [allowed]) == child.resolve()
    with pytest.raises(WorkspaceDeniedError):
        resolve_workspace(outside, [allowed])


def test_resolve_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceDeniedError):
        resolve_workspace(link, [allowed])


def test_subprocess_environment_is_allowlisted() -> None:
    result = sanitized_subprocess_env(
        ["PATH", "HOME"],
        {"PATH": "/bin", "HOME": "/tmp/home", "TELEGRAM_BOT_TOKEN": "secret"},
    )
    assert result["PATH"] == "/bin"
    assert result["HOME"] == "/tmp/home"
    assert "TELEGRAM_BOT_TOKEN" not in result
    assert result["NO_COLOR"] == "1"


def test_subprocess_environment_drops_relative_and_world_writable_path_entries(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe-bin"
    unsafe.mkdir()
    unsafe.chmod(0o777)

    result = sanitized_subprocess_env(
        ["PATH"],
        {"PATH": f".:relative:{unsafe}:/tmp:/usr/bin:/bin:"},
    )

    assert result["PATH"] == "/usr/bin:/bin"
