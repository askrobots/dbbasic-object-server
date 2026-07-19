"""Persistent worker-process pool for near-in-process object execution.

Objects that are not listed in ``DBBASIC_TRUSTED_IN_PROCESS_OBJECTS`` run
through ``object_execution.execute_python_object_subprocess`` today, which
spawns a brand new interpreter for every single request. That keeps process
isolation and wall-clock timeout enforcement, but a cold interpreter boot
dominates latency (roughly 100-200ms) for objects that could otherwise run
in a couple of milliseconds.

This module keeps a small pool of long-lived worker subprocesses around
instead. Each worker runs a tiny read-request/write-response loop
(``_worker_main`` below, reachable as ``python -m object_worker_pool
--worker``) and re-loads the target object's source fresh on every request
via ``python_object_runtime.PythonObjectRuntime`` -- so a warm worker still
picks up source edits made between requests (the "live edit" guarantee
objects depend on). Isolation and timeout enforcement are preserved because
every request still runs in its own OS process; only the process-spawn cost
is amortized away by keeping the process around between requests.

Disabled by default: ``DBBASIC_WORKER_POOL_SIZE`` unset or ``0`` means the
pool is never constructed and callers should keep using the existing
spawn-per-request path untouched.

Wire protocol
--------------
Parent and worker exchange one JSON message per request/response, each
framed as a 4-byte big-endian length prefix followed by that many bytes of
UTF-8 JSON (``_encode_message`` / ``_read_message_async`` /
``_read_message_sync``).

Request message::

    {
        "object_id": str,
        "method": str,
        "payload": dict,
        "correlation_id": str | None,
        "path": str | None,        # pre-resolved by the pool manager
        "base_dir": str | None,    # optional PythonObjectRuntime base_dir override
    }

Response message is ``object_execution.ObjectExecutionResult.to_dict()``.

Worker stderr is left connected to the parent's stderr (not piped), so
worker-side tracebacks/log noise land in the normal server logs.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import signal
import struct
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import object_execution
from object_execution import (
    ObjectExecutionError,
    ObjectExecutionRequest,
    ObjectExecutionResult,
)
from object_namespace import resolve_object_id

WORKER_POOL_SIZE_ENV = "DBBASIC_WORKER_POOL_SIZE"

_LENGTH_PREFIX = struct.Struct(">I")
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # guard against a corrupt/desynced stream

# Reuse object_execution's private timestamp/duration/format helpers so the
# results this module builds (timeouts, crashes, worker responses) are
# byte-for-byte identical in shape to the ones execute_python_object_subprocess
# produces. These are considered part of the internal contract between the
# two modules, not object_execution's public API.
_utc_timestamp = object_execution._utc_timestamp
_duration_ms = object_execution._duration_ms
_format_seconds = object_execution._format_seconds


# ---------------------------------------------------------------------------
# Wire protocol helpers
# ---------------------------------------------------------------------------


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return _LENGTH_PREFIX.pack(len(body)) + body


async def _read_message_async(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(4)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > _MAX_MESSAGE_BYTES:
        raise ValueError(f"worker message too large ({length} bytes)")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


def _read_message_sync(stream: Any) -> dict[str, Any] | None:
    header = _read_exact_sync(stream, 4)
    if header is None:
        return None
    (length,) = _LENGTH_PREFIX.unpack(header)
    body = _read_exact_sync(stream, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _read_exact_sync(stream: Any, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Worker process (child side)
# ---------------------------------------------------------------------------


def _worker_main() -> None:
    """Entry point for a worker subprocess (``python -m object_worker_pool --worker``)."""
    from python_object_runtime import PythonObjectRuntime

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        try:
            request_message = _read_message_sync(stdin)
        except Exception:
            return
        if request_message is None:
            return  # parent closed stdin (pool shutdown) -- exit cleanly

        response = _handle_worker_request(request_message, PythonObjectRuntime())
        try:
            stdout.write(_encode_message(response))
            stdout.flush()
        except Exception:
            return


def _handle_worker_request(message: dict[str, Any], runtime: Any) -> dict[str, Any]:
    object_id = message.get("object_id") or ""
    method = message.get("method") or "GET"
    payload = message.get("payload") or {}
    correlation_id = message.get("correlation_id")
    path_value = message.get("path")

    request = ObjectExecutionRequest(
        object_id=object_id,
        method=method,
        payload=payload,
        path=Path(path_value) if path_value else None,
        correlation_id=correlation_id,
    )

    try:
        result = object_execution.execute_object(runtime, request, roots=None)
    except BaseException as exc:  # keep the worker loop alive no matter what happened
        result = _worker_error_result(request, exc)

    return result.to_dict()


def _worker_error_result(request: ObjectExecutionRequest, exc: BaseException) -> ObjectExecutionResult:
    import traceback

    now = _utc_timestamp()
    return ObjectExecutionResult(
        object_id=request.object_id,
        method=request.normalized_method(),
        path=request.path,
        ok=False,
        result=None,
        error=ObjectExecutionError(
            type=object_execution.WORKER_ERROR_TYPE,
            message=f"Object worker failed: {type(exc).__name__}: {exc}",
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ),
        started_at=now,
        finished_at=now,
        duration_ms=0.0,
        correlation_id=request.correlation_id,
    )


# ---------------------------------------------------------------------------
# Pool manager (parent side)
# ---------------------------------------------------------------------------

_LAZY = object()  # sentinel occupying a queue slot: "spawn a worker on demand"


class _Worker:
    __slots__ = ("process", "pid")

    def __init__(self, process: "asyncio.subprocess.Process"):
        self.process = process
        self.pid = process.pid


def _error_result(
    request: ObjectExecutionRequest,
    method: str,
    error_type: str,
    message: str,
    started_at: str,
    started_perf: float,
) -> ObjectExecutionResult:
    return ObjectExecutionResult(
        object_id=request.object_id,
        method=method,
        path=request.path,
        ok=False,
        result=None,
        error=ObjectExecutionError(type=error_type, message=message),
        started_at=started_at,
        finished_at=_utc_timestamp(),
        duration_ms=_duration_ms(started_perf),
        correlation_id=request.correlation_id,
    )


def _result_from_dict(data: dict[str, Any]) -> ObjectExecutionResult:
    error = None
    error_data = data.get("error")
    if error_data:
        error = ObjectExecutionError(
            type=error_data["type"],
            message=error_data["message"],
            traceback=error_data.get("traceback", ""),
        )

    path_value = data.get("path")
    return ObjectExecutionResult(
        object_id=data["object_id"],
        method=data["method"],
        path=Path(path_value) if path_value else None,
        ok=bool(data["ok"]),
        result=data.get("result"),
        error=error,
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        duration_ms=data["duration_ms"],
        correlation_id=data.get("correlation_id"),
    )


def _request_with_resolved_path(
    request: ObjectExecutionRequest,
    roots: Iterable[Path] | None,
) -> ObjectExecutionRequest:
    if request.path is not None:
        return request
    return ObjectExecutionRequest(
        object_id=request.object_id,
        method=request.method,
        payload=request.payload,
        path=resolve_object_id(request.object_id, roots),
        correlation_id=request.correlation_id,
    )


class WorkerPool:
    """Async pool of persistent worker subprocesses.

    Workers are spawned lazily -- nothing is started until the first
    ``execute()`` call needs one, and at most ``size`` workers ever run at
    once. Each worker handles one request at a time; ``execute()`` acquires
    a free worker (or spawns one if a lazy slot is available), ships it one
    request over the wire protocol above, and returns it to the pool when
    done. A worker that times out or crashes is killed/discarded and its
    slot is put back as "lazy" so a replacement spawns on next use.
    """

    def __init__(
        self,
        size: int,
        *,
        env: dict[str, str] | None = None,
        python_executable: str | None = None,
    ) -> None:
        if size <= 0:
            raise ValueError("WorkerPool size must be positive")
        self.size = size
        self._env = dict(env) if env is not None else None
        self._python_executable = python_executable or sys.executable
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._known_pids: set[int] = set()
        self._closed = False
        atexit.register(self._atexit_kill)

    # -- queue / lifecycle -------------------------------------------------

    def _queue_for_current_loop(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A pool must not straddle event loops: asyncio subprocess
            # transports are bound to the loop that created them. If we're
            # being used from a new loop (process restart of the loop, or a
            # test harness that opens a fresh loop per call), hard-kill any
            # previously known workers by pid (loop-independent) and start
            # over with fresh lazy slots.
            self._reap_stale_workers()
            self._loop = loop
            self._queue = asyncio.Queue()
            for _ in range(self.size):
                self._queue.put_nowait(_LAZY)
        assert self._queue is not None
        return self._queue

    def _reap_stale_workers(self) -> None:
        for pid in self._known_pids:
            _hard_kill(pid)
        self._known_pids.clear()

    def _atexit_kill(self) -> None:
        for pid in list(self._known_pids):
            _hard_kill(pid)

    async def _spawn_worker(self) -> _Worker:
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": None,  # inherit -- worker stderr flows to server logs
        }
        if self._env is not None:
            kwargs["env"] = self._env
        process = await asyncio.create_subprocess_exec(
            self._python_executable,
            "-m",
            "object_worker_pool",
            "--worker",
            **kwargs,
        )
        worker = _Worker(process)
        self._known_pids.add(worker.pid)
        return worker

    async def _kill_worker(self, worker: _Worker) -> None:
        process = worker.process
        self._known_pids.discard(worker.pid)
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def _exit_code(self, worker: _Worker) -> int | None:
        process = worker.process
        if process.returncode is not None:
            return process.returncode
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            return None
        return process.returncode

    # -- execution -----------------------------------------------------

    async def execute(
        self,
        request: ObjectExecutionRequest,
        *,
        timeout_seconds: float,
        roots: Iterable[Path] | None = None,
    ) -> ObjectExecutionResult:
        """Execute one object method on a warm worker, enforcing timeout_seconds."""
        if self._closed:
            raise RuntimeError("WorkerPool is closed")

        started_perf = time.perf_counter()
        started_at = _utc_timestamp()
        method = request.normalized_method()
        resolved_request = _request_with_resolved_path(request, roots)

        queue = self._queue_for_current_loop()
        slot = await queue.get()
        worker = slot if isinstance(slot, _Worker) else None

        if worker is None:
            try:
                worker = await self._spawn_worker()
            except Exception as exc:
                queue.put_nowait(_LAZY)
                return _error_result(
                    resolved_request,
                    method,
                    object_execution.WORKER_ERROR_TYPE,
                    f"Failed to start object worker: {type(exc).__name__}: {exc}",
                    started_at,
                    started_perf,
                )

        message = {
            "object_id": resolved_request.object_id,
            "method": method,
            "payload": resolved_request.normalized_payload(),
            "correlation_id": resolved_request.correlation_id,
            "path": str(resolved_request.path) if resolved_request.path is not None else None,
        }

        worker_died = False
        try:
            response = await asyncio.wait_for(
                _talk_to_worker(worker, message),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            worker_died = True
            await self._kill_worker(worker)
            result = _error_result(
                resolved_request,
                method,
                object_execution.TIMEOUT_ERROR_TYPE,
                (
                    f"{method} timed out for object {resolved_request.object_id} "
                    f"after {_format_seconds(timeout_seconds)} seconds"
                ),
                started_at,
                started_perf,
            )
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
            worker_died = True
            exit_code = await self._exit_code(worker)
            self._known_pids.discard(worker.pid)
            result = _error_result(
                resolved_request,
                method,
                object_execution.WORKER_ERROR_TYPE,
                f"Object worker exited without a result (exit code {exit_code})",
                started_at,
                started_perf,
            )
        else:
            result = _result_from_dict(response)

        if worker_died:
            queue.put_nowait(_LAZY)
        else:
            queue.put_nowait(worker)

        return result

    async def shutdown(self) -> None:
        """Best-effort termination of every known worker. Never blocks long."""
        self._closed = True
        atexit.unregister(self._atexit_kill)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._queue is None or loop is not self._loop:
            # Either never used, or being shut down from a different event
            # loop than the one that spawned the current workers (asyncio
            # subprocess transports can't be awaited from another loop).
            # Hard-kill by pid instead of the graceful kill()+wait() path.
            self._reap_stale_workers()
            return

        workers: list[_Worker] = []
        while not self._queue.empty():
            slot = self._queue.get_nowait()
            if isinstance(slot, _Worker):
                workers.append(slot)
        for worker in workers:
            await self._kill_worker(worker)
        self._known_pids.clear()


async def _talk_to_worker(worker: _Worker, message: dict[str, Any]) -> dict[str, Any]:
    process = worker.process
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(_encode_message(message))
    await process.stdin.drain()
    return await _read_message_async(process.stdout)


def _hard_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--worker" in args:
        _worker_main()
        return 0
    print("usage: python -m object_worker_pool --worker", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
