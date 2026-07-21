"""Minimal ASGI app for DBBASIC Object Server.

This is the first public server slice. Source writes are disabled by default
while the production auth and mutation paths are extracted.
"""

from __future__ import annotations

import asyncio
import contextlib
import base64
import binascii
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Mapping

import http_api_contract
import object_activity
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
import object_handlers
import object_identity
import object_ids
import object_logs
import object_mcp
import object_metadata
import object_metrics_history
import object_multipart
import object_ops_log
import object_package_changes
import object_permission_audit
import object_permission_store
import object_permission_status
import object_permissions
import object_packages
import object_rate_limit
import object_reader
import object_reconciles
import object_record_changes
import object_finance
import object_records
import object_schema_versions
import object_stock
import object_ai
import object_backup_index
import object_realtime
import object_schemas
import object_search
import object_service_keys
import object_tts
import object_user_files
import object_site_routes
import object_source
import object_source_changes
import object_state
import object_versions
import object_worker_pool
import object_namespace
from object_namespace import (
    get_base_object_roots,
    get_object_roots,
    get_override_root,
    iter_object_sources,
    parse_user_object_id,
    resolve_object_id,
    validate_object_id,
)
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
LOGIN_LOCKOUT_ATTEMPTS_ENV = "DBBASIC_LOGIN_LOCKOUT_ATTEMPTS"
LOGIN_LOCKOUT_WINDOW_SECONDS_ENV = "DBBASIC_LOGIN_LOCKOUT_WINDOW_SECONDS"
DEFAULT_LOGIN_LOCKOUT_ATTEMPTS = 5
DEFAULT_LOGIN_LOCKOUT_WINDOW_SECONDS = 900
LOGIN_LOCKED_MESSAGE = "Too many failed attempts. Try again later."
SESSION_COOKIE_NAME = "dbbasic_session"
COOKIE_SECURE_ENV = "DBBASIC_COOKIE_SECURE"
SITE_ROUTES_ENV = "DBBASIC_ENABLE_SITE_ROUTES"
METRICS_SNAPSHOT_SECONDS_ENV = "DBBASIC_METRICS_SNAPSHOT_SECONDS"
DEFAULT_METRICS_SNAPSHOT_SECONDS = 60
RECORD_EVENTS_ENV = "DBBASIC_ENABLE_RECORD_EVENTS"
EVENT_KEEP_COUNT_ENV = "DBBASIC_EVENT_KEEP_COUNT"
EVENT_KEEP_SECONDS_ENV = "DBBASIC_EVENT_KEEP_SECONDS"
# 58's <block>_enabled kill switch, default ON: field-filter query params are
# read and applied unless explicitly disabled. Off degrades to "ignore the
# filter params" (unfiltered, same as before this feature), never to a 400 --
# see _handle_collection_records_get and 58's Degradation section.
FILTERING_ENABLED_ENV = "DBBASIC_ENABLE_FILTERING"
# 63 optimistic concurrency: gates whether an update's If-Match/expected_rev
# precondition is honored. Default on; flipping it off makes writes carrying
# a precondition behave as last-write-wins rather than turning a working
# write path into an error path under brownout (same posture as filtering).
CONCURRENCY_ENABLED_ENV = "DBBASIC_ENABLE_CONCURRENCY"
# 64 (feed): the <block>_enabled kill switch for GET /api/feed and the
# get_feed MCP verb. Default on; off degrades to an empty, non-error
# response (spec's Degradation section) -- `follows` itself, and the
# follow button, are untouched by this flag, only the composed read is.
FEED_ENABLED_ENV = "DBBASIC_ENABLE_FEED"
# Query params the collection-records GET route reserves for pagination/
# sort/search rather than treating as a field filter (58's Encoding
# section); every other param is a `field` or `field.op` filter condition.
FILTER_RESERVED_PARAMS = frozenset({"limit", "offset", "sort", "q"})
# `field.in=a,b,c,...` cap (58's Open Questions: bound the list so `in`
# can't become an unbounded OR by the back door).
FILTER_IN_MAX_VALUES = 50
PACKAGES_DIR_ENV = "DBBASIC_PACKAGES_DIR"
PRIVATE_PACKAGES_DIR_ENV = "DBBASIC_PRIVATE_PACKAGES_DIR"
DEFAULT_PRIVATE_PACKAGES_DIR = "packages-private"
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

REALTIME_ENABLED_ENV = "DBBASIC_ENABLE_REALTIME"
REALTIME_QUEUE_MAX_ENV = "DBBASIC_REALTIME_QUEUE_MAX"
WEBSOCKET_PATH = "/ws"
MAX_WS_SUBSCRIPTIONS = 64
_realtime_hub = object_realtime.RealtimeHub()

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

_worker_pool: object_worker_pool.WorkerPool | None = None
_worker_pool_size_used: int = 0
_worker_pool_lock = asyncio.Lock()


async def app(scope: dict[str, Any], receive, send) -> None:
    """ASGI application entry point."""
    if scope["type"] == "lifespan":
        await _handle_lifespan(receive, send)
        return

    if scope["type"] == "websocket":
        await _handle_websocket(scope, receive, send)
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


def _realtime_enabled() -> bool:
    value = os.environ.get(REALTIME_ENABLED_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


def _realtime_queue_max() -> int:
    return _env_int(REALTIME_QUEUE_MAX_ENV, object_realtime.DEFAULT_QUEUE_MAX)


async def _handle_websocket(scope: dict[str, Any], receive, send) -> None:
    """Live push transport: authenticated, permission-filtered record events.

    A client connects to /ws with its session (cookie or bearer),
    subscribes to collections it is allowed to read, and receives a small
    signal ({type: record, collection, record_id, action}) whenever a
    matching record it can see changes — never the record body, so no
    surface can leak fields. The client refetches through the normal
    permission-enforced API. The durable event log stays as the poll-based
    fallback for missed events and multi-worker deployments.
    """
    headers = _parse_headers(scope.get("headers", []))

    # Consume the connect frame first so we can accept or reject cleanly.
    connect = await receive()
    if connect.get("type") != "websocket.connect":
        return

    if not _realtime_enabled() or scope.get("path") != WEBSOCKET_PATH:
        await send({"type": "websocket.close", "code": 1008})
        return

    subject = _permission_subject(headers)
    if subject.user_id is None and "admin" not in subject.roles:
        await send({"type": "websocket.close", "code": 1008})
        return

    await send({"type": "websocket.accept"})
    queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=_realtime_queue_max())
    subscriber = object_realtime.Subscriber(subject=subject, queue=queue)
    _realtime_hub.add(subscriber)
    await _ws_send(send, {"type": "welcome", "user": subject.user_id})

    reader = asyncio.ensure_future(_ws_reader(receive, send, subscriber))
    writer = asyncio.ensure_future(_ws_writer(send, queue))
    try:
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
    finally:
        _realtime_hub.remove(subscriber)
        with contextlib.suppress(Exception):
            await send({"type": "websocket.close"})


async def _ws_reader(receive, send, subscriber) -> None:
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return
        if kind != "websocket.receive":
            continue
        text = message.get("text")
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        action = data.get("action")
        if action == "subscribe":
            for collection in _ws_clean_collections(data.get("collections")):
                if len(subscriber.collections) >= MAX_WS_SUBSCRIPTIONS:
                    break
                if _ws_can_subscribe(subscriber.subject, collection):
                    subscriber.collections.add(collection)
            await _ws_send(
                send, {"type": "subscribed", "collections": sorted(subscriber.collections)}
            )
        elif action == "unsubscribe":
            for collection in _ws_clean_collections(data.get("collections")):
                subscriber.collections.discard(collection)
            await _ws_send(
                send, {"type": "subscribed", "collections": sorted(subscriber.collections)}
            )
        elif action == "ping":
            await _ws_send(send, {"type": "pong"})


async def _ws_writer(send, queue) -> None:
    while True:
        event = await queue.get()
        await _ws_send(send, event)


async def _ws_send(send, payload: dict[str, Any]) -> None:
    await send({"type": "websocket.send", "text": json.dumps(payload, default=str)})


