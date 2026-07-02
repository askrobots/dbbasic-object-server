"""Minimal ASGI app for DBBASIC Object Server.

This is the first public server slice. Source writes are disabled by default
while the production auth and mutation paths are extracted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from collections import Counter, deque
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Mapping

import http_api_contract
import object_backup
import object_collections
import object_correlation
import object_credentials
import object_daemon_control
import object_daemon_status
import object_events
import object_execution
import object_field_permissions
import object_file_changes
import object_files
import object_identity
import object_logs
import object_metadata
import object_package_changes
import object_permission_audit
import object_permission_store
import object_permission_status
import object_permissions
import object_packages
import object_rate_limit
import object_record_changes
import object_records
import object_schema_versions
import object_schemas
import object_source
import object_source_changes
import object_state
import object_versions
from object_namespace import get_object_roots, iter_object_sources, parse_user_object_id, validate_object_id
from object_versions import InvalidObjectIdError
from python_object_runtime import MethodNotSupportedError, PythonObjectRuntime

SOURCE_WRITES_ENV = "DBBASIC_ENABLE_SOURCE_WRITES"
FILE_WRITES_ENV = "DBBASIC_ENABLE_FILE_WRITES"
ADMIN_TOKEN_ENV = "DBBASIC_ADMIN_TOKEN"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"
MAX_REQUEST_BYTES_ENV = "DBBASIC_MAX_REQUEST_BYTES"
MAX_OBJECT_FILE_BYTES_ENV = "DBBASIC_MAX_OBJECT_FILE_BYTES"
MAX_CONCURRENT_REQUESTS_ENV = "DBBASIC_MAX_CONCURRENT_REQUESTS"
MAX_CONCURRENT_EXECUTIONS_ENV = "DBBASIC_MAX_CONCURRENT_EXECUTIONS"
RATE_LIMIT_REQUESTS_ENV = "DBBASIC_RATE_LIMIT_REQUESTS"
RATE_LIMIT_WINDOW_SECONDS_ENV = "DBBASIC_RATE_LIMIT_WINDOW_SECONDS"
RATE_LIMIT_TRUST_PROXY_HEADERS_ENV = "DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS"
OBJECT_TIMEOUT_SECONDS_ENV = "DBBASIC_OBJECT_TIMEOUT_SECONDS"
TRUSTED_IN_PROCESS_OBJECTS_ENV = "DBBASIC_TRUSTED_IN_PROCESS_OBJECTS"
PERMISSION_ENFORCEMENT_ENV = "DBBASIC_ENABLE_PERMISSION_ENFORCEMENT"
PERMISSION_UNREADY_ENFORCEMENT_ENV = "DBBASIC_ALLOW_UNREADY_PERMISSION_ENFORCEMENT"
PERMISSION_AUDIT_ENV = "DBBASIC_ENABLE_PERMISSION_AUDIT"
PERMISSION_TRUST_HEADERS_ENV = "DBBASIC_PERMISSION_TRUST_HEADERS"
REQUIRE_KNOWN_IDENTITY_USERS_ENV = "DBBASIC_REQUIRE_KNOWN_IDENTITY_USERS"
SESSION_LOGIN_ENV = "DBBASIC_ENABLE_SESSION_LOGIN"
SESSION_LOGIN_TOKEN_ENV = "DBBASIC_SESSION_LOGIN_TOKEN"
SESSION_ADMIN_GATES_ENV = "DBBASIC_ENABLE_SESSION_ADMIN_GATES"
PASSWORD_LOGIN_ENV = "DBBASIC_ENABLE_PASSWORD_LOGIN"
PASSWORD_LOGIN_FAILURE_DELAY_SECONDS = 0.5
SESSION_COOKIE_NAME = "dbbasic_session"
COOKIE_SECURE_ENV = "DBBASIC_COOKIE_SECURE"
RECORD_EVENTS_ENV = "DBBASIC_ENABLE_RECORD_EVENTS"
EVENT_KEEP_COUNT_ENV = "DBBASIC_EVENT_KEEP_COUNT"
EVENT_KEEP_SECONDS_ENV = "DBBASIC_EVENT_KEEP_SECONDS"
PACKAGES_DIR_ENV = "DBBASIC_PACKAGES_DIR"
PACKAGE_INSTALLS_ENABLED_ENV = "DBBASIC_ENABLE_PACKAGE_INSTALLS"
PACKAGE_RESTORE_ENABLED_ENV = "DBBASIC_ENABLE_PACKAGE_RESTORE"
DEFAULT_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_MAX_OBJECT_FILE_BYTES = DEFAULT_MAX_REQUEST_BYTES
DEFAULT_MAX_CONCURRENT_REQUESTS = 64
DEFAULT_MAX_CONCURRENT_EXECUTIONS = 8
DEFAULT_RATE_LIMIT_REQUESTS = 0
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_OBJECT_TIMEOUT_SECONDS = 0.0
TRUE_VALUES = {"1", "true", "yes", "on"}
SENSITIVE_GET_FLAGS = {
    "source",
    "source_changes",
    "changes",
    "state",
    "logs",
    "metadata",
    "versions",
    "files",
    "file",
}
RECORD_EVENT_TYPES = {
    "create": "collection.record.created",
    "update": "collection.record.updated",
    "delete": "collection.record.deleted",
}
ADMIN_CHANGE_KINDS = frozenset({"source", "file", "record", "package"})

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
    request_headers = _parse_headers(scope.get("headers", []))
    correlation_id = object_correlation.ensure_correlation_id(
        request_headers.get(object_correlation.CORRELATION_ID_HEADER)
    )
    correlation_token = object_correlation.set_current_correlation_id(correlation_id)

    async def send_with_metrics(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message.get("status", status_code))
            headers = list(message.get("headers", []))
            if not any(
                name.lower() == object_correlation.CORRELATION_ID_HEADER.encode("latin-1")
                for name, _value in headers
            ):
                headers.append(
                    (
                        object_correlation.CORRELATION_ID_HEADER.encode("latin-1"),
                        correlation_id.encode("latin-1"),
                    )
                )
            message = {**message, "headers": headers}
        await send(message)

    try:
        await _handle_http(scope, receive, send_with_metrics)
    finally:
        object_correlation.reset_current_correlation_id(correlation_token)
        duration_ms = (time.perf_counter() - started_at) * 1000
        _metrics.record_request(method, path, status_code, duration_ms)


async def _handle_http(scope: dict[str, Any], receive, send) -> None:
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    query = _parse_query(scope.get("query_string", b""))
    headers = _parse_headers(scope.get("headers", []))
    correlation_id = object_correlation.current_correlation_id()
    if correlation_id is not None:
        headers[object_correlation.CORRELATION_ID_HEADER] = correlation_id

    if path == "/health":
        if _is_detailed_health(query) and await _send_rate_limit_if_needed(scope, headers, send):
            return
        await _handle_health(send, query, headers)
        return

    if await _send_rate_limit_if_needed(scope, headers, send):
        return

    if (
        method not in {"GET", "HEAD", "OPTIONS"}
        and _authorization_token(headers) is None
        and _session_cookie_token(headers) is not None
        and not _cookie_request_origin_allowed(headers)
    ):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie request rejected"},
            status=403,
        )
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

        if path == http_api_contract.LOGIN_PATH:
            await _handle_login(send, method, query, body, headers)
            return

        if path == http_api_contract.LOGOUT_PATH:
            await _handle_logout(send, method, headers)
            return

        if path == http_api_contract.IDENTITY_ACCOUNTS_PATH:
            await _handle_identity_accounts(send, method, body, headers)
            return

        identity_account_prefix = http_api_contract.IDENTITY_ACCOUNTS_PATH + "/"
        if path.startswith(identity_account_prefix):
            account_id = path[len(identity_account_prefix):]
            await _handle_identity_account(send, method, account_id, headers)
            return

        if path == http_api_contract.IDENTITY_USERS_PATH:
            await _handle_identity_users(send, method, query, body, headers)
            return

        identity_user_prefix = http_api_contract.IDENTITY_USERS_PATH + "/"
        if path.startswith(identity_user_prefix):
            user_id = path[len(identity_user_prefix):]
            await _handle_identity_user(send, method, user_id, body, headers)
            return

        if path == http_api_contract.IDENTITY_SESSIONS_PATH:
            await _handle_identity_sessions(send, method, body, headers)
            return

        identity_session_prefix = http_api_contract.IDENTITY_SESSIONS_PATH + "/"
        if path.startswith(identity_session_prefix):
            session_id = path[len(identity_session_prefix):]
            await _handle_identity_session(send, method, session_id, headers)
            return

        if path == http_api_contract.IDENTITY_CURRENT_SESSION_PATH:
            await _handle_identity_current_session(send, method, body, headers)
            return

        if path == http_api_contract.IDENTITY_PATH:
            await _handle_identity(send, method, headers)
            return

        if path == http_api_contract.PERMISSIONS_POLICY_PATH:
            await _handle_permissions_policy(send, method, body, headers)
            return

        if path == http_api_contract.PERMISSIONS_STATUS_PATH:
            await _handle_permissions_status(send, method, headers)
            return

        if path == http_api_contract.PERMISSIONS_CHECK_PATH:
            await _handle_permissions_check(send, method, body, headers)
            return

        if path == http_api_contract.PERMISSIONS_AUDIT_PATH:
            await _handle_permissions_audit(send, method, query, headers)
            return

        if path == http_api_contract.EVENTS_PATH:
            await _handle_events(send, method, query, body, headers)
            return

        if path == http_api_contract.EVENT_DELIVERIES_PATH:
            await _handle_event_deliveries(send, method, query, headers)
            return

        if path == http_api_contract.EVENT_SUBSCRIPTIONS_PATH:
            await _handle_event_subscriptions(send, method, query, body, headers)
            return

        if path == http_api_contract.ADMIN_STATUS_PATH:
            await _handle_admin_status(send, method, headers)
            return

        if path == http_api_contract.ADMIN_CHANGES_PATH:
            await _handle_admin_changes(send, method, query, headers)
            return

        if path == http_api_contract.ADMIN_FILES_PATH:
            await _handle_admin_files(send, method, query, headers)
            return

        admin_files_prefix = f"{http_api_contract.ADMIN_FILES_PATH}/"
        if path.startswith(admin_files_prefix):
            object_id = path.removeprefix(admin_files_prefix)
            await _handle_admin_object_files(send, method, object_id, query, body, headers)
            return

        if path == http_api_contract.ADMIN_OBJECTS_PATH:
            await _handle_admin_objects(send, method, body, headers)
            return

        admin_objects_prefix = f"{http_api_contract.ADMIN_OBJECTS_PATH}/"
        if path.startswith(admin_objects_prefix):
            object_tail = path.removeprefix(admin_objects_prefix)
            if object_tail.endswith("/execute"):
                object_id = object_tail.removesuffix("/execute")
                await _handle_admin_object_execute(send, method, object_id, body, headers)
                return

            object_id = object_tail
            await _handle_admin_object(send, method, object_id, query, body, headers)
            return

        if path == http_api_contract.ADMIN_COLLECTIONS_PATH:
            await _handle_admin_collections(send, method, headers)
            return

        admin_collections_prefix = f"{http_api_contract.ADMIN_COLLECTIONS_PATH}/"
        if path.startswith(admin_collections_prefix):
            collection_tail = path.removeprefix(admin_collections_prefix)
            await _handle_admin_collection(send, method, collection_tail, query, body, headers)
            return

        if path == http_api_contract.ADMIN_SCHEMAS_PATH:
            await _handle_admin_schemas(send, method, headers)
            return

        admin_schemas_prefix = f"{http_api_contract.ADMIN_SCHEMAS_PATH}/"
        if path.startswith(admin_schemas_prefix):
            schema = path.removeprefix(admin_schemas_prefix)
            await _handle_admin_schema(send, method, schema, query, body, headers)
            return

        if path == http_api_contract.ADMIN_IDENTITY_ACCOUNTS_PATH:
            await _handle_admin_identity_accounts(send, method, headers)
            return

        admin_identity_accounts_prefix = f"{http_api_contract.ADMIN_IDENTITY_ACCOUNTS_PATH}/"
        if path.startswith(admin_identity_accounts_prefix):
            account_id = path.removeprefix(admin_identity_accounts_prefix)
            await _handle_admin_identity_account(send, method, account_id, headers)
            return

        if path == http_api_contract.ADMIN_IDENTITY_USERS_PATH:
            await _handle_admin_identity_users(send, method, query, headers)
            return

        admin_identity_users_prefix = f"{http_api_contract.ADMIN_IDENTITY_USERS_PATH}/"
        if path.startswith(admin_identity_users_prefix):
            user_id = path.removeprefix(admin_identity_users_prefix)
            await _handle_admin_identity_user(send, method, user_id, body, headers)
            return

        if path == http_api_contract.ADMIN_IDENTITY_SESSIONS_PATH:
            await _handle_admin_identity_sessions(send, method, headers)
            return

        admin_identity_sessions_prefix = f"{http_api_contract.ADMIN_IDENTITY_SESSIONS_PATH}/"
        if path.startswith(admin_identity_sessions_prefix):
            session_id = path.removeprefix(admin_identity_sessions_prefix)
            await _handle_admin_identity_session(send, method, session_id, headers)
            return

        if path == http_api_contract.DAEMON_STATUS_PATH:
            await _handle_daemon_status(send, method, headers)
            return

        if path == http_api_contract.DAEMON_SCHEDULER_TASKS_PATH:
            await _handle_daemon_scheduler_tasks(send, method, query, body, headers)
            return

        daemon_scheduler_prefix = http_api_contract.DAEMON_SCHEDULER_TASKS_PATH + "/"
        if path.startswith(daemon_scheduler_prefix):
            task_id = path[len(daemon_scheduler_prefix):]
            await _handle_daemon_scheduler_task(send, method, task_id, body, headers)
            return

        if path == http_api_contract.DAEMON_QUEUE_MESSAGES_PATH:
            await _handle_daemon_queue_messages(send, method, query, body, headers)
            return

        daemon_queue_prefix = http_api_contract.DAEMON_QUEUE_MESSAGES_PATH + "/"
        if path.startswith(daemon_queue_prefix):
            message_id = path[len(daemon_queue_prefix):]
            await _handle_daemon_queue_message(send, method, message_id, body, headers)
            return

        if path == http_api_contract.PACKAGES_PATH:
            await _handle_packages(send, method, headers)
            return

        if path.startswith(f"{http_api_contract.PACKAGES_PATH}/"):
            package_tail = path.removeprefix(f"{http_api_contract.PACKAGES_PATH}/")
            package_parts = package_tail.split("/")
            if len(package_parts) == 2 and package_parts[1] == "install":
                await _handle_package_install(send, method, package_parts[0], body, headers)
            elif len(package_parts) == 2 and package_parts[1] == "restore":
                await _handle_package_restore(send, method, package_parts[0], body, headers)
            elif len(package_parts) == 2 and package_parts[1] == "changes":
                await _handle_package_changes(send, method, package_parts[0], query, headers)
            else:
                await _handle_package(send, method, package_tail, query, headers)
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

            if method == "POST":
                await _handle_objects_post(send, body, headers)
                return

            await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
            return

        if path == http_api_contract.COLLECTIONS_PATH:
            await _handle_collections(send, method, headers)
            return

        if path.startswith(f"{http_api_contract.COLLECTIONS_PATH}/"):
            collection_path = path.removeprefix(f"{http_api_contract.COLLECTIONS_PATH}/")
            collection_parts = collection_path.split("/")
            if len(collection_parts) == 2 and collection_parts[1] == "changes":
                await _handle_collection_changes(
                    send,
                    method,
                    collection_parts[0],
                    query,
                    headers,
                )
                return

            if len(collection_parts) == 2 and collection_parts[1] == "records":
                await _handle_collection_records(
                    send,
                    method,
                    collection_parts[0],
                    query,
                    body,
                    headers,
                )
                return

            if (
                len(collection_parts) == 4
                and collection_parts[1] == "records"
                and collection_parts[3] == "changes"
            ):
                await _handle_collection_record_changes(
                    send,
                    method,
                    collection_parts[0],
                    collection_parts[2],
                    query,
                    headers,
                )
                return

            if len(collection_parts) == 3 and collection_parts[1] == "records":
                await _handle_collection_record(
                    send,
                    method,
                    collection_parts[0],
                    collection_parts[2],
                    body,
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
            await _handle_schema(send, method, schema, query, body, headers)
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


async def _handle_identity(send, method: str, headers: dict[str, str]) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    subject, auth_method = _permission_identity(headers)
    await _send_json(
        send,
        {
            "status": "ok",
            "subject": _permission_subject_payload(subject),
            "auth": {
                "method": auth_method,
                "trusted_headers_enabled": _env_enabled(PERMISSION_TRUST_HEADERS_ENV),
                "trusted_headers_present": _trusted_identity_headers_present(headers),
            },
            "permissions": {
                "enforcement_enabled": _permission_enforcement_enabled(),
                "enforcement_requested": _permission_enforcement_requested(),
                "enforcement_blocked": _permission_enforcement_blocked(),
                "audit_enabled": _permission_audit_enabled(),
            },
        },
    )


async def _handle_identity_accounts(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity accounts require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        accounts = object_identity.list_accounts(base_dir=_data_dir())
        await _send_json(
            send,
            {
                "status": "ok",
                "accounts": accounts,
                "count": len(accounts),
            },
        )
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            account = object_identity.create_account(payload, base_dir=_data_dir())
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return

        await _send_json(send, {"status": "ok", "account": account}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_account(
    send,
    method: str,
    account_id: str,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity accounts require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if "/" in account_id or not account_id:
        await _send_json(send, {"status": "error", "error": "Invalid account id"}, status=400)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    try:
        account = object_identity.get_account(account_id, base_dir=_data_dir())
    except object_identity.InvalidIdentityPayloadError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_identity.AccountNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(send, {"status": "ok", "account": account})


async def _handle_identity_users(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity users require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            users = object_identity.list_users(
                account_id=query.get("account_id"),
                base_dir=_data_dir(),
            )
        except object_identity.InvalidIdentityPayloadError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "users": users,
                "count": len(users),
            },
        )
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            user = object_identity.create_user(payload, base_dir=_data_dir())
        except object_identity.AccountNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return

        await _send_json(send, {"status": "ok", "user": user}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_user(
    send,
    method: str,
    user_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity users require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if user_id.endswith("/password"):
        target_user_id = user_id.removesuffix("/password")
        if "/" in target_user_id or not target_user_id:
            await _send_json(send, {"status": "error", "error": "Invalid user id"}, status=400)
            return
        await _handle_identity_user_password(send, method, target_user_id, body)
        return

    if "/" in user_id or not user_id:
        await _send_json(send, {"status": "error", "error": "Invalid user id"}, status=400)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    try:
        user = object_identity.get_user(user_id, base_dir=_data_dir())
    except object_identity.InvalidIdentityPayloadError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_identity.UserNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(send, {"status": "ok", "user": user})


async def _handle_identity_user_password(
    send,
    method: str,
    user_id: str,
    body: bytes,
) -> None:
    try:
        user = object_identity.get_user(user_id, base_dir=_data_dir())
    except object_identity.InvalidIdentityPayloadError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_identity.UserNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            password = payload.get("password")
            result = object_credentials.set_password(
                user["user_id"],
                password,
                base_dir=_data_dir(),
            )
        except object_credentials.InvalidPasswordError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "user_id": result["user_id"],
                "operation": result["operation"],
                "updated_at": result["updated_at"],
            },
        )
        return

    if method == "DELETE":
        try:
            removed = object_credentials.remove_password(user["user_id"], base_dir=_data_dir())
        except object_credentials.InvalidPasswordError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "user_id": user["user_id"],
                "removed": removed,
            },
        )
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_sessions(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity sessions require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        sessions = object_identity.list_sessions(base_dir=_data_dir())
        await _send_json(
            send,
            {
                "status": "ok",
                "sessions": sessions,
                "count": len(sessions),
            },
        )
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            result = object_identity.create_session(
                payload,
                base_dir=_data_dir(),
                require_known_user=_env_enabled(REQUIRE_KNOWN_IDENTITY_USERS_ENV),
            )
        except object_identity.UserNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except object_identity.AccountNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return

        await _send_json(send, {"status": "ok", **result}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_session(
    send,
    method: str,
    session_id: str,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Identity sessions require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if "/" in session_id or not session_id:
        await _send_json(send, {"status": "error", "error": "Invalid session id"}, status=400)
        return

    if method == "GET":
        try:
            session = object_identity.get_session(session_id, base_dir=_data_dir())
        except object_identity.InvalidSessionPayloadError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_identity.SessionNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return

        await _send_json(send, {"status": "ok", "session": session})
        return

    if method == "DELETE":
        try:
            session = object_identity.revoke_session(session_id, base_dir=_data_dir())
        except object_identity.InvalidSessionPayloadError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_identity.SessionNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return

        await _send_json(send, {"status": "ok", "session": session})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_current_session(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method not in {"GET", "POST", "DELETE"}:
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if method == "POST":
        try:
            peeked_payload = _parse_json_body(body)
        except ValueError:
            peeked_payload = None
        if isinstance(peeked_payload, dict) and "password" in peeked_payload:
            await _handle_identity_password_login(send, peeked_payload)
            return

        gate_error = _session_login_gate_error(headers)
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

        try:
            payload = _parse_json_body(body)
            result = object_identity.create_session(
                _identity_session_login_payload(payload),
                base_dir=_data_dir(),
                require_known_user=True,
            )
        except object_identity.UserNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except object_identity.AccountNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return

        await _send_json(send, {"status": "ok", **result}, status=201)
        return

    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send,
            {"status": "error", "error": "Active session token required"},
            status=401,
        )
        return

    if method == "GET":
        await _send_json(send, {"status": "ok", "session": session.public_payload()})
        return

    try:
        revoked = object_identity.revoke_session(session.session_id, base_dir=_data_dir())
    except (object_identity.InvalidSessionPayloadError, object_identity.SessionNotFoundError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    await _send_json(send, {"status": "ok", "session": revoked})


async def _handle_identity_password_login(
    send,
    payload: dict[str, Any],
) -> None:
    if not _env_enabled(PASSWORD_LOGIN_ENV):
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Password login is disabled. Set {PASSWORD_LOGIN_ENV}=true.",
            },
            status=403,
        )
        return

    try:
        login = _identity_password_login_payload(payload)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    user = _password_login_user(login)
    lookup_user_id = user["user_id"] if user is not None else "__unknown_password_login__"
    verified = object_credentials.verify_password(
        lookup_user_id,
        login["password"],
        base_dir=_data_dir(),
    )

    if user is None or not verified:
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_json(send, {"status": "error", "error": "Invalid credentials"}, status=401)
        return

    try:
        result = object_identity.create_session(
            {
                "user_id": user["user_id"],
                "label": login["label"],
                "ttl_seconds": login["ttl_seconds"],
            },
            base_dir=_data_dir(),
            require_known_user=True,
        )
    except ValueError:
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_json(send, {"status": "error", "error": "Invalid credentials"}, status=401)
        return

    await _send_json(send, {"status": "ok", **result}, status=201)


def _identity_password_login_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed_keys = {"user_id", "email", "password", "label", "ttl_seconds"}
    unexpected = sorted(set(payload) - allowed_keys)
    if unexpected:
        raise ValueError(f"Unsupported password login fields: {', '.join(unexpected)}")

    password = payload.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")

    user_id = payload.get("user_id")
    email = payload.get("email")
    if (user_id is None) == (email is None):
        raise ValueError("Provide exactly one of user_id or email")

    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        raise ValueError("label must be a string")

    return {
        "user_id": user_id,
        "email": email,
        "password": password,
        "label": label or "password login",
        "ttl_seconds": payload.get("ttl_seconds"),
    }


async def _handle_login(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "GET":
        if not _env_enabled(PASSWORD_LOGIN_ENV):
            await _send_login_page(send, error="Password login is disabled on this server.", status=403)
            return
        if _current_identity_session(headers) is not None:
            await _send_redirect(send, _safe_next_path(query.get("next")))
            return
        error = "Invalid email or password." if query.get("error") else None
        await _send_login_page(send, error=error, next_path=_safe_next_path(query.get("next")))
        return

    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if not _env_enabled(PASSWORD_LOGIN_ENV):
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Password login is disabled. Set {PASSWORD_LOGIN_ENV}=true.",
            },
            status=403,
        )
        return

    form = _parse_form_body(body, headers)
    if form is None:
        await _send_json(
            send,
            {
                "status": "error",
                "error": "Login form must be application/x-www-form-urlencoded; JSON clients should use POST /identity/session.",
            },
            status=400,
        )
        return

    next_path = _safe_next_path(form.get("next"))
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    failure_location = f"{http_api_contract.LOGIN_PATH}?error=1"
    if next_path != "/":
        failure_location += f"&next={urllib.parse.quote(next_path)}"

    if not email or not password:
        await _send_redirect(send, failure_location)
        return

    login = {"user_id": None, "email": email, "password": password}
    user = _password_login_user(login)
    lookup_user_id = user["user_id"] if user is not None else "__unknown_password_login__"
    verified = object_credentials.verify_password(lookup_user_id, password, base_dir=_data_dir())

    if user is None or not verified:
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_redirect(send, failure_location)
        return

    try:
        result = object_identity.create_session(
            {"user_id": user["user_id"], "label": "browser login"},
            base_dir=_data_dir(),
            require_known_user=True,
        )
    except ValueError:
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_redirect(send, failure_location)
        return

    await _send_redirect(
        send,
        next_path,
        extra_headers=[
            _session_cookie_header(
                result["token"],
                max_age=object_identity.DEFAULT_SESSION_TTL_SECONDS,
            )
        ],
    )


async def _handle_logout(send, method: str, headers: dict[str, str]) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    session = _current_identity_session(headers)
    if session is not None:
        try:
            object_identity.revoke_session(session.session_id, base_dir=_data_dir())
        except (OSError, ValueError, LookupError):
            pass

    await _send_redirect(
        send,
        http_api_contract.LOGIN_PATH,
        extra_headers=[_session_cookie_header("", max_age=0)],
    )


def _parse_form_body(body: bytes, headers: dict[str, str]) -> dict[str, str] | None:
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type != "application/x-www-form-urlencoded":
        return None
    try:
        pairs = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return None
    return {name: value for name, value in pairs}


def _safe_next_path(value: str | None) -> str:
    candidate = (value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or any(ord(char) < 32 for char in candidate)
    ):
        return "/"
    return candidate


def _session_cookie_header(token: str, *, max_age: int) -> tuple[str, str]:
    attributes = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if _cookie_secure_enabled():
        attributes.append("Secure")
    return ("set-cookie", "; ".join(attributes))


def _cookie_secure_enabled() -> bool:
    value = os.environ.get(COOKIE_SECURE_ENV, "").strip().lower()
    if not value:
        return True
    return value in TRUE_VALUES


async def _send_redirect(
    send,
    location: str,
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    headers = [
        ("location", location),
        ("content-type", "text/html; charset=utf-8"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    body = f'<a href="{html.escape(location, quote=True)}">Continue</a>'.encode("utf-8")
    await _send_response(send, status=303, headers=headers, body=body)


async def _send_login_page(
    send,
    *,
    error: str | None = None,
    next_path: str = "/",
    status: int = 200,
) -> None:
    error_block = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #f5f5f4; margin: 0;
       display: flex; min-height: 100vh; align-items: center; justify-content: center; }}
form {{ background: #fff; border: 1px solid #d6d3d1; border-radius: 8px;
        padding: 2rem; width: 20rem; }}
h1 {{ font-size: 1.1rem; margin: 0 0 1rem; }}
label {{ display: block; font-size: 0.85rem; margin: 0.75rem 0 0.25rem; }}
input {{ width: 100%; box-sizing: border-box; padding: 0.5rem;
         border: 1px solid #d6d3d1; border-radius: 4px; }}
button {{ margin-top: 1.25rem; width: 100%; padding: 0.6rem; border: 0;
          border-radius: 4px; background: #1c1917; color: #fff; cursor: pointer; }}
.error {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b;
          border-radius: 4px; padding: 0.5rem; font-size: 0.85rem; }}
</style>
</head>
<body>
<form method="post" action="{html.escape(http_api_contract.LOGIN_PATH, quote=True)}">
<h1>Sign in</h1>
{error_block}
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="username" required autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
<button type="submit">Sign in</button>
</form>
</body>
</html>"""
    await _send_response(
        send,
        status=status,
        headers=[("content-type", "text/html; charset=utf-8")],
        body=page.encode("utf-8"),
    )


