from __future__ import annotations

import asyncio
import os
import stat
import textwrap
from pathlib import Path

import pytest

from runtime.process import ProcessRunner


def _executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(source), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.asyncio
async def test_runner_uses_stdin_drains_both_streams_and_redacts_stderr(tmp_path: Path) -> None:
    executable = _executable(
        tmp_path / "fake-agent",
        """
        import sys

        prompt = sys.stdin.read()
        print("first:" + prompt, flush=True)
        print("token=super-secret-value-123456", file=sys.stderr, flush=True)
        print("second", flush=True)
        """,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    runner = ProcessRunner(output_limit_bytes=1_024)

    result = await runner.run(
        "stdin-run",
        [str(executable)],
        cwd=str(tmp_path),
        stdin="prompt with ; $(not-a-command)",
        timeout=5,
        on_stdout_line=lambda line: stdout_lines.append(line),
        on_stderr_line=lambda line: stderr_lines.append(line),
    )

    assert result.exit_code == 0
    assert stdout_lines == ["first:prompt with ; $(not-a-command)", "second"]
    assert stderr_lines == ["token=[REDACTED]"]
    assert "super-secret" not in result.stderr
    assert "[REDACTED]" in result.stderr
    assert not result.cancelled
    assert not result.timed_out


@pytest.mark.asyncio
async def test_runner_caps_combined_capture_and_stops_persistable_callbacks(tmp_path: Path) -> None:
    executable = _executable(
        tmp_path / "noisy-agent",
        """
        import sys

        sys.stdin.read()
        print("A" * 200, flush=True)
        print("B" * 200, file=sys.stderr, flush=True)
        print("after-limit", flush=True)
        """,
    )
    lines: list[str] = []
    runner = ProcessRunner(output_limit_bytes=64)

    result = await runner.run(
        "bounded-run",
        [str(executable)],
        cwd=str(tmp_path),
        stdin="x",
        timeout=5,
        on_stdout_line=lambda line: lines.append(line),
    )

    assert result.exit_code == 0
    assert result.output_truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 64
    assert lines == []


@pytest.mark.asyncio
async def test_runner_caps_event_callback_lines(tmp_path: Path) -> None:
    executable = _executable(
        tmp_path / "many-events-agent",
        """
        import sys

        sys.stdin.read()
        for index in range(10):
            print(index, flush=True)
        """,
    )
    lines: list[str] = []
    runner = ProcessRunner(output_limit_bytes=1_024, event_line_limit=3)

    result = await runner.run(
        "event-limit-run",
        [str(executable)],
        cwd=str(tmp_path),
        stdin="",
        timeout=5,
        on_stdout_line=lambda line: lines.append(line),
    )

    assert lines == ["0", "1", "2"]
    assert result.output_truncated


@pytest.mark.asyncio
async def test_runner_timeout_stops_process_group(tmp_path: Path) -> None:
    executable = _executable(
        tmp_path / "sleeping-agent",
        """
        import time

        time.sleep(30)
        """,
    )
    runner = ProcessRunner(
        output_limit_bytes=1_024,
        interrupt_grace_seconds=0.1,
        terminate_grace_seconds=0.1,
    )

    result = await runner.run(
        "timeout-run",
        [str(executable)],
        cwd=str(tmp_path),
        stdin="",
        timeout=0.05,
    )

    assert result.timed_out
    assert not result.cancelled
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_stdout_callback_failure_stops_agent_and_propagates_error(tmp_path: Path) -> None:
    executable = _executable(
        tmp_path / "callback-agent",
        """
        import time

        print("event", flush=True)
        time.sleep(30)
        """,
    )
    runner = ProcessRunner(
        output_limit_bytes=1_024,
        interrupt_grace_seconds=0.1,
        terminate_grace_seconds=0.1,
    )

    async def reject_event(_line: str) -> None:
        raise RuntimeError("event store unavailable")

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await asyncio.wait_for(
            runner.run(
                "callback-run",
                [str(executable)],
                cwd=str(tmp_path),
                stdin="",
                timeout=5,
                on_stdout_line=reject_event,
            ),
            timeout=2,
        )


@pytest.mark.asyncio
async def test_cancel_escalates_from_sigint_to_sigterm_and_sigkill(tmp_path: Path) -> None:
    signal_log = tmp_path / "signals.log"
    ready = tmp_path / "ready"
    executable = _executable(
        tmp_path / "stubborn-agent",
        """
        import os
        import signal
        import sys
        import time

        signal_log, ready = sys.argv[1:]

        def record(name):
            def handler(_signum, _frame):
                fd = os.open(signal_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (name + "\\n").encode())
                finally:
                    os.close(fd)
            return handler

        signal.signal(signal.SIGINT, record("SIGINT"))
        signal.signal(signal.SIGTERM, record("SIGTERM"))
        open(ready, "w", encoding="utf-8").close()
        while True:
            time.sleep(0.05)
        """,
    )
    runner = ProcessRunner(
        output_limit_bytes=1_024,
        interrupt_grace_seconds=0.1,
        terminate_grace_seconds=0.1,
    )
    task = asyncio.create_task(
        runner.run(
            "cancel-run",
            [str(executable), str(signal_log), str(ready)],
            cwd=str(tmp_path),
            stdin="",
            timeout=5,
        )
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()

    assert await runner.cancel("cancel-run")
    result = await asyncio.wait_for(task, timeout=2)

    assert result.cancelled
    assert not result.timed_out
    assert result.exit_code == -9
    assert signal_log.read_text(encoding="utf-8").splitlines() == ["SIGINT", "SIGTERM"]
    assert not await runner.cancel("cancel-run")


@pytest.mark.asyncio
async def test_runner_starts_a_new_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = _executable(tmp_path / "session-agent", "")
    observed: dict[str, object] = {}
    original = asyncio.create_subprocess_exec

    async def recording_exec(*argv: str, **kwargs: object):
        observed["argv"] = argv
        observed.update(kwargs)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    runner = ProcessRunner(output_limit_bytes=1_024)

    await runner.run("session-run", [str(executable), ""], cwd=str(tmp_path), stdin="", timeout=5)

    assert observed["argv"] == (str(executable), "")
    assert observed["start_new_session"] is True
    assert "shell" not in observed
    assert observed["stdin"] == asyncio.subprocess.PIPE
    assert observed["stdout"] == asyncio.subprocess.PIPE
    assert observed["stderr"] == asyncio.subprocess.PIPE
    assert os.path.samefile(str(observed["cwd"]), tmp_path)


@pytest.mark.asyncio
async def test_cancel_during_process_start_never_writes_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt_marker = tmp_path / "prompt-received"
    executable = _executable(
        tmp_path / "stdin-gated-agent",
        """
        import pathlib
        import sys

        prompt = sys.stdin.read()
        if prompt:
            pathlib.Path(sys.argv[1]).write_text(prompt, encoding="utf-8")
        """,
    )
    original = asyncio.create_subprocess_exec
    starting = asyncio.Event()
    release = asyncio.Event()

    async def delayed_exec(*argv: str, **kwargs: object):
        starting.set()
        await release.wait()
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_exec)
    runner = ProcessRunner(
        output_limit_bytes=1_024,
        interrupt_grace_seconds=0.1,
        terminate_grace_seconds=0.1,
    )
    task = asyncio.create_task(
        runner.run(
            "cancel-before-start",
            [str(executable), str(prompt_marker)],
            cwd=str(tmp_path),
            stdin="must-not-be-delivered",
            timeout=5,
        )
    )
    await asyncio.wait_for(starting.wait(), timeout=1)

    assert await runner.cancel("cancel-before-start")
    release.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.cancelled
    assert not prompt_marker.exists()