def _ws_clean_collections(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and object_collections.validate_collection_name(item):
            out.append(item)
        if len(out) >= MAX_WS_SUBSCRIPTIONS:
            break
    return out


def _ws_can_subscribe(subject, collection: str) -> bool:
    """A subject may follow a collection only if it may read it."""
    if not _permission_checks_enabled() or not _permission_enforcement_enabled():
        return True
    try:
        policy = object_permission_store.load_policy(_data_dir())
    except ValueError:
        return False
    decision = object_permissions.check_permission(
        subject, object_permissions.READ, policy=policy, collection=collection
    )
    return decision.allowed


def _realtime_publish(collection: str, record_id: str, action: str, record) -> None:
    """Push a record-change signal to permitted live subscribers.

    Runs the same read decision as a GET: with enforcement on, a
    subscriber only hears about a record its row filter would let it see.
    Only the id and action travel — never the record body.
    """
    if not _realtime_enabled() or not collection:
        return
    subscribers = _realtime_hub.wanting(collection)
    if not subscribers:
        return

    enforced = _permission_checks_enabled() and _permission_enforcement_enabled()
    policy = None
    if enforced:
        try:
            policy = object_permission_store.load_policy(_data_dir())
        except ValueError:
            return  # cannot filter safely -> do not push

    event = {
        "type": "record",
        "collection": collection,
        "record_id": record_id,
        "action": action,
    }
    for subscriber in subscribers:
        if enforced and record is not None:
            decision = object_permissions.check_permission(
                subscriber.subject,
                object_permissions.READ,
                policy=policy,
                collection=collection,
                record=record,
            )
            if not decision.allowed:
                continue
        subscriber.deliver(dict(event))


async def _handle_http(scope: dict[str, Any], receive, send) -> None:
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    query = _parse_query(scope.get("query_string", b""))
    headers = _parse_headers(scope.get("headers", []))
    correlation_id = object_correlation.current_correlation_id()
    if correlation_id is not None:
        headers[object_correlation.CORRELATION_ID_HEADER] = correlation_id

    _maybe_append_metrics_snapshot()

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

        if path == http_api_contract.MCP_PATH:
            await _handle_mcp(send, method, body, headers)
            return

        if path == http_api_contract.SEARCH_PATH:
            await _handle_search(send, method, query, headers)
            return

        if path == http_api_contract.AI_CHAT_PATH:
            await _handle_ai_chat(send, method, body, headers)
            return

        if path == http_api_contract.TTS_PATH:
            await _handle_tts(send, method, body, headers)
            return

        if path == http_api_contract.READ_PATH:
            await _handle_read(send, method, body, headers)
            return

        schema_meta_prefix = f"{http_api_contract.SCHEMA_META_PATH}/"
        if path.startswith(schema_meta_prefix):
            await _handle_schema_meta(send, method, path.removeprefix(schema_meta_prefix))
            return

        if path == http_api_contract.FLAGS_PATH:
            await _handle_flags(send, method, headers)
            return

        if path == http_api_contract.ACTIVITY_PATH:
            await _handle_activity(send, method, query, headers)
            return

        # 64 (feed): the composed follow-graph read. Lives at /api/feed, not
        # bare /feed, because /feed is this feature's own PAGE route (site_feed,
        # packages/app-feed), resolved further down by the site-route
        # convention -- the same /api/{x} (data) vs /{x} (page) split
        # /api/activity above already uses opposite site_activity, so the two
        # surfaces never collide on one path.
        if path == "/api/feed":
            await _handle_feed(send, method, query, headers)
            return

        # Stage 7 derived-read verbs: folded/computed data an agent can't get
        # as a plain collection read. Owner-scoped (the caller's own books/
        # stock), thin wrappers over object_stock/object_finance's own pure
        # fold functions -- the MCP get_stock_levels/get_finance_summary verbs
        # route here.
        if path == "/api/stock":
            await _handle_stock_levels(send, method, headers)
            return
        if path == "/api/finance/summary":
            await _handle_finance_summary(send, method, headers)
            return

        if path == http_api_contract.PREFS_PATH:
            await _handle_prefs(send, method, headers)
            return

        prefs_prefix = http_api_contract.PREFS_PATH + "/"
        if path.startswith(prefs_prefix):
            await _handle_pref(send, method, path[len(prefs_prefix):], body, headers)
            return

        if path == http_api_contract.USER_FILES_PATH:
            await _handle_user_file_upload(send, method, body, headers)
            return

        user_file_prefix = http_api_contract.USER_FILES_PATH + "/"
        if path.startswith(user_file_prefix):
            await _handle_user_file(send, method, path[len(user_file_prefix):], headers)
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

        if path == http_api_contract.ADMIN_STORAGE_PATH:
            await _handle_admin_storage(send, method, headers)
            return

        if path == http_api_contract.ADMIN_CHANGES_PATH:
            await _handle_admin_changes(send, method, query, headers)
            return

        if path == http_api_contract.ADMIN_OPS_PATH:
            await _handle_admin_ops(send, method, query, headers)
            return

        if path == http_api_contract.ADMIN_BACKUPS_PATH:
            await _handle_admin_backups(send, method, headers)
            return

        admin_backups_prefix = f"{http_api_contract.ADMIN_BACKUPS_PATH}/"
        if path.startswith(admin_backups_prefix):
            tail = path.removeprefix(admin_backups_prefix)
            if tail.endswith("/download"):
                backup_id = tail.removesuffix("/download")
                await _handle_admin_backup_download(send, method, backup_id, headers)
                return
            if tail.endswith("/preview"):
                backup_id = tail.removesuffix("/preview")
                await _handle_admin_backup_preview(send, method, backup_id, query, headers)
                return
            if tail.endswith("/record"):
                backup_id = tail.removesuffix("/record")
                await _handle_admin_backup_record(send, method, backup_id, query, headers)
                return
            await _send_json(send, {"status": "error", "error": "Not found"}, status=404)
            return

        if path == http_api_contract.ADMIN_FILES_PATH:
            await _handle_admin_files(send, method, query, headers)
            return

        admin_files_prefix = f"{http_api_contract.ADMIN_FILES_PATH}/"
        if path.startswith(admin_files_prefix):
            object_id = path.removeprefix(admin_files_prefix)
            await _handle_admin_object_files(send, method, object_id, query, body, headers)
            return

        if path == http_api_contract.ADMIN_RECONCILES_PATH:
            await _handle_admin_reconciles(send, method, query, headers)
            return

        admin_reconciles_prefix = f"{http_api_contract.ADMIN_RECONCILES_PATH}/"
        if path.startswith(admin_reconciles_prefix):
            reconcile_tail = path.removeprefix(admin_reconciles_prefix)
            if reconcile_tail.endswith("/resolve"):
                reconcile_id = reconcile_tail.removesuffix("/resolve")
                await _handle_admin_reconcile_resolve(send, method, reconcile_id, body, headers)
            else:
                await _handle_admin_reconcile(send, method, reconcile_tail, headers)
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

            if object_tail.endswith("/override"):
                object_id = object_tail.removesuffix("/override")
                await _handle_admin_object_override(send, method, object_id, headers)
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

        if await _handle_site_route(send, method, path, query, body, headers):
            return

        await _send_json(send, {"status": "error", "error": "Not found"}, status=404)
    finally:
        request_token.release()


async def _handle_site_route(
    send,
    method: str,
    path: str,
    query: dict[str, str],
    body: bytes,
    headers: dict[str, str],
) -> bool:
    """Serve clean public URLs by resolving them to objects.

    Runs only after every built-in route family has declined the path, so
    reserved surfaces can never be shadowed. Resolution order: convention
    (`/about` -> `site_about`), then `site_routes` records patterns, then the
    `site_404` object. The resolved object executes through the normal
    execution path, so permission policy, audit, timeouts, and correlation
    ids all apply. Returns False when site routing is disabled or nothing
    resolves, leaving the plain JSON 404.
    """
    if not _env_enabled(SITE_ROUTES_ENV):
        return False

    if method not in {"GET", "POST", "PUT", "DELETE"}:
        return False

    site = object_site_routes.resolve_host(headers.get("host"), _site_host_records())
    params: dict[str, str] = {}
    target_id = object_site_routes.convention_object_id(
        path,
        prefix=site["prefix"],
        home=site["home"],
    )
    if target_id is None or resolve_object_id(target_id, get_object_roots()) is None:
        match = object_site_routes.match_records(
            path,
            _site_route_records(),
            host=site["host"],
        )
        if match is not None:
            target_id, params = match
        elif resolve_object_id(site["not_found"], get_object_roots()) is not None:
            target_id = site["not_found"]
            params = {"path": path}
        elif (
            site["not_found"] != object_site_routes.NOT_FOUND_OBJECT_ID
            and resolve_object_id(object_site_routes.NOT_FOUND_OBJECT_ID, get_object_roots())
            is not None
        ):
            target_id = object_site_routes.NOT_FOUND_OBJECT_ID
            params = {"path": path}
        else:
            return False

    try:
        if method == "GET":
            payload: dict[str, Any] = dict(query)
        else:
            payload = _parse_post_payload(body, query, headers)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return True

    payload.update(params)
    # The raw matched path, so a routed object can resolve a capture-LESS
    # route (an index/list view like /entities, whose views.route has no
    # {param}) -- route captures alone can only identify detail views. A
    # reserved, underscore-prefixed key (like _identity); objects that don't
    # need it ignore it. See view_render._resolve_view_and_record.
    payload["_path"] = path

    permission_action = object_permissions.EXECUTE
    if method == "PUT":
        permission_action = object_permissions.UPDATE
    elif method == "DELETE":
        permission_action = object_permissions.DELETE

    await _execute_object_method(
        send,
        target_id,
        method,
        payload,
        headers,
        permission_action=permission_action,
    )
    return True


def _site_route_records() -> list[dict[str, Any]]:
    try:
        return object_records.read_collection_records(
            object_site_routes.SITE_ROUTES_COLLECTION,
            base_dir=_data_dir(),
        )
    except (ValueError, LookupError, OSError):
        return []


def _site_host_records() -> list[dict[str, Any]]:
    try:
        return object_records.read_collection_records(
            object_site_routes.SITE_HOSTS_COLLECTION,
            base_dir=_data_dir(),
        )
    except (ValueError, LookupError, OSError):
        return []


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
    if query.get("metrics") == "true" and query.get("history") == "true":
        try:
            payload["history"] = object_metrics_history.read_history(
                base_dir=_data_dir(),
                limit=_query_int(query, "history_limit", default=360, minimum=1, maximum=2000),
            )
        except (OSError, ValueError):
            payload["history"] = []
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


def _service_keys_route_parts(user_id: str) -> tuple[str | None, str | None]:
    """Split '{user}/service-keys[/{service}]' route tails; (None, None) otherwise."""
    if "/service-keys" not in user_id:
        return None, None
    target, _, tail = user_id.partition("/service-keys")
    if "/" in target or not target:
        return None, None
    if tail == "":
        return target, None
    if tail.startswith("/") and tail.count("/") == 1 and len(tail) > 1:
        return target, tail[1:]
    return None, None


async def _handle_identity_user_service_keys(
    send,
    method: str,
    user_id: str,
    service: str | None,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Write-only per-user service keys: set, list status, delete — never read.

    A signed-in user manages their own keys; the admin gate covers operator
    use. Key material never appears in any response.
    """
    session = _current_identity_session(headers)
    self_service = session is not None and session.user_id == user_id
    if not self_service:
        gate_error = _admin_token_gate_error(
            headers, "Service keys require the account owner's session or the admin gate."
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    if (
        method in {"PUT", "POST", "DELETE"}
        and _session_cookie_token(headers)
        and not _authorization_token(headers)
        and not _cookie_request_origin_allowed(headers)
    ):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
            status=403,
        )
        return

    if method == "GET" and service is None:
        try:
            statuses = object_service_keys.list_service_key_status(
                user_id, base_dir=_data_dir()
            )
        except object_service_keys.InvalidServiceKeyError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        await _send_json(send, {"status": "ok", "user_id": user_id, "services": statuses})
        return

    if method in {"PUT", "POST"} and service is None:
        try:
            payload = _parse_json_body(body)
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        try:
            result = object_service_keys.set_service_key(
                user_id,
                payload.get("service"),
                payload.get("key"),
                base_dir=_data_dir(),
            )
        except object_service_keys.InvalidServiceKeyError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        _append_ops_auth_event(
            event="service_key_set",
            identifier=user_id,
            label=result["service"],
        )
        await _send_json(send, {"status": "ok", **result})
        return

    if method == "DELETE" and service is not None:
        try:
            deleted = object_service_keys.remove_service_key(
                user_id, service, base_dir=_data_dir()
            )
        except object_service_keys.InvalidServiceKeyError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        if not deleted:
            await _send_json(
                send, {"status": "error", "error": "No key stored for that service"}, status=404
            )
            return
        _append_ops_auth_event(
            event="service_key_removed",
            identifier=user_id,
            label=service,
        )
        await _send_json(send, {"status": "ok", "deleted": True, "service": service})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_identity_user(
    send,
    method: str,
    user_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    service_keys_target, service_name = _service_keys_route_parts(user_id)
    if service_keys_target is not None:
        await _handle_identity_user_service_keys(
            send, method, service_keys_target, service_name, body, headers
        )
        return

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

        _append_ops_auth_event(
            "session_minted",
            user_id=result["session"]["user_id"],
            method="session_login",
            label=result["session"].get("label"),
        )
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

    attempted = login["user_id"] or login["email"] or ""
    if _login_locked(attempted):
        _append_ops_auth_event("login_locked", identifier=attempted, method="password")
        await _send_json(
            send,
            {"status": "error", "error": LOGIN_LOCKED_MESSAGE},
            status=429,
        )
        return

    user = _password_login_user(login)
    lookup_user_id = user["user_id"] if user is not None else "__unknown_password_login__"
    verified = object_credentials.verify_password(
        lookup_user_id,
        login["password"],
        base_dir=_data_dir(),
    )

    if user is None or not verified:
        _append_ops_auth_event("login_failed", identifier=attempted, method="password")
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
        _append_ops_auth_event("login_failed", identifier=attempted, method="password")
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_json(send, {"status": "error", "error": "Invalid credentials"}, status=401)
        return

    _append_ops_auth_event(
        "login_succeeded",
        identifier=attempted,
        user_id=user["user_id"],
        method="password",
        label=login["label"],
    )
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


async def _handle_mcp(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    protocol_header = headers.get("mcp-protocol-version", "").strip()
    if protocol_header and protocol_header not in object_mcp.SUPPORTED_MCP_PROTOCOL_VERSIONS:
        await _send_json(
            send,
            object_mcp.jsonrpc_error(
                -32600, f"Unsupported MCP protocol version: {protocol_header}"
            ),
            status=400,
        )
        return

    gate_error = _admin_token_gate_error(headers, f"MCP requires {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, object_mcp.jsonrpc_error(-32000, message), status=status)
        return

    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await _send_json(send, object_mcp.jsonrpc_error(-32700, "Parse error"))
        return

    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        await _send_json(
            send,
            object_mcp.jsonrpc_error(-32600, "Invalid Request: missing jsonrpc 2.0"),
        )
        return

    rpc_method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")

    if not isinstance(rpc_method, str) or not rpc_method:
        await _send_json(
            send,
            object_mcp.jsonrpc_error(-32600, "Invalid Request: missing method", request_id),
        )
        return

    if rpc_method.startswith("notifications/"):
        await _send_response(send, status=202, headers=[], body=b"")
        return

    if rpc_method == "initialize":
        session_id = headers.get("mcp-session-id", "").strip() or object_ids.new_uuid4()
        await _send_json(
            send,
            object_mcp.jsonrpc_response(object_mcp.handle_initialize(params), request_id),
            headers=[("mcp-session-id", session_id)],
        )
        return

    if rpc_method == "tools/list":
        await _send_json(
            send,
            object_mcp.jsonrpc_response(object_mcp.handle_tools_list(), request_id),
        )
        return

    if rpc_method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            route_method, path, query_string, route_body = object_mcp.tool_route(
                tool_name, arguments
            )
        except ValueError as exc:
            await _send_json(
                send,
                object_mcp.jsonrpc_response(
                    {
                        "content": [
                            {"type": "text", "text": json.dumps({"error": str(exc)})}
                        ],
                        "isError": True,
                    },
                    request_id,
                ),
            )
            return

        status, payload = await _internal_request(
            route_method,
            path,
            query_string,
            route_body,
            authorization=headers.get("authorization", ""),
        )
        await _send_json(
            send,
            object_mcp.jsonrpc_response(
                object_mcp.tool_result_content(status, payload), request_id
            ),
        )
        return

    await _send_json(
        send,
        object_mcp.jsonrpc_error(-32601, f"Method not found: {rpc_method}", request_id),
    )


async def _internal_request(
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    *,
    authorization: str,
) -> tuple[int, Any]:
    """Dispatch one request through the server's own routing.

    Used by the MCP layer so tool calls hit the exact same gates, permission
    checks, audit trail, and correlation ids as external HTTP callers.
    """
    messages: list[dict[str, Any]] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope_headers = [(b"content-type", b"application/json")]
    if authorization:
        scope_headers.append((b"authorization", authorization.encode("latin-1")))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string.encode("utf-8"),
        "headers": scope_headers,
        "client": ("127.0.0.1", 0),
    }
    await app(scope, receive, send)

    status = 500
    for message in messages:
        if message["type"] == "http.response.start":
            status = message["status"]
            break
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = raw.decode("utf-8", errors="replace")
    return status, payload


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
        error = None
        if query.get("error") == "locked":
            error = LOGIN_LOCKED_MESSAGE
        elif query.get("error"):
            error = "Invalid email or password."
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

    if _login_locked(email):
        _append_ops_auth_event("login_locked", identifier=email, method="password_form")
        locked_location = f"{http_api_contract.LOGIN_PATH}?error=locked"
        if next_path != "/":
            locked_location += f"&next={urllib.parse.quote(next_path)}"
        await _send_redirect(send, locked_location)
        return

    login = {"user_id": None, "email": email, "password": password}
    user = _password_login_user(login)
    lookup_user_id = user["user_id"] if user is not None else "__unknown_password_login__"
    verified = object_credentials.verify_password(lookup_user_id, password, base_dir=_data_dir())

    if user is None or not verified:
        _append_ops_auth_event("login_failed", identifier=email, method="password_form")
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
        _append_ops_auth_event("login_failed", identifier=email, method="password_form")
        await asyncio.sleep(PASSWORD_LOGIN_FAILURE_DELAY_SECONDS)
        await _send_redirect(send, failure_location)
        return

    _append_ops_auth_event(
        "login_succeeded",
        identifier=email,
        user_id=user["user_id"],
        method="password_form",
        label="browser login",
    )
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
        _append_ops_auth_event("logout", user_id=session.user_id)

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

    # This is the GLOBAL per-request DoS limiter, so it stays fail-open on
    # storage failure (fail_closed=False): a broken ratelimit dir must not
    # take the whole site down. But the degradation is no longer silent --
    # check_rate_limit flags it and we log it, because an invisibly-degraded
    # limiter is a security hole. Public-write surfaces get their own check
    # with fail_closed=True (see object_rate_limit.check_rate_limit).
    result = object_rate_limit.check_rate_limit(
        directory=_rate_limit_dir(),
        identity=_rate_limit_identity(scope, headers),
        limit=limit,
        window_seconds=_rate_limit_window_seconds(),
    )

    if result.degraded:
        try:
            object_logs.append_object_log(
                "object_rate_limit",
                "WARNING",
                "rate limiter degraded (storage unavailable); "
                f"failing {'open' if result.allowed else 'closed'} for this request",
                base_dir=_data_dir(),
            )
        except Exception:
            pass

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


async def _handle_admin_storage(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    """GET /admin/storage: compaction-observability stats for every
    append-mode collection (docs/storage-modes.md "Compaction").

    Must stay fast regardless of collection size or cache/sidecar state --
    passes allow_fold=False so no single collection's stats call can turn
    this into an O(file) scan; a collection whose cheap sources (warm
    _RECORDS_CACHE, coherent id->offset sidecar) can't answer comes back
    as an "estimated" entry (file_bytes only) instead of triggering a full
    fold. See object_records.append_collection_stats.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Admin storage requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    append_collections = object_records.list_append_collection_stats(
        base_dir=_data_dir(),
        allow_fold=False,
    )
    await _send_json(
        send,
        {"status": "ok", "append_collections": append_collections},
    )


async def _handle_admin_collection_compact(
    send,
    collection: str,
    headers: dict[str, str],
) -> None:
    """POST /admin/collections/{collection}/compact: run
    object_records.compact_collection under the same gates that protect
    other mutating admin record routes (admin token, then the same
    collection-level write-permission check _record_write_denied_before_
    lookup applies to record create/update/delete -- there is no single
    record here, so the collection-level check is the whole check, same
    as it is for those routes before their own per-record lookup runs).

    404 for an unknown collection, 400 for a collection not currently in
    append storage mode (nothing to compact -- compact_collection itself
    tolerates being called on a classic collection as a no-op, but this
    endpoint reports that as a client error instead of a silent 200, so
    "I asked to compact X" never comes back "ok" for a collection that
    was never compactable to begin with).
    """
    if await _record_write_denied_before_lookup(
        send,
        headers,
        object_permissions.UPDATE,
        collection=collection,
    ):
        return

    try:
        mode_stats = object_records.append_collection_stats(collection, base_dir=_data_dir())
    except object_collections.InvalidCollectionNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return

    if mode_stats is None:
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Collection is not in append storage mode: {collection}",
            },
            status=400,
        )
        return

    started_at = time.perf_counter()
    try:
        summary = object_records.compact_collection(collection, base_dir=_data_dir())
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not compact collection: {exc}"},
            status=500,
        )
        return
    duration_ms = (time.perf_counter() - started_at) * 1000

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            **summary,
            "duration_ms": duration_ms,
        },
    )


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


