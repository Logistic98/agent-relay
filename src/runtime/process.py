from __future__ import annotations

import asyncio
import inspect
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field

from core.security import redact_text

LineCallback = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    cancelled: bool = False
    timed_out: bool = False
    output_truncated: bool = False


@dataclass(slots=True)
class _BoundedCapture:
    limit: int
    streams: dict[str, bytearray] = field(default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()})
    size: int = 0
    truncated: bool = False
    delivered_lines: int = 0
    callbacks_suppressed: bool = False

    def add(self, stream_name: str, chunk: bytes) -> None:
        remaining = max(0, self.limit - self.size)
        if remaining:
            captured = chunk[:remaining]
            self.streams[stream_name].extend(captured)
            self.size += len(captured)
        if len(chunk) > remaining:
            self.truncated = True

    def text(self, stream_name: str) -> str:
        return self.streams[stream_name].decode("utf-8", errors="replace")


@dataclass(slots=True)
class _ProcessHandle:
    process: asyncio.subprocess.Process
    wait_task: asyncio.Task[int]
    process_group_id: int
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    timed_out: bool = False


class ProcessRunner:
    """Run one-shot agent CLIs in isolated POSIX process groups.

    The runner intentionally never accepts a shell command string. Arguments are
    passed directly to ``create_subprocess_exec`` and prompts are written through
    stdin so untrusted prompt text cannot become command-line syntax.
    """

    def __init__(
        self,
        *,
        output_limit_bytes: int = 2_000_000,
        event_line_limit: int = 5_000,
        interrupt_grace_seconds: float = 5,
        terminate_grace_seconds: float = 3,
    ) -> None:
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if event_line_limit <= 0:
            raise ValueError("event_line_limit must be positive")
        if interrupt_grace_seconds < 0 or terminate_grace_seconds < 0:
            raise ValueError("process grace periods cannot be negative")
        self.output_limit_bytes = output_limit_bytes
        self.event_line_limit = event_line_limit
        self.interrupt_grace_seconds = interrupt_grace_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self._active: dict[str, _ProcessHandle] = {}
        self._starting: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._state_lock = asyncio.Lock()

    async def run(
        self,
        run_id: str,
        argv: Sequence[str],
        *,
        cwd: str,
        stdin: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_stdout_line: LineCallback | None = None,
        on_stderr_line: LineCallback | None = None,
    ) -> ProcessResult:
        if (
            not argv
            or not isinstance(argv[0], str)
            or not argv[0]
            or not all(isinstance(argument, str) for argument in argv)
        ):
            raise ValueError("argv must start with a non-empty executable and contain only strings")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")

        async with self._state_lock:
            if run_id in self._active or run_id in self._starting:
                raise RuntimeError(f"run {run_id!r} already has an active process")
            self._starting.add(run_id)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            async with self._state_lock:
                self._starting.discard(run_id)
                self._cancel_requested.discard(run_id)
            raise

        wait_task = asyncio.create_task(process.wait(), name=f"process-wait:{run_id}")
        handle = _ProcessHandle(process=process, wait_task=wait_task, process_group_id=process.pid)
        async with self._state_lock:
            self._starting.discard(run_id)
            self._active[run_id] = handle
            cancel_before_start = run_id in self._cancel_requested
            self._cancel_requested.discard(run_id)

        capture = _BoundedCapture(self.output_limit_bytes)
        callback_errors: list[BaseException] = []
        callback_error_event = asyncio.Event()
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            self._consume_stream(
                process.stdout,
                "stdout",
                capture,
                on_stdout_line,
                callback_errors,
                callback_error_event,
            ),
            name=f"stdout:{run_id}",
        )
        stderr_task = asyncio.create_task(
            self._consume_stream(
                process.stderr,
                "stderr",
                capture,
                on_stderr_line,
                callback_errors,
                callback_error_event,
            ),
            name=f"stderr:{run_id}",
        )
        callback_monitor = asyncio.create_task(
            callback_error_event.wait(),
            name=f"callback-monitor:{run_id}",
        )

        try:
            if cancel_before_start or handle.cancelled:
                handle.cancelled = True
                await self._stop_process(handle)
            else:
                await self._write_stdin(process, stdin)
                completed, _ = await asyncio.wait(
                    {wait_task, callback_monitor},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not completed:
                    handle.timed_out = True
                    await self._stop_process(handle)
                elif callback_monitor in completed:
                    # Event persistence/forwarding is part of the safety boundary.
                    # If it fails, do not let an unsupervised agent keep mutating.
                    await self._stop_process(handle)
            await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            handle.cancelled = True
            await asyncio.shield(self._stop_process(handle))
            await asyncio.shield(asyncio.gather(stdout_task, stderr_task, return_exceptions=True))
            raise
        except BaseException:
            await asyncio.shield(self._stop_process(handle))
            await asyncio.shield(asyncio.gather(stdout_task, stderr_task, return_exceptions=True))
            raise
        finally:
            callback_monitor.cancel()
            await asyncio.gather(callback_monitor, return_exceptions=True)
            if not wait_task.done():
                await self._force_kill(handle)
            async with self._state_lock:
                if self._active.get(run_id) is handle:
                    self._active.pop(run_id, None)
                self._cancel_requested.discard(run_id)

        if callback_errors:
            raise callback_errors[0]

        exit_code = process.returncode if process.returncode is not None else -signal.SIGKILL
        stdout, stdout_truncated = self._truncate_utf8(capture.text("stdout"), self.output_limit_bytes)
        stderr_budget = max(0, self.output_limit_bytes - len(stdout.encode("utf-8")))
        stderr, stderr_truncated = self._truncate_utf8(redact_text(capture.text("stderr")), stderr_budget)
        return ProcessResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cancelled=handle.cancelled,
            timed_out=handle.timed_out,
            output_truncated=capture.truncated or stdout_truncated or stderr_truncated,
        )

    async def cancel(self, run_id: str) -> bool:
        async with self._state_lock:
            handle = self._active.get(run_id)
            if handle is None:
                if run_id in self._starting:
                    self._cancel_requested.add(run_id)
                    return True
                return False
            if handle.wait_task.done() or handle.process.returncode is not None:
                return False
            handle.cancelled = True
        await self._stop_process(handle)
        return True

    async def _consume_stream(
        self,
        stream: asyncio.StreamReader,
        stream_name: str,
        capture: _BoundedCapture,
        callback: LineCallback | None,
        callback_errors: list[BaseException],
        callback_error_event: asyncio.Event,
    ) -> None:
        pending = bytearray()
        line_truncated = False
        while chunk := await stream.read(65_536):
            capture.add(stream_name, chunk)
            if callback is None or callback_errors or capture.truncated or capture.callbacks_suppressed:
                continue
            segments = chunk.split(b"\n")
            for index, segment in enumerate(segments):
                if not line_truncated:
                    remaining = max(0, self.output_limit_bytes - len(pending))
                    pending.extend(segment[:remaining])
                    line_truncated = len(segment) > remaining
                if index == len(segments) - 1:
                    continue
                raw_line = b"[stream line exceeded configured output limit]" if line_truncated else bytes(pending)
                pending.clear()
                line_truncated = False
                if capture.delivered_lines >= self.event_line_limit:
                    capture.callbacks_suppressed = True
                    capture.truncated = True
                    break
                capture.delivered_lines += 1
                if not await self._deliver_line(
                    callback,
                    raw_line,
                    stream_name,
                    callback_errors,
                    callback_error_event,
                ):
                    break
        if (
            callback is not None
            and (pending or line_truncated)
            and not callback_errors
            and not capture.truncated
            and not capture.callbacks_suppressed
        ):
            if capture.delivered_lines >= self.event_line_limit:
                capture.callbacks_suppressed = True
                capture.truncated = True
                return
            capture.delivered_lines += 1
            await self._deliver_line(
                callback,
                b"[stream line exceeded configured output limit]" if line_truncated else bytes(pending),
                stream_name,
                callback_errors,
                callback_error_event,
            )

    async def _deliver_line(
        self,
        callback: LineCallback,
        raw_line: bytes,
        stream_name: str,
        callback_errors: list[BaseException],
        callback_error_event: asyncio.Event,
    ) -> bool:
        try:
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
            if stream_name == "stderr":
                line = redact_text(line, limit=self.output_limit_bytes)
            await self._invoke_callback(callback, line)
        except BaseException as exc:  # callbacks are application boundaries
            callback_errors.append(exc)
            callback_error_event.set()
            return False
        return True

    @staticmethod
    async def _invoke_callback(callback: LineCallback, line: str) -> None:
        result = callback(line)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _write_stdin(process: asyncio.subprocess.Process, value: str) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(value.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

    async def _stop_process(self, handle: _ProcessHandle) -> None:
        async with handle.stop_lock:
            if not self._group_exists(handle.process_group_id):
                if not handle.wait_task.done():
                    await asyncio.shield(handle.wait_task)
                return
            await self._signal_and_wait(
                handle,
                signal.SIGINT,
                self.interrupt_grace_seconds,
            )
            if self._group_exists(handle.process_group_id):
                await self._signal_and_wait(
                    handle,
                    signal.SIGTERM,
                    self.terminate_grace_seconds,
                )
            if self._group_exists(handle.process_group_id):
                await self._force_kill(handle)
            elif not handle.wait_task.done():
                await asyncio.shield(handle.wait_task)

    async def _signal_and_wait(
        self,
        handle: _ProcessHandle,
        sig: signal.Signals,
        grace_seconds: float,
    ) -> None:
        self._signal_group(handle.process_group_id, sig)
        deadline = time.monotonic() + grace_seconds
        while self._group_exists(handle.process_group_id) and time.monotonic() < deadline:
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    async def _force_kill(self, handle: _ProcessHandle) -> None:
        self._signal_group(handle.process_group_id, signal.SIGKILL)
        if not handle.wait_task.done():
            await asyncio.shield(handle.wait_task)

    @staticmethod
    def _signal_group(process_group_id: int, sig: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, sig)

    @staticmethod
    def _group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _truncate_utf8(value: str, limit_bytes: int) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= limit_bytes:
            return value, False
        return encoded[:limit_bytes].decode("utf-8", errors="ignore"), True