def _password_login_user(login: Mapping[str, Any]) -> dict[str, Any] | None:
    if login["user_id"] is not None:
        if not isinstance(login["user_id"], str):
            return None
        try:
            user = object_identity.get_user(login["user_id"], base_dir=_data_dir())
        except (ValueError, LookupError):
            return None
        return user if user.get("status") == "active" else None

    email = login["email"]
    if not isinstance(email, str) or not email.strip():
        return None
    normalized_email = email.strip().casefold()
    try:
        users = object_identity.list_users(base_dir=_data_dir())
    except ValueError:
        return None
    matches = [
        user
        for user in users
        if (user.get("email") or "").casefold() == normalized_email
        and user.get("status") == "active"
    ]
    if len(matches) != 1:
        return None
    return matches[0]


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
            "file_writes_enabled": _env_enabled(FILE_WRITES_ENV),
            "max_request_bytes": _max_request_bytes(),
            "max_object_file_bytes": _max_object_file_bytes(),
            "max_concurrent_requests": _max_concurrent_requests(),
            "max_concurrent_executions": _max_concurrent_executions(),
            "rate_limit_requests": _rate_limit_requests(),
            "rate_limit_window_seconds": _rate_limit_window_seconds(),
            "rate_limit_trust_proxy_headers": _env_enabled(RATE_LIMIT_TRUST_PROXY_HEADERS_ENV),
            "object_timeout_seconds": _object_timeout_seconds(),
            "trusted_in_process_objects": sorted(_trusted_in_process_object_ids()),
            "permission_enforcement_enabled": _permission_enforcement_enabled(),
            "permission_enforcement_requested": _permission_enforcement_requested(),
            "permission_enforcement_blocked": _permission_enforcement_blocked(),
            "permission_allow_unready_enforcement": _permission_unready_enforcement_allowed(),
            "permission_audit_enabled": _permission_audit_enabled(),
            "permission_trust_headers": _env_enabled(PERMISSION_TRUST_HEADERS_ENV),
            "require_known_identity_users": _env_enabled(REQUIRE_KNOWN_IDENTITY_USERS_ENV),
            "event_keep_count": _event_keep_count(),
            "event_keep_seconds": _event_keep_seconds(),
        },
        "checks": {
            "storage": storage_check,
        },
        "system": _system_snapshot(),
    }

    if include_metrics:
        payload["metrics"] = metrics

    return payload