async def _handle_admin_object_override(
    send,
    method: str,
    object_id: str,
    headers: dict[str, str],
) -> None:
    """Create or remove an override that shadows a package object by id.

    Overrides are the conflict-free customization path (Rule 2 in
    docs/upgrade-and-customization.md): creating an override here copies the
    *current package* source into the override root so execution and source
    reads/writes resolve to the override from now on, while install/upgrade
    logic keeps operating on the pristine package copy (see
    get_base_object_roots()). This endpoint requires DBBASIC_OVERRIDES_DIR to
    be configured; when it is unset the whole override subsystem is inert.
    """
    if method not in ("POST", "DELETE"):
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    # Creating/removing an override mutates what a source read/write and
    # execution resolve to, so it is gated exactly like a direct source
    # write (admin token + DBBASIC_ENABLE_SOURCE_WRITES).
    gate_error = _source_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if not validate_object_id(object_id):
        await _send_json(send, {"status": "error", "error": f"Invalid object id: {object_id}"}, status=400)
        return

    if get_override_root() is None:
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Overrides are not enabled; set {object_namespace.OVERRIDES_DIR_ENV}",
            },
            status=400,
        )
        return

    dest = object_namespace.override_path(object_id)
    if dest is None:
        await _send_json(
            send,
            {"status": "error", "error": f"Object id does not support overrides: {object_id}"},
            status=400,
        )
        return

    if method == "POST":
        try:
            source = object_source.get_object_source(object_id, get_base_object_roots())
        except InvalidObjectIdError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
            return
        except object_source.ObjectSourceNotFoundError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return

        if dest.exists():
            await _send_json(
                send,
                {"status": "error", "error": f"Override already exists: {object_id}"},
                status=409,
            )
            return

        _write_bytes_atomic(dest, source.encode("utf-8"))
        await _send_json(
            send,
            {
                "status": "ok",
                "object_id": object_id,
                "override_path": str(dest),
                "created": True,
            },
        )
        return

    # DELETE: remove the override so resolution falls back to the package copy.
    if not dest.is_file():
        await _send_json(
            send,
            {"status": "error", "error": f"No override exists: {object_id}"},
            status=404,
        )
        return

    dest.unlink()
    await _send_json(
        send,
        {
            "status": "ok",
            "object_id": object_id,
            "removed": True,
        },
    )


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


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


async def _handle_admin_reconciles(
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
        f"Reconcile listing requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    reconciles = object_reconciles.list_reconciles(
        base_dir=_data_dir(),
        status=_optional_query_text(query, "status"),
        package=_optional_query_text(query, "package"),
    )
    await _send_json(
        send,
        {
            "status": "ok",
            "reconciles": reconciles,
            "count": len(reconciles),
        },
    )


