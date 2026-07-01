"""Object execution contract.

This module defines the small result shape shared by the daemon, future ASGI
server, DBBASIC Scroll, and AI-assisted repair loop. It does not implement the
object runtime itself; it wraps an existing runtime object and turns execution
success or failure into structured data.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import object_correlation
from object_namespace import resolve_object_id


@dataclass(frozen=True)
class ObjectExecutionRequest:
    """A request to execute one object method with a payload."""

    object_id: str
    method: str = "GET"
    payload: Mapping[str, Any] | None = None
    path: Path | None = None
    correlation_id: str | None = None

    def normalized_method(self) -> str:
        """Return the method name used by object runtimes."""
        method = self.method.strip() if isinstance(self.method, str) else ""
        return method.upper() or "GET"

    def normalized_payload(self) -> dict[str, Any]:
        """Return a mutable dict payload for object execution."""
        if self.payload is None:
            return {}
        return dict(self.payload)


@dataclass(frozen=True)
class ObjectExecutionError:
    """Structured execution failure details."""

    type: str
    message: str
    traceback: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "message": self.message,
            "traceback": self.traceback,
        }


@dataclass(frozen=True)
class ObjectExecutionResult:
    """Structured result for one object execution attempt."""

    object_id: str
    method: str
    path: Path | None
    ok: bool
    result: Any
    error: ObjectExecutionError | None
    started_at: str
    finished_at: str
    duration_ms: float
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "method": self.method,
            "path": str(self.path) if self.path is not None else None,
            "ok": self.ok,
            "result": self.result,
            "error": self.error.to_dict() if self.error else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "correlation_id": self.correlation_id,
        }


class ObjectRuntimeProtocol(Protocol):
    """Runtime boundary required by this execution contract."""

    def load_object(self, path: Path, object_id: str | None = None) -> "ExecutableObjectProtocol":
        ...


class ExecutableObjectProtocol(Protocol):
    """Object boundary required by this execution contract."""

    def execute(self, method: str, payload: dict[str, Any]) -> Any:
        ...


class ObjectExecutionFailure(RuntimeError):
    """Exception wrapper for callers that still need raise-on-failure behavior."""

    def __init__(self, execution_result: ObjectExecutionResult):
        self.execution_result = execution_result
        message = "Object execution failed"
        if execution_result.error is not None:
            message = execution_result.error.message
        super().__init__(message)


TIMEOUT_ERROR_TYPE = "ObjectExecutionTimeoutError"
WORKER_ERROR_TYPE = "ObjectWorkerError"


def execute_object(
    runtime: ObjectRuntimeProtocol,
    request: ObjectExecutionRequest,
    roots: Iterable[Path] | None = None,
    *,
    raise_on_error: bool = False,
) -> ObjectExecutionResult:
    """Execute an object and capture success or failure as structured data."""
    correlation_id = object_correlation.ensure_correlation_id(
        request.correlation_id or object_correlation.current_correlation_id()
    )
    request = _request_with_correlation_id(request, correlation_id)
    token = object_correlation.set_current_correlation_id(correlation_id)

    try:
        return _execute_object_with_context(runtime, request, roots, raise_on_error=raise_on_error)
    finally:
        object_correlation.reset_current_correlation_id(token)


def _execute_object_with_context(
    runtime: ObjectRuntimeProtocol,
    request: ObjectExecutionRequest,
    roots: Iterable[Path] | None = None,
    *,
    raise_on_error: bool = False,
) -> ObjectExecutionResult:
    """Execute an object with a correlation context already installed."""
    started_perf = time.perf_counter()
    started_at = _utc_timestamp()
    method = request.normalized_method()
    payload = request.normalized_payload()

    path = request.path
    if path is None:
        path = resolve_object_id(request.object_id, roots)

    if path is None or not path.exists():
        result = _finish_error(
            request=request,
            method=method,
            path=path,
            error=ObjectExecutionError(
                type="ObjectNotFoundError",
                message=f"Object not found: {request.object_id}",
            ),
            started_at=started_at,
            started_perf=started_perf,
        )
        if raise_on_error:
            raise ObjectExecutionFailure(result)
        return result

    try:
        obj = runtime.load_object(path, request.object_id)
        output = obj.execute(method, payload)
    except Exception as exc:
        result = _finish_error(
            request=request,
            method=method,
            path=path,
            error=_error_from_exception(exc),
            started_at=started_at,
            started_perf=started_perf,
        )
        if raise_on_error:
            raise ObjectExecutionFailure(result) from exc
        return result

    return _finish_success(
        request=request,
        method=method,
        path=path,
        output=output,
        started_at=started_at,
        started_perf=started_perf,
    )


def execute_python_object_subprocess(
    request: ObjectExecutionRequest,
    roots: Iterable[Path] | None = None,
    *,
    timeout_seconds: float,
    raise_on_error: bool = False,
) -> ObjectExecutionResult:
    """Execute a Python object in a subprocess with a wall-clock timeout."""
    correlation_id = object_correlation.ensure_correlation_id(
        request.correlation_id or object_correlation.current_correlation_id()
    )
    request = _request_with_correlation_id(request, correlation_id)

    if timeout_seconds <= 0:
        from python_object_runtime import PythonObjectRuntime

        return execute_object(
            PythonObjectRuntime(),
            request,
            roots,
            raise_on_error=raise_on_error,
        )

    started_perf = time.perf_counter()
    started_at = _utc_timestamp()
    method = request.normalized_method()
    roots_list = list(roots) if roots is not None else None
    resolved_request = _request_with_resolved_path(request, roots_list)

    context = get_context("spawn")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_execute_python_object_child,
        args=(child_conn, resolved_request, roots_list),
        daemon=True,
    )
    process.start()
    child_conn.close()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        parent_conn.close()
        result = _finish_error(
            request=resolved_request,
            method=method,
            path=resolved_request.path,
            error=ObjectExecutionError(
                type=TIMEOUT_ERROR_TYPE,
                message=(
                    f"{method} timed out for object {resolved_request.object_id} "
                    f"after {_format_seconds(timeout_seconds)} seconds"
                ),
            ),
            started_at=started_at,
            started_perf=started_perf,
        )
        if raise_on_error:
            raise ObjectExecutionFailure(result)
        return result

    try:
        if parent_conn.poll():
            result = parent_conn.recv()
        else:
            result = _finish_error(
                request=resolved_request,
                method=method,
                path=resolved_request.path,
                error=ObjectExecutionError(
                    type=WORKER_ERROR_TYPE,
                    message=(
                        f"Object worker exited without a result "
                        f"(exit code {process.exitcode})"
                    ),
                ),
                started_at=started_at,
                started_perf=started_perf,
            )
    finally:
        parent_conn.close()

    if raise_on_error and not result.ok:
        raise ObjectExecutionFailure(result)
    return result


def _finish_success(
    *,
    request: ObjectExecutionRequest,
    method: str,
    path: Path,
    output: Any,
    started_at: str,
    started_perf: float,
) -> ObjectExecutionResult:
    return ObjectExecutionResult(
        object_id=request.object_id,
        method=method,
        path=path,
        ok=True,
        result=output,
        error=None,
        started_at=started_at,
        finished_at=_utc_timestamp(),
        duration_ms=_duration_ms(started_perf),
        correlation_id=request.correlation_id,
    )


def _finish_error(
    *,
    request: ObjectExecutionRequest,
    method: str,
    path: Path | None,
    error: ObjectExecutionError,
    started_at: str,
    started_perf: float,
) -> ObjectExecutionResult:
    return ObjectExecutionResult(
        object_id=request.object_id,
        method=method,
        path=path,
        ok=False,
        result=None,
        error=error,
        started_at=started_at,
        finished_at=_utc_timestamp(),
        duration_ms=_duration_ms(started_perf),
        correlation_id=request.correlation_id,
    )


def _request_with_correlation_id(
    request: ObjectExecutionRequest,
    correlation_id: str,
) -> ObjectExecutionRequest:
    return ObjectExecutionRequest(
        object_id=request.object_id,
        method=request.method,
        payload=request.payload,
        path=request.path,
        correlation_id=correlation_id,
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


def _execute_python_object_child(
    connection: Connection,
    request: ObjectExecutionRequest,
    roots: list[Path] | None,
) -> None:
    try:
        from python_object_runtime import PythonObjectRuntime

        result = execute_object(PythonObjectRuntime(), request, roots)
        connection.send(result)
    except BaseException as exc:
        error_result = _finish_error(
            request=request,
            method=request.normalized_method(),
            path=request.path,
            error=ObjectExecutionError(
                type=WORKER_ERROR_TYPE,
                message=f"Object worker failed: {type(exc).__name__}: {exc}",
                traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            ),
            started_at=_utc_timestamp(),
            started_perf=time.perf_counter(),
        )
        try:
            connection.send(error_result)
        except BaseException:
            pass
    finally:
        connection.close()


def _error_from_exception(exc: Exception) -> ObjectExecutionError:
    return ObjectExecutionError(
        type=exc.__class__.__name__,
        message=str(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )


def _duration_ms(started_perf: float) -> float:
    return round((time.perf_counter() - started_perf) * 1000, 3)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _format_seconds(seconds: float) -> str:
    return f"{seconds:g}"