async def _handle_admin_status(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin status requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        payload = _admin_status_payload()
    except object_packages.InvalidPackageManifestError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not build admin status: {exc}"},
            status=500,
        )
        return

    status_code = 503 if payload["status"] == "degraded" else 200
    await _send_json(send, payload, status=status_code)


async def _handle_admin_changes(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin change history requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        limit = _query_int(
            query,
            "limit",
            default=100,
            minimum=1,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        offset = _query_int(
            query,
            "offset",
            default=0,
            minimum=0,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        payload = _admin_changes_payload(query, limit=limit, offset=offset)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (InvalidObjectIdError, object_collections.InvalidCollectionNameError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (object_records.InvalidRecordIdError, object_packages.InvalidPackageIdError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_file_changes.InvalidFileChangeError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not list admin changes: {exc}"},
            status=500,
        )
        return

    await _send_json(send, {"status": "ok", **payload})


async def _handle_admin_objects(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "POST":
        await _handle_objects_post(send, body, headers)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin object listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    await _send_json(send, _list_objects_payload())


async def _handle_admin_object(
    send,
    method: str,
    object_id: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "PUT" and query.get("source") == "true":
        await _handle_object_source_put(send, object_id, body, headers)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin object inspection requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in admin inspection"},
            status=400,
        )
        return

    if query.get("source") == "true":
        await _handle_object_source_get(send, object_id)
        return

    if query.get("state") == "true":
        await _handle_object_state_get(send, object_id)
        return

    if query.get("logs") == "true":
        await _handle_object_logs_get(send, object_id, query)
        return

    if query.get("versions") == "true":
        await _handle_object_versions_get(send, object_id, query)
        return

    if "version" in query:
        await _handle_object_version_get(send, object_id, query)
        return

    if query.get("source_changes") == "true":
        await _handle_object_source_changes_get(send, object_id, query)
        return

    if query.get("changes") == "true":
        await _handle_object_changes_get(send, object_id, query)
        return

    if query.get("files") == "true":
        await _handle_object_files_get(send, object_id)
        return

    if "file" in query:
        await _handle_object_file_get(send, object_id, query)
        return

    if query.get("metadata") == "true" or query.get("format") == "json" or not query:
        await _handle_object_metadata_get(send, object_id)
        return

    await _send_json(
        send,
        {
            "status": "error",
            "error": "Unsupported admin object inspection query",
            "allowed_queries": [
                "metadata=true",
                "source=true",
                "state=true",
                "logs=true",
                "versions=true",
                "version=<id>",
                "source_changes=true",
                "changes=true",
                "files=true",
                "file=<name>",
            ],
        },
        status=400,
    )


async def _handle_admin_object_execute(
    send,
    method: str,
    object_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin object execution requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in admin execution"},
            status=400,
        )
        return

    try:
        payload = _parse_json_body(body)
        execute_method = _admin_execute_method(payload)
        execute_payload = _admin_execute_payload(payload)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    permission_action = object_permissions.EXECUTE
    if execute_method == "PUT":
        permission_action = object_permissions.UPDATE
    elif execute_method == "DELETE":
        permission_action = object_permissions.DELETE

    await _execute_object_method(
        send,
        object_id,
        execute_method,
        execute_payload,
        headers,
        permission_action=permission_action,
    )


async def _handle_admin_files(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin file inspection requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    object_id = _optional_query_text(query, "object_id")
    if "file" in query:
        if object_id is None:
            await _send_json(
                send,
                {"status": "error", "error": "Query parameter 'object_id' is required"},
                status=400,
            )
            return
        await _handle_object_file_get(send, object_id, query)
        return

    try:
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        if object_id is not None:
            _ensure_object_source_exists(object_id)
        files = object_files.list_all_object_files(
            base_dir=_data_dir(),
            object_id=object_id,
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
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not read object files: {exc}"},
            status=500,
        )
        return

    page = files[offset : offset + limit]
    await _send_json(
        send,
        {
            "status": "ok",
            "files": page,
            "count": len(page),
            "total": len(files),
        },
    )


async def _handle_admin_object_files(
    send,
    method: str,
    object_id: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in admin inspection"},
            status=400,
        )
        return

    if method in {"POST", "PUT", "DELETE"}:
        await _handle_admin_object_file_write(send, method, object_id, query, body, headers)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin file inspection requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if "file" in query:
        await _handle_object_file_get(send, object_id, query)
        return

    await _handle_object_files_get(send, object_id)


async def _handle_admin_object_file_write(
    send,
    method: str,
    object_id: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _file_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if await _send_permission_denied_if_needed(
        send,
        headers,
        object_permissions.FILES,
        object_id=object_id,
        method=method,
    ):
        return

    try:
        _ensure_object_source_exists(object_id)
        if method in {"POST", "PUT"}:
            filename, content = _file_write_payload(body)
            metadata = object_files.write_object_file(
                object_id,
                filename,
                content,
                base_dir=_data_dir(),
                overwrite=method == "PUT",
                max_bytes=_max_object_file_bytes(),
            )
            operation = "created" if method == "POST" else "updated"
            status_code = 201 if method == "POST" else 200
        else:
            filename = _file_delete_filename(query, body)
            metadata = object_files.delete_object_file(
                object_id,
                filename,
                base_dir=_data_dir(),
            )
            operation = "deleted"
            status_code = 200
    except object_files.ObjectFileTooLargeError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=413)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_files.InvalidObjectFilenameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_files.ObjectFileExistsError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=409)
        return
    except object_files.ObjectFileNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not write object file: {exc}"},
            status=500,
        )
        return

    correlation_id = object_correlation.current_correlation_id()
    _append_file_log(object_id, operation=operation, metadata=metadata, method=method)
    change = _append_file_change_log(
        object_id,
        operation=operation,
        metadata=metadata,
        method=method,
        actor=_record_change_actor(headers),
        correlation_id=correlation_id,
    )
    payload = {
        "status": "ok",
        "message": f"File {operation}: {metadata['name']}",
        "object_id": object_id,
        "operation": operation,
        "file": {"object_id": object_id, **metadata},
        "correlation_id": correlation_id,
    }
    if change is not None:
        payload["change"] = change

    await _send_json(send, payload, status=status_code)


async def _handle_admin_collections(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin collection listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    await _handle_collections(send, method, headers)


async def _handle_admin_collection(
    send,
    method: str,
    collection_tail: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    parts = collection_tail.split("/")

    if method == "POST" and len(parts) == 2 and parts[1] == "records":
        await _handle_collection_record_create(send, parts[0], body, headers)
        return

    if method == "PUT" and len(parts) == 3 and parts[1] == "records":
        await _handle_collection_record_update(send, parts[0], parts[2], body, headers)
        return

    if method == "DELETE" and len(parts) == 3 and parts[1] == "records":
        await _handle_collection_record_delete(send, parts[0], parts[2], headers)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin collection inspection requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if len(parts) == 2 and parts[1] == "changes":
        await _handle_collection_changes(send, method, parts[0], query, headers)
        return

    if len(parts) == 2 and parts[1] == "records":
        await _handle_collection_records_get(send, parts[0], query, headers)
        return

    if len(parts) == 4 and parts[1] == "records" and parts[3] == "changes":
        await _handle_collection_record_changes(send, method, parts[0], parts[2], query, headers)
        return

    if len(parts) == 3 and parts[1] == "records":
        await _handle_collection_record_get(send, parts[0], parts[2], headers)
        return

    if len(parts) == 1:
        await _handle_collection_get(send, method, collection_tail, headers)
        return

    await _send_json(
        send,
        {
            "status": "error",
            "error": "Unsupported admin collection inspection path",
            "allowed_paths": [
                "/admin/collections/{collection}",
                "/admin/collections/{collection}/records",
                "/admin/collections/{collection}/records/{record_id}",
                "/admin/collections/{collection}/changes",
                "/admin/collections/{collection}/records/{record_id}/changes",
            ],
        },
        status=400,
    )


async def _handle_admin_schemas(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin schema listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    await _handle_schemas(send, method, headers)


async def _handle_admin_schema(
    send,
    method: str,
    schema: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "PUT":
        await _handle_schema_put(send, schema, body, headers)
        return

    if method == "POST":
        await _handle_schema_post(send, schema, body, headers)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin schema inspection requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    supported_schema_keys = {"format", "limit", "version", "versions"}
    only_format = set(query).issubset({"format"})
    supported = set(query).issubset(supported_schema_keys) and (
        not query or only_format or query.get("versions") == "true" or "version" in query
    )
    if not supported:
        await _send_json(
            send,
            {
                "status": "error",
                "error": "Unsupported admin schema inspection query",
                "allowed_queries": ["versions=true", "version=<id>"],
            },
            status=400,
        )
        return

    await _handle_schema(send, method, schema, query, b"", headers)


async def _handle_admin_identity_accounts(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_accounts(send, method, b"", headers)


async def _handle_admin_identity_account(
    send,
    method: str,
    account_id: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_account(send, method, account_id, headers)


async def _handle_admin_identity_users(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_users(send, method, query, b"", headers)


async def _handle_admin_identity_user(
    send,
    method: str,
    user_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if user_id.endswith("/password") and method in {"POST", "DELETE"}:
        await _handle_identity_user(send, method, user_id, body, headers)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_user(send, method, user_id, b"", headers)


async def _handle_admin_identity_sessions(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_sessions(send, method, b"", headers)


async def _handle_admin_identity_session(
    send,
    method: str,
    session_id: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_identity_session(send, method, session_id, headers)


async def _handle_daemon_status(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Daemon status requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        payload = object_daemon_status.daemon_status(
            base_dir=_data_dir(),
            rate_limit_dir=_rate_limit_dir(),
            event_keep_count=_event_keep_count(),
            event_keep_seconds=_event_keep_seconds(),
        )
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not build daemon status: {exc}"},
            status=500,
        )
        return

    await _send_json(send, payload)


async def _handle_daemon_scheduler_tasks(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Daemon scheduler controls require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            tasks = object_daemon_control.list_scheduler_tasks(
                base_dir=_data_dir(),
                status=_optional_query_text(query, "status"),
                limit=_query_int(query, "limit", default=100, minimum=1, maximum=1000),
                offset=_query_int(query, "offset", default=0, minimum=0),
                include_payload=_optional_query_bool(query, "include_payload") is True,
            )
        except (object_daemon_control.DaemonControlError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not read scheduler tasks: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", **tasks})
        return

    if method == "POST":
        try:
            task = object_daemon_control.create_scheduler_task(
                _parse_json_body(body),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
                include_payload=_optional_query_bool(query, "include_payload") is True,
            )
        except (
            object_daemon_control.DaemonControlError,
            InvalidObjectIdError,
            ValueError,
        ) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not create scheduler task: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "task": task}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_daemon_scheduler_task(
    send,
    method: str,
    task_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Daemon scheduler controls require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    clean_task_id = urllib.parse.unquote(task_id)
    if method in {"PATCH", "PUT"}:
        try:
            task = object_daemon_control.update_scheduler_task(
                clean_task_id,
                _parse_json_body(body),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
            )
        except (
            object_daemon_control.DaemonControlError,
            InvalidObjectIdError,
            ValueError,
        ) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_daemon_control.DaemonItemNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not update scheduler task: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "task": task})
        return

    if method == "DELETE":
        try:
            task = object_daemon_control.delete_scheduler_task(clean_task_id, base_dir=_data_dir())
        except object_daemon_control.DaemonControlError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_daemon_control.DaemonItemNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not delete scheduler task: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "deleted": True, "task": task})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_daemon_queue_messages(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Daemon queue controls require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            messages = object_daemon_control.list_queue_messages(
                base_dir=_data_dir(),
                status=_optional_query_text(query, "status"),
                queue_name=_optional_query_text(query, "queue_name"),
                limit=_query_int(query, "limit", default=100, minimum=1, maximum=1000),
                offset=_query_int(query, "offset", default=0, minimum=0),
                include_payload=_optional_query_bool(query, "include_payload") is True,
            )
        except (object_daemon_control.DaemonControlError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not read queue messages: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", **messages})
        return

    if method == "POST":
        try:
            message = object_daemon_control.enqueue_message(
                _parse_json_body(body),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
                include_payload=_optional_query_bool(query, "include_payload") is True,
            )
        except (
            object_daemon_control.DaemonControlError,
            InvalidObjectIdError,
            ValueError,
        ) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not enqueue message: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "message": message}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_daemon_queue_message(
    send,
    method: str,
    message_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Daemon queue controls require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    clean_message_id = urllib.parse.unquote(message_id)
    if method in {"PATCH", "PUT"}:
        try:
            message = object_daemon_control.update_queue_message(
                clean_message_id,
                _parse_json_body(body),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
            )
        except (object_daemon_control.DaemonControlError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_daemon_control.DaemonItemNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not update queue message: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "message": message})
        return

    if method == "DELETE":
        try:
            message = object_daemon_control.delete_queue_message(clean_message_id, base_dir=_data_dir())
        except object_daemon_control.DaemonControlError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_daemon_control.DaemonItemNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not delete queue message: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "deleted": True, "message": message})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


def _admin_status_payload() -> dict[str, Any]:
    health = _health_payload(include_metrics=True)
    collections = object_collections.list_collections(base_dir=_data_dir())
    schemas = object_schemas.list_schemas(base_dir=_data_dir())
    packages = _admin_package_summaries()
    permissions = _permissions_status_payload()

    return {
        "status": health["status"],
        "timestamp": health["timestamp"],
        "version": health["version"],
        "station_id": health["station_id"],
        "health": health,
        "inventory": {
            "objects": health["objects"]["count"],
            "collections": len(collections),
            "schemas": len(schemas),
            "packages": len(packages),
        },
        "capabilities": _admin_capabilities_payload(),
        "packages": packages,
        "permissions": {
            "enforcement_enabled": permissions["permissions"]["enforcement_enabled"],
            "audit_enabled": permissions["permissions"]["audit_enabled"],
            "readiness": permissions["readiness"],
            "warnings": permissions["warnings"],
        },
    }


def _admin_capabilities_payload() -> dict[str, Any]:
    return {
        "source_writes": {
            "enabled": _env_enabled(SOURCE_WRITES_ENV),
            "env": SOURCE_WRITES_ENV,
        },
        "file_writes": {
            "enabled": _env_enabled(FILE_WRITES_ENV),
            "env": FILE_WRITES_ENV,
            "max_bytes": _max_object_file_bytes(),
            "max_bytes_env": MAX_OBJECT_FILE_BYTES_ENV,
        },
        "package_installs": {
            "enabled": _env_enabled(PACKAGE_INSTALLS_ENABLED_ENV),
            "env": PACKAGE_INSTALLS_ENABLED_ENV,
        },
        "package_restore": {
            "enabled": _env_enabled(PACKAGE_RESTORE_ENABLED_ENV),
            "env": PACKAGE_RESTORE_ENABLED_ENV,
        },
        "permission_enforcement": {
            "enabled": _permission_enforcement_enabled(),
            "requested": _permission_enforcement_requested(),
            "blocked": _permission_enforcement_blocked(),
            "env": PERMISSION_ENFORCEMENT_ENV,
        },
        "permission_audit": {
            "enabled": _permission_audit_enabled(),
            "env": PERMISSION_AUDIT_ENV,
        },
        "record_events": {
            "enabled": _record_events_enabled(),
            "env": RECORD_EVENTS_ENV,
            "keep_count": _event_keep_count(),
            "keep_seconds": _event_keep_seconds(),
        },
        "identity": {
            "trusted_headers_enabled": _env_enabled(PERMISSION_TRUST_HEADERS_ENV),
            "require_known_identity_users": _env_enabled(REQUIRE_KNOWN_IDENTITY_USERS_ENV),
            "session_login_enabled": _env_enabled(SESSION_LOGIN_ENV),
            "session_login_token_configured": bool(os.environ.get(SESSION_LOGIN_TOKEN_ENV, "")),
            "session_admin_gates_enabled": _env_enabled(SESSION_ADMIN_GATES_ENV),
            "session_admin_gates_env": SESSION_ADMIN_GATES_ENV,
            "password_login_enabled": _env_enabled(PASSWORD_LOGIN_ENV),
            "password_login_env": PASSWORD_LOGIN_ENV,
        },
        "limits": {
            "max_request_bytes": _max_request_bytes(),
            "max_object_file_bytes": _max_object_file_bytes(),
            "max_concurrent_requests": _max_concurrent_requests(),
            "max_concurrent_executions": _max_concurrent_executions(),
            "rate_limit_requests": _rate_limit_requests(),
            "rate_limit_window_seconds": _rate_limit_window_seconds(),
            "object_timeout_seconds": _object_timeout_seconds(),
        },
    }


def _admin_package_summaries() -> list[dict[str, Any]]:
    packages = object_packages.list_packages(root=_packages_dir())
    return [_admin_package_summary(package) for package in packages]


def _admin_package_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    package_id = str(package["id"])
    plan = object_packages.dry_run_package(
        package_id,
        root=_packages_dir(),
        base_dir=_data_dir(),
    )
    changes = object_package_changes.list_package_changes(
        package_id,
        base_dir=_data_dir(),
        limit=1,
    )
    installed = _package_plan_installed_count(plan)
    total = _package_plan_installable_count(plan)
    summary = dict(package)
    summary["status"] = _package_install_status(installed=installed, total=total)
    summary["install"] = {
        "installed_count": installed,
        "installable_count": total,
        "safe_to_install": bool(plan.get("safe_to_install")),
        "install_enabled": _env_enabled(PACKAGE_INSTALLS_ENABLED_ENV),
        "warnings": list(plan.get("warnings", [])),
    }
    summary["changes"] = {
        "total": changes["total"],
        "latest": changes["changes"][0] if changes["changes"] else None,
    }
    return summary


def _package_plan_installed_count(plan: Mapping[str, Any]) -> int:
    count = 0
    for section in ("objects", "schemas", "seed"):
        count += sum(1 for entry in plan.get(section, []) if entry.get("installed"))
    count += sum(1 for entry in plan.get("migrations", []) if entry.get("applied"))
    return count


def _package_plan_installable_count(plan: Mapping[str, Any]) -> int:
    return sum(
        len(plan.get(section, []))
        for section in ("objects", "schemas", "seed", "migrations")
    )


def _package_install_status(*, installed: int, total: int) -> str:
    if total == 0:
        return "available"
    if installed == total:
        return "installed"
    if installed > 0:
        return "partial"
    return "available"


async def _handle_permissions_status(
    send,
    method: str,
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

    await _send_json(send, _permissions_status_payload())


def _permissions_status_payload() -> dict[str, Any]:
    permissions = {
        **_permission_readiness_inputs(),
        "enforcement_enabled": _permission_enforcement_enabled(),
        "enforcement_requested": _permission_enforcement_requested(),
        "enforcement_blocked": _permission_enforcement_blocked(),
        "allow_unready_enforcement": _permission_unready_enforcement_allowed(),
        "audit_enabled": _permission_audit_enabled(),
    }
    return object_permission_status.build_permissions_status(
        base_dir=_data_dir(),
        permissions=permissions,
        require_known_identity_users_env=REQUIRE_KNOWN_IDENTITY_USERS_ENV,
    )


def _permission_readiness_inputs() -> dict[str, Any]:
    return {
        "admin_token_configured": bool(os.environ.get(ADMIN_TOKEN_ENV, "")),
        "trusted_headers_enabled": _env_enabled(PERMISSION_TRUST_HEADERS_ENV),
        "require_known_identity_users": _env_enabled(REQUIRE_KNOWN_IDENTITY_USERS_ENV),
        "session_login_enabled": _env_enabled(SESSION_LOGIN_ENV),
        "session_login_token_configured": bool(os.environ.get(SESSION_LOGIN_TOKEN_ENV, "")),
        "password_login_enabled": _env_enabled(PASSWORD_LOGIN_ENV),
    }


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


async def _handle_events(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Events API requires {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            since = None
            if "since" in query:
                since = _query_int(query, "since", minimum=0)
            limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
            offset = _query_int(query, "offset", default=0, minimum=0)
            events = object_events.list_events(
                event_type=_optional_query_text(query, "event_type"),
                since=since,
                base_dir=_data_dir(),
                limit=limit,
                offset=offset,
            )
        except (object_events.InvalidEventTypeError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not read events: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", **events})
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            event = object_events.publish_event(
                _required_payload_text(payload, "event_type"),
                payload=payload.get("payload", {}),
                source=_payload_text(payload, "source", "api"),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
                keep_count=_event_keep_count(),
                keep_seconds=_event_keep_seconds(),
            )
        except (object_events.InvalidEventTypeError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not publish event: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "event": event}, status=201)
        return

    if method == "DELETE":
        try:
            keep_count = _event_retention_query_int(
                query,
                "keep_count",
                default=_event_keep_count(),
                maximum=object_events.MAX_EVENT_KEEP_COUNT,
            )
            keep_seconds = _event_retention_query_int(
                query,
                "keep_seconds",
                default=_event_keep_seconds(),
            )
            result = object_events.prune_events(
                base_dir=_data_dir(),
                keep_count=keep_count,
                keep_seconds=keep_seconds,
            )
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not prune events: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "retention": result})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_event_subscriptions(
    send,
    method: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Events API requires {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
            offset = _query_int(query, "offset", default=0, minimum=0)
            subscriptions = object_events.list_subscriptions(
                event_type=_optional_query_text(query, "event_type"),
                base_dir=_data_dir(),
                limit=limit,
                offset=offset,
            )
        except (object_events.InvalidEventTypeError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not read event subscriptions: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", **subscriptions})
        return

    if method == "POST":
        try:
            payload = _parse_json_body(body)
            subscription = object_events.subscribe_event(
                _required_payload_text(payload, "event_type"),
                subscriber_id=_optional_payload_text(payload, "subscriber_id"),
                callback_url=_payload_text(payload, "callback_url", ""),
                actor=_record_change_actor(headers),
                base_dir=_data_dir(),
            )
        except (
            object_events.InvalidEventTypeError,
            object_events.InvalidSubscriberIdError,
            ValueError,
        ) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not save event subscription: {exc}"},
                status=500,
            )
            return

        await _send_json(send, {"status": "ok", "subscription": subscription}, status=201)
        return

    if method == "DELETE":
        try:
            subscription = object_events.delete_subscription(
                _required_query_text(query, "event_type"),
                _required_query_text(query, "subscriber_id"),
                base_dir=_data_dir(),
            )
        except (
            object_events.InvalidEventTypeError,
            object_events.InvalidSubscriberIdError,
            ValueError,
        ) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_events.SubscriptionNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        except OSError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Could not delete event subscription: {exc}"},
                status=500,
            )
            return

        await _send_json(
            send,
            {
                "status": "ok",
                "deleted": True,
                "subscription": subscription,
            },
        )
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_event_deliveries(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(headers, f"Events API requires {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    try:
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        event_limit = _query_int(query, "event_limit", default=10, minimum=0, maximum=1000)
        deliveries = object_events.list_event_deliveries(
            event_type=_optional_query_text(query, "event_type"),
            delivery_status=_optional_query_text(query, "delivery_status"),
            pending=_optional_query_bool(query, "pending"),
            include_callback_url=_optional_query_bool(query, "include_callback_url") is True,
            include_events=_optional_query_bool(query, "include_events") is True,
            event_limit=event_limit,
            base_dir=_data_dir(),
            limit=limit,
            offset=offset,
        )
    except (object_events.InvalidEventTypeError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not read event deliveries: {exc}"},
            status=500,
        )
        return

    await _send_json(send, {"status": "ok", **deliveries})


async def _handle_packages(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Package listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        packages = object_packages.list_packages(root=_packages_dir())
    except object_packages.InvalidPackageManifestError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "packages": packages,
            "count": len(packages),
        },
    )


async def _handle_package(
    send,
    method: str,
    package_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Package detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        dry_run = _optional_query_bool(query, "dry_run") is True
        if dry_run:
            plan = object_packages.dry_run_package(
                package_id,
                root=_packages_dir(),
                base_dir=_data_dir(),
            )
            change = object_package_changes.append_package_change(
                package_id=package_id,
                action="dry_run",
                package_version=plan["package"].get("version"),
                actor=_record_change_actor(headers),
                details=object_package_changes.dry_run_change_details(plan),
                base_dir=_data_dir(),
            )
            payload = {
                "status": "ok",
                "dry_run": plan,
                "change": change,
            }
        else:
            payload = {
                "status": "ok",
                "package": object_packages.get_package(
                    package_id,
                    root=_packages_dir(),
                ),
            }
    except object_packages.InvalidPackageIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_packages.PackageNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_packages.InvalidPackageManifestError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    except object_package_changes.InvalidPackageChangeError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(send, {"status": "error", "error": f"Could not record package change: {exc}"}, status=500)
        return

    await _send_json(send, payload)


async def _handle_package_install(
    send,
    method: str,
    package_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Package installs require {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if not _env_enabled(PACKAGE_INSTALLS_ENABLED_ENV):
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Package installs are disabled. Set {PACKAGE_INSTALLS_ENABLED_ENV}=true.",
            },
            status=403,
        )
        return

    plan = None
    requested_change = None

    try:
        payload = _parse_json_body(body)
        allow_replace = _optional_payload_bool(payload, "allow_replace") is True
        plan = object_packages.dry_run_package(
            package_id,
            root=_packages_dir(),
            base_dir=_data_dir(),
        )
        requested_change = object_package_changes.append_package_change(
            package_id=package_id,
            action="install_requested",
            package_version=plan["package"].get("version"),
            actor=_record_change_actor(headers),
            details=object_package_changes.dry_run_change_details(plan),
            base_dir=_data_dir(),
        )

        restore_point: dict[str, Any] | None = None

        def create_restore_point(_: Mapping[str, Any]) -> dict[str, Any]:
            nonlocal restore_point
            summary = object_backup.create_runtime_restore_point(
                f"package-{package_id}",
                data_dir=_data_dir(),
            )
            restore_point = _backup_summary_payload(summary)
            return restore_point

        install_result = object_packages.install_package(
            package_id,
            root=_packages_dir(),
            base_dir=_data_dir(),
            allow_replace=allow_replace,
            before_write=create_restore_point,
        )
        restore_point = install_result.get("restore_point", restore_point)
        installed_change = object_package_changes.append_package_change(
            package_id=package_id,
            action="installed",
            package_version=install_result["package"].get("version"),
            actor=_record_change_actor(headers),
            details=_package_change_details(install_result, restore_point=restore_point),
            base_dir=_data_dir(),
        )
    except object_packages.InvalidPackageIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_packages.PackageNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_packages.PackageInstallError as exc:
        failed_change = _try_append_package_install_failure(
            package_id=package_id,
            plan=plan,
            headers=headers,
            error=str(exc),
        )
        changes = _package_install_changes(requested_change, failed=failed_change)
        await _send_json(
            send,
            {
                "status": "error",
                "error": str(exc),
                "changes": changes,
            },
            status=409,
        )
        return
    except object_packages.InvalidPackageManifestError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    except object_package_changes.InvalidPackageChangeError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        failed_change = _try_append_package_install_failure(
            package_id=package_id,
            plan=plan,
            headers=headers,
            error=str(exc),
        )
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Package install failed: {exc}",
                "changes": _package_install_changes(requested_change, failed=failed_change),
            },
            status=500,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "install": install_result,
            "changes": {
                "requested": requested_change,
                "installed": installed_change,
            },
            "restore_point": restore_point,
        },
        status=201,
    )


async def _handle_package_restore(
    send,
    method: str,
    package_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Package restore requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if not _env_enabled(PACKAGE_RESTORE_ENABLED_ENV):
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Package restore is disabled. Set {PACKAGE_RESTORE_ENABLED_ENV}=true.",
            },
            status=403,
        )
        return

    from_change: dict[str, Any] | None = None
    restore_point: dict[str, Any] | None = None

    try:
        payload = _parse_json_body(body)
        if _required_payload_text(payload, "confirm") != "restore-runtime":
            raise ValueError("Request JSON field 'confirm' must be 'restore-runtime'")
        change_id = _required_payload_text(payload, "change_id")
        from_change = object_package_changes.get_package_change(
            package_id,
            change_id,
            base_dir=_data_dir(),
        )
        if from_change is None:
            await _send_json(
                send,
                {"status": "error", "error": f"Package change not found: {change_id}"},
                status=404,
            )
            return

        restore_point = _restore_point_from_package_change(from_change)
        restore_summary = object_backup.restore_runtime_backup(
            restore_point["path"],
            objects_dir=_primary_objects_dir(),
            data_dir=_data_dir(),
            overwrite=True,
            prune_extra=True,
        )
        requested_change = object_package_changes.append_package_change(
            package_id=package_id,
            action="restore_requested",
            package_version=from_change.get("package_version"),
            actor=_record_change_actor(headers),
            details=_package_restore_change_details(
                from_change,
                restore_point,
                restore=restore_summary.to_dict(),
            ),
            base_dir=_data_dir(),
        )
        rolled_back_change = object_package_changes.append_package_change(
            package_id=package_id,
            action="rolled_back",
            package_version=from_change.get("package_version"),
            actor=_record_change_actor(headers),
            details=_package_restore_change_details(
                from_change,
                restore_point,
                restore=restore_summary.to_dict(),
            ),
            base_dir=_data_dir(),
        )
    except (object_packages.InvalidPackageIdError, object_package_changes.InvalidPackageChangeError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (ValueError, object_backup.BackupRestoreError) as exc:
        failed_change = _try_append_package_restore_failure(
            package_id=package_id,
            from_change=from_change,
            restore_point=restore_point,
            headers=headers,
            error=str(exc),
        )
        await _send_json(
            send,
            {
                "status": "error",
                "error": str(exc),
                "changes": _package_restore_changes(failed=failed_change),
            },
            status=400,
        )
        return
    except OSError as exc:
        failed_change = _try_append_package_restore_failure(
            package_id=package_id,
            from_change=from_change,
            restore_point=restore_point,
            headers=headers,
            error=str(exc),
        )
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Package restore failed: {exc}",
                "changes": _package_restore_changes(failed=failed_change),
            },
            status=500,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "restore": restore_summary.to_dict(),
            "restore_point": restore_point,
            "from_change": from_change,
            "changes": {
                "requested": requested_change,
                "rolled_back": rolled_back_change,
            },
        },
    )


async def _handle_package_changes(
    send,
    method: str,
    package_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Package change history requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        changes = object_package_changes.list_package_changes(
            package_id,
            base_dir=_data_dir(),
            limit=limit,
            offset=offset,
        )
    except object_packages.InvalidPackageIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", **changes})


def _backup_summary_payload(summary: object_backup.BackupSummary) -> dict[str, Any]:
    return {
        "path": summary.path,
        "format_version": summary.format_version,
        "created_at": summary.created_at,
        "files": summary.files,
        "bytes": summary.bytes,
        "warnings": summary.warnings,
    }


def _package_change_details(
    plan: Mapping[str, Any],
    *,
    restore_point: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    details = object_package_changes.dry_run_change_details(plan)
    if restore_point is not None:
        details["restore_point"] = dict(restore_point)
    if error:
        details["error"] = error
    return details


def _try_append_package_install_failure(
    *,
    package_id: str,
    plan: Mapping[str, Any] | None,
    headers: dict[str, str],
    error: str,
) -> dict[str, Any] | None:
    failed_details: dict[str, Any] = {"error": error}
    failed_version = None
    if plan is not None:
        failed_details = _package_change_details(plan, error=error)
        failed_version = plan["package"].get("version")

    try:
        return object_package_changes.append_package_change(
            package_id=package_id,
            action="failed",
            package_version=failed_version,
            actor=_record_change_actor(headers),
            message=error,
            details=failed_details,
            base_dir=_data_dir(),
        )
    except Exception:
        return None


def _package_install_changes(
    requested_change: Mapping[str, Any] | None,
    *,
    failed: Mapping[str, Any] | None = None,
) -> dict[str, Any | None]:
    changes: dict[str, Any | None] = {}
    if requested_change is not None:
        changes["requested"] = dict(requested_change)
    changes["failed"] = dict(failed) if failed is not None else None
    return changes


def _restore_point_from_package_change(change: Mapping[str, Any]) -> dict[str, Any]:
    details = change.get("details")
    if not isinstance(details, Mapping):
        raise object_backup.BackupRestoreError("Package change has no restore point details")

    restore_point = details.get("restore_point")
    if not isinstance(restore_point, Mapping):
        raise object_backup.BackupRestoreError("Package change has no restore point")

    restore_path = restore_point.get("path")
    if not isinstance(restore_path, str) or not restore_path.strip() or "\x00" in restore_path:
        raise object_backup.BackupRestoreError("Package restore point path is not safe")

    candidate = Path(restore_path)
    allowed_root = Path(
        os.environ.get(object_backup.BACKUPS_DIR_ENV)
        or (Path(_data_dir()) / object_backup.BACKUPS_DIR)
    )
    resolved_candidate = candidate.resolve(strict=False)
    resolved_allowed = allowed_root.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_allowed)
    except ValueError as exc:
        raise object_backup.BackupRestoreError(
            "Package restore point is outside the configured backup directory"
        ) from exc

    payload = dict(restore_point)
    payload["path"] = str(candidate)
    return payload


def _package_restore_change_details(
    from_change: Mapping[str, Any],
    restore_point: Mapping[str, Any],
    *,
    restore: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "from_change": {
            "change_id": from_change.get("change_id"),
            "timestamp": from_change.get("timestamp"),
            "action": from_change.get("action"),
            "package_version": from_change.get("package_version"),
        },
        "restore_mode": "runtime_snapshot",
        "restore_point": dict(restore_point),
    }
    if restore is not None:
        details["restore"] = dict(restore)
    if error:
        details["error"] = error
    return details


def _try_append_package_restore_failure(
    *,
    package_id: str,
    from_change: Mapping[str, Any] | None,
    restore_point: Mapping[str, Any] | None,
    headers: dict[str, str],
    error: str,
) -> dict[str, Any] | None:
    details: dict[str, Any] = {"error": error}
    package_version = None
    if from_change is not None:
        package_version = from_change.get("package_version")
        details = _package_restore_change_details(
            from_change,
            restore_point or {},
            error=error,
        )

    try:
        return object_package_changes.append_package_change(
            package_id=package_id,
            action="failed",
            package_version=package_version,
            actor=_record_change_actor(headers),
            message=error,
            details=details,
            base_dir=_data_dir(),
        )
    except Exception:
        return None


def _package_restore_changes(
    *,
    failed: Mapping[str, Any] | None = None,
) -> dict[str, Any | None]:
    return {"failed": dict(failed) if failed is not None else None}


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

    if query.get("source_changes") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.SOURCE,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_source_changes_get(send, object_id, query)
        return

    if query.get("changes") == "true":
        if await _send_permission_denied_if_needed(
            send,
            headers,
            object_permissions.SOURCE,
            object_id=object_id,
            method="GET",
        ):
            return
        await _handle_object_changes_get(send, object_id, query)
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
        await _handle_object_source_get(send, object_id)
        return

    await _execute_object_method(
        send,
        object_id,
        "GET",
        query,
        headers,
        permission_action=object_permissions.EXECUTE,
    )


async def _handle_object_source_get(send, object_id: str) -> None:
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


async def _handle_object_source_changes_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    try:
        _ensure_object_source_exists(object_id)
        limit = _query_int(
            query,
            "limit",
            default=100,
            minimum=1,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        offset = _query_int(
            query,
            "offset",
            default=0,
            minimum=0,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        payload = object_source_changes.list_source_changes(
            object_id,
            base_dir=_data_dir(),
            limit=limit,
            offset=offset,
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

    await _send_json(send, {"status": "ok", **payload})


async def _handle_object_changes_get(
    send,
    object_id: str,
    query: dict[str, str],
) -> None:
    try:
        _ensure_object_source_exists(object_id)
        limit = _query_int(
            query,
            "limit",
            default=100,
            minimum=1,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        offset = _query_int(
            query,
            "offset",
            default=0,
            minimum=0,
            maximum=object_source_changes.MAX_CHANGE_LIMIT,
        )
        payload = _object_changes_payload(object_id, query, limit=limit, offset=offset)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_file_changes.InvalidFileChangeError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not list object changes: {exc}"},
            status=500,
        )
        return

    await _send_json(send, {"status": "ok", **payload})


def _object_changes_payload(
    object_id: str,
    query: dict[str, str],
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    kind = _admin_change_kind_filter(query)
    if kind not in {None, "source", "file"}:
        raise ValueError("Object changes only support kind=source or kind=file")

    entries: list[dict[str, Any]] = []
    if kind in {None, "source"}:
        entries.extend(_source_admin_changes(object_id=object_id))
    if kind in {None, "file"}:
        entries.extend(
            _file_admin_changes(
                object_id=object_id,
                file_name=_admin_change_file_filter(query),
            )
        )

    entries = _sort_admin_changes(entries)
    total = len(entries)
    window = entries[offset:offset + limit]
    return {
        "object_id": object_id,
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }


def _admin_changes_payload(
    query: dict[str, str],
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    kind = _admin_change_kind_filter(query)
    object_id = _admin_change_object_filter(query)
    collection = _admin_change_collection_filter(query)
    record_id = _admin_change_record_filter(query)
    package_id = _admin_change_package_filter(query)
    file_name = _admin_change_file_filter(query)

    entries: list[dict[str, Any]] = []
    if kind in {None, "source"}:
        entries.extend(_source_admin_changes(object_id=object_id))
    if kind in {None, "file"}:
        entries.extend(_file_admin_changes(object_id=object_id, file_name=file_name))
    if kind in {None, "record"}:
        entries.extend(_record_admin_changes(collection=collection, record_id=record_id))
    if kind in {None, "package"}:
        entries.extend(_package_admin_changes(package_id=package_id))

    entries = [
        entry
        for entry in entries
        if _admin_change_matches(
            entry,
            object_id=object_id,
            collection=collection,
            record_id=record_id,
            package_id=package_id,
            file_name=file_name,
        )
    ]
    entries = _sort_admin_changes(entries)
    total = len(entries)
    window = entries[offset:offset + limit]
    return {
        "changes": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
        "filters": {
            "kind": kind,
            "object_id": object_id,
            "collection": collection,
            "record_id": record_id,
            "package_id": package_id,
            "file": file_name,
        },
    }


def _source_admin_changes(object_id: str | None = None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for candidate in _admin_change_object_ids(
        object_source_changes.SOURCE_CHANGES_DIR,
        object_id=object_id,
    ):
        payload = object_source_changes.list_source_changes(
            candidate,
            base_dir=_data_dir(),
            limit=object_source_changes.MAX_CHANGE_LIMIT,
        )
        changes.extend(_normalize_source_admin_change(change) for change in payload["changes"])
    return changes


def _file_admin_changes(
    object_id: str | None = None,
    file_name: str | None = None,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for candidate in _admin_change_object_ids(
        object_file_changes.FILE_CHANGES_DIR,
        object_id=object_id,
    ):
        payload = object_file_changes.list_file_changes(
            candidate,
            file_name=file_name,
            base_dir=_data_dir(),
            limit=object_file_changes.MAX_CHANGE_LIMIT,
        )
        changes.extend(_normalize_file_admin_change(change) for change in payload["changes"])
    return changes


def _record_admin_changes(
    collection: str | None = None,
    record_id: str | None = None,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for candidate in _admin_change_collections(collection=collection):
        payload = object_record_changes.list_record_changes(
            candidate,
            record_id=record_id,
            base_dir=_data_dir(),
            limit=object_record_changes.MAX_CHANGE_LIMIT,
        )
        changes.extend(_normalize_record_admin_change(change) for change in payload["changes"])
    return changes


def _package_admin_changes(package_id: str | None = None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for candidate in _admin_change_package_ids(package_id=package_id):
        payload = object_package_changes.list_package_changes(
            candidate,
            base_dir=_data_dir(),
            limit=object_package_changes.MAX_CHANGE_LIMIT,
        )
        changes.extend(_normalize_package_admin_change(change) for change in payload["changes"])
    return changes


def _admin_change_object_ids(
    change_dir: str,
    *,
    object_id: str | None,
) -> list[str]:
    if object_id is not None:
        if not validate_object_id(object_id):
            raise InvalidObjectIdError(f"Invalid object ID: {object_id}")
        return [object_id]

    root = Path(_data_dir()) / change_dir
    if not root.exists():
        return []
    if not root.is_dir():
        raise OSError(f"Change root is not a directory: {root}")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and validate_object_id(path.name)
    )


def _admin_change_collections(*, collection: str | None) -> list[str]:
    if collection is not None:
        if not object_collections.validate_collection_name(collection):
            raise object_collections.InvalidCollectionNameError(
                f"Invalid collection name: {collection}"
            )
        return [collection]

    root = Path(_data_dir()) / object_record_changes.RECORD_CHANGES_DIR
    if not root.exists():
        return []
    if not root.is_dir():
        raise OSError(f"Change root is not a directory: {root}")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and object_collections.validate_collection_name(path.name)
    )


def _admin_change_package_ids(*, package_id: str | None) -> list[str]:
    if package_id is not None:
        if not object_packages.validate_package_id(package_id):
            raise object_packages.InvalidPackageIdError(f"Invalid package id: {package_id}")
        return [package_id]

    root = Path(_data_dir()) / object_package_changes.PACKAGE_CHANGES_DIR
    if not root.exists():
        return []
    if not root.is_dir():
        raise OSError(f"Change root is not a directory: {root}")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and object_packages.validate_package_id(path.name)
    )


def _admin_change_kind_filter(query: dict[str, str]) -> str | None:
    kind = _optional_query_text(query, "kind")
    if kind is None:
        return None
    if kind not in ADMIN_CHANGE_KINDS:
        allowed = ", ".join(sorted(ADMIN_CHANGE_KINDS))
        raise ValueError(f"Query parameter 'kind' must be one of: {allowed}")
    return kind


def _admin_change_object_filter(query: dict[str, str]) -> str | None:
    object_id = _optional_query_text(query, "object_id")
    if object_id is not None and not validate_object_id(object_id):
        raise InvalidObjectIdError(f"Invalid object ID: {object_id}")
    return object_id


def _admin_change_collection_filter(query: dict[str, str]) -> str | None:
    collection = _optional_query_text(query, "collection")
    if collection is not None and not object_collections.validate_collection_name(collection):
        raise object_collections.InvalidCollectionNameError(
            f"Invalid collection name: {collection}"
        )
    return collection


def _admin_change_record_filter(query: dict[str, str]) -> str | None:
    record_id = _optional_query_text(query, "record_id")
    if record_id is not None and not object_records.validate_record_id(record_id):
        raise object_records.InvalidRecordIdError(f"Invalid record id: {record_id}")
    return record_id


def _admin_change_package_filter(query: dict[str, str]) -> str | None:
    package_id = _optional_query_text(query, "package_id")
    if package_id is not None and not object_packages.validate_package_id(package_id):
        raise object_packages.InvalidPackageIdError(f"Invalid package id: {package_id}")
    return package_id


def _admin_change_file_filter(query: dict[str, str]) -> str | None:
    file_name = _optional_query_text(query, "file")
    if file_name is None:
        return None
    if "\x00" in file_name or file_name.startswith("/") or ".." in file_name:
        raise object_file_changes.InvalidFileChangeError(f"Invalid file name: {file_name!r}")
    path = Path(file_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise object_file_changes.InvalidFileChangeError(f"Invalid file name: {file_name!r}")
    return path.as_posix()


def _admin_change_matches(
    entry: dict[str, Any],
    *,
    object_id: str | None,
    collection: str | None,
    record_id: str | None,
    package_id: str | None,
    file_name: str | None,
) -> bool:
    target = entry.get("target", {})
    if not isinstance(target, dict):
        return False
    if object_id is not None and target.get("object_id") != object_id:
        return False
    if collection is not None and target.get("collection") != collection:
        return False
    if record_id is not None and target.get("record_id") != record_id:
        return False
    if package_id is not None and target.get("package_id") != package_id:
        return False
    if file_name is not None and target.get("file_name") != file_name:
        return False
    return True


def _sort_admin_changes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            str(entry.get("timestamp") or ""),
            str(entry.get("change_id") or ""),
        ),
        reverse=True,
    )


def _normalize_source_admin_change(change: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "source",
        "change_id": change.get("change_id"),
        "timestamp": change.get("timestamp"),
        "action": change.get("action"),
        "actor": change.get("actor"),
        "summary": change.get("message") or _source_change_summary(change),
        "correlation_id": change.get("correlation_id"),
        "target": {
            "object_id": change.get("object_id"),
            "version_id": change.get("version_id"),
            "from_version_id": change.get("from_version_id"),
        },
        "change": dict(change),
    }


def _normalize_file_admin_change(change: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "file",
        "change_id": change.get("change_id"),
        "timestamp": change.get("timestamp"),
        "action": change.get("action"),
        "actor": change.get("actor"),
        "summary": change.get("message") or _file_change_summary(change),
        "correlation_id": change.get("correlation_id"),
        "target": {
            "object_id": change.get("object_id"),
            "file_name": change.get("file_name"),
            "file_size": change.get("file_size"),
        },
        "change": dict(change),
    }


def _normalize_record_admin_change(change: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "record",
        "change_id": change.get("change_id"),
        "timestamp": change.get("timestamp"),
        "action": change.get("action"),
        "actor": change.get("actor"),
        "summary": change.get("message") or _record_change_summary(change),
        "correlation_id": change.get("correlation_id"),
        "target": {
            "collection": change.get("collection"),
            "record_id": change.get("record_id"),
            "changed_fields": change.get("changed_fields", []),
        },
        "change": dict(change),
    }


def _normalize_package_admin_change(change: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "package",
        "change_id": change.get("change_id"),
        "timestamp": change.get("timestamp"),
        "action": change.get("action"),
        "actor": change.get("actor"),
        "summary": change.get("message") or _package_change_summary(change),
        "correlation_id": change.get("correlation_id"),
        "target": {
            "package_id": change.get("package_id"),
            "package_version": change.get("package_version"),
        },
        "change": dict(change),
    }


def _source_change_summary(change: Mapping[str, Any]) -> str:
    return f"{change.get('action', 'source_change')} {change.get('object_id', '')}".strip()


def _file_change_summary(change: Mapping[str, Any]) -> str:
    return f"{change.get('action', 'file_change')} {change.get('file_name', '')}".strip()


def _record_change_summary(change: Mapping[str, Any]) -> str:
    return (
        f"{change.get('action', 'record_change')} "
        f"{change.get('collection', '')}/{change.get('record_id', '')}"
    ).strip()


def _package_change_summary(change: Mapping[str, Any]) -> str:
    return f"{change.get('action', 'package_change')} {change.get('package_id', '')}".strip()


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
        author = _payload_text(payload, "author", _source_actor_from_headers(headers))
        message = _payload_text(payload, "message", f"Rollback to version {version_id}")
        correlation_id = object_correlation.current_correlation_id()
        new_version_id = object_source.rollback_object_source(
            object_id=object_id,
            to_version=version_id,
            author=author,
            message=message,
            version_manager=_version_manager(),
            correlation_id=correlation_id,
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
            "correlation_id": object_correlation.current_correlation_id(),
        },
    )
    _append_source_change_log(
        object_id,
        action="source_rollback",
        version_id=new_version_id,
        from_version_id=version_id,
        actor=author,
        message=message,
        correlation_id=correlation_id,
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

    author = _payload_text(payload, "author", _source_actor_from_headers(headers))
    message = _payload_text(payload, "message", "Updated via API")
    correlation_id = object_correlation.current_correlation_id()

    try:
        version_id = object_source.update_object_source(
            object_id=object_id,
            new_code=code,
            author=author,
            message=message,
            version_manager=_version_manager(),
            correlation_id=correlation_id,
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
            "correlation_id": object_correlation.current_correlation_id(),
        },
    )
    _append_source_change_log(
        object_id,
        action="source_update",
        version_id=version_id,
        actor=author,
        message=message,
        correlation_id=correlation_id,
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
        payload=_payload_with_identity(payload, headers),
        correlation_id=object_correlation.current_correlation_id(),
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


def _payload_with_identity(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    subject, auth_method = _permission_identity(headers)
    request_payload = dict(payload)
    request_payload["_identity"] = {
        "user_id": subject.user_id,
        "account_id": subject.account_id,
        "roles": list(subject.roles),
        "subscriptions": list(subject.subscriptions),
        "auth_method": auth_method,
    }
    return request_payload


def _list_objects_payload() -> dict[str, Any]:
    objects = [_object_source_payload(source) for source in iter_object_sources()]
    return {
        "status": "ok",
        "objects": objects,
        "count": len(objects),
    }


async def _handle_objects_post(send, body: bytes, headers: dict[str, str]) -> None:
    gate_error = _source_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        payload = _parse_json_body(body)
        object_id = _created_object_id(payload, headers)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    if await _send_permission_denied_if_needed(
        send,
        headers,
        object_permissions.SOURCE,
        object_id=object_id,
        method="POST",
    ):
        return

    code = payload.get("code")
    if not isinstance(code, str):
        await _send_json(
            send,
            {"status": "error", "error": "Request JSON field 'code' must be a string"},
            status=400,
        )
        return

    author = _payload_text(payload, "author", _source_actor_from_headers(headers))
    message = _payload_text(payload, "message", "Created via API")
    correlation_id = object_correlation.current_correlation_id()

    try:
        version_id = object_source.create_object_source(
            object_id=object_id,
            code=code,
            author=author,
            message=message,
            version_manager=_version_manager(),
            correlation_id=correlation_id,
        )
    except InvalidObjectIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_source.ObjectSourceExistsError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=409)
        return

    _append_source_change_log(
        object_id,
        action="source_create",
        version_id=version_id,
        actor=author,
        message=message,
        correlation_id=correlation_id,
        details={"description": _payload_text(payload, "description", "")},
    )

    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Object created: {object_id}",
            "object_id": object_id,
            "version_id": version_id,
            "correlation_id": object_correlation.current_correlation_id(),
        },
        status=201,
    )


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
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "GET":
        await _handle_collection_records_get(send, collection, query, headers)
        return

    if method == "POST":
        await _handle_collection_record_create(send, collection, body, headers)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_collection_records_get(
    send,
    collection: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    permission_check = None
    if _permission_checks_enabled():
        permission_check = await _collection_permission_check(
            send,
            headers,
            object_permissions.READ,
            collection=collection,
            method="GET",
        )
        if permission_check is None:
            return
    else:
        gate_error = _admin_token_gate_error(
            headers,
            f"Collection records require {ADMIN_TOKEN_ENV}.",
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    try:
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        records = object_records.read_collection_records(collection, base_dir=_data_dir())
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    if permission_check is not None and permission_check["enforced"]:
        try:
            records = _filter_records_for_permission(
                records,
                collection=collection,
                subject=permission_check["subject"],
                policy=permission_check["policy"],
            )
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return

    records_payload = object_records.collection_records_payload(
        collection,
        records,
        limit=limit,
        offset=offset,
    )
    await _send_json(send, {"status": "ok", **records_payload})


async def _handle_collection_changes(
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
        f"Collection change history requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        object_collections.get_collection(collection, base_dir=_data_dir())
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        changes = object_record_changes.list_record_changes(
            collection,
            base_dir=_data_dir(),
            limit=limit,
            offset=offset,
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

    await _send_json(send, {"status": "ok", **changes})


async def _handle_collection_record_changes(
    send,
    method: str,
    collection: str,
    record_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Collection record change history requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        object_collections.get_collection(collection, base_dir=_data_dir())
        limit = _query_int(query, "limit", default=100, minimum=1, maximum=1000)
        offset = _query_int(query, "offset", default=0, minimum=0)
        changes = object_record_changes.list_record_changes(
            collection,
            record_id=record_id,
            base_dir=_data_dir(),
            limit=limit,
            offset=offset,
        )
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_records.InvalidRecordIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", **changes})


async def _handle_collection_record_create(
    send,
    collection: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    try:
        record_payload = _record_payload_from_body(body, require_id=True)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    permission_check = await _authorize_collection_write(
        send,
        headers,
        object_permissions.CREATE,
        collection=collection,
        method="POST",
        record=record_payload,
        gate_message=f"Collection record writes require {ADMIN_TOKEN_ENV}.",
    )
    if permission_check is None:
        return
    if permission_check["enforced"]:
        try:
            denied_fields = _schema_denied_write_fields(
                collection,
                record_payload.keys(),
                subject=permission_check["subject"],
                policy=permission_check["policy"],
                record=record_payload,
            )
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return
        if denied_fields:
            await _send_json(
                send,
                {
                    "status": "error",
                    "error": _field_write_denied_message(denied_fields),
                    "code": "forbidden",
                    "denied_fields": denied_fields,
                },
                status=403,
            )
            return

    try:
        record = object_records.create_collection_record(
            collection,
            record_payload,
            base_dir=_data_dir(),
        )
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_records.DuplicateRecordIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=409)
        return
    except (object_records.InvalidRecordIdError, object_records.InvalidRecordPayloadError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not save collection record: {exc}"},
            status=500,
        )
        return

    try:
        change = object_record_changes.append_record_change(
            collection=collection,
            record_id=record["id"],
            action="create",
            before=None,
            after=record,
            actor=_record_change_actor(headers),
            base_dir=_data_dir(),
        )
    except (OSError, ValueError) as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not record collection change: {exc}"},
            status=500,
        )
        return

    _publish_record_change_event(change)

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
        },
        status=201,
    )


async def _handle_collection_record(
    send,
    method: str,
    collection: str,
    record_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method == "GET":
        await _handle_collection_record_get(send, collection, record_id, headers)
        return

    if method == "PUT":
        await _handle_collection_record_update(send, collection, record_id, body, headers)
        return

    if method == "DELETE":
        await _handle_collection_record_delete(send, collection, record_id, headers)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_collection_record_get(
    send,
    collection: str,
    record_id: str,
    headers: dict[str, str],
) -> None:
    if not _permission_checks_enabled():
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

    if _permission_checks_enabled():
        permission_check = await _collection_permission_check(
            send,
            headers,
            object_permissions.READ,
            collection=collection,
            method="GET",
            record=record,
        )
        if permission_check is None:
            return
        if permission_check["enforced"]:
            try:
                record = _apply_record_field_policy(
                    record,
                    permission_check["decision"],
                    collection=collection,
                    subject=permission_check["subject"],
                    policy=permission_check["policy"],
                )
            except ValueError as exc:
                await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
                return

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
        },
    )


async def _handle_collection_record_update(
    send,
    collection: str,
    record_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if not _permission_checks_enabled():
        gate_error = _admin_token_gate_error(
            headers,
            f"Collection record writes require {ADMIN_TOKEN_ENV}.",
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    try:
        changes = _record_payload_from_body(body)
        existing = object_records.get_collection_record(
            collection,
            record_id,
            base_dir=_data_dir(),
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_records.InvalidRecordIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (object_collections.CollectionNotFoundError, object_records.RecordNotFoundError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    candidate = dict(existing)
    candidate.update(changes)
    candidate["id"] = record_id

    permission_check = await _authorize_collection_write(
        send,
        headers,
        object_permissions.UPDATE,
        collection=collection,
        method="PUT",
        record=existing,
        gate_message=f"Collection record writes require {ADMIN_TOKEN_ENV}.",
    )
    if permission_check is None:
        return

    if _permission_enforcement_enabled():
        permission_check = await _authorize_collection_write(
            send,
            headers,
            object_permissions.UPDATE,
            collection=collection,
            method="PUT",
            record=candidate,
            gate_message=f"Collection record writes require {ADMIN_TOKEN_ENV}.",
        )
        if permission_check is None:
            return

    if permission_check["enforced"]:
        try:
            denied_fields = _schema_denied_write_fields(
                collection,
                changes.keys(),
                subject=permission_check["subject"],
                policy=permission_check["policy"],
                record=candidate,
            )
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return
        if denied_fields:
            await _send_json(
                send,
                {
                    "status": "error",
                    "error": _field_write_denied_message(denied_fields),
                    "code": "forbidden",
                    "denied_fields": denied_fields,
                },
                status=403,
            )
            return

    try:
        record = object_records.update_collection_record(
            collection,
            record_id,
            changes,
            base_dir=_data_dir(),
        )
    except object_records.RecordNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except (object_records.InvalidRecordIdError, object_records.InvalidRecordPayloadError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not save collection record: {exc}"},
            status=500,
        )
        return

    try:
        change = object_record_changes.append_record_change(
            collection=collection,
            record_id=record_id,
            action="update",
            before=existing,
            after=record,
            actor=_record_change_actor(headers),
            base_dir=_data_dir(),
        )
    except (OSError, ValueError) as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not record collection change: {exc}"},
            status=500,
        )
        return

    _publish_record_change_event(change)

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
        },
    )


async def _handle_collection_record_delete(
    send,
    collection: str,
    record_id: str,
    headers: dict[str, str],
) -> None:
    if not _permission_checks_enabled():
        gate_error = _admin_token_gate_error(
            headers,
            f"Collection record writes require {ADMIN_TOKEN_ENV}.",
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    try:
        existing = object_records.get_collection_record(
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

    permission_check = await _authorize_collection_write(
        send,
        headers,
        object_permissions.DELETE,
        collection=collection,
        method="DELETE",
        record=existing,
        gate_message=f"Collection record writes require {ADMIN_TOKEN_ENV}.",
    )
    if permission_check is None:
        return

    try:
        record = object_records.delete_collection_record(
            collection,
            record_id,
            base_dir=_data_dir(),
        )
    except object_records.RecordNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_records.InvalidRecordIdError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not delete collection record: {exc}"},
            status=500,
        )
        return

    try:
        change = object_record_changes.append_record_change(
            collection=collection,
            record_id=record_id,
            action="delete",
            before=record,
            after=None,
            actor=_record_change_actor(headers),
            base_dir=_data_dir(),
        )
    except (OSError, ValueError) as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not record collection change: {exc}"},
            status=500,
        )
        return

    _publish_record_change_event(change)

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
            "deleted": True,
        },
    )


async def _authorize_collection_write(
    send,
    headers: dict[str, str],
    action: str,
    *,
    collection: str,
    method: str,
    record: dict[str, Any],
    gate_message: str,
) -> dict[str, Any] | None:
    if _permission_enforcement_enabled():
        return await _collection_permission_check(
            send,
            headers,
            action,
            collection=collection,
            method=method,
            record=record,
        )

    if _permission_audit_enabled():
        permission_check = await _collection_permission_check(
            send,
            headers,
            action,
            collection=collection,
            method=method,
            record=record,
        )
        if permission_check is None:
            return None

    gate_error = _admin_token_gate_error(headers, gate_message)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return None

    return {
        "subject": _permission_subject(headers),
        "policy": object_permissions.PermissionPolicy(access_mode="public"),
        "decision": object_permissions.PermissionDecision.allow("admin token"),
        "enforced": False,
    }


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
    await _handle_schema(send, method, schema, {}, b"", headers)


async def _handle_schema(
    send,
    method: str,
    schema: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        if method == "PUT":
            await _handle_schema_put(send, schema, body, headers)
            return
        if method == "POST":
            await _handle_schema_post(send, schema, body, headers)
            return
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if query.get("versions") == "true":
        await _handle_schema_versions_get(send, schema, query, headers)
        return

    if "version" in query:
        await _handle_schema_version_get(send, schema, query, headers)
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


async def _handle_schema_put(
    send,
    schema: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(
        headers,
        f"Schema writes require {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        payload = _parse_json_body(body)
        schema_payload = _schema_payload_from_request(payload)
        normalized_schema = object_schemas.normalize_schema(schema, schema_payload, source="manual")
        author = _payload_text(payload, "author", "api")
        message = _payload_text(payload, "message", "Updated schema via API")
        version_id = _schema_version_manager().save_version(
            schema=schema,
            content=object_schema_versions.schema_version_content(normalized_schema),
            author=author,
            message=message,
        )
        saved_schema = object_schemas.replace_schema(
            schema,
            normalized_schema,
            base_dir=_data_dir(),
        )
    except object_schemas.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_schema_versions.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Schema updated to version {version_id}",
            "version_id": version_id,
            "collection": schema,
            "schema": saved_schema,
        },
    )


async def _handle_schema_post(
    send,
    schema: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    if payload.get("action") != "rollback":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _handle_schema_rollback_post(send, schema, payload, headers)


async def _handle_schema_versions_get(
    send,
    schema: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(
        headers,
        f"Schema versions require {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        object_schemas.get_schema(schema, base_dir=_data_dir())
        limit = _query_int(query, "limit", default=10, minimum=1, maximum=100)
        versions = _schema_version_manager().get_history(schema, limit=limit)
    except object_schemas.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_schemas.SchemaNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_schema_versions.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": schema,
            "versions": versions,
            "count": len(versions),
        },
    )


async def _handle_schema_version_get(
    send,
    schema: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(
        headers,
        f"Schema version detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        version_id = _query_int(query, "version", minimum=1)
        version = _schema_version_manager().get_version(schema, version_id)
    except object_schema_versions.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    if version is None:
        await _send_json(
            send,
            {"status": "error", "error": f"Version {version_id} not found for schema {schema}"},
            status=404,
        )
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": schema,
            "version": _schema_version_payload(version),
        },
    )


async def _handle_schema_rollback_post(
    send,
    schema: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> None:
    gate_error = _admin_token_gate_error(
        headers,
        f"Schema rollback requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        version_id = _payload_int(payload, "version_id", minimum=1)
        author = _payload_text(payload, "author", "api")
        message = _payload_text(payload, "message", f"Rollback schema to version {version_id}")
        new_version_id = _schema_version_manager().rollback(
            schema=schema,
            to_version=version_id,
            author=author,
            message=message,
        )
        new_version = _schema_version_manager().get_version(schema, new_version_id)
        if new_version is None:
            raise object_schema_versions.SchemaVersionNotFoundError(
                f"Version {new_version_id} not found for schema {schema}"
            )
        schema_payload = json.loads(new_version["content"])
        saved_schema = object_schemas.replace_schema(schema, schema_payload, base_dir=_data_dir())
    except object_schemas.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_schema_versions.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_schema_versions.SchemaVersionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except (json.JSONDecodeError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except OSError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Rolled back schema to version {version_id}",
            "version_id": version_id,
            "new_version_id": new_version_id,
            "collection": schema,
            "schema": saved_schema,
        },
    )


def _object_source_payload(source) -> dict[str, str]:
    return {
        "object_id": source.object_id,
        "path": source.relative_path.as_posix(),
        "owner": _object_owner(source.object_id),
    }


def _schema_version_payload(version: dict[str, Any]) -> dict[str, Any]:
    payload = dict(version)
    content = payload.get("content")
    if isinstance(content, str):
        try:
            payload["schema"] = json.loads(content)
        except json.JSONDecodeError:
            pass
    return payload


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
                correlation_id=result.correlation_id,
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
            correlation_id=result.correlation_id,
            method=result.method,
            status="error",
            duration_ms=result.duration_ms,
            error_type=error_type,
            error=error,
        )
    except Exception:
        # Logging is feedback for the dev loop; it should not change the object response.
        pass


def _append_file_log(
    object_id: str,
    *,
    operation: str,
    metadata: Mapping[str, Any],
    method: str,
) -> None:
    try:
        object_logs.append_object_log(
            object_id,
            "INFO",
            f"File {operation}: {metadata['name']}",
            base_dir=_data_dir(),
            method=method,
            status="success",
            file_name=metadata.get("name"),
            file_size=metadata.get("size"),
            file_operation=operation,
        )
    except Exception:
        # File logs are operator feedback; file writes should not fail because logging failed.
        pass


def _append_file_change_log(
    object_id: str,
    *,
    operation: str,
    metadata: Mapping[str, Any],
    method: str,
    actor: str,
    correlation_id: str | None,
) -> dict[str, Any] | None:
    action = {
        "created": "file_create",
        "updated": "file_update",
        "deleted": "file_delete",
    }.get(operation)
    if action is None:
        return None

    try:
        return object_file_changes.append_file_change(
            object_id=object_id,
            action=action,
            file_name=str(metadata.get("name") or ""),
            file_size=int(metadata["size"]) if "size" in metadata else None,
            actor=actor,
            message=f"File {operation}: {metadata.get('name')}",
            correlation_id=correlation_id,
            details={
                "method": method,
                "modified": metadata.get("modified"),
            },
            base_dir=_data_dir(),
        )
    except Exception:
        # File-change entries are operator feedback; file writes already succeeded.
        return None


def _append_source_change_log(
    object_id: str,
    *,
    action: str,
    version_id: int,
    from_version_id: int | None = None,
    actor: str = "api",
    message: str = "",
    correlation_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    fields: dict[str, Any] = {
        "action": action,
        "version_id": version_id,
    }
    if from_version_id is not None:
        fields["from_version_id"] = from_version_id

    try:
        object_source_changes.append_source_change(
            object_id=object_id,
            action=action,
            version_id=version_id,
            from_version_id=from_version_id,
            actor=actor,
            message=message,
            correlation_id=correlation_id,
            details=details,
            base_dir=_data_dir(),
        )
        object_logs.append_object_log(
            object_id,
            "INFO",
            f"{action} version {version_id}",
            base_dir=_data_dir(),
            correlation_id=correlation_id or object_correlation.current_correlation_id(),
            **fields,
        )
    except Exception:
        # Source-change entries are operator feedback; source writes already succeeded.
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


def _identity_session_login_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed_fields = {"user_id", "label", "ttl_seconds"}
    unsupported = sorted(set(payload) - allowed_fields)
    if unsupported:
        fields = ", ".join(unsupported)
        raise ValueError(f"Unsupported session login field(s): {fields}")

    return {key: payload[key] for key in allowed_fields if key in payload}


def _record_payload_from_body(body: bytes, *, require_id: bool = False) -> dict[str, str]:
    payload = _parse_json_body(body)
    if not payload:
        raise ValueError("Record payload must not be empty")
    return object_records.normalize_record_payload(payload, require_id=require_id)


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


def _admin_execute_method(payload: dict[str, Any]) -> str:
    value = payload.get("method", "GET")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Request JSON field 'method' must be one of GET, POST, PUT, DELETE")

    method = value.strip().upper()
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError("Request JSON field 'method' must be one of GET, POST, PUT, DELETE")
    return method


def _admin_execute_payload(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("payload", {})
    if not isinstance(value, dict):
        raise ValueError("Request JSON field 'payload' must be an object")
    return value


def _optional_payload_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool):
        return value
    raise ValueError(f"Request JSON field '{key}' must be a boolean")


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


def _created_object_id(payload: dict[str, Any], headers: dict[str, str]) -> str:
    object_id = _optional_payload_text(payload, "object_id")
    if object_id is not None:
        if not validate_object_id(object_id):
            raise ValueError(f"Invalid object ID: {object_id}")
        return object_id

    name = _required_payload_text(payload, "name")
    owner_user_id = _created_object_owner_user_id(payload, headers)
    if owner_user_id is not None:
        object_id = f"u_{owner_user_id}_{name}"
        if parse_user_object_id(object_id) is None:
            raise ValueError(
                "Request JSON field 'name' must start with a letter and contain only "
                "letters, numbers, and underscores for user-scoped objects"
            )
        return object_id

    if not validate_object_id(name):
        raise ValueError(f"Invalid object ID: {name}")
    return name


def _created_object_owner_user_id(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> str | None:
    if "owner_user_id" in payload:
        value = payload["owner_user_id"]
        if isinstance(value, int):
            owner_user_id = str(value)
        elif isinstance(value, str):
            owner_user_id = value.strip()
        else:
            raise ValueError("Request JSON field 'owner_user_id' must be a positive integer")
        if not owner_user_id.isdigit() or int(owner_user_id) <= 0:
            raise ValueError("Request JSON field 'owner_user_id' must be a positive integer")
        return owner_user_id

    session = _current_identity_session(headers)
    if session is not None and session.user_id.isdigit():
        return session.user_id
    return None


def _source_actor_from_headers(headers: dict[str, str]) -> str:
    session = _current_identity_session(headers)
    if session is not None:
        return session.user_id
    return "api"


def _file_write_payload(body: bytes) -> tuple[str, bytes]:
    payload = _parse_json_body(body)
    filename = _optional_payload_text(payload, "name")
    if filename is None:
        filename = _optional_payload_text(payload, "filename")
    if filename is None:
        raise ValueError("Request JSON field 'name' is required")

    content_base64 = payload.get("content_base64")
    if not isinstance(content_base64, str):
        raise ValueError("Request JSON field 'content_base64' must be a base64 string")

    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Request JSON field 'content_base64' must be valid base64") from exc

    max_bytes = _max_object_file_bytes()
    if len(content) > max_bytes:
        raise object_files.ObjectFileTooLargeError(
            f"Object file exceeds max size: {len(content)} bytes > {max_bytes} bytes"
        )
    return filename, content


def _file_delete_filename(query: dict[str, str], body: bytes) -> str:
    filename = _optional_query_text(query, "file")
    if filename is not None:
        return filename

    payload = _parse_json_body(body)
    filename = _optional_payload_text(payload, "name")
    if filename is None:
        filename = _optional_payload_text(payload, "filename")
    if filename is None:
        raise ValueError("Query parameter 'file' or JSON field 'name' is required")
    return filename


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


def _schema_payload_from_request(payload: dict[str, Any]) -> dict[str, Any]:
    schema_payload = payload.get("schema", payload)
    if not isinstance(schema_payload, dict):
        raise ValueError("Request JSON field 'schema' must be an object")
    return schema_payload


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


def _required_query_text(query: dict[str, str], key: str) -> str:
    value = _optional_query_text(query, key)
    if value is None:
        raise ValueError(f"Query parameter '{key}' is required")
    return value


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


def _file_write_gate_error(headers: dict[str, str]) -> tuple[int, str] | None:
    if not _env_enabled(FILE_WRITES_ENV):
        return (
            403,
            f"File writes are disabled. Set {FILE_WRITES_ENV}=true and {ADMIN_TOKEN_ENV}.",
        )

    return _admin_token_gate_error(headers, f"File writes require {ADMIN_TOKEN_ENV}.")


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


async def _collection_permission_check(
    send,
    headers: dict[str, str],
    action: str,
    *,
    collection: str,
    method: str,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    subject = _permission_subject(headers)
    enforced = _permission_enforcement_enabled()

    try:
        policy = object_permission_store.load_policy(_data_dir())
        decision = object_permissions.check_permission(
            subject,
            action,
            policy=policy,
            collection=collection,
            record=record,
        )
    except ValueError as exc:
        _append_permission_audit_entry(
            action=action,
            object_id=None,
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
            return None
        return {
            "subject": subject,
            "policy": object_permissions.PermissionPolicy(access_mode="public"),
            "decision": object_permissions.PermissionDecision.deny(str(exc)),
            "enforced": enforced,
        }

    _append_permission_audit_entry(
        action=action,
        object_id=None,
        collection=collection,
        method=method,
        subject=subject,
        enforced=enforced,
        decision=decision,
    )

    if enforced and not decision.allowed:
        await _send_json(
            send,
            {
                "status": "error",
                "error": decision.reason,
                "code": decision.code,
            },
            status=decision.http_status,
        )
        return None

    return {
        "subject": subject,
        "policy": policy,
        "decision": decision,
        "enforced": enforced,
    }


def _filter_records_for_permission(
    records: list[dict[str, str]],
    *,
    collection: str,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
) -> list[dict[str, str]]:
    allowed_records: list[dict[str, str]] = []
    for record in records:
        decision = object_permissions.check_permission(
            subject,
            object_permissions.READ,
            policy=policy,
            collection=collection,
            record=record,
        )
        if decision.allowed:
            allowed_records.append(
                _apply_record_field_policy(
                    record,
                    decision,
                    collection=collection,
                    subject=subject,
                    policy=policy,
                )
            )
    return allowed_records


def _apply_record_field_policy(
    record: dict[str, str],
    decision: object_permissions.PermissionDecision,
    *,
    collection: str | None = None,
    subject: object_permissions.PermissionSubject | None = None,
    policy: object_permissions.PermissionPolicy | None = None,
) -> dict[str, str]:
    filtered = dict(record)
    if decision.fields is not None:
        filtered = {key: value for key, value in filtered.items() if key in decision.fields}
    for field in decision.denied_fields:
        filtered.pop(field, None)
    if collection is not None and subject is not None and policy is not None:
        filtered = object_field_permissions.redact_record(
            collection,
            filtered,
            subject=subject,
            policy=policy,
            base_dir=_data_dir(),
        )
    return filtered


def _schema_denied_write_fields(
    collection: str,
    submitted_fields,
    *,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    record: dict[str, Any],
) -> list[str]:
    return object_field_permissions.denied_write_fields(
        collection,
        submitted_fields,
        subject=subject,
        policy=policy,
        record=record,
        base_dir=_data_dir(),
    )


def _field_write_denied_message(fields: list[str]) -> str:
    if len(fields) == 1:
        return f"Record field '{fields[0]}' is not editable for this subject"
    return f"Record fields are not editable for this subject: {', '.join(fields)}"


def _permission_checks_enabled() -> bool:
    return _permission_enforcement_requested() or _permission_audit_enabled()


def _permission_enforcement_enabled() -> bool:
    if not _permission_enforcement_requested():
        return False
    if _permission_unready_enforcement_allowed():
        return True
    try:
        readiness = object_permission_status.enforcement_readiness(
            base_dir=_data_dir(),
            permissions=_permission_readiness_inputs(),
        )
    except (OSError, ValueError):
        return False
    return bool(readiness.get("can_enable_enforcement"))


def _permission_audit_enabled() -> bool:
    return _env_enabled(PERMISSION_AUDIT_ENV) or _permission_enforcement_requested()


def _permission_enforcement_requested() -> bool:
    return _env_enabled(PERMISSION_ENFORCEMENT_ENV)


def _permission_unready_enforcement_allowed() -> bool:
    return _env_enabled(PERMISSION_UNREADY_ENFORCEMENT_ENV)


def _permission_enforcement_blocked() -> bool:
    return _permission_enforcement_requested() and not _permission_enforcement_enabled()


def _permission_subject(headers: dict[str, str]) -> object_permissions.PermissionSubject:
    return _permission_identity(headers)[0]


def _permission_identity(
    headers: dict[str, str],
) -> tuple[object_permissions.PermissionSubject, str]:
    token = _authorization_token(headers)
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if token and admin_token and hmac.compare_digest(token, admin_token):
        return object_permissions.PermissionSubject(user_id="admin", roles=("admin",)), "admin_token"

    cookie_token = None if token else _session_cookie_token(headers)
    session_token = token or cookie_token
    if session_token:
        try:
            session = object_identity.resolve_session_token(session_token, base_dir=_data_dir())
        except (OSError, ValueError):
            session = None
        if session is not None:
            return session.subject(), "session_cookie" if cookie_token else "session_token"

    if not _env_enabled(PERMISSION_TRUST_HEADERS_ENV):
        return object_permissions.PermissionSubject.anonymous(), "anonymous"

    user_id = _optional_header_text(headers, "x-dbbasic-user-id")
    account_id = _optional_header_text(headers, "x-dbbasic-account-id")
    subject = object_permissions.PermissionSubject(
        user_id=user_id,
        account_id=account_id,
        roles=_csv_header(headers.get("x-dbbasic-roles", "")),
        subscriptions=_csv_header(headers.get("x-dbbasic-subscriptions", "")),
    )
    method = "trusted_headers" if _trusted_identity_headers_present(headers) else "anonymous"
    return subject, method


def _current_identity_session(headers: dict[str, str]) -> object_identity.IdentitySession | None:
    token = _authorization_token(headers) or _session_cookie_token(headers)
    if not token:
        return None

    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if admin_token and hmac.compare_digest(token, admin_token):
        return None

    try:
        return object_identity.resolve_session_token(token, base_dir=_data_dir())
    except (OSError, ValueError):
        return None


def _session_cookie_token(headers: dict[str, str]) -> str | None:
    cookie_header = headers.get("cookie", "")
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE_NAME and value:
            return value
    return None


def _cookie_request_origin_allowed(headers: dict[str, str]) -> bool:
    source = headers.get("origin", "").strip() or headers.get("referer", "").strip()
    if not source:
        return True
    host = headers.get("host", "").strip().lower()
    if not host:
        return False
    return (urllib.parse.urlsplit(source).netloc or "").lower() == host


def _trusted_identity_headers_present(headers: dict[str, str]) -> bool:
    return any(
        _optional_header_text(headers, name) is not None
        for name in (
            "x-dbbasic-user-id",
            "x-dbbasic-account-id",
            "x-dbbasic-roles",
            "x-dbbasic-subscriptions",
        )
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
    object_id: str | None,
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
        "correlation_id": object_correlation.current_correlation_id(),
        "method": method,
        "object_id": object_id,
        "collection": collection,
        "action": action,
        "subject": _permission_subject_payload(subject),
        "enforced": enforced,
        "enforcement_requested": _permission_enforcement_requested(),
    }
    if entry["enforcement_requested"] and not enforced:
        entry["enforcement_blocked"] = True
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


def _record_change_actor(headers: dict[str, str]) -> str:
    subject = _permission_subject(headers)
    if subject.user_id:
        return subject.user_id
    if subject.account_id:
        return f"account:{subject.account_id}"
    if subject.roles:
        return ",".join(subject.roles)
    return "api"


def _publish_record_change_event(change: dict[str, Any]) -> dict[str, Any] | None:
    if not _record_events_enabled():
        return None

    event_type = RECORD_EVENT_TYPES.get(str(change.get("action", "")))
    if event_type is None:
        return None

    payload = {
        "change_id": change.get("change_id"),
        "collection": change.get("collection"),
        "record_id": change.get("record_id"),
        "action": change.get("action"),
        "actor": change.get("actor"),
        "timestamp": change.get("timestamp"),
        "changed_fields": change.get("changed_fields", []),
    }

    try:
        return object_events.publish_event(
            event_type,
            payload=payload,
            source="record_changes",
            actor=str(change.get("actor") or "api"),
            base_dir=_data_dir(),
            keep_count=_event_keep_count(),
            keep_seconds=_event_keep_seconds(),
        )
    except (OSError, ValueError):
        return None


def _record_events_enabled() -> bool:
    value = os.environ.get(RECORD_EVENTS_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


def _event_keep_count() -> int | None:
    value = _env_int(EVENT_KEEP_COUNT_ENV, object_events.DEFAULT_EVENT_KEEP_COUNT)
    if value <= 0:
        return None
    return min(value, object_events.MAX_EVENT_KEEP_COUNT)


def _event_keep_seconds() -> int | None:
    value = _env_int(EVENT_KEEP_SECONDS_ENV, object_events.DEFAULT_EVENT_KEEP_SECONDS)
    if value <= 0:
        return None
    return value


def _event_retention_query_int(
    query: dict[str, str],
    key: str,
    *,
    default: int | None,
    maximum: int | None = None,
) -> int | None:
    if key not in query:
        return default
    value = _query_int(query, key, default=0, minimum=0, maximum=maximum)
    return value or None


def _admin_token_gate_error(
    headers: dict[str, str],
    missing_token_message: str,
) -> tuple[int, str] | None:
    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    session_admin_gates = _env_enabled(SESSION_ADMIN_GATES_ENV)
    if not admin_token and not session_admin_gates:
        return (403, missing_token_message)

    request_token = _authorization_token(headers)
    if request_token is None:
        return (401, "Unauthorized")

    if admin_token and hmac.compare_digest(request_token, admin_token):
        return None

    if session_admin_gates and _admin_session_authorized(request_token):
        return None

    return (401, "Unauthorized")


def _admin_session_authorized(token: str) -> bool:
    try:
        session = object_identity.resolve_session_token(token, base_dir=_data_dir())
    except (OSError, ValueError):
        return False
    if session is None:
        return False

    try:
        policy = object_permission_store.load_policy(_data_dir())
    except ValueError:
        policy = object_permissions.PermissionPolicy()

    return object_permissions.subject_has_admin_role(session.subject(), policy)


def _session_login_gate_error(headers: dict[str, str]) -> tuple[int, str] | None:
    if not _env_enabled(SESSION_LOGIN_ENV):
        return (403, f"Session login is disabled. Set {SESSION_LOGIN_ENV}=true.")

    login_token = os.environ.get(SESSION_LOGIN_TOKEN_ENV, "")
    if not login_token:
        return (403, f"Session login token is not configured. Set {SESSION_LOGIN_TOKEN_ENV}.")

    request_token = _authorization_token(headers)
    if request_token is None or not hmac.compare_digest(request_token, login_token):
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


def _schema_version_manager() -> object_schema_versions.SchemaVersionManager:
    return object_schema_versions.SchemaVersionManager(_data_dir())


def _data_dir() -> str:
    return os.environ.get(DATA_DIR_ENV, object_versions.DEFAULT_DATA_DIR)


def _packages_dir() -> str:
    return os.environ.get(PACKAGES_DIR_ENV, object_packages.PACKAGES_DIR)


def _primary_objects_dir() -> str:
    return str(get_object_roots()[0])


def _max_concurrent_requests() -> int:
    return _env_int(MAX_CONCURRENT_REQUESTS_ENV, DEFAULT_MAX_CONCURRENT_REQUESTS)


def _max_concurrent_executions() -> int:
    return _env_int(MAX_CONCURRENT_EXECUTIONS_ENV, DEFAULT_MAX_CONCURRENT_EXECUTIONS)


def _max_request_bytes() -> int:
    max_bytes = _env_int(MAX_REQUEST_BYTES_ENV, DEFAULT_MAX_REQUEST_BYTES)
    if max_bytes < 0:
        return DEFAULT_MAX_REQUEST_BYTES
    return max_bytes


def _max_object_file_bytes() -> int:
    max_bytes = _env_int(MAX_OBJECT_FILE_BYTES_ENV, DEFAULT_MAX_OBJECT_FILE_BYTES)
    if max_bytes < 0:
        return DEFAULT_MAX_OBJECT_FILE_BYTES
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
            "correlation_id": result.correlation_id,
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