async def _handle_admin_reconcile(
    send,
    method: str,
    reconcile_id: str,
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Reconcile detail requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if not object_reconciles.validate_reconcile_id(reconcile_id):
        await _send_json(send, {"status": "error", "error": f"Invalid reconcile id: {reconcile_id}"}, status=400)
        return

    reconcile = object_reconciles.get_reconcile(reconcile_id, base_dir=_data_dir())
    if reconcile is None:
        await _send_json(send, {"status": "error", "error": f"Reconcile not found: {reconcile_id}"}, status=404)
        return

    await _send_json(send, {"status": "ok", "reconcile": reconcile})


async def _handle_admin_reconcile_resolve(
    send,
    method: str,
    reconcile_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(
        headers,
        f"Reconcile resolution requires {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    # Resolving "take theirs" can write to live object source, so it needs the
    # same gate as any other source write, in addition to the admin gate above.
    gate_error = _source_write_gate_error(headers)
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if not object_reconciles.validate_reconcile_id(reconcile_id):
        await _send_json(send, {"status": "error", "error": f"Invalid reconcile id: {reconcile_id}"}, status=400)
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    choice = payload.get("choice")
    if not isinstance(choice, str) or choice not in object_reconciles.RECONCILE_CHOICES:
        await _send_json(
            send,
            {
                "status": "error",
                "error": "Request JSON field 'choice' must be one of: "
                + ", ".join(object_reconciles.RECONCILE_CHOICES),
            },
            status=400,
        )
        return

    try:
        reconcile = object_reconciles.resolve_reconcile(
            reconcile_id,
            choice,
            base_dir=_data_dir(),
            object_roots=get_base_object_roots(),
            resolved_at=_utc_timestamp(),
        )
    except object_source.ObjectSourceNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except (InvalidObjectIdError, object_schemas.InvalidSchemaNameError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", "reconcile": reconcile})


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

    if method == "POST" and len(parts) == 2 and parts[1] == "compact":
        await _handle_admin_collection_compact(send, parts[0], headers)
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
                "/admin/collections/{collection}/compact",
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
        "overrides": {
            "enabled": get_override_root() is not None,
            "env": object_namespace.OVERRIDES_DIR_ENV,
        },
        "packages": {
            "available": True,
            "can_install": _env_enabled(PACKAGE_INSTALLS_ENABLED_ENV),
            "can_restore": _env_enabled(PACKAGE_RESTORE_ENABLED_ENV),
            "install_env": PACKAGE_INSTALLS_ENABLED_ENV,
        },
        "backups": {
            "available": True,
            "can_create": True,
            "can_download": True,
            "can_preview": True,
            "can_restore": False,
            **_backup_schedule_payload(),
        },
        "storage": {
            "append_collections": len(
                object_schemas.list_append_storage_collections(base_dir=_data_dir())
            ),
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
        "filtering": {
            "enabled": _filtering_enabled(),
            "env": FILTERING_ENABLED_ENV,
        },
        "event_handlers": {
            "enabled": object_handlers.handlers_enabled(),
            "env": object_handlers.HANDLERS_ENABLED_ENV,
        },
        "worker_pool": {
            "enabled": _worker_pool_size() > 0,
            "size": _worker_pool_size(),
            "env": object_worker_pool.WORKER_POOL_SIZE_ENV,
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
    packages = _list_all_packages()
    return [_admin_package_summary(package) for package in packages]


def _admin_package_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    package_id = str(package["id"])
    plan = object_packages.dry_run_package(
        package_id,
        root=_root_for_package(package_id),
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
        packages = _list_all_packages()
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
                root=_root_for_package(package_id),
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
                    root=_root_for_package(package_id),
                ),
                "provenance": object_packages.package_status(
                    package_id,
                    root=_root_for_package(package_id),
                    base_dir=_data_dir(),
                    object_roots=get_base_object_roots(),
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
            root=_root_for_package(package_id),
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
            root=_root_for_package(package_id),
            base_dir=_data_dir(),
            object_roots=get_base_object_roots(),
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
        payload = _parse_post_payload(body, query, headers)
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
        if body.strip() and _is_form_content_type(headers):
            payload: dict[str, Any] = _form_fields_payload(body)
        elif body.strip() and _is_multipart_content_type(headers):
            payload = object_multipart.parse_multipart(body, headers.get("content-type", ""))
        elif body.strip():
            payload = _parse_json_body(body)
        else:
            payload = dict(query)
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

    object_handlers.invalidate()

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

    object_handlers.invalidate()

    methods, method_warnings = object_source.source_method_report(code)
    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Code updated to version {version_id}",
            "version_id": version_id,
            "object_id": object_id,
            "methods": methods,
            "warnings": method_warnings,
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
            pool = await _get_worker_pool()
            if pool is not None:
                result = await pool.execute(
                    execution_request,
                    timeout_seconds=timeout_seconds,
                )
            else:
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

    _append_ops_execution_error(object_id, method, result, headers)
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

    object_handlers.invalidate()

    _append_source_change_log(
        object_id,
        action="source_create",
        version_id=version_id,
        actor=author,
        message=message,
        correlation_id=correlation_id,
        details={"description": _payload_text(payload, "description", "")},
    )

    methods, method_warnings = object_source.source_method_report(code)
    await _send_json(
        send,
        {
            "status": "ok",
            "message": f"Object created: {object_id}",
            "object_id": object_id,
            "version_id": version_id,
            "methods": methods,
            "warnings": method_warnings,
            "correlation_id": object_correlation.current_correlation_id(),
        },
        status=201,
    )


USER_FILES_ENABLED_ENV = "DBBASIC_ENABLE_USER_FILES"
USER_FILES_QUOTA_ENV = "DBBASIC_USER_FILES_QUOTA_BYTES"
DEFAULT_USER_FILES_QUOTA = 104_857_600
USER_FILES_COLLECTION = "files"
_INLINE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
)


async def _handle_user_file_upload(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Accept one multipart upload; bytes to disk, metadata to the files collection.

    The metadata record is the authority: owner_id comes from the session
    (unspoofable), size and content type are measured server-side, and the
    create is permission-checked like any record write. Quotas count real
    bytes on disk.
    """
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    if not _env_enabled(USER_FILES_ENABLED_ENV):
        await _send_json(
            send,
            {"status": "error", "error": f"User files are disabled. Set {USER_FILES_ENABLED_ENV}=true."},
            status=403,
        )
        return
    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send, {"status": "error", "error": "File uploads require a signed-in session."}, status=401
        )
        return
    if (
        _session_cookie_token(headers)
        and not _authorization_token(headers)
        and not _cookie_request_origin_allowed(headers)
    ):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
            status=403,
        )
        return

    content_type = headers.get("content-type", "")
    if not object_multipart.is_multipart_content_type(content_type):
        await _send_json(
            send,
            {"status": "error", "error": "Upload must be multipart/form-data with a file field."},
            status=400,
        )
        return
    try:
        payload = object_multipart.parse_multipart(body, content_type)
    except object_multipart.InvalidMultipartError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    uploads = payload.get("_files") or {}
    upload = uploads.get("file") or next(iter(uploads.values()), None)
    if upload is None:
        await _send_json(
            send, {"status": "error", "error": "Upload needs a file form field."}, status=400
        )
        return

    content = base64.b64decode(upload.get("content_base64", ""))
    quota = _env_int(USER_FILES_QUOTA_ENV, DEFAULT_USER_FILES_QUOTA)
    used = object_user_files.usage_bytes(session.user_id, base_dir=_data_dir())
    if used + len(content) > quota:
        await _send_json(
            send,
            {
                "status": "error",
                "error": f"Storage quota exceeded: {used + len(content)} of {quota} bytes.",
                "code": "quota_exceeded",
            },
            status=413,
        )
        return

    file_id = object_ids.new_uuid4()
    record = {
        "id": file_id,
        "filename": (upload.get("filename") or "unnamed")[:255],
        "content_type": upload.get("content_type") or "application/octet-stream",
        "size": str(len(content)),
        "description": str(payload.get("description") or "")[:300],
        "is_public": str(payload.get("is_public") or "false"),
        "owner_id": session.user_id,
    }
    if payload.get("project_id"):
        record["project_id"] = str(payload["project_id"])

    permission_check = await _authorize_collection_write(
        send,
        headers,
        object_permissions.CREATE,
        collection=USER_FILES_COLLECTION,
        method="POST",
        record=record,
        gate_message=f"Collection record writes require {ADMIN_TOKEN_ENV}.",
    )
    if permission_check is None:
        return

    try:
        stored = object_records.create_collection_record(
            USER_FILES_COLLECTION, record, base_dir=_data_dir(), actor=_record_change_actor(headers)
        )
    except object_collections.CollectionNotFoundError:
        await _send_json(
            send,
            {"status": "error", "error": "The files collection is not installed (app-files package)."},
            status=404,
        )
        return
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    object_user_files.save_file(session.user_id, file_id, content, base_dir=_data_dir())
    await _send_json(send, {"status": "ok", "file": stored, "url": f"/api/files/{file_id}"}, status=201)


async def _handle_user_file(
    send,
    method: str,
    file_id: str,
    headers: dict[str, str],
) -> None:
    """Serve or delete one file, authorized against its metadata record."""
    if not _env_enabled(USER_FILES_ENABLED_ENV):
        await _send_json(
            send,
            {"status": "error", "error": f"User files are disabled. Set {USER_FILES_ENABLED_ENV}=true."},
            status=403,
        )
        return
    if "/" in file_id or not file_id:
        await _send_json(send, {"status": "error", "error": "Invalid file id"}, status=400)
        return

    try:
        record = object_records.get_collection_record(
            USER_FILES_COLLECTION, file_id, base_dir=_data_dir()
        )
    except (LookupError, ValueError):
        record = None

    action = object_permissions.READ if method == "GET" else object_permissions.DELETE
    if _permission_checks_enabled():
        permission_check = await _collection_permission_check(
            send,
            headers,
            action,
            collection=USER_FILES_COLLECTION,
            method=method,
            record=record,
        )
        if permission_check is None:
            return
    else:
        gate_error = _admin_token_gate_error(headers, f"Files require {ADMIN_TOKEN_ENV}.")
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    if record is None:
        await _send_json(send, {"status": "error", "error": "File not found"}, status=404)
        return
    owner_id = record.get("owner_id", "")

    if method == "GET":
        try:
            content = object_user_files.read_file(owner_id, file_id, base_dir=_data_dir())
        except (object_user_files.UserFileNotFoundError, object_user_files.InvalidUserFileError):
            await _send_json(send, {"status": "error", "error": "File bytes missing"}, status=404)
            return
        content_type = record.get("content_type") or "application/octet-stream"
        disposition = "inline" if content_type in _INLINE_CONTENT_TYPES else "attachment"
        filename = (record.get("filename") or "download").replace('"', "")
        await _send_bytes(
            send,
            content,
            content_type=content_type,
            extra_headers=[
                (b"content-disposition", f'{disposition}; filename="{filename}"'.encode("latin-1", "replace")),
                (b"x-content-type-options", b"nosniff"),
            ],
        )
        return

    if method == "DELETE":
        if (
            _session_cookie_token(headers)
            and not _authorization_token(headers)
            and not _cookie_request_origin_allowed(headers)
        ):
            await _send_json(
                send,
                {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
                status=403,
            )
            return
        try:
            object_records.delete_collection_record(
                USER_FILES_COLLECTION, file_id, base_dir=_data_dir(), actor=_record_change_actor(headers)
            )
        except (LookupError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
            return
        try:
            object_user_files.delete_file(owner_id, file_id, base_dir=_data_dir())
        except object_user_files.InvalidUserFileError:
            pass
        await _send_json(send, {"status": "ok", "deleted": True, "file_id": file_id})
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _send_bytes(
    send,
    content: bytes,
    *,
    content_type: str,
    status: int = 200,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    response_headers = [
        (b"content-type", content_type.encode("latin-1", "replace")),
        (b"content-length", str(len(content)).encode("ascii")),
    ]
    response_headers.extend(extra_headers or [])
    await send(
        {"type": "http.response.start", "status": status, "headers": response_headers}
    )
    await send({"type": "http.response.body", "body": content})


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


AI_CHAT_ENABLED_ENV = "DBBASIC_ENABLE_AI_CHAT"
AI_CHAT_TIMEOUT_ENV = "DBBASIC_AI_TIMEOUT_SECONDS"
AI_DEFAULT_MODEL_ENV = "DBBASIC_AI_DEFAULT_MODEL"
DEFAULT_AI_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_AI_TIMEOUT_SECONDS = 60.0
AI_PRICES_COLLECTION = "ai_prices"
AI_USAGE_COLLECTION = "ai_usage"


async def _handle_ai_chat(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """One AI conversation turn using the caller's stored provider key.

    The model may call a caller-chosen subset of the MCP tool catalog;
    every tool call dispatches through the server's own routing with the
    caller's credentials, so the AI can do exactly what the caller could
    do directly — nothing more — and it all lands in the audit trail.
    """
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if not _env_enabled(AI_CHAT_ENABLED_ENV):
        await _send_json(
            send,
            {"status": "error", "error": f"AI chat is disabled. Set {AI_CHAT_ENABLED_ENV}=true."},
            status=403,
        )
        return

    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send,
            {"status": "error", "error": "AI chat requires a signed-in session."},
            status=401,
        )
        return

    cookie_token = _session_cookie_token(headers)
    if cookie_token and not _authorization_token(headers) and not _cookie_request_origin_allowed(headers):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
            status=403,
        )
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    message = payload.get("message")
    model = payload.get("model") or os.environ.get(AI_DEFAULT_MODEL_ENV, DEFAULT_AI_MODEL)
    tool_names = payload.get("tools") or []
    system = payload.get("system")

    if not isinstance(tool_names, list) or not all(
        isinstance(name, str) and name for name in tool_names
    ):
        await _send_json(
            send,
            {"status": "error", "error": "tools must be a list of MCP tool names"},
            status=400,
        )
        return

    try:
        service, model_name = object_ai.split_model(model)
        provider_tools = object_ai.mcp_tools_as_provider_tools(
            tool_names, object_mcp.TOOLS, service=service
        )
        max_rounds_raw = payload.get("max_rounds", object_ai.DEFAULT_MAX_ROUNDS)
        if isinstance(max_rounds_raw, bool) or not isinstance(max_rounds_raw, int):
            raise object_ai.InvalidChatRequestError("max_rounds must be an integer")
        max_rounds = max_rounds_raw
    except (object_ai.InvalidChatRequestError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    key = object_service_keys.get_service_key(session.user_id, service, base_dir=_data_dir())
    if key is None:
        await _send_json(
            send,
            {
                "status": "error",
                "error": (
                    f"No {service} key stored. Set one with "
                    f"PUT /identity/users/{session.user_id}/service-keys."
                ),
            },
            status=400,
        )
        return

    authorization = headers.get("authorization") or f"Bearer {cookie_token}"
    allowed_tools = set(tool_names)
    loop = asyncio.get_running_loop()
    timeout = _env_float(AI_CHAT_TIMEOUT_ENV, DEFAULT_AI_TIMEOUT_SECONDS)

    def send_http(url: str, request_headers, request_body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url, data=request_body, method="POST", headers=dict(request_headers)
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in allowed_tools:
            return {"http_status": 400, "response": {"error": f"Tool not offered: {name}"}}
        try:
            tool_method, tool_path, tool_query, tool_body = object_mcp.tool_route(
                name, dict(arguments or {})
            )
        except ValueError as exc:
            return {"http_status": 400, "response": {"error": str(exc)}}
        future = asyncio.run_coroutine_threadsafe(
            _internal_request(
                tool_method, tool_path, tool_query, tool_body, authorization=authorization
            ),
            loop,
        )
        status, response_payload = future.result(timeout=timeout)
        return {"http_status": status, "response": response_payload}

    try:
        result = await asyncio.to_thread(
            object_ai.run_chat,
            send_http=send_http,
            dispatch_tool=dispatch_tool,
            service=service,
            model=model_name,
            key=key,
            message=message,
            system=system if isinstance(system, str) else None,
            tools=provider_tools,
            history=payload.get("history"),
            max_rounds=max_rounds,
        )
    except object_ai.InvalidChatRequestError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_ai.AIProviderError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=502)
        return
    except (TimeoutError, OSError) as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"AI provider request failed: {exc}"},
            status=502,
        )
        return

    # Cost recording is server-authoritative: the client never sees this
    # write and cannot omit or falsify it. Prices live in the ai_prices
    # collection (editable live, never hardcoded) -- a missing price row
    # still records the turn's tokens, just with a null cost, so pricing
    # gaps never fail a chat that otherwise succeeded.
    usage = result.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    price_row = object_ai.select_price_row(
        _read_ai_prices_or_empty(), provider=service, model=model_name
    )
    cost_cents = object_ai.compute_cost_cents(tokens_in, tokens_out, price_row)
    usage["cost_cents"] = cost_cents

    usage_record = {
        "id": object_ids.new_uuid4(),
        "owner_id": session.user_id,
        "provider": service,
        "model": model_name,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }
    if cost_cents is not None:
        usage_record["cost_cents"] = cost_cents
    object_records.create_collection_record(
        AI_USAGE_COLLECTION,
        usage_record,
        base_dir=_data_dir(),
        roots=get_object_roots(),
        actor=session.user_id,
    )

    await _send_json(send, {"status": "ok", "model": model, **result})


def _read_ai_prices_or_empty() -> list[dict[str, str]]:
    try:
        return object_records.read_collection_records(
            AI_PRICES_COLLECTION, base_dir=_data_dir(), roots=get_object_roots()
        )
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return []


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


TTS_ENABLED_ENV = "DBBASIC_ENABLE_TTS"
TTS_MAX_CHARS = 800


async def _handle_tts(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Speak one line of text through whatever engine is installed.

    Mirrors _handle_ai_chat's gating exactly: a feature flag first (off by
    default), then a signed-in session, then the same cross-origin cookie
    check every authenticated POST here uses. Successful audio is cached
    to disk (object_tts.cache_path) so a repeated line costs one
    synthesis, not one per request.
    """
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if not _env_enabled(TTS_ENABLED_ENV):
        await _send_json(
            send,
            {"status": "error", "error": f"TTS is disabled. Set {TTS_ENABLED_ENV}=true."},
            status=403,
        )
        return

    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send, {"status": "error", "error": "TTS requires a signed-in session."}, status=401
        )
        return

    cookie_token = _session_cookie_token(headers)
    if cookie_token and not _authorization_token(headers) and not _cookie_request_origin_allowed(headers):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
            status=403,
        )
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        await _send_json(send, {"status": "error", "error": "text is required"}, status=400)
        return
    if len(text) > TTS_MAX_CHARS:
        await _send_json(
            send,
            {"status": "error", "error": f"text exceeds {TTS_MAX_CHARS} characters"},
            status=413,
        )
        return

    voice = payload.get("voice")
    if voice is not None and not isinstance(voice, str):
        await _send_json(send, {"status": "error", "error": "voice must be a string"}, status=400)
        return

    try:
        audio, _from_cache = await asyncio.to_thread(
            object_tts.synthesize, text, voice, base_dir=_data_dir()
        )
    except object_tts.TTSEngineNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=503)
        return
    except object_tts.TTSNotSupportedError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=501)
        return
    except object_tts.TTSSynthesisError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=502)
        return

    await _send_bytes(send, audio, content_type="audio/wav")


READER_ENABLED_ENV = "DBBASIC_ENABLE_READER"
READER_TIMEOUT_SECONDS_ENV = "DBBASIC_READER_TIMEOUT_SECONDS"
READER_MAX_BYTES_ENV = "DBBASIC_READER_MAX_BYTES"
DEFAULT_READER_TIMEOUT_SECONDS = 10.0
DEFAULT_READER_MAX_BYTES = 2_000_000


async def _handle_read(
    send,
    method: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Fetch one URL server-side and return {title, text, links}.

    Mirrors _handle_tts's gating shape exactly: a feature flag first (off
    by default), then a signed-in session, then the same cross-origin
    cookie check every authenticated POST here uses -- the capability
    being gated is the server reaching outbound, not the fetched content
    (which is public web data either way). The global per-request rate
    limiter above already covers this path like every other route; the
    SSRF gate itself lives in object_reader and cannot be bypassed here.
    """
    if method != "POST":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if not _env_enabled(READER_ENABLED_ENV):
        await _send_json(
            send,
            {"status": "error", "error": f"Reader is disabled. Set {READER_ENABLED_ENV}=true."},
            status=403,
        )
        return

    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send, {"status": "error", "error": "Reader requires a signed-in session."}, status=401
        )
        return

    cookie_token = _session_cookie_token(headers)
    if cookie_token and not _authorization_token(headers) and not _cookie_request_origin_allowed(headers):
        await _send_json(
            send,
            {"status": "error", "error": "Cross-origin cookie writes are not allowed."},
            status=403,
        )
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        await _send_json(send, {"status": "error", "error": "url is required"}, status=400)
        return

    timeout = _env_float(READER_TIMEOUT_SECONDS_ENV, DEFAULT_READER_TIMEOUT_SECONDS)
    max_bytes = _env_int(READER_MAX_BYTES_ENV, DEFAULT_READER_MAX_BYTES)

    try:
        result = await asyncio.to_thread(
            object_reader.read_page, url.strip(), timeout=timeout, max_bytes=max_bytes
        )
    except object_reader.ReaderError as exc:
        try:
            object_logs.append_object_log(
                "object_reader", "WARNING", f"read_page refused/failed: {exc}", base_dir=_data_dir()
            )
        except Exception:
            pass
        await _send_json(send, {"status": "error", "error": str(exc)}, status=502)
        return

    try:
        object_logs.append_object_log(
            "object_reader",
            "INFO",
            f"read_page fetched {url.strip()} -> {result['final_url']} "
            f"truncated={result['truncated']} actor={session.user_id}",
            base_dir=_data_dir(),
        )
    except Exception:
        pass

    await _send_json(send, {"status": "ok", **result})


async def _handle_search(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    search_query = (query.get("q") or "").strip()
    if not search_query:
        await _send_json(
            send,
            {"status": "error", "error": "Search requires a q query parameter."},
            status=400,
        )
        return

    try:
        limit = _query_int(
            query,
            "limit",
            default=object_search.DEFAULT_COLLECTION_LIMIT,
            minimum=1,
            maximum=object_search.MAX_COLLECTION_LIMIT,
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    requested_raw = (query.get("collections") or "").strip()
    requested = {name.strip() for name in requested_raw.split(",") if name.strip()} or None

    subject = None
    policy = None
    enforced = False
    if _permission_checks_enabled():
        subject = _permission_subject(headers)
        enforced = _permission_enforcement_enabled()
        try:
            policy = object_permission_store.load_policy(_data_dir())
        except ValueError as exc:
            await _send_json(
                send,
                {"status": "error", "error": f"Permission policy is invalid: {exc}"},
                status=500,
            )
            return
    else:
        gate_error = _admin_token_gate_error(headers, f"Search requires {ADMIN_TOKEN_ENV}.")
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return

    results: dict[str, list[dict[str, str]]] = {}
    warnings: list[str] = []
    total = 0
    for summary in object_schemas.list_schemas(base_dir=_data_dir()):
        name = summary["name"]
        if requested is not None and name not in requested:
            continue
        try:
            schema = object_schemas.get_schema(name, base_dir=_data_dir())
        except (LookupError, ValueError):
            continue

        try:
            config = object_search.search_config(schema)
        except object_search.InvalidSearchConfigError as exc:
            warnings.append(f"{name}: {exc}")
            continue
        if config is None:
            continue

        if subject is not None and policy is not None:
            decision = object_permissions.check_permission(
                subject,
                object_permissions.READ,
                policy=policy,
                collection=name,
            )
            _append_permission_audit_entry(
                action=object_permissions.READ,
                object_id=None,
                collection=name,
                method="GET",
                subject=subject,
                enforced=enforced,
                decision=decision,
            )
            if enforced and not decision.allowed:
                continue

        try:
            records = object_records.read_collection_records(name, base_dir=_data_dir())
        except (
            object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError,
        ):
            records = []
        except ValueError:
            continue

        if subject is not None and policy is not None and enforced:
            records = _filter_records_for_permission(
                records,
                collection=name,
                subject=subject,
                policy=policy,
            )

        matches = object_search.search_records(records, search_query, config, limit=limit)
        results[name] = matches
        total += len(matches)

    payload: dict[str, Any] = {
        "status": "ok",
        "query": search_query,
        "limit": limit,
        "results": results,
        "total_count": total,
    }
    if warnings:
        payload["warnings"] = warnings
    await _send_json(send, payload)


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

    enforced = permission_check is not None and permission_check["enforced"]

    # 58's field filter (plan/vocabulary/58-query-filter-spec.md). Parsed
    # here, against the schema now known-valid (the read above already
    # confirmed the collection exists), but APPLIED below only after the
    # permission row filter has already run -- see the ordering note there.
    normalized_where: dict[str, list[dict[str, Any]]] = {}
    if _filtering_enabled():
        is_filterable = (
            _filterable_field_predicate(
                collection,
                subject=permission_check["subject"],
                policy=permission_check["policy"],
                decision=permission_check["decision"],
            )
            if enforced
            else _always_filterable
        )
        try:
            normalized_where = _parse_collection_record_filters(
                query,
                collection=collection,
                is_filterable=is_filterable,
            )
        except FilterParamError as exc:
            await _send_json(
                send,
                {
                    "status": "error",
                    "error": str(exc),
                    "code": "invalid_filter",
                    "param": exc.param,
                },
                status=400,
            )
            return
    elif any(key not in FILTER_RESERVED_PARAMS for key in query):
        _note_filtering_disabled_once()

    if enforced:
        try:
            # The permission row filter: applied first, unconditionally,
            # over every record read above. redact=False: field redaction
            # is deferred past the field filter below, so a filter on a
            # field an admin may filter but not see (schema-hidden) can
            # still match against its real value -- see
            # _filter_records_for_permission's docstring.
            records = _filter_records_for_permission(
                records,
                collection=collection,
                subject=permission_check["subject"],
                policy=permission_check["policy"],
                redact=False,
            )
        except ValueError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return

    if normalized_where:
        # The field filter: an ADDITIONAL AND over the row filter's own
        # output only (never the original `records`), which is what
        # structurally guarantees it can narrow the readable set but
        # never widen it -- see object_records.filter_records.
        records = object_records.filter_records(records, normalized_where)

    if enforced:
        try:
            records = _redact_records_for_permission(
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


def _collection_has_owner_field(collection: str) -> bool:
    """True if the collection's schema declares an owner_id field.

    Used to decide whether a create should be stamped with the session's
    user as the owner. Schemaless or unknown collections return False (no
    field to stamp).
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=_data_dir())
    except Exception:
        return False
    return any(f.get("name") == "owner_id" for f in (schema.get("fields") or []))


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

    # Ownership is server-authoritative. For a signed-in create on a collection
    # that declares an owner_id field, stamp it from the session rather than
    # trusting the client -- a client could otherwise create a record owned by
    # someone else, or (as raw-API and agent/MCP creates did) leave it empty,
    # which silently breaks every owner-scoped rule and owner-gated transition
    # on that record. Admin-token writes (no session) keep any explicit
    # owner_id so seeding and migration can set ownership deliberately.
    session = _current_identity_session(headers)
    if session is not None and session.user_id and _collection_has_owner_field(collection):
        record_payload = {**record_payload, "owner_id": session.user_id}

    try:
        record = object_records.create_collection_record(
            collection,
            record_payload,
            base_dir=_data_dir(),
            actor=_record_change_actor(headers),
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

    # object_records.create_collection_record already durably appended the
    # attributed change (universal attribution); this only re-reads it so
    # realtime/handler/event-bus fan-out can use the real persisted
    # change_id/timestamp. A read failure here is not fatal -- the change
    # itself is already safely on disk -- so it only costs this one
    # best-effort publish.
    change = _record_change_for_publish(collection, record["id"])
    if change is not None:
        _publish_record_change_event(change, record=record)

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

    # 63: fingerprint the FULL stored record, before any field-visibility
    # policy trims it -- the write-side precondition (update_collection_record,
    # inside the lock) compares against the full surfaced row, so the token
    # a caller reads here must be computed over the same full record for the
    # two to agree. A caller who can't see a field still gets a `_rev` that
    # depends on it, which only ever fails CLOSED (a hidden-field change 409s
    # them conservatively); the hash discloses nothing about the field's value.
    rev = object_records.compute_record_rev(record)

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
            # Sibling metadata, never merged into record fields (must never
            # collide with a schema field or appear on write-back).
            object_records.REV_FIELD: rev,
        },
    )


