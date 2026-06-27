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
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from object_namespace import resolve_object_id


@dataclass(frozen=True)
class ObjectExecutionRequest:
    """A request to execute one object method with a payload."""

    object_id: str
    method: str = "GET"
    payload: Mapping[str, Any] | None = None
    path: Path | None = None

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


def execute_object(
    runtime: ObjectRuntimeProtocol,
    request: ObjectExecutionRequest,
    roots: Iterable[Path] | None = None,
    *,
    raise_on_error: bool = False,
) -> ObjectExecutionResult:
    """Execute an object and capture success or failure as structured data."""
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
    )


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
