"""Minimal ASGI app for DBBASIC Object Server.

This is the first public server slice. Source writes are disabled by default
while the production auth and mutation paths are extracted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from collections import Counter, deque
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from typing import Any

import http_api_contract
import object_collections
import object_execution
import object_files
import object_logs
import object_metadata
import object_permission_audit
import object_permission_store
import object_permissions
import object_rate_limit
import object_records
import object_schemas
import object_source
import object_state
import object_versions
from object_namespace import iter_object_sources, parse_user_object_id
from object_versions import InvalidObjectIdError
from python_object_runtime import MethodNotSupportedError, PythonObjectRuntime

SOURCE_WRITES_ENV = "DBBASIC_ENABLE_SOURCE_WRITES"
ADMIN_TOKEN_ENV = "DBBASIC_ADMIN_TOKEN"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"
MAX_REQUEST_BYTES_ENV = "DBBASIC_MAX_REQUEST_BYTES"
MAX_CONCURRENT_REQUESTS_ENV = "DBBASIC_MAX_CONCURRENT_REQUESTS"
MAX_CONCURRENT_EXECUTIONS_ENV = "DBBASIC_MAX_CONCURRENT_EXECUTIONS"
RATE_LIMIT_REQUESTS_ENV = "DBBASIC_RATE_LIMIT_REQUESTS"
RATE_LIMIT_WINDOW_SECONDS_ENV = "DBBASIC_RATE_LIMIT_WINDOW_SECONDS"
RATE_LIMIT_TRUST_PROXY_HEADERS_ENV = "DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS"
OBJECT_TIMEOUT_SECONDS_ENV = "DBBASIC_OBJECT_TIMEOUT_SECONDS"
TRUSTED_IN_PROCESS_OBJECTS_ENV = "DBBASIC_TRUSTED_IN_PROCESS_OBJECTS"
PERMISSION_ENFORCEMENT_ENV = "DBBASIC_ENABLE_PERMISSION_ENFORCEMENT"
PERMISSION_AUDIT_ENV = "DBBASIC_ENABLE_PERMISSION_AUDIT"
PERMISSION_TRUST_HEADERS_ENV = "DBBASIC_PERMISSION_TRUST_HEADERS"
DEFAULT_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_MAX_CONCURRENT_REQUESTS = 64
DEFAULT_MAX_CONCURRENT_EXECUTIONS = 8
DEFAULT_RATE_LIMIT_REQUESTS = 0
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_OBJECT_TIMEOUT_SECONDS = 0.0
TRUE_VALUES = {"1", "true", "yes", "on"}
SENSITIVE_GET_FLAGS = {"source", "state", "logs", "metadata", "versions", "files", "file"}

_runtime = PythonObjectRuntime()


class RequestMetrics:
    """Thread-safe, in-process request metrics for health and Scroll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._total_requests = 0
        self._total_errors = 0
        self._total_4xx = 0
        self._response_times_ms: deque[float] = deque(maxlen=1000)
        self._methods: Counter[str] = Counter()
        self._status_codes: Counter[int] = Counter()
        self._paths: Counter[str] = Counter()
        self._path_errors: Counter[str] = Counter()
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=50)

    def record_request(self, method: str, path: str, status: int, duration_ms: float) -> None:
        with self._lock:
            self._total_requests += 1
            self._methods[method] += 1
            self._status_codes[status] += 1
            self._paths[path] += 1
            self._response_times_ms.append(duration_ms)

            if status >= 500:
                self._total_errors += 1
                self._path_errors[path] += 1
            if 400 <= status < 500:
                self._total_4xx += 1
                self._path_errors[path] += 1
            if status >= 400:
                self._recent_errors.append(
                    {
                        "timestamp": _utc_timestamp(),
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round(duration_ms, 2),
                    }
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime_seconds = max(time.time() - self._start_time, 0.001)
            response_times = list(self._response_times_ms)
            total_requests = self._total_requests
            total_errors = self._total_errors
            total_4xx = self._total_4xx
            methods = dict(self._methods)
            status_codes = {str(code): count for code, count in self._status_codes.items()}
            top_paths = [
                {
                    "path": path,
                    "requests": count,
                    "errors": self._path_errors.get(path, 0),
                }
                for path, count in self._paths.most_common(10)
            ]
            recent_errors = list(self._recent_errors)

        return {
            "uptime_seconds": round(uptime_seconds, 3),
            "uptime_human": _human_duration(uptime_seconds),
            "total_requests": total_requests,
            "total_errors": total_errors,
            "total_4xx": total_4xx,
            "requests_per_second": round(total_requests / uptime_seconds, 2),
            "error_rate": round((total_errors / total_requests * 100) if total_requests else 0, 2),
            "response_time_ms": _response_time_summary(response_times),
            "methods": methods,
            "status_codes": status_codes,
            "top_paths": top_paths,
            "recent_errors": recent_errors,
        }


class RequestBodyTooLargeError(ValueError):
    """Raised when an inbound HTTP request body exceeds the configured cap."""

    def __init__(self, *, max_bytes: int, actual_bytes: int):
        self.max_bytes = max_bytes
        self.actual_bytes = actual_bytes
        super().__init__("Request body too large")


class ConcurrencyToken:
    """Release handle for a claimed concurrency slot."""

    def __init__(self, limiter: "ConcurrencyLimiter | None"):
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if self._released:
            return

        self._released = True
        if self._limiter is not None:
            self._limiter.release()


class ConcurrencyLimiter:
    """Small per-process non-queueing concurrency limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self, limit: int) -> ConcurrencyToken | None:
        if limit <= 0:
            return ConcurrencyToken(None)

        with self._lock:
            if self._in_flight >= limit:
                return None
            self._in_flight += 1

        return ConcurrencyToken(self)

    def release(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    def snapshot(self, limit: int) -> dict[str, Any]:
        with self._lock:
            in_flight = self._in_flight

        if limit <= 0:
            return {
                "in_flight": in_flight,
                "max": limit,
                "available": None,
                "limited": False,
            }

        return {
            "in_flight": in_flight,
            "max": limit,
            "available": max(limit - in_flight, 0),
            "limited": True,
        }


_request_limiter = ConcurrencyLimiter()
_execution_limiter = ConcurrencyLimiter()
_metrics = RequestMetrics()


async def app(scope: dict[str, Any], receive, send) -> None:
    """ASGI application entry point."""
    if scope["type"] == "lifespan":
        await _handle_lifespan(receive, send)
        return

    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1003})
        return

    if scope["type"] != "http":
        return

    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    status_code = 500
    started_at = time.perf_counter()

    async def send_with_metrics(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message.get("status", status_code))
        await send(message)

    try:
        await _handle_http(scope, receive, send_with_metrics)
    finally:
        duration_ms = (time.perf_counter() - started_at) * 1000
        _metrics.record_request(method, path, status_code, duration_ms)


async def _handle_http(scope: dict[str, Any], receive, send) -> None:
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    query = _parse_query(scope.get("query_string", b""))
    headers = _parse_headers(scope.get("headers", []))

    if path == "/health":
        if _is_detailed_health(query) and await _send_rate_limit_if_needed(scope, headers, send):
            return
        await _handle_health(send, query, headers)
        return

    if await _send_rate_limit_if_needed(scope, headers, send):
        return

    request_limit = _max_concurrent_requests()
    request_token = _request_limiter.try_acquire(request_limit)
    if request_token is None:
        await _send_capacity_error(send, limit_name="requests", max_concurrent=request_limit)
        return

    try:
        try:
            body = await _read_body(receive, headers=headers)
        except RequestBodyTooLargeError as exc:
            await _send_request_too_large(send, exc)
            return

        if path == http_api_contract.PERMISSIONS_POLICY_PATH:
            await _handle_permissions_policy(send, method, body, headers)
            return

        if path == http_api_contract.PERMISSIONS_CHECK_PATH:
            await _handle_permissions_check(send, method, body, headers)
            return

        if path == http_api_contract.PERMISSIONS_AUDIT_PATH:
            await _handle_permissions_audit(send, method, query, headers)
            return

        if path == http_api_contract.OBJECTS_PATH:
            if method == "GET":
                gate_error = _admin_token_gate_error(
                    headers,
                    f"Object listing requires {ADMIN_TOKEN_ENV}.",
                )
                if gate_error is not None:
                    status, message = gate_error
                    await _send_json(send, {"status": "error", "error": message}, status=status)
                    return

                await _send_json(send, _list_objects_payload())
                return

            await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
            return

        if path == http_api_contract.COLLECTIONS_PATH:
            await _handle_collections(send, method, headers)
            return

        if path.startswith(f"{http_api_contract.COLLECTIONS_PATH}/"):
            collection_path = path.removeprefix(f"{http_api_contract.COLLECTIONS_PATH}/")
            collection_parts = collection_path.split("/")
            if len(collection_parts) == 2 and collection_parts[1] == "records":
                await _handle_collection_records(
                    send,
                    method,
                    collection_parts[0],
                    query,
                    headers,
                )
                return

            if len(collection_parts) == 3 and collection_parts[1] == "records":
                await _handle_collection_record_get(
                    send,
                    method,
                    collection_parts[0],
                    collection_parts[2],
                    headers,
                )
                return

            collection = collection_path
            await _handle_collection_get(send, method, collection, headers)
            return

        if path == http_api_contract.SCHEMAS_PATH:
            await _handle_schemas(send, method, headers)
            return

        if path.startswith(f"{http_api_contract.SCHEMAS_PATH}/"):
            schema = path.removeprefix(f"{http_api_contract.SCHEMAS_PATH}/")
            await _handle_schema_get(send, method, schema, headers)
            return

        if path.startswith(f"{http_api_contract.OBJECTS_PATH}/"):
            object_id = path.removeprefix(f"{http_api_contract.OBJECTS_PATH}/")
            if method == "GET":
                await _handle_object_get(send, object_id, query, headers)
                return

            if method == "POST":
                await _handle_object_post(send, object_id, body, query, headers)
                return

            if method == "PUT" and query.get("source") == "true":
                await _handle_object_source_put(send, object_id, body, headers)
                return

            if method == "PUT":
                await _handle_object_body_method(send, object_id, "PUT", body, query, headers)
                return

            if method == "DELETE":
                await _handle_object_body_method(send, object_id, "DELETE", body, query, headers)
                return

            await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
            return

        await _send_json(send, {"status": "error", "error": "Not found"}, status=404)
    finally:
        request_token.release()


async def _handle_health(
    send,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if not _is_detailed_health(query):
        await _send_json(send, {"status": "ok"})
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Health capacity requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    payload = _health_payload(include_metrics=query.get("metrics") == "true")
    status_code = 503 if payload["status"] == "degraded" else 200
    await _send_json(send, payload, status=status_code)


def _is_detailed_health(query: dict[str, str]) -> bool:
    return query.get("capacity") == "true" or query.get("metrics") == "true"


async def _send_rate_limit_if_needed(
    scope: dict[str, Any],
    headers: dict[str, str],
    send,
) -> bool:
    limit = _rate_limit_requests()
    if limit <= 0:
        return False

    try:
        result = object_rate_limit.check_rate_limit(
            directory=_rate_limit_dir(),
            identity=_rate_limit_identity(scope, headers),
            limit=limit,
            window_seconds=_rate_limit_window_seconds(),
        )
    except OSError:
        return False

    if result.allowed:
        return False

    await _send_rate_limit_error(send, result)
    return True


def _health_payload(*, include_metrics: bool) -> dict[str, Any]:
    metrics = _metrics.snapshot()
    storage_check = _storage_check()
    status = "ok" if storage_check["status"] == "ok" else "degraded"
    payload: dict[str, Any] = {
        "status": status,
        "timestamp": _utc_timestamp(),
        "version": _package_version(),
        "station_id": _station_id(),
        "pid": os.getpid(),
        "threads": _thread_count(),
        "uptime": metrics["uptime_human"],
        "uptime_seconds": metrics["uptime_seconds"],
        "requests": metrics["total_requests"],
        "errors": metrics["total_errors"],
        "rps": metrics["requests_per_second"],
        "error_rate": metrics["error_rate"],
        "response_time_ms": metrics["response_time_ms"],
        "objects": {
            "count": _object_count(),
        },
        "capacity": {
            "requests": _request_limiter.snapshot(_max_concurrent_requests()),
            "object_executions": _execution_limiter.snapshot(_max_concurrent_executions()),
        },
        "config": {
            "source_writes_enabled": _env_enabled(SOURCE_WRITES_ENV),
            "max_request_bytes": _max_request_bytes(),
            "max_concurrent_requests": _max_concurrent_requests(),
            "max_concurrent_executions": _max_concurrent_executions(),
            "rate_limit_requests": _rate_limit_requests(),
            "rate_limit_window_seconds": _rate_limit_window_seconds(),
            "rate_limit_trust_proxy_headers": _env_enabled(RATE_LIMIT_TRUST_PROXY_HEADERS_ENV),
            "object_timeout_seconds": _object_timeout_seconds(),
            "trusted_in_process_objects": sorted(_trusted_in_process_object_ids()),
            "permission_enforcement_enabled": _permission_enforcement_enabled(),
            "permission_audit_enabled": _permission_audit_enabled(),
            "permission_trust_headers": _env_enabled(PERMISSION_TRUST_HEADERS_ENV),
        },
        "checks": {
            "storage": storage_check,
        },
        "system": _system_snapshot(),
    }

    if include_metrics:
        payload["metrics"] = metrics

    return payload


async def _handle_permissions_policy(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _permissions_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            policy = object_permission_store.load_policy(_data_dir())
        except ValueError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Permission policy is invalid: {exc}"},
                status=500,
            )
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "policy": object_permissions.policy_to_dict(policy),
            },
        )
        return

    if method == "PUT":
        try:
            payload = _parse_json_body(body)
            policy_payload = _policy_payload_from_request(payload)
            policy = object_permission_store.replace_policy(policy_payload, _data_dir())
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not save permission policy: {exc}"},
                status=500,
            )
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "policy": object_permissions.policy_to_dict(policy),
            },
        )
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_permissions_check(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _permissions_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    try:
        payload = _parse_json_body(body)
        policy = _policy_for_check_request(payload)
        subject = object_permissions.subject_from_dict(payload.get("subject", payload.get("user")))
        action = _required_payload_text(payload, "action")
        collection = _optional_payload_text(payload, "collection")
        object_id = _optional_payload_text(payload, "object_id")
        record = _optional_record_payload(payload)
        checked_at = _optional_datetime_payload(payload, "now")
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    decision = object_permissions.check_permission(
        subject,
        action,
        policy=policy,
        collection=collection,
        object_id=object_id,
        record=record,
        now=checked_at,
    )
    await _send_json(
        send,
        {
            "status": "ok",
            "decision": object_permissions.decision_to_dict(decision),
        },
    )


async def _handle_permissions_audit(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    gate_error = _permissions_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    try:
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        entries = object_permission_audit.get_permission_audit(
            _data_dir(),
            limit=limit,
            action=_optional_query_text(query, "action"),
            object_id=_optional_query_text(query, "object_id"),
            collection=_optional_query_text(query, "collection"),
            allowed=_optional_query_bool(query, "allowed"),
            enforced=_optional_query_bool(query, "enforced"),
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not read permission audit: {exc}"},
            status=500,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "entries": entries,
            "count": len(entries),
        },
    )


async def _handle_object_get(
    send,
    object_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in this server"},
            status=400,
        )
        return

    if _is_sensitive_get(query):
        gate_error = _admin_token_gate_error(
            headers,
            f"Object introspection requires {ADMIN_TOKEN_ENV}.",
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    if query.get("versions") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.VERSIONS,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_versions_get(send, object_id, query)
        return

    if "version" in query:
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.VERSIONS,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_version_get(send, object_id, query)
        return

    if query.get("state") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.STATE,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_state_get(send, object_id)
        return

    if query.get("logs") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.LOGS,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_logs_get(send, object_id, query)
        return

    if query.get("files") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.FILES,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_files_get(send, object_id)
        return

    if "file" in query:
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.FILES,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_file_get(send, object_id, query)
        return

    if query.get("metadata") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.READ,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_metadata_get(send, object_id)
        return

    if query.get("source") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.SOURCE,
            object_id=object_id,
            method="GET",
        ):
            return
        try:
            source = object_source.get_object_source(object_id)
        except InvalidObjectIdError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_source.ObjectSourceNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "object_id": object_id,
                "source": source,
            },
        )
        return

    await _execute_object_method(
        send,
        object_id,
        "GET",
        query,
        headers,
        permission_action=object_permissions.EXECUTE,
    )


async def _handle_object_state_get(send, object_id: str) -> None:
    try:
        _ensure_object_source_exists(object_id)
        state = object_state.get_object_state(object_id, base_dir=_data_dir())
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "state": state,
        },
    )


async def _handle_object_metadata_get(send, object_id: str) -> None:
    try:
        metadata = object_metadata.get_object_metadata(object_id, base_dir=_data_dir())
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "metadata": metadata,
        },
    )


async def _handle_object_logs_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    try:
        _ensure_object_source_exists(object_id)
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        logs = object_logs.get_object_logs(
            object_id,
            base_dir=_data_dir(),
            level=query.get("level"),
            limit=limit,
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "logs": logs,
            "count": len(logs),
        },
    )


async def _handle_object_files_get(send, object_id: str) -> None:
    try:
        _ensure_object_source_exists(object_id)
        files = object_files.list_object_files(object_id, base_dir=_data_dir())
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not read object files: {exc}"},
            status=500,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "files": files,
            "count": len(files),
        },
    )


async def _handle_object_file_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    filename = query.get("file", "")
    try:
        _ensure_object_source_exists(object_id)
        content, metadata = object_files.read_object_file(
            object_id,
            filename,
            base_dir=_data_dir(),
        )
    except object_files.InvalidObjectFilenameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_files.ObjectFileNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not read object file: {exc}"},
            status=500,
        )
        return

    content_type, _ = mimetypes.guess_type(metadata["name"])
    if content_type is None:
        content_type = "application/octet-stream"

    disposition = "inline" if content_type.startswith("image/") else "attachment"
    download_name = _download_filename(metadata["name"])
    await _send_response(
        send,
        status=200,
        headers=[
            ("content-type", content_type),
            ("content-length", str(len(content))),
            ("content-disposition", f'{disposition}; filename="{download_name}"'),
        ],
        body=content,
    )


async def _handle_object_versions_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    try:
        _ensure_object_source_exists(object_id)
        limit = _query_int(query, "limit", default=10, minimum=1, maximum=100)
        versions = _version_manager().get_history(object_id, limit=limit)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "versions": versions,
            "count": len(versions),
        },
    )


async def _handle_object_version_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    try:
        _ensure_object_source_exists(object_id)
        version_id = _query_int(query, "version", minimum=1)
        version = _version_manager().get_version(object_id, version_id)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    if version is None:
        await _send_json(
            send,
            {"status": "error", "error": f"Version {version_id} not found for object {object_id}"},
            status=404,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "version": version,
        },
    )


async def _handle_object_post(
    send,
    object_id: str,
    body: bytes,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in this server"},
            status=400,
        )
        return

    try:
        payload = _parse_post_payload(body, query)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    if payload.get("action") == "rollback":
        await _handle_object_rollback_post(send, object_id, payload, headers)
        return

    await _execute_object_method(
        send,
        object_id,
        "POST",
        payload,
        headers,
        permission_action=object_permissions.EXECUTE,
    )


async def _handle_object_body_method(
    send,
    object_id: str,
    method: str,
    body: bytes,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in this server"},
            status=400,
        )
        return

    try:
        payload = _parse_json_body(body) if body.strip() else dict(query)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    permission_action = object_permissions.EXECUTE
    if method == "PUT":
        permission_action = object_permissions.UPDATE
    elif method == "DELETE":
        permission_action = object_permissions.DELETE

    await _execute_object_method(
        send,
        object_id,
        method,
        payload,
        headers,
        permission_action=permission_action,
    )


async def _handle_object_rollback_post(
    send,
    object_id: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> None:
    gate_error = _source_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if await _send_permission_denied_if_needed(
        send,
        headers,
        object_permissions.SOURCE,
        object_id=object_id,
        method="POST",
    ):
        return

    try:
        version_id = _payload_int(payload, "version_id", minimum=1)
        author = _payload_text(payload, "author", "api")
        message = _payload_text(payload, "message", f"Rollback to version {version_id}")
        new_version_id = object_source.rollback_object_source(
            object_id=object_id,
            to_version=version_id,
            author=author,
            message=message,
            version_manager=_version_manager(),
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_versions.VersionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Rolled back to version {version_id}",
            "version_id": version_id,
            "new_version_id": new_version_id,
            "object_id": object_id,
        },
    )


async def _handle_object_source_put(
    send,
    object_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in this server"},
            status=400,
        )
        return

    gate_error = _source_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if await _send_permission_denied_if_needed(
        send,
        headers,
        object_permissions.SOURCE,
        object_id=object_id,
        method="PUT",
    ):
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    code = payload.get("code")
    if not isinstance(code, str):
        await _send_json(
            send,
            {"status": "error", "error": "Request JSON field 'code' must be a string"},
            status=400,
        )
        return

    author = _payload_text(payload, "author", "api")
    message = _payload_text(payload, "message", "Updated via API")

    try:
        version_id = object_source.update_object_source(
            object_id=object_id,
            new_code=code,
            author=author,
            message=message,
            version_manager=_version_manager(),
        )
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Code updated to version {version_id}",
            "version_id": version_id,
            "object_id": object_id,
        },
    )


async def _execute_object_method(
    send,
    object_id: str,
    method: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    permission_action: str,
) -> None:
    if await _send_permission_denied_if_needed(
        send,
        headers,
        permission_action,
        object_id=object_id,
        method=method,
    ):
        return

    execution_limit = _max_concurrent_executions()
    execution_token = _execution_limiter.try_acquire(execution_limit)
    if execution_token is None:
        await _send_capacity_error(
            send,
            limit_name="object_executions",
            max_concurrent=execution_limit,
        )
        return

    execution_request = object_execution.ObjectExecutionRequest(
        object_id=object_id,
        method=method,
        payload=payload,
    )
    timeout_seconds = _object_timeout_seconds()

    try:
        if timeout_seconds > 0 and not _object_runs_in_process(object_id):
            result = object_execution.execute_python_object_subprocess(
                execution_request,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = object_execution.execute_object(_runtime, execution_request)
        _append_execution_log(result)
    finally:
        execution_token.release()

    if result.ok:
        await _send_object_response(send, result.result)
        return

    await _send_execution_error(send, result)


def _list_objects_payload() -> dict[str, Any]:
    objects = [_object_source_payload(source) for source in iter_object_sources()]
    return {
        "status": "ok",
        "objects": objects,
        "count": len(objects),
    }


async def _handle_collections(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Collection listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        collections = object_collections.list_collections(base_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "collections": collections,
            "count": len(collections),
        },
    )


async def _handle_collection_get(
    send,
    method: str,
    collection: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Collection detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        summary = object_collections.get_collection(collection, base_dir=_data_dir())
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": summary,
        },
    )


async def _handle_collection_records(
    send,
    method: str,
    collection: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Collection records require {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        records_payload = object_records.list_collection_records(
            collection,
            base_dir=_data_dir(),
            limit=_query_int(query, "limit", default=100, minimum=1, maximum=1000),
            offset=_query_int(query, "offset", default=0, minimum=0),
        )
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", **records_payload})


async def _handle_collection_record_get(
    send,
    method: str,
    collection: str,
    record_id: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Collection record detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        record = object_records.get_collection_record(
            collection,
            record_id,
            base_dir=_data_dir(),
        )
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_records.InvalidRecordIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (object_collections.CollectionNotFoundError, object_records.RecordNotFoundError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
        },
    )


async def _handle_schemas(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Schema listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        schemas = object_schemas.list_schemas(base_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "schemas": schemas,
            "count": len(schemas),
        },
    )


async def _handle_schema_get(
    send,
    method: str,
    schema: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Schema detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        schema_payload = object_schemas.get_schema(schema, base_dir=_data_dir())
    except object_schemas.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_schemas.SchemaNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "schema": schema_payload,
        },
    )


def _object_source_payload(source) -> dict[str, str]:
    return {
        "object_id": source.object_id,
        "path": source.relative_path.as_posix(),
        "owner": _object_owner(source.object_id),
    }


def _object_owner(object_id: str) -> str:
    parsed = parse_user_object_id(object_id)
    if parsed is None:
        return "system"
    user_id, _ = parsed
    return str(user_id)


def _object_collection(object_id: str) -> str | None:
    parsed = parse_user_object_id(object_id)
    if parsed is not None:
        _, name = parsed
        return name.split("_", 1)[0] if name else None

    return object_id.split("_", 1)[0] if object_id else None


def _append_execution_log(result: object_execution.ObjectExecutionResult) -> None:
    if result.path is None:
        return

    try:
        if result.ok:
            object_logs.append_object_log(
                result.object_id,
                "DEBUG",
                f"{result.method} completed successfully",
                base_dir=_data_dir(),
                method=result.method,
                status="success",
                duration_ms=result.duration_ms,
            )
            return

        error_type = result.error.type if result.error is not None else None
        error = result.error.message if result.error is not None else None
        object_logs.append_object_log(
            result.object_id,
            "ERROR",
            f"{result.method} failed: {error}",
            base_dir=_data_dir(),
            method=result.method,
            status="error",
            duration_ms=result.duration_ms,
            error_type=error_type,
            error=error,
        )
    except Exception:
        # Logging is feedback for the dev loop; it should not change the object response.
        pass


def _object_count() -> int:
    return sum(1 for _ in iter_object_sources())


def _package_version() -> str:
    try:
        return importlib_metadata.version("dbbasic-object-server")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.1"


def _station_id() -> str:
    return os.environ.get("DBBASIC_STATION_ID") or os.environ.get("STATION_ID") or "standalone"


def _rate_limit_identity(scope: dict[str, Any], headers: dict[str, str]) -> str:
    token = _authorization_token(headers)
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if token and admin_token and hmac.compare_digest(token, admin_token):
        return f"token:{_hash_identity(token)}"

    return f"ip:{_client_ip(scope, headers)}"


def _hash_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _client_ip(scope: dict[str, Any], headers: dict[str, str]) -> str:
    if _env_enabled(RATE_LIMIT_TRUST_PROXY_HEADERS_ENV):
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()

        real_ip = headers.get("x-real-ip", "")
        if real_ip:
            return real_ip.strip()

    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])

    return "127.0.0.1"


def _storage_check() -> dict[str, str]:
    check_path = os.path.join(_data_dir(), ".health_check")
    try:
        os.makedirs(_data_dir(), exist_ok=True)
        with open(check_path, "w", encoding="utf-8") as handle:
            handle.write("ok")
        with open(check_path, encoding="utf-8") as handle:
            handle.read()
        os.unlink(check_path)
    except OSError as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {"status": "ok"}


def _system_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "cpu_count": os.cpu_count(),
    }

    try:
        snapshot["load_average"] = [round(value, 3) for value in os.getloadavg()]
    except OSError:
        snapshot["load_average"] = None

    memory = _linux_memory_snapshot()
    if memory is not None:
        snapshot["memory"] = memory

    return snapshot


def _linux_memory_snapshot() -> dict[str, Any] | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    values_kb: dict[str, int] = {}
    for line in lines:
        key, _, value = line.partition(":")
        parts = value.strip().split()
        if not parts:
            continue
        try:
            values_kb[key] = int(parts[0])
        except ValueError:
            continue

    total_kb = values_kb.get("MemTotal")
    available_kb = values_kb.get("MemAvailable")
    if not total_kb or available_kb is None:
        return None

    used_kb = max(total_kb - available_kb, 0)
    return {
        "total_mb": round(total_kb / 1024, 2),
        "available_mb": round(available_kb / 1024, 2),
        "used_mb": round(used_kb / 1024, 2),
        "used_percent": round(used_kb / total_kb * 100, 2),
    }


def _thread_count() -> int:
    task_dir = f"/proc/{os.getpid()}/task"
    try:
        return len(os.listdir(task_dir))
    except OSError:
        return threading.active_count()


def _response_time_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "avg": 0,
            "p50": 0,
            "p95": 0,
            "p99": 0,
        }

    return {
        "avg": round(sum(values) / len(values), 2),
        "p50": round(_percentile(values, 50), 2),
        "p95": round(_percentile(values, 95), 2),
        "p99": round(_percentile(values, 99), 2),
    }


def _percentile(values: list[float], percentile: int) -> float:
    sorted_values = sorted(values)
    index = int((len(sorted_values) - 1) * percentile / 100)
    return sorted_values[index]


def _human_duration(seconds: float) -> str:
    remaining = int(seconds)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_query(query_string: bytes | str) -> dict[str, str]:
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8")
    return dict(urllib.parse.parse_qsl(query_string))


def _parse_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    parsed = {}
    for name, value in headers:
        parsed[name.decode("latin-1").lower()] = value.decode("latin-1")
    return parsed


def _parse_json_body(body: bytes) -> dict[str, Any]:
    if not body.strip():
        return {}

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")

    return payload


def _parse_post_payload(body: bytes, query: dict[str, str]) -> dict[str, Any]:
    if not body.strip():
        return dict(query)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**query, "body": body}

    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")

    for key, value in query.items():
        payload.setdefault(key, value)

    return payload


def _payload_text(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        return default
    return value


def _required_payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Request JSON field '{key}' must be a non-empty string")
    return value


def _optional_payload_text(payload: dict[str, Any], key: str) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Request JSON field '{key}' must be a non-empty string")
    return value


def _policy_payload_from_request(payload: dict[str, Any]) -> dict[str, Any]:
    policy_payload = payload.get("policy", payload)
    if not isinstance(policy_payload, dict):
        raise ValueError("Request JSON field 'policy' must be an object")
    return policy_payload


def _policy_for_check_request(payload: dict[str, Any]) -> object_permissions.PermissionPolicy:
    if "policy" in payload:
        policy_payload = _policy_payload_from_request(payload)
        return object_permissions.policy_from_dict(policy_payload)

    return object_permission_store.load_policy(_data_dir())


def _optional_record_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if "record" not in payload or payload["record"] is None:
        return None
    record = payload["record"]
    if not isinstance(record, dict):
        raise ValueError("Request JSON field 'record' must be an object")
    return record


def _optional_datetime_payload(payload: dict[str, Any], key: str) -> datetime | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Request JSON field '{key}' must be an ISO timestamp")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Request JSON field '{key}' must be an ISO timestamp") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload_int(payload: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    if key not in payload:
        raise ValueError(f"Request JSON field '{key}' is required")

    value = payload[key]
    if isinstance(value, bool):
        raise ValueError(f"Request JSON field '{key}' must be an integer")

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Request JSON field '{key}' must be an integer") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"Request JSON field '{key}' must be at least {minimum}")

    return parsed


def _query_int(
    query: dict[str, str],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = query.get(key)
    if value is None:
        if default is None:
            raise ValueError(f"Query parameter '{key}' is required")
        parsed = default
    else:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"Query parameter '{key}' must be an integer") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"Query parameter '{key}' must be at least {minimum}")

    if maximum is not None and parsed > maximum:
        raise ValueError(f"Query parameter '{key}' must be at most {maximum}")

    return parsed


def _optional_query_text(query: dict[str, str], key: str) -> str | None:
    if key not in query:
        return None
    value = query[key].strip()
    return value or None


def _optional_query_bool(query: dict[str, str], key: str) -> bool | None:
    if key not in query:
        return None

    value = query[key].strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Query parameter '{key}' must be a boolean")


def _ensure_object_source_exists(object_id: str) -> None:
    object_source.get_object_source(object_id)


def _source_write_gate_error(headers: dict[str, str]) -> tuple[int, str] | None:
    if not _env_enabled(SOURCE_WRITES_ENV):
        return (
            403,
            f"Source writes are disabled. Set {SOURCE_WRITES_ENV}=true and {ADMIN_TOKEN_ENV}.",
        )

    return _admin_token_gate_error(headers, f"Source writes require {ADMIN_TOKEN_ENV}.")


def _permissions_gate_error(headers: dict[str, str]) -> tuple[int, str] | None:
    return _admin_token_gate_error(headers, f"Permissions API requires {ADMIN_TOKEN_ENV}.")


async def _send_permission_denied_if_needed(
    send,
    headers: dict[str, str],
    action: str,
    *,
    object_id: str,
    method: str,
) -> bool:
    if not _permission_checks_enabled():
        return False

    subject = _permission_subject(headers)
    enforced = _permission_enforcement_enabled()
    collection = _object_collection(object_id)

    try:
        policy = object_permission_store.load_policy(_data_dir())
        decision = object_permissions.check_permission(
            subject,
            action,
            policy=policy,
            collection=collection,
            object_id=object_id,
        )
    except ValueError as exc:
        _append_permission_audit_entry(
            action=action,
            object_id=object_id,
            collection=collection,
            method=method,
            subject=subject,
            enforced=enforced,
            error=f"Permission policy is invalid: {exc}",
        )
        if enforced:
            await _send_json(
                send,
                {"status": "error", "error": f"Permission policy is invalid: {exc}"},
                status=500,
            )
            return True
        return False

    _append_permission_audit_entry(
        action=action,
        object_id=object_id,
        collection=collection,
        method=method,
        subject=subject,
        enforced=enforced,
        decision=decision,
    )

    if not enforced or decision.allowed:
        return False

    await _send_json(
        send,
        {
            "status": "error",
            "error": decision.reason,
            "code": decision.code,
        },
        status=decision.http_status,
    )
    return True


def _permission_checks_enabled() -> bool:
    return _permission_enforcement_enabled() or _permission_audit_enabled()


def _permission_enforcement_enabled() -> bool:
    return _env_enabled(PERMISSION_ENFORCEMENT_ENV)


def _permission_audit_enabled() -> bool:
    return _env_enabled(PERMISSION_AUDIT_ENV) or _permission_enforcement_enabled()


def _permission_subject(headers: dict[str, str]) -> object_permissions.PermissionSubject:
    token = _authorization_token(headers)
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if token and admin_token and hmac.compare_digest(token, admin_token):
        return object_permissions.PermissionSubject(user_id="admin", roles=("admin",))

    if not _env_enabled(PERMISSION_TRUST_HEADERS_ENV):
        return object_permissions.PermissionSubject.anonymous()

    user_id = _optional_header_text(headers, "x-dbbasic-user-id")
    account_id = _optional_header_text(headers, "x-dbbasic-account-id")
    return object_permissions.PermissionSubject(
        user_id=user_id,
        account_id=account_id,
        roles=_csv_header(headers.get("x-dbbasic-roles", "")),
        subscriptions=_csv_header(headers.get("x-dbbasic-subscriptions", "")),
    )


def _optional_header_text(headers: dict[str, str], name: str) -> str | None:
    value = headers.get(name, "").strip()
    return value or None


def _csv_header(value: str) -> tuple[str, ...]:
    values = []
    seen = set()
    for item in value.split(","):
        normalized = item.strip()
        if normalized and normalized not in seen:
            values.append(normalized)
            seen.add(normalized)
    return tuple(values)


def _append_permission_audit_entry(
    *,
    action: str,
    object_id: str,
    collection: str | None,
    method: str,
    subject: object_permissions.PermissionSubject,
    enforced: bool,
    decision: object_permissions.PermissionDecision | None = None,
    error: str | None = None,
) -> None:
    if not _permission_audit_enabled():
        return

    entry: dict[str, Any] = {
        "timestamp": _utc_timestamp(),
        "method": method,
        "object_id": object_id,
        "collection": collection,
        "action": action,
        "subject": _permission_subject_payload(subject),
        "enforced": enforced,
    }
    if decision is not None:
        entry["decision"] = object_permissions.decision_to_dict(decision)
    if error is not None:
        entry["error"] = error

    try:
        object_permission_audit.append_permission_audit(entry, base_dir=_data_dir())
    except (OSError, ValueError):
        pass


def _permission_subject_payload(subject: object_permissions.PermissionSubject) -> dict[str, Any]:
    return {
        "user_id": subject.user_id,
        "account_id": subject.account_id,
        "roles": list(subject.roles),
        "subscriptions": list(subject.subscriptions),
        "authenticated": subject.is_authenticated,
    }


def _admin_token_gate_error(
    headers: dict[str, str],
    missing_token_message: str,
) -> tuple[int, str] | None:
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if not admin_token:
        return (403, missing_token_message)

    request_token = _authorization_token(headers)
    if request_token is None or not hmac.compare_digest(request_token, admin_token):
        return (401, "Unauthorized")

    return None


def _is_sensitive_get(query: dict[str, str]) -> bool:
    if "version" in query:
        return True
    if "file" in query:
        return True

    return any(query.get(flag) == "true" for flag in SENSITIVE_GET_FLAGS)


def _authorization_token(headers: dict[str, str]) -> str | None:
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() not in {"token", "bearer"} or not token:
        return None
    return token


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


def _version_manager() -> object_versions.VersionManager:
    return object_versions.VersionManager(_data_dir())


def _data_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, object_versions.DEFAULT_DATA_DIR)


def _max_concurrent_requests() -> int:
    return _env_int(MAX_CONCURRENT_REQUESTS_ENV, DEFAULT_MAX_CONCURRENT_REQUESTS)


def _max_concurrent_executions() -> int:
    return _env_int(MAX_CONCURRENT_EXECUTIONS_ENV, DEFAULT_MAX_CONCURRENT_EXECUTIONS)


def _max_request_bytes() -> int:
    max_bytes = _env_int(MAX_REQUEST_BYTES_ENV, DEFAULT_MAX_REQUEST_BYTES)
    if max_bytes < 0:
        return DEFAULT_MAX_REQUEST_BYTES
    return max_bytes


def _rate_limit_requests() -> int:
    requests = _env_int(RATE_LIMIT_REQUESTS_ENV, DEFAULT_RATE_LIMIT_REQUESTS)
    if requests < 0:
        return DEFAULT_RATE_LIMIT_REQUESTS
    return requests


def _rate_limit_window_seconds() -> int:
    seconds = _env_int(RATE_LIMIT_WINDOW_SECONDS_ENV, DEFAULT_RATE_LIMIT_WINDOW_SECONDS)
    if seconds <= 0:
        return DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    return seconds


def _object_timeout_seconds() -> float:
    seconds = _env_float(OBJECT_TIMEOUT_SECONDS_ENV, DEFAULT_OBJECT_TIMEOUT_SECONDS)
    if seconds < 0:
        return DEFAULT_OBJECT_TIMEOUT_SECONDS
    return seconds


def _object_runs_in_process(object_id: str) -> bool:
    return object_id in _trusted_in_process_object_ids()


def _trusted_in_process_object_ids() -> frozenset[str]:
    value = os.environ.get(TRUSTED_IN_PROCESS_OBJECTS_ENV, "")
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _rate_limit_dir() -> str:
    return os.path.join(_data_dir(), "ratelimit")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default

    try:
        return float(value)
    except ValueError:
        return default


def _content_length(headers: dict[str, str]) -> int | None:
    value = headers.get("content-length")
    if value is None:
        return None

    try:
        length = int(value)
    except ValueError:
        return None

    if length < 0:
        return None

    return length


async def _read_body(receive, *, headers: dict[str, str]) -> bytes:
    max_bytes = _max_request_bytes()
    content_length = _content_length(headers)
    if content_length is not None and content_length > max_bytes:
        raise RequestBodyTooLargeError(max_bytes=max_bytes, actual_bytes=content_length)

    body_parts = []
    body_size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(body_parts)
        if message["type"] != "http.request":
            continue

        chunk = message.get("body", b"")
        body_size += len(chunk)
        if body_size > max_bytes:
            raise RequestBodyTooLargeError(max_bytes=max_bytes, actual_bytes=body_size)

        body_parts.append(chunk)
        if not message.get("more_body", False):
            return b"".join(body_parts)


async def _send_request_too_large(send, error: RequestBodyTooLargeError) -> None:
    await _send_json(
        send,
        {
            "status": "error",
            "error": str(error),
            "max_bytes": error.max_bytes,
        },
        status=413,
    )


async def _send_capacity_error(send, *, limit_name: str, max_concurrent: int) -> None:
    await _send_json(
        send,
        {
            "status": "error",
            "error": "Server is busy",
            "limit": limit_name,
            "max_concurrent": max_concurrent,
        },
        status=503,
    )


async def _send_rate_limit_error(
    send,
    result: object_rate_limit.RateLimitResult,
) -> None:
    await _send_json(
        send,
        {
            "status": "error",
            "error": "Rate limit exceeded",
            "retry_after": result.retry_after,
            "limit": result.limit,
            "window_seconds": result.window_seconds,
        },
        status=429,
        headers=[("retry-after", str(result.retry_after))],
    )


async def _send_json(
    send,
    payload: Any,
    status: int = 200,
    headers: list[tuple[str, str]] | None = None,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    response_headers = [("content-type", "application/json; charset=utf-8")]
    if headers:
        response_headers.extend(headers)
    await _send_response(
        send,
        status=status,
        headers=response_headers,
        body=body,
    )


async def _send_object_response(send, payload: Any) -> None:
    status, headers, body = _normalize_object_response(payload)
    await _send_response(send, status=status, headers=headers, body=body)


def _normalize_object_response(payload: Any) -> tuple[int, list[tuple[str, str]], bytes]:
    if _is_response_tuple(payload):
        status, headers, body = payload
        return _normalize_status(status), _normalize_headers(headers), _normalize_body(body)

    if isinstance(payload, dict) and payload.get("content_type"):
        status = payload.get("status_code", payload.get("http_status", payload.get("status", 200)))
        headers = [("content-type", str(payload["content_type"]))]
        headers.extend(_normalize_headers(payload.get("headers", [])))
        return _normalize_status(status), headers, _normalize_body(payload.get("body", b""))

    if isinstance(payload, str):
        return 200, [("content-type", "text/html; charset=utf-8")], payload.encode("utf-8")

    if isinstance(payload, bytes):
        return 200, [("content-type", "application/octet-stream")], payload

    body = json.dumps(payload).encode("utf-8")
    return 200, [("content-type", "application/json; charset=utf-8")], body


async def _send_response(
    send,
    *,
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _is_response_tuple(payload: Any) -> bool:
    return isinstance(payload, tuple) and len(payload) == 3


def _normalize_status(status: Any) -> int:
    if isinstance(status, bool):
        return 200

    try:
        value = int(status)
    except (TypeError, ValueError):
        return 200

    if value < 100 or value > 599:
        return 200

    return value


def _normalize_headers(headers: Any) -> list[tuple[str, str]]:
    if headers is None:
        return []

    if isinstance(headers, dict):
        header_items = headers.items()
    elif isinstance(headers, (list, tuple)):
        header_items = headers
    else:
        return []

    normalized = []
    for item in header_items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name, value = item
        header_name = _header_text(name).lower()
        if header_name:
            normalized.append((header_name, _header_text(value)))
    return normalized


def _header_text(value: Any) -> str:
    if isinstance(value, bytes):
        text = value.decode("latin-1")
    else:
        text = str(value)
    return text.replace("\r", "").replace("\n", "")


def _download_filename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1]
    return name.replace("\\", "_").replace('"', "_") or "download"


def _normalize_body(body: Any) -> bytes:
    if body is None:
        return b""

    if isinstance(body, bytes):
        return body

    if isinstance(body, str):
        return body.encode("utf-8")

    if isinstance(body, (list, tuple)):
        parts = []
        for part in body:
            parts.append(_normalize_body(part))
        return b"".join(parts)

    return str(body).encode("utf-8")


async def _send_execution_error(
    send,
    result: object_execution.ObjectExecutionResult,
) -> None:
    error = result.error
    error_message = "Object execution failed"
    status = 500

    if error is not None:
        error_message = error.message
        if error.type == "ObjectNotFoundError":
            status = 404
        elif error.type == MethodNotSupportedError.__name__:
            status = 405
        elif error.type == object_execution.TIMEOUT_ERROR_TYPE:
            status = 504

    prefix = "Execution failed: "
    if status == 404:
        prefix = ""

    await _send_json(
        send,
        {
            "status": "error",
            "error": f"{prefix}{error_message}",
        },
        status=status,
    )


async def _handle_lifespan(receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("object_server:app", host="127.0.0.1", port=8001, reload=False)