async def _handle_collection_record_update(
    send,
    collection: str,
    record_id: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    if await _record_write_denied_before_lookup(
        send,
        headers,
        object_permissions.UPDATE,
        collection=collection,
    ):
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

    # 63: resolve the optimistic-concurrency precondition. HTTP callers send
    # it as an `If-Match` header; the MCP bridge (no header channel) sends it
    # as a reserved `expected_rev` body key. Both are stripped here so neither
    # -- nor a `_rev` a client round-tripped back from a full-record read --
    # is ever treated as a field write. Header wins if both are present.
    expected_rev = headers.get("if-match")
    if isinstance(changes, dict):
        body_expected = changes.pop("expected_rev", None)
        changes.pop(object_records.REV_FIELD, None)
        if expected_rev is None and isinstance(body_expected, str):
            expected_rev = body_expected
    if expected_rev is not None:
        expected_rev = expected_rev.strip() or None
    if not _concurrency_enabled():
        expected_rev = None  # flag off: precondition ignored, last-write-wins

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
            actor=_record_change_actor(headers),
            transition_subject=permission_check["subject"],
            expected_rev=expected_rev,
        )
    except object_records.RecordNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except object_records.VersionConflictError as exc:
        # 63: the record changed since the caller read it -- no write, no
        # side effects. The caller re-GETs to see current state (and its new
        # `_rev`) and retries; the server does not echo the current row here.
        await _send_json(
            send,
            {"status": "error", "error": str(exc), "code": "conflict"},
            status=409,
        )
        return
    except object_records.TransitionNotAllowedError as exc:
        await _send_json(
            send,
            {"status": "error", "error": str(exc), "code": "forbidden"},
            status=403,
        )
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

    # See the matching comment in _handle_collection_record_create: the
    # attributed change is already durably appended by
    # object_records.update_collection_record; this just re-reads it for
    # publish.
    change = _record_change_for_publish(collection, record_id)
    if change is not None:
        _publish_record_change_event(change, record=record)

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
            # 63: the new fingerprint after this write -- a caller doing a
            # read-modify-write loop uses it as the If-Match for its next PUT
            # without a separate GET round-trip.
            object_records.REV_FIELD: object_records.compute_record_rev(record),
        },
    )


async def _handle_collection_record_delete(
    send,
    collection: str,
    record_id: str,
    headers: dict[str, str],
) -> None:
    if await _record_write_denied_before_lookup(
        send,
        headers,
        object_permissions.DELETE,
        collection=collection,
    ):
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
            actor=_record_change_actor(headers),
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

    # See the matching comment in _handle_collection_record_create: the
    # attributed change is already durably appended by
    # object_records.delete_collection_record; this just re-reads it for
    # publish.
    change = _record_change_for_publish(collection, record_id)
    if change is not None:
        _publish_record_change_event(change, record=record)

    await _send_json(
        send,
        {
            "status": "ok",
            "collection": collection,
            "record": record,
            "deleted": True,
        },
    )


async def _record_write_denied_before_lookup(
    send,
    headers: dict[str, str],
    action: str,
    *,
    collection: str,
) -> bool:
    """Deny unauthorized record writes before any record existence lookup.

    Without this, a 404 for a missing record would leak record-id existence to
    callers who are not allowed to write at all. Subjects allowed at the
    collection level (including row-filtered allows) proceed to the normal
    record-aware authorization.
    """
    if not _permission_enforcement_enabled():
        gate_error = _admin_token_gate_error(
            headers,
            f"Collection record writes require {ADMIN_TOKEN_ENV}.",
        )
        if gate_error is not None:
            status, message = gate_error
            await _send_json(send, {"status": "error", "error": message}, status=status)
            return True
        return False

    subject = _permission_subject(headers)
    try:
        policy = object_permission_store.load_policy(_data_dir())
        decision = object_permissions.check_permission(
            subject,
            action,
            policy=policy,
            collection=collection,
        )
    except ValueError:
        return False

    if decision.allowed:
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


async def _handle_schema_meta(send, method: str, collection: str) -> None:
    """Public schema metadata for building UIs (form/list generators).

    Returns the *structure* only — field types, labels, enums, relations,
    validation, forms, views, search — never data. Records stay
    permission-gated; knowing a collection's field shape is not a leak, and
    it lets any surface (web, anonymous public pages, agents) render itself
    from the schema.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    try:
        schema = object_schemas.get_schema(collection, base_dir=_data_dir())
    except object_schemas.InvalidSchemaNameError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except (LookupError, ValueError):
        await _send_json(send, {"status": "error", "error": "Schema not found"}, status=404)
        return

    meta = {key: schema.get(key) for key in ("name", "title", "fields", "forms", "views", "search", "flow")}
    await _send_json(send, {"status": "ok", "schema": meta})


USER_PREFS_COLLECTION = "user_prefs"
FEATURE_FLAGS_COLLECTION = "feature_flags"
FLAG_PREF_PREFIX = "flag:"
_PREF_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _valid_pref_key(key: str) -> bool:
    """Route-safe key charset: no slashes, no whitespace, bounded length."""
    return isinstance(key, str) and bool(_PREF_KEY_RE.fullmatch(key))


def _read_user_prefs_or_empty() -> list[dict[str, str]]:
    try:
        return object_records.read_collection_records(
            USER_PREFS_COLLECTION, base_dir=_data_dir(), roots=get_object_roots()
        )
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return []


def _read_feature_flags_or_empty() -> list[dict[str, str]]:
    try:
        return object_records.read_collection_records(
            FEATURE_FLAGS_COLLECTION, base_dir=_data_dir(), roots=get_object_roots()
        )
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return []


def _user_prefs_map(user_id: str) -> dict[str, str]:
    """Collapse one user's own user_prefs rows to a key -> value map."""
    prefs: dict[str, str] = {}
    for row in _read_user_prefs_or_empty():
        if row.get("owner_id") == user_id and row.get("key"):
            prefs[row["key"]] = row.get("value", "")
    return prefs


def _find_user_pref(user_id: str, key: str) -> dict[str, str] | None:
    for row in _read_user_prefs_or_empty():
        if row.get("owner_id") == user_id and row.get("key") == key:
            return row
    return None


def _upsert_user_pref(user_id: str, key: str, value: str) -> dict[str, str]:
    """Create or update the caller's own user_prefs row for ``key``.

    ``user_id`` must come from the session-derived subject, never from
    client input -- this is the only thing that keeps prefs owner-scoped.
    """
    existing = _find_user_pref(user_id, key)
    if existing is not None:
        return object_records.update_collection_record(
            USER_PREFS_COLLECTION,
            existing["id"],
            {"value": value},
            base_dir=_data_dir(),
            roots=get_object_roots(),
            actor=user_id,
        )
    return object_records.create_collection_record(
        USER_PREFS_COLLECTION,
        {
            "id": object_ids.new_uuid4(),
            "owner_id": user_id,
            "key": key,
            "value": value,
        },
        base_dir=_data_dir(),
        roots=get_object_roots(),
        actor=user_id,
    )


def _resolve_flags(user_id: str | None) -> dict[str, str]:
    """Effective flag values: instance feature_flags overlaid by the
    caller's own ``flag:<name>`` user_prefs entries.

    Resolution order is user override -> instance value, per
    docs/upgrade-and-customization.md Rule 5. Anonymous callers (user_id is
    None) only ever see the instance-wide values.
    """
    flags: dict[str, str] = {}
    for row in _read_feature_flags_or_empty():
        flag = row.get("flag")
        if flag:
            flags[flag] = row.get("value", "")
    if user_id is not None:
        for row in _read_user_prefs_or_empty():
            if row.get("owner_id") != user_id:
                continue
            key = row.get("key") or ""
            if key.startswith(FLAG_PREF_PREFIX):
                flags[key[len(FLAG_PREF_PREFIX):]] = row.get("value", "")
    return flags


async def _handle_prefs(send, method: str, headers: dict[str, str]) -> None:
    """GET /prefs -> the caller's own user_prefs rows collapsed to a map.

    Session-scoped by construction: the owner filter always comes from the
    caller's own identity, so this never needs the general collection
    permission engine. Anonymous callers get an empty map rather than a
    401 -- reading your own (empty) preferences is harmless.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    subject = _permission_subject(headers)
    prefs = _user_prefs_map(subject.user_id) if subject.user_id is not None else {}
    await _send_json(send, {"status": "ok", "prefs": prefs})


async def _handle_pref(
    send,
    method: str,
    key: str,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """GET/PUT one preference for the caller, by key.

    Never accepts owner_id/user_id from the client -- the row is always
    looked up and written against the session-derived subject.
    """
    if method not in {"GET", "PUT"}:
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    if not _valid_pref_key(key):
        await _send_json(send, {"status": "error", "error": "Invalid preference key"}, status=400)
        return

    subject = _permission_subject(headers)

    if method == "GET":
        row = _find_user_pref(subject.user_id, key) if subject.user_id is not None else None
        if row is None:
            await _send_json(send, {"status": "error", "error": "Preference not found"}, status=404)
            return
        await _send_json(send, {"status": "ok", "key": key, "value": row.get("value", "")})
        return

    if subject.user_id is None:
        await _send_json(
            send,
            {"status": "error", "error": "Setting preferences requires a signed-in session."},
            status=401,
        )
        return

    try:
        payload = _parse_json_body(body)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    value = payload.get("value")
    if not isinstance(value, str):
        await _send_json(
            send,
            {"status": "error", "error": "Request JSON field 'value' must be a string"},
            status=400,
        )
        return

    try:
        _upsert_user_pref(subject.user_id, key, value)
    except (
        object_collections.InvalidCollectionNameError,
        object_records.InvalidRecordIdError,
        object_records.InvalidRecordPayloadError,
        ValueError,
    ) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    except object_collections.CollectionNotFoundError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=404)
        return
    except OSError as exc:
        await _send_json(
            send,
            {"status": "error", "error": f"Could not save preference: {exc}"},
            status=500,
        )
        return

    await _send_json(send, {"status": "ok", "key": key, "value": value})


async def _handle_flags(send, method: str, headers: dict[str, str]) -> None:
    """GET /api/flags -> effective feature flags for the caller.

    Public/session-aware like /api/schema: works anonymously (instance
    values only) and overlays the caller's own per-user overrides when
    signed in.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    subject = _permission_subject(headers)
    flags = _resolve_flags(subject.user_id)
    await _send_json(send, {"status": "ok", "flags": flags})


