"""Minimal ASGI app for DBBASIC Object Server.

This is the first public server slice. Source writes are disabled by default
while the production auth and mutation paths are extracted.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.parse
from typing import Any

import http_api_contract
import object_execution
import object_logs
import object_metadata
import object_source
import object_state
import object_versions
from object_namespace import iter_object_sources, parse_user_object_id
from object_versions import InvalidObjectIdError
from python_object_runtime import MethodNotSupportedError, PythonObjectRuntime


SOURCE_WRITES_ENV = "DBBASIC_ENABLE_SOURCE_WRITES"
ADMIN_TOKEN_ENV = "DBBASIC_ADMIN_TOKEN"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"
TRUE_VALUES = {"1", "true", "yes", "on"}

_runtime = PythonObjectRuntime()


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

    await _handle_http(scope, receive, send)


async def _handle_http(scope: dict[str, Any], receive, send) -> None:
    body = await _read_body(receive)

    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    query = _parse_query(scope.get("query_string", b""))
    headers = _parse_headers(scope.get("headers", []))

    if path == "/health":
        await _send_json(send, {"status": "ok"})
        return

    if path == http_api_contract.OBJECTS_PATH:
        if method == "GET":
            await _send_json(send, _list_objects_payload())
            return

        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if path.startswith(f"{http_api_contract.OBJECTS_PATH}/"):
        object_id = path.removeprefix(f"{http_api_contract.OBJECTS_PATH}/")
        if method == "GET":
            await _handle_object_get(send, object_id, query)
            return

        if method == "POST":
            await _handle_object_post(send, object_id, body, query, headers)
            return

        if method == "PUT" and query.get("source") == "true":
            await _handle_object_source_put(send, object_id, body, headers)
            return

        if method == "PUT":
            await _handle_object_body_method(send, object_id, "PUT", body, query)
            return

        if method == "DELETE":
            await _handle_object_body_method(send, object_id, "DELETE", body, query)
            return

        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    await _send_json(send, {"status": "error", "error": "Not found"}, status=404)


async def _handle_object_get(send, object_id: str, query: dict[str, str]) -> None:
    if "@" in object_id:
        await _send_json(
            send,
            {"status": "error", "error": "Station routing is not available in this server"},
            status=400,
        )
        return

    if query.get("versions") == "true":
        await _handle_object_versions_get(send, object_id, query)
        return

    if "version" in query:
        await _handle_object_version_get(send, object_id, query)
        return

    if query.get("state") == "true":
        await _handle_object_state_get(send, object_id)
        return

    if query.get("logs") == "true":
        await _handle_object_logs_get(send, object_id, query)
        return

    if query.get("metadata") == "true":
        await _handle_object_metadata_get(send, object_id)
        return

    if query.get("source") == "true":
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

    await _execute_object_method(send, object_id, "GET", query)


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

    await _execute_object_method(send, object_id, "POST", payload)


async def _handle_object_body_method(
    send,
    object_id: str,
    method: str,
    body: bytes,
    query: dict[str, str],
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

    await _execute_object_method(send, object_id, method, payload)


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
) -> None:
    result = object_execution.execute_object(
        _runtime,
        object_execution.ObjectExecutionRequest(
            object_id=object_id,
            method=method,
            payload=payload,
        ),
    )
    _append_execution_log(result)

    if result.ok:
        await _send_json(send, result.result)
        return

    await _send_execution_error(send, result)


def _list_objects_payload() -> dict[str, Any]:
    objects = [_object_source_payload(source) for source in iter_object_sources()]
    return {
        "status": "ok",
        "objects": objects,
        "count": len(objects),
    }


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


def _ensure_object_source_exists(object_id: str) -> None:
    object_source.get_object_source(object_id)


def _source_write_gate_error(headers: dict[str, str]) -> tuple[int, str] | None:
    if not _env_enabled(SOURCE_WRITES_ENV):
        return (
            403,
            f"Source writes are disabled. Set {SOURCE_WRITES_ENV}=true and {ADMIN_TOKEN_ENV}.",
        )

    admin_token = os.environ.get(ADMIN_TOKEN_ENV, "")
    if not admin_token:
        return (403, f"Source writes require {ADMIN_TOKEN_ENV}.")

    request_token = _authorization_token(headers)
    if request_token is None or not hmac.compare_digest(request_token, admin_token):
        return (401, "Unauthorized")

    return None


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


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return body
        if message["type"] != "http.request":
            continue

        body += message.get("body", b"")
        if not message.get("more_body", False):
            return body


async def _send_json(send, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
