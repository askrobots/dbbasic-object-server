"""Minimal ASGI app for DBBASIC Object Server.

This is the first public server slice. It intentionally implements only
read-only endpoints while the runtime, auth, and mutation paths are extracted.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import http_api_contract
import object_source
from object_namespace import iter_object_sources, parse_user_object_id
from object_versions import InvalidObjectIdError


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
    await _read_body(receive)

    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")
    query = _parse_query(scope.get("query_string", b""))

    if path == "/health":
        await _send_json(send, {"status": "ok"})
        return

    if method != "GET":
        await _send_json(send, {"status": "error", "error": "Method not allowed"}, status=405)
        return

    if path == http_api_contract.OBJECTS_PATH:
        await _send_json(send, _list_objects_payload())
        return

    if path.startswith(f"{http_api_contract.OBJECTS_PATH}/"):
        object_id = path.removeprefix(f"{http_api_contract.OBJECTS_PATH}/")
        await _handle_object_get(send, object_id, query)
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

    await _send_json(
        send,
        {"status": "error", "error": "Object execution is not implemented yet"},
        status=501,
    )


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


def _parse_query(query_string: bytes | str) -> dict[str, str]:
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8")
    return dict(urllib.parse.parse_qsl(query_string))


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


async def _send_json(send, payload: dict[str, Any], status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