async def _handle_activity(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    """GET /api/activity -> the caller's own recent activity feed.

    A thin read-only wrapper over object_activity.recent_activity, scoped to
    the signed-in caller's own changes (see that module's docstring for why
    v1 only does "your activity"). Unlike /prefs, which hands anonymous
    callers an empty map because reading your own (empty) preferences is
    harmless, a feed endpoint rejects anonymous callers outright -- there is
    no legitimate empty-but-still-answerable case here, only "no session".
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    session = _current_identity_session(headers)
    if session is None:
        await _send_json(
            send,
            {"status": "error", "error": "Activity requires a signed-in session."},
            status=401,
        )
        return

    try:
        limit = _query_int(query, "limit", default=50, minimum=1, maximum=200)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    entries = object_activity.recent_activity(
        base_dir=_data_dir(), actor=session.user_id, limit=limit
    )
    await _send_json(send, {"status": "ok", "activity": entries})


async def _handle_stock_levels(send, method: str, headers: dict[str, str]) -> None:
    """GET /api/stock -> Stage-7 get_stock_levels: the caller's on-hand stock,
    folded from stock_moves (object_stock.stock_levels). Owner-scoped -- the
    same fold the site_stock page uses, exposed as JSON for the MCP verb. A
    missing stock_moves collection or an anonymous caller returns an empty,
    non-error summary (nothing to fold), never a 500.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    subject = _permission_subject(headers)
    if not subject.user_id:
        await _send_json(send, {"status": "ok", "levels": [], "totals": [], "authenticated": False})
        return
    try:
        levels = object_stock.stock_levels(base_dir=_data_dir(), owner=subject.user_id)
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        levels = {"levels": [], "totals": []}
    except (OSError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    await _send_json(send, {"status": "ok", **levels})


async def _handle_finance_summary(send, method: str, headers: dict[str, str]) -> None:
    """GET /api/finance/summary -> Stage-7 get_finance_summary: the caller's
    trial balance (per-account debit/credit totals over POSTED journals),
    folded by object_finance.trial_balance. Owner-scoped, same report the
    site_trial_balance page renders, as JSON for the MCP verb. No journals /
    anonymous -> empty rows, never a 500.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return
    subject = _permission_subject(headers)
    if not subject.user_id:
        await _send_json(send, {"status": "ok", "rows": [], "authenticated": False})
        return
    try:
        rows = object_finance.trial_balance(base_dir=_data_dir(), owner=subject.user_id)
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        rows = []
    except (OSError, ValueError) as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return
    await _send_json(send, {"status": "ok", "rows": rows})


async def _handle_feed(
    send,
    method: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    """GET /api/feed -> 64's composed, permission-gated follow-graph read.

    For the signed-in caller V: recent PUBLIC content authored by the
    accounts V follows, newest-first (plan/vocabulary/64-feed-spec.md).
    v1 is a pure filtered-read COMPOSITION -- two existing permission-gated
    /collections/{c}/records reads (58), riding V's own forwarded
    credentials through _internal_request -- never a direct read of the
    record layer. That is what guarantees the core privacy invariant: a
    followed account's PRIVATE content can never enter the feed, because
    every source read is subject to that collection's own row-filtered
    permission rule (e.g. articles' `public read where is_public=true`)
    exactly as if V had queried it directly. Following someone grants no
    additional read access -- it only adds an `owner_id IN (followed set)`
    predicate on top of the row filter that already applies.

    No 401 for an anonymous caller (unlike /api/activity above): the spec's
    Surfaces section says the feed is simply "not rendered" for anonymous
    visitors, and Degradation asks for an empty, non-error response in
    every degraded case (feed disabled, no viewer, no follows) -- so every
    early-return here is a 200 with an empty `items` list plus a flag
    saying why, never a 4xx/5xx.
    """
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    subject = _permission_subject(headers)
    viewer_id = subject.user_id
    if not viewer_id:
        await _send_json(
            send,
            {"status": "ok", "items": [], "count": 0, "authenticated": False},
        )
        return

    if not _feed_enabled():
        await _send_json(
            send,
            {"status": "ok", "items": [], "count": 0, "enabled": False},
        )
        return

    try:
        limit = _query_int(query, "limit", default=50, minimum=1, maximum=200)
        offset = _query_int(query, "offset", default=0, minimum=0)
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    # Forward V's own credentials to every sub-read: a bearer/admin token
    # header if present, else the session cookie re-expressed as a bearer
    # token (_internal_request only carries an Authorization header, no
    # cookie jar) -- see _permission_identity for the same cookie-or-token
    # resolution order used to build `subject` above.
    authorization = headers.get("authorization") or ""
    if not authorization:
        cookie_token = _session_cookie_token(headers)
        if cookie_token:
            authorization = f"Bearer {cookie_token}"

    follows_query = urllib.parse.urlencode([("follower_id.eq", viewer_id), ("limit", "500")])
    follows_status, follows_payload = await _internal_request(
        "GET",
        "/collections/follows/records",
        follows_query,
        b"",
        authorization=authorization,
    )

    following_ids: list[str] = []
    if follows_status == 200 and isinstance(follows_payload, dict):
        for row in follows_payload.get("records") or []:
            following_id = row.get("following_id") if isinstance(row, dict) else None
            if following_id and following_id not in following_ids:
                following_ids.append(following_id)

    # 58's `in` cap (FILTER_IN_MAX_VALUES) directly bounds how many followed
    # accounts one feed read can scan. Over the cap: truncate to the first
    # N and say so, rather than silently dropping accounts or raising 58's
    # cap on this call site's behalf -- the spec's Storage section names
    # this exact cap-hit as the forcing function for the materialized
    # feed_items path, not something to paper over here.
    truncated_following = len(following_ids) > FILTER_IN_MAX_VALUES
    if truncated_following:
        following_ids = following_ids[:FILTER_IN_MAX_VALUES]

    response: dict[str, Any] = {"status": "ok", "authenticated": True}
    if truncated_following:
        response["truncated_following"] = True

    if not following_ids:
        response.update({"items": [], "count": 0})
        await _send_json(send, response)
        return

    items: list[dict[str, Any]] = []
    for source in _feed_sources():
        pairs = [(f"{source['owner_field']}.in", ",".join(following_ids))]
        if source["visibility_field"] and source["visibility_true_value"] is not None:
            pairs.append(
                (f"{source['visibility_field']}.eq", str(source["visibility_true_value"]))
            )
        pairs.append(("limit", "200"))
        source_status, source_payload = await _internal_request(
            "GET",
            f"/collections/{source['collection']}/records",
            urllib.parse.urlencode(pairs),
            b"",
            authorization=authorization,
        )
        if source_status != 200 or not isinstance(source_payload, dict):
            # One bad/missing/disallowed source degrades to fewer sources,
            # never an error -- same isolation 14's rollup definitions use
            # for "one bad definition is logged and skipped", applied here
            # to feed sources (spec's Degradation section).
            continue
        for record in source_payload.get("records") or []:
            if not isinstance(record, dict):
                continue
            items.append(
                {
                    "source_collection": source["collection"],
                    "source_id": record.get(source["link_field"]) or record.get("id"),
                    "author_id": record.get(source["owner_field"]),
                    "time": record.get(source["time_field"]) or "",
                    "summary": _feed_item_summary(record, source["summary_fields"]),
                    "record": record,
                }
            )

    # Step 3 of the spec's read shape: merge every source's already-small,
    # already-filtered rows by time_field, newest first -- no server sort
    # param exists for /collections/{c}/records (58's own Open Questions),
    # so this handler does the k-way merge itself.
    items.sort(key=lambda item: item["time"], reverse=True)
    total = len(items)
    window = items[offset : offset + limit]
    response.update({"items": window, "count": len(window), "total": total})
    await _send_json(send, response)


def _feed_enabled() -> bool:
    value = os.environ.get(FEED_ENABLED_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


def _feed_sources() -> list[dict[str, Any]]:
    """Discover feed-source collections via schema metadata (64's `blocks.feed`).

    A collection opts into the feed by declaring `blocks.feed` in its
    schema (plan/vocabulary/64-feed-spec.md's Parameterization section) --
    additive-opt-in, same posture as every other block key in this
    vocabulary; nothing is swept in by default.

    `blocks` is a first-class schema metadata key (whitelisted in
    object_schemas._normalize_schema alongside `flow`/`views`), so it
    survives normalization and install and is read through the ordinary,
    cached get_schema path -- never a raw side-channel file read.
    """
    sources: list[dict[str, Any]] = []
    try:
        summaries = object_schemas.list_schemas(base_dir=_data_dir())
    except (OSError, ValueError):
        return sources

    for summary in summaries:
        if summary.get("source") != "manual":
            continue  # blocks.feed can only be declared in a manual schema
        name = summary.get("name")
        if not name or not object_schemas.validate_schema_name(name):
            continue
        try:
            raw = object_schemas.get_schema(name, base_dir=_data_dir())
        except (object_schemas.SchemaNotFoundError, OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue

        blocks = raw.get("blocks")
        feed_block = blocks.get("feed") if isinstance(blocks, dict) else None
        if not isinstance(feed_block, dict):
            continue

        owner_field = feed_block.get("owner_field")
        time_field = feed_block.get("time_field")
        if not isinstance(owner_field, str) or not owner_field:
            continue
        if not isinstance(time_field, str) or not time_field:
            continue

        summary_fields = feed_block.get("summary_fields")
        if not isinstance(summary_fields, list) or not summary_fields:
            views = raw.get("views")
            summary_fields = views.get("list_fields") if isinstance(views, dict) else None
        if not isinstance(summary_fields, list):
            summary_fields = []
        summary_fields = [field for field in summary_fields if isinstance(field, str)]

        link_field = feed_block.get("link_field")
        if not isinstance(link_field, str) or not link_field:
            link_field = "id"

        visibility_field = feed_block.get("visibility_field")
        if not isinstance(visibility_field, str) or not visibility_field:
            visibility_field = None

        sources.append(
            {
                "collection": name,
                "owner_field": owner_field,
                "visibility_field": visibility_field,
                "visibility_true_value": feed_block.get("visibility_true_value"),
                "time_field": time_field,
                "summary_fields": summary_fields,
                "link_field": link_field,
            }
        )
    return sources


def _feed_item_summary(record: dict[str, Any], summary_fields: list[str]) -> str:
    parts = [str(record[field]).strip() for field in summary_fields if record.get(field)]
    return " — ".join(part for part in parts if part)


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


async def _handle_admin_ops(
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
        f"Ops events require {ADMIN_TOKEN_ENV}.",
    )
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        events = object_ops_log.read_events(
            base_dir=_data_dir(),
            limit=_query_int(query, "limit", default=100, minimum=1, maximum=1000),
            kind=_optional_query_text(query, "kind"),
            event=_optional_query_text(query, "event"),
            identifier=_optional_query_text(query, "identifier"),
        )
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
            "events": events,
            "count": len(events),
        },
    )


BACKUP_SCHEDULE_ENV = "DBBASIC_BACKUP_SCHEDULE"


def _backup_schedule_payload() -> dict[str, Any]:
    """Automatic backups are a config option; report whether one is set.

    Scheduled runs are driven by an external timer (systemd/cron) calling
    `object_backup.py create`; DBBASIC_BACKUP_SCHEDULE records the operator's
    intent so clients can show it. On-demand create/download always work.
    """
    schedule = os.environ.get(BACKUP_SCHEDULE_ENV, "").strip()
    return {"scheduled": bool(schedule), "schedule": schedule or None, "env": BACKUP_SCHEDULE_ENV}


async def _handle_admin_backups(
    send,
    method: str,
    headers: dict[str, str],
) -> None:
    """List runtime backups (GET) or create one on demand (POST). Admin only."""
    gate_error = _admin_token_gate_error(headers, f"Backups require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    if method == "GET":
        try:
            backups = object_backup_index.list_backups(data_dir=_data_dir())
        except (OSError, ValueError) as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return
        await _send_json(
            send,
            {
                "status": "ok",
                "backups": backups,
                "count": len(backups),
                "schedule": _backup_schedule_payload(),
            },
        )
        return

    if method == "POST":
        try:
            backup = object_backup_index.create_backup(data_dir=_data_dir())
        except object_backup.BackupError as exc:
            await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
            return
        except OSError as exc:
            await _send_json(
                send, {"status": "error", "error": f"Could not create backup: {exc}"}, status=500
            )
            return
        _append_ops_auth_event(event="backup_created", identifier=_record_change_actor(headers),
                               label=str(backup.get("id")))
        await _send_json(send, {"status": "ok", "backup": backup}, status=201)
        return

    await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)


async def _handle_admin_backup_download(
    send,
    method: str,
    backup_id: str,
    headers: dict[str, str],
) -> None:
    """Stream one backup archive. Admin only — it contains all runtime data."""
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(headers, f"Backups require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        path = object_backup_index.backup_path(backup_id, data_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    if not path.is_file():
        await _send_json(send, {"status": "error", "error": "Backup not found"}, status=404)
        return

    try:
        content = path.read_bytes()
    except OSError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=500)
        return

    _append_ops_auth_event(event="backup_downloaded", identifier=_record_change_actor(headers),
                           label=backup_id)
    await _send_bytes(
        send,
        content,
        content_type="application/gzip",
        extra_headers=[
            (b"content-disposition", f'attachment; filename="{backup_id}"'.encode("latin-1", "replace")),
            (b"x-content-type-options", b"nosniff"),
        ],
    )


async def _handle_admin_backup_preview(
    send,
    method: str,
    backup_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    """Preview what restoring part of a backup would change. Read-only, admin only."""
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(headers, f"Backups require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        path = object_backup_index.backup_path(backup_id, data_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    if not path.is_file():
        await _send_json(send, {"status": "error", "error": "Backup not found"}, status=404)
        return

    kind = _optional_query_text(query, "kind")
    if kind != "collection":
        await _send_json(
            send, {"status": "error", "error": "unsupported preview kind"}, status=400
        )
        return

    name = _optional_query_text(query, "name")
    if not name:
        await _send_json(
            send, {"status": "error", "error": "Query parameter 'name' is required"}, status=400
        )
        return

    try:
        preview = object_backup_index.preview_collection(backup_id, name, data_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", "preview": preview})


async def _handle_admin_backup_record(
    send,
    method: str,
    backup_id: str,
    query: dict[str, str],
    headers: dict[str, str],
) -> None:
    """Pull one record's backup-vs-live status, without restoring. Admin only."""
    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    gate_error = _admin_token_gate_error(headers, f"Backups require {ADMIN_TOKEN_ENV}.")
    if gate_error is not None:
        status, message = gate_error
        await _send_json(send, {"status": "error", "error": message}, status=status)
        return

    try:
        path = object_backup_index.backup_path(backup_id, data_dir=_data_dir())
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return
    if not path.is_file():
        await _send_json(send, {"status": "error", "error": "Backup not found"}, status=404)
        return

    collection = _optional_query_text(query, "collection")
    if not collection:
        await _send_json(
            send,
            {"status": "error", "error": "Query parameter 'collection' is required"},
            status=400,
        )
        return

    record_id = _optional_query_text(query, "id")
    if not record_id:
        await _send_json(
            send, {"status": "error", "error": "Query parameter 'id' is required"}, status=400
        )
        return

    try:
        record = object_backup_index.preview_record(
            backup_id, collection, record_id, data_dir=_data_dir()
        )
    except ValueError as exc:
        await _send_json(send, {"status": "error", "error": str(exc)}, status=400)
        return

    await _send_json(send, {"status": "ok", "record": record})


def _append_ops_execution_error(
    object_id: str,
    method: str,
    result: object_execution.ObjectExecutionResult,
    headers: dict[str, str],
) -> None:
    try:
        subject = _permission_subject(headers)
        object_ops_log.append_event(
            object_ops_log.EXECUTION_ERROR,
            {
                "object_id": object_id,
                "method": method,
                "error_type": result.error.type if result.error is not None else None,
                "message": (result.error.message if result.error is not None else "")[:500],
                "user_id": subject.user_id,
                "correlation_id": object_correlation.current_correlation_id(),
            },
            base_dir=_data_dir(),
        )
    except (OSError, ValueError):
        pass


def _login_lockout_attempts() -> int:
    value = os.environ.get(LOGIN_LOCKOUT_ATTEMPTS_ENV, "")
    try:
        return int(value) if value.strip() else DEFAULT_LOGIN_LOCKOUT_ATTEMPTS
    except ValueError:
        return DEFAULT_LOGIN_LOCKOUT_ATTEMPTS


def _login_lockout_window_seconds() -> int:
    value = os.environ.get(LOGIN_LOCKOUT_WINDOW_SECONDS_ENV, "")
    try:
        return int(value) if value.strip() else DEFAULT_LOGIN_LOCKOUT_WINDOW_SECONDS
    except ValueError:
        return DEFAULT_LOGIN_LOCKOUT_WINDOW_SECONDS


def _login_locked(identifier: str) -> bool:
    """Return whether recent failures lock this identifier out of login.

    Counts `login_failed` ops events for the identifier that are newer than
    the lockout window and newer than the identifier's last successful login.
    Locked attempts are recorded as `login_locked`, so an attacker cannot
    extend the lockout window by hammering a locked identifier.
    """
    attempts = _login_lockout_attempts()
    if attempts <= 0 or not identifier:
        return False

    window = _login_lockout_window_seconds()
    cutoff = datetime.now(timezone.utc).timestamp() - window
    try:
        events = object_ops_log.read_events(
            base_dir=_data_dir(),
            kind=object_ops_log.AUTH,
            identifier=identifier,
            limit=max(attempts * 3, 20),
        )
    except (OSError, ValueError):
        return False

    failures = 0
    for entry in events:
        try:
            timestamp = datetime.fromisoformat(
                str(entry.get("timestamp", "")).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            continue
        if timestamp < cutoff:
            break
        event = entry.get("event")
        if event == "login_succeeded":
            break
        if event == "login_failed":
            failures += 1
            if failures >= attempts:
                return True
    return False


def _append_ops_auth_event(
    event: str,
    *,
    identifier: str | None = None,
    user_id: str | None = None,
    method: str | None = None,
    label: str | None = None,
) -> None:
    try:
        object_ops_log.append_event(
            object_ops_log.AUTH,
            {
                "event": event,
                "identifier": identifier,
                "user_id": user_id,
                "auth_method": method,
                "label": label,
                "correlation_id": object_correlation.current_correlation_id(),
            },
            base_dir=_data_dir(),
        )
    except (OSError, ValueError):
        pass


_METRICS_SNAPSHOT_LOCK = threading.Lock()
_LAST_METRICS_SNAPSHOT = 0.0


def _maybe_append_metrics_snapshot() -> None:
    """Persist one metrics history row per interval while traffic flows."""
    global _LAST_METRICS_SNAPSHOT
    interval = _metrics_snapshot_seconds()
    if interval <= 0:
        return

    now = time.time()
    with _METRICS_SNAPSHOT_LOCK:
        if now - _LAST_METRICS_SNAPSHOT < interval:
            return
        _LAST_METRICS_SNAPSHOT = now

    try:
        metrics = _metrics.snapshot()
        system = _system_snapshot()
        object_metrics_history.append_snapshot(
            {
                "uptime_seconds": metrics["uptime_seconds"],
                "requests": metrics["total_requests"],
                "errors": metrics["total_errors"],
                "rps": metrics["requests_per_second"],
                "error_rate": metrics["error_rate"],
                "p50_ms": metrics["response_time_ms"].get("p50"),
                "p95_ms": metrics["response_time_ms"].get("p95"),
                "cpu_percent": system.get("cpu_percent"),
                "memory_used_percent": (system.get("memory") or {}).get("used_percent"),
                "disk_used_percent": (system.get("disk") or {}).get("used_percent"),
            },
            base_dir=_data_dir(),
        )
    except (OSError, ValueError, KeyError):
        pass


def _metrics_snapshot_seconds() -> int:
    value = os.environ.get(METRICS_SNAPSHOT_SECONDS_ENV, "")
    try:
        return int(value) if value.strip() else DEFAULT_METRICS_SNAPSHOT_SECONDS
    except ValueError:
        return DEFAULT_METRICS_SNAPSHOT_SECONDS


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

    cpu_percent = _cpu_percent_snapshot()
    if cpu_percent is not None:
        snapshot["cpu_percent"] = cpu_percent

    disk = _disk_snapshot()
    if disk is not None:
        snapshot["disk"] = disk

    return snapshot


_CPU_TIMES_LOCK = threading.Lock()
_LAST_CPU_TIMES: tuple[int, int] | None = None


def _cpu_percent_snapshot() -> float | None:
    """CPU utilization since the previous metrics call, from /proc/stat deltas."""
    global _LAST_CPU_TIMES
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None

    parts = first_line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(part) for part in parts[1:]]
    except ValueError:
        return None

    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)

    with _CPU_TIMES_LOCK:
        previous = _LAST_CPU_TIMES
        _LAST_CPU_TIMES = (idle, total)

    if previous is None:
        return None

    idle_delta = idle - previous[0]
    total_delta = total - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)


def _disk_snapshot() -> dict[str, Any] | None:
    """Capacity of the filesystem holding the runtime data directory."""
    try:
        usage = shutil.disk_usage(_data_dir())
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return {
        "total_gb": round(usage.total / 1024**3, 2),
        "used_gb": round(usage.used / 1024**3, 2),
        "used_percent": round(usage.used / usage.total * 100, 2),
    }


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


def _parse_post_payload(
    body: bytes,
    query: dict[str, str],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not body.strip():
        return dict(query)

    if headers is not None and _is_form_content_type(headers):
        payload: dict[str, Any] = _form_fields_payload(body)
        for key, value in query.items():
            payload.setdefault(key, value)
        return payload

    if headers is not None and _is_multipart_content_type(headers):
        payload = object_multipart.parse_multipart(body, headers.get("content-type", ""))
        for key, value in query.items():
            payload.setdefault(key, value)
        return payload

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**query, "body": body}

    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")

    for key, value in query.items():
        payload.setdefault(key, value)

    return payload


def _is_form_content_type(headers: dict[str, str]) -> bool:
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    return content_type == "application/x-www-form-urlencoded"


def _is_multipart_content_type(headers: dict[str, str]) -> bool:
    return object_multipart.is_multipart_content_type(headers.get("content-type", ""))


def _form_fields_payload(body: bytes) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Form body must be valid UTF-8 form encoding") from exc
    return {name: value for name, value in pairs}


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


class FilterParamError(ValueError):
    """One 58 field-filter query param failed validation.

    Covers every case 58's Degradation section calls out as a 400: an
    unknown field, a non-filterable (hidden) field, an unknown operator,
    an oversize `in` list, or a value that fails schema type validation.
    Carries the offending param name so the 400 response can point at it
    rather than making the caller guess which of several filter params
    was the problem.
    """

    def __init__(self, param: str, message: str):
        super().__init__(message)
        self.param = param


def _split_filter_param(raw_key: str) -> tuple[str, str]:
    """Split one query param name into (field, op).

    58's Encoding: `field=value` is an implicit `eq`; `field.op=value` is
    the dotted-suffix form (`status.in=open,assigned`,
    `created_at.gte=2026-07-01`). Schema field names never contain a dot
    (object_schemas._FIELD_NAME_RE), so splitting on the LAST dot is
    unambiguous.
    """
    if "." in raw_key:
        field_name, _, op = raw_key.rpartition(".")
        return field_name, op.strip().lower()
    return raw_key, "eq"


def _schema_field_defs(collection: str) -> dict[str, dict[str, Any]]:
    """Return {field_name: field_def} for a collection's declared schema.

    Empty for a schemaless/derived collection (no manual schema file) --
    callers treat that the same way every other schema-optional check in
    this codebase does: permissive, since there is no declared field list
    to validate a filter's field name or hidden-ness against.
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=_data_dir())
    except (object_schemas.InvalidSchemaNameError, object_schemas.SchemaNotFoundError):
        return {}
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return {}
    return {
        field["name"]: field
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }


def _always_filterable(_name: str) -> bool:
    return True


def _filterable_field_predicate(
    collection: str,
    *,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    decision: object_permissions.PermissionDecision,
) -> Callable[[str], bool]:
    """Return a predicate: may `subject` use field `name` in a 58 filter?

    58's Permissions Posture: filterable fields = readable fields. A
    field this subject can't read is not filterable either, closing the
    inference channel where filtering a hidden field would probe its
    value via which already-readable rows come back. A field is unreadable
    here for either of two independent reasons, both checked:

    - the matched row-permission rule scopes reads to an explicit
      `fields` allow-list, or denies specific fields via `denied_fields`
      (`decision.fields`/`decision.denied_fields`, from the same READ
      decision `_filter_records_for_permission` uses for row/field
      redaction -- this is the general, record-independent shape of that
      decision, from `_collection_permission_check`);
    - the schema marks the field `hidden` for this subject
      (`object_field_permissions.field_access`, the same check
      `redact_record` applies per record on the way out).

    Admins bypass both, mirroring `check_permission`'s own admin
    short-circuit (an admin's decision already carries no row_filter/
    fields/denied_fields, and admins see every field here too).
    """
    if object_permissions.subject_has_admin_role(subject, policy):
        return _always_filterable

    denied = set(decision.denied_fields)
    allowed = set(decision.fields) if decision.fields is not None else None

    for name, field in _schema_field_defs(collection).items():
        access = object_field_permissions.field_access(
            field, subject=subject, policy=policy, record=None
        )
        if access == object_field_permissions.HIDDEN:
            denied.add(name)

    def is_filterable(name: str) -> bool:
        if name in denied:
            return False
        if allowed is not None and name not in allowed:
            return False
        return True

    return is_filterable


def _parse_collection_record_filters(
    query: dict[str, str],
    *,
    collection: str,
    is_filterable: Callable[[str], bool],
) -> dict[str, list[dict[str, Any]]]:
    """Parse a collection-records GET query string into a normalized 58
    field filter.

    Returns {field: [condition, ...]} -- a LIST of conditions per field
    (each `object_permissions.filter_condition`-shaped) so a range
    (`created_at.gte=X&created_at.lte=Y`, two different query params that
    both name `created_at`) ANDs both conditions instead of the second
    silently overwriting the first. Raises FilterParamError -- caught by
    the caller and turned into a structured 400 -- on any of 58's
    Degradation cases: unknown field, non-filterable field, unknown
    operator, an oversize `in` list, or a value that fails schema type
    validation. Reserved params (FILTER_RESERVED_PARAMS) are skipped, not
    treated as filters.
    """
    field_defs = _schema_field_defs(collection)
    normalized: dict[str, list[dict[str, Any]]] = {}

    for raw_key, raw_value in query.items():
        if raw_key in FILTER_RESERVED_PARAMS:
            continue

        field_name, op = _split_filter_param(raw_key)
        if op not in object_permissions.FILTER_OPERATORS:
            raise FilterParamError(
                raw_key, f"Unknown filter operator '{op}' on field '{field_name}'"
            )
        if field_defs and field_name not in field_defs:
            raise FilterParamError(raw_key, f"Unknown filter field '{field_name}'")
        if not is_filterable(field_name):
            raise FilterParamError(
                raw_key, f"Field '{field_name}' is not filterable for this subject"
            )

        field_def = field_defs.get(field_name)
        try:
            if op == "in":
                raw_values = [item.strip() for item in raw_value.split(",")]
                if not raw_values or any(item == "" for item in raw_values):
                    raise FilterParamError(
                        raw_key,
                        "Filter operator 'in' requires a non-empty comma-separated list",
                    )
                if len(raw_values) > FILTER_IN_MAX_VALUES:
                    raise FilterParamError(
                        raw_key,
                        f"Filter operator 'in' supports at most {FILTER_IN_MAX_VALUES} values",
                    )
                value: Any = tuple(
                    object_records.normalize_filter_value(field_def, op, item)
                    for item in raw_values
                )
            else:
                value = object_records.normalize_filter_value(field_def, op, raw_value)
        except object_records.InvalidRecordPayloadError as exc:
            raise FilterParamError(raw_key, str(exc)) from exc

        normalized.setdefault(field_name, []).append(
            object_permissions.filter_condition(op, value)
        )

    return normalized


def _filter_records_for_permission(
    records: list[dict[str, str]],
    *,
    collection: str,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
    redact: bool = True,
) -> list[dict[str, str]]:
    """Return the records `subject` may read from `collection` -- the
    permission ROW filter, applied first and unconditionally (58's
    Permissions Posture) ahead of any field filter a caller adds on top.

    `redact=False` skips the per-record field-redaction step (schema
    hidden-field stripping plus the matched rule's fields/denied_fields)
    and returns the row-permitted records with every field still present.
    A 58 field filter MUST be applied to that unredacted form -- see
    _handle_collection_records_get -- so a filter an admin is allowed to
    use on an otherwise-hidden field (_filterable_field_predicate) can
    still see the value to match against; call
    _redact_records_for_permission once filtering is done to get the same
    caller-facing shape a plain read (redact=True, the default) already
    produces in one pass.
    """
    allowed_records: list[dict[str, str]] = []
    for record in records:
        decision = object_permissions.check_permission(
            subject,
            object_permissions.READ,
            policy=policy,
            collection=collection,
            record=record,
        )
        if not decision.allowed:
            continue
        if redact:
            allowed_records.append(
                _apply_record_field_policy(
                    record,
                    decision,
                    collection=collection,
                    subject=subject,
                    policy=policy,
                )
            )
        else:
            allowed_records.append(dict(record))
    return allowed_records


def _redact_records_for_permission(
    records: list[dict[str, str]],
    *,
    collection: str,
    subject: object_permissions.PermissionSubject,
    policy: object_permissions.PermissionPolicy,
) -> list[dict[str, str]]:
    """Apply field redaction to an already row-permitted record list.

    The other half of what _filter_records_for_permission's default
    (redact=True) does in one pass, split out so a 58 field filter can run
    BETWEEN row-permission filtering and redaction -- see there.
    """
    redacted: list[dict[str, str]] = []
    for record in records:
        decision = object_permissions.check_permission(
            subject,
            object_permissions.READ,
            policy=policy,
            collection=collection,
            record=record,
        )
        redacted.append(
            _apply_record_field_policy(
                record,
                decision,
                collection=collection,
                subject=subject,
                policy=policy,
            )
        )
    return redacted


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
            subject = _with_accessible_projects(session.subject())
            return subject, "session_cookie" if cookie_token else "session_token"

    if not _env_enabled(PERMISSION_TRUST_HEADERS_ENV):
        return object_permissions.PermissionSubject.anonymous(), "anonymous"

    user_id = _optional_header_text(headers, "x-dbbasic-user-id")
    account_id = _optional_header_text(headers, "x-dbbasic-account-id")
    subject = _with_accessible_projects(
        object_permissions.PermissionSubject(
            user_id=user_id,
            account_id=account_id,
            roles=_csv_header(headers.get("x-dbbasic-roles", "")),
            subscriptions=_csv_header(headers.get("x-dbbasic-subscriptions", "")),
        )
    )
    method = "trusted_headers" if _trusted_identity_headers_present(headers) else "anonymous"
    return subject, method


PROJECT_ACCESS_COLLECTION = "project_access"
PROJECTS_COLLECTION = "projects"


def _with_accessible_projects(
    subject: object_permissions.PermissionSubject,
) -> object_permissions.PermissionSubject:
    """Resolve the subject's project grants and ownership before checks run.

    Grants live in the plain ``project_access`` records collection
    (project_id, user_id, permission), so sharing is data: browseable,
    audited, and versioned like every other record. Rules opt in with the
    ``$accessible_projects`` row-filter value; ``$owned_projects``
    resolves from the projects collection's owner_id, which is what lets
    "owners may share their own projects" stay a plain row filter.
    ``$writable_projects`` narrows accessible grants to rows whose
    ``permission`` is ``"write"``, for rules and transition guards that
    need more than read-only membership.
    """
    if subject.user_id is None:
        return subject

    access_rows = [
        record
        for record in _read_records_or_empty(PROJECT_ACCESS_COLLECTION)
        if record.get("user_id") == subject.user_id and record.get("project_id")
    ]
    project_ids = [record["project_id"] for record in access_rows]
    writable_project_ids = [
        record["project_id"] for record in access_rows if record.get("permission") == "write"
    ]
    owned_project_ids = [
        record["id"]
        for record in _read_records_or_empty(PROJECTS_COLLECTION)
        if record.get("owner_id") == subject.user_id and record.get("id")
    ]
    if not project_ids and not owned_project_ids:
        return subject
    return subject.with_projects(
        dict.fromkeys(project_ids),
        dict.fromkeys(owned_project_ids),
        dict.fromkeys(writable_project_ids),
    )


def _read_records_or_empty(collection: str) -> list[dict[str, str]]:
    try:
        return object_records.read_collection_records(collection, base_dir=_data_dir())
    except (
        object_collections.CollectionNotFoundError,
        object_collections.InvalidCollectionNameError,
        OSError,
        ValueError,
    ):
        return []


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
    payload: dict[str, Any] = {
        "user_id": subject.user_id,
        "account_id": subject.account_id,
        "roles": list(subject.roles),
        "subscriptions": list(subject.subscriptions),
        "authenticated": subject.is_authenticated,
    }
    if subject.project_ids:
        payload["project_ids"] = list(subject.project_ids)
    if subject.owned_project_ids:
        payload["owned_project_ids"] = list(subject.owned_project_ids)
    if subject.writable_project_ids:
        payload["writable_project_ids"] = list(subject.writable_project_ids)
    return payload


def _record_change_actor(headers: dict[str, str]) -> str:
    subject = _permission_subject(headers)
    if subject.user_id:
        return subject.user_id
    if subject.account_id:
        return f"account:{subject.account_id}"
    if subject.roles:
        return ",".join(subject.roles)
    return "api"


def _record_change_for_publish(collection: str, record_id: str) -> dict[str, Any] | None:
    """Re-read the change object_records just durably appended, for publish.

    object_records.create/update/delete_collection_record already emit the
    attributed change record as part of the write itself (universal
    attribution -- see object_record_changes). The HTTP handlers still want
    the real persisted change_id/timestamp/changed_fields for realtime and
    event-bus fan-out, so this fetches the newest entry back rather than
    emitting a second, separate change. Best-effort: a read failure here
    only costs the realtime/event-bus publish, not the write or its log
    entry, which are already safely on disk.
    """
    try:
        payload = object_record_changes.list_record_changes(
            collection, record_id=record_id, limit=1, base_dir=_data_dir()
        )
    except (OSError, ValueError):
        return None
    changes = payload.get("changes") or []
    return changes[0] if changes else None


def _publish_record_change_event(
    change: dict[str, Any], record: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    action = str(change.get("action", ""))
    event_type = RECORD_EVENT_TYPES.get(action)
    if event_type is None:
        return None

    collection = str(change.get("collection") or "")
    record_id = str(change.get("record_id") or "")

    # Live push overlay (independent of the durable log's on/off flag).
    _realtime_publish(collection, record_id, action, record)

    # HANDLES-declared event handler objects (Phase 5a; see
    # docs/event-hooks-decisions.md). Post-commit, best-effort, gated off by
    # default: fully guarded internally so it can never raise into the
    # write path.
    _dispatch_event_handlers(collection, record_id, action, record)

    if not _record_events_enabled():
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


def _dispatch_event_handlers(
    collection: str, record_id: str, action: str, record: dict[str, Any] | None
) -> None:
    """Fire HANDLES-declared handler objects for a committed record write.

    Post-commit, best-effort, system-objects-only (Phase 5a; see
    docs/event-hooks-decisions.md). One bad handler can never affect the
    write or any other handler: every failure mode here is caught and
    logged, never raised. Dispatch is synchronous in-process for v1; async
    dispatch is a future optimization.
    """
    try:
        if not object_handlers.handlers_enabled():
            return

        event = object_handlers.event_name(collection, action)
        if event is None:
            return

        if object_handlers.current_depth() >= object_handlers.MAX_DISPATCH_DEPTH:
            try:
                object_logs.append_object_log(
                    "object_handlers",
                    "WARNING",
                    f"Event dispatch depth limit reached for {event}; skipping handlers",
                    base_dir=_data_dir(),
                )
            except Exception:
                pass
            return

        handler_ids = object_handlers.get_handlers(event, get_object_roots())
        if not handler_ids:
            return

        # Signal-shaped payload, same posture as /ws: no full record body by
        # default. A handler that needs the record fetches it via the
        # record API using collection/record_id.
        event_payload = {
            "event": event,
            "collection": collection,
            "record_id": record_id,
            "action": action,
        }

        for handler_id in handler_ids:
            try:
                with object_handlers.dispatch_guard():
                    request = object_execution.ObjectExecutionRequest(
                        object_id=handler_id,
                        method="EVENT",
                        payload=event_payload,
                        correlation_id=object_correlation.current_correlation_id(),
                    )
                    result = object_execution.execute_object(_runtime, request)
                    _append_execution_log(result)
                    if not result.ok:
                        _append_ops_execution_error(handler_id, "EVENT", result, {})
            except Exception:
                # One bad handler must never affect the write or its peers.
                continue
    except Exception:
        return


def _record_events_enabled() -> bool:
    value = os.environ.get(RECORD_EVENTS_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


def _filtering_enabled() -> bool:
    value = os.environ.get(FILTERING_ENABLED_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


def _concurrency_enabled() -> bool:
    """63: whether the If-Match/expected_rev precondition is enforced.
    Default on; off means the precondition is ignored (never a 409) and
    writes are last-write-wins, per the spec's Degradation section.
    """
    value = os.environ.get(CONCURRENCY_ENABLED_ENV)
    if value is None:
        return True
    return value.strip().lower() in TRUE_VALUES


_FILTERING_DISABLED_LOGGED = False


def _note_filtering_disabled_once() -> None:
    """Log once (not per-request) that filter params are being ignored
    because FILTERING_ENABLED_ENV is off -- 58's Degradation section asks
    for this so an operator who flipped the flag can find out why filters
    stopped narrowing results, without a log line on every list request.
    """
    global _FILTERING_DISABLED_LOGGED
    if _FILTERING_DISABLED_LOGGED:
        return
    _FILTERING_DISABLED_LOGGED = True
    print(
        f"[dbbasic] Filtering is disabled ({FILTERING_ENABLED_ENV}=false); "
        f"field-filter query params are being ignored.",
        file=sys.stderr,
    )


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
        # Browser pages authenticate with the session cookie. Only the
        # session-admin path accepts it; the raw admin token never does.
        if session_admin_gates:
            cookie_token = _session_cookie_token(headers)
            if cookie_token is not None and _admin_session_authorized(cookie_token):
                return None
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


def _private_packages_dir() -> str:
    return os.environ.get(PRIVATE_PACKAGES_DIR_ENV, DEFAULT_PRIVATE_PACKAGES_DIR)


def _package_roots() -> list[str]:
    """Package source roots, highest precedence first. The private overlay
    (closed-source packages, gitignored -- see packages-private/README.md)
    is searched ahead of the open-core `packages/` dir, so a private package
    shadows an open one that shares its id. The private root is included only
    when it actually exists, so an open-only checkout behaves exactly as
    before."""
    roots: list[str] = []
    private = _private_packages_dir()
    if private and Path(private).is_dir():
        roots.append(private)
    roots.append(_packages_dir())
    return roots


def _root_for_package(package_id: str) -> str:
    """The root a specific package id resolves from (private overlay wins).
    Falls back to the open packages dir when neither root has it, so the
    caller still gets the normal PackageNotFoundError from downstream."""
    for root in _package_roots():
        if (Path(root) / package_id / object_packages.MANIFEST_FILE).is_file():
            return root
    return _packages_dir()


def _list_all_packages() -> list[dict[str, Any]]:
    """Package summaries merged across every root, private overlay first so a
    private package shadows an open one with the same id."""
    seen: dict[str, dict[str, Any]] = {}
    for root in _package_roots():
        for package in object_packages.list_packages(root=root):
            seen.setdefault(str(package["id"]), package)
    return list(seen.values())


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


def _worker_pool_size() -> int:
    size = _env_int(object_worker_pool.WORKER_POOL_SIZE_ENV, 0)
    return size if size > 0 else 0


async def _get_worker_pool() -> object_worker_pool.WorkerPool | None:
    """Return the shared warm-worker pool, lazily creating/resizing it.

    Disabled (size <= 0) is the default: returns None and the caller falls
    back to the existing spawn-per-request path. Reads the size fresh on
    every call so tests (and, in principle, an operator) can flip the
    feature by env var without a process restart; a pool already running at
    a different size is shut down and replaced.
    """
    global _worker_pool, _worker_pool_size_used

    size = _worker_pool_size()
    if size <= 0:
        if _worker_pool is not None:
            async with _worker_pool_lock:
                stale = _worker_pool
                _worker_pool = None
                _worker_pool_size_used = 0
            if stale is not None:
                await stale.shutdown()
        return None

    async with _worker_pool_lock:
        if _worker_pool is None or _worker_pool_size_used != size:
            stale = _worker_pool
            _worker_pool = object_worker_pool.WorkerPool(size=size)
            _worker_pool_size_used = size
        else:
            stale = None
        pool = _worker_pool

    if stale is not None:
        await stale.shutdown()
    return pool


async def _shutdown_worker_pool() -> None:
    global _worker_pool, _worker_pool_size_used

    async with _worker_pool_lock:
        pool = _worker_pool
        _worker_pool = None
        _worker_pool_size_used = 0

    if pool is not None:
        await pool.shutdown()


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
            await _shutdown_worker_pool()
            await send({"type": "lifespan.shutdown.complete"})
            return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("object_server:app", host="127.0.0.1", port=8001, reload=False)
