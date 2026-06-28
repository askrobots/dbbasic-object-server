import asyncio
import json

import object_server
import object_versions


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def request(path, method="GET", query_string="", body=b"", headers=None):
    return asyncio.run(
        asgi_request(
            path,
            method=method,
            query_string=query_string,
            body=body,
            headers=headers,
        )
    )


async def asgi_request(path, method="GET", query_string="", body=b"", headers=None):
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope_headers = [(b"accept", b"application/json")]
    for name, value in headers or []:
        if isinstance(name, str):
            name = name.encode("latin-1")
        if isinstance(value, str):
            value = value.encode("latin-1")
        scope_headers.append((name, value))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string.encode("utf-8"),
        "headers": scope_headers,
    }
    await object_server.app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body_parts = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ]
    payload = b"".join(body_parts).decode("utf-8")
    return start["status"], dict(start["headers"]), json.loads(payload)


def test_health_endpoint_returns_ok():
    status, headers, payload = request("/health")

    assert status == 200
    assert headers[b"content-type"] == b"application/json; charset=utf-8"
    assert payload == {"status": "ok"}


def test_object_list_returns_existing_contract_shape(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    write_source(root / "users" / "42" / "deals.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects", query_string="format=json")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["count"] == 2
    assert payload["objects"] == [
        {
            "object_id": "basics_counter",
            "path": "basics/counter.py",
            "owner": "system",
        },
        {
            "object_id": "u_42_deals",
            "path": "users/42/deals.py",
            "owner": "42",
        },
    ]


def test_object_list_returns_empty_when_objects_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "missing"))

    status, _, payload = request("/objects", query_string="format=json")

    assert status == 200
    assert payload == {"status": "ok", "objects": [], "count": 0}


def test_get_source_returns_existing_contract_shape(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="source=true&format=json",
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "source": "def GET(request):\n    return {'count': 1}\n",
    }


def test_get_source_rejects_station_routing_for_now(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_counter@station2",
        query_string="source=true&format=json",
    )

    assert status == 400
    assert payload["status"] == "error"
    assert "Station routing is not available" in payload["error"]


def test_get_source_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))

    status, _, payload = request("/objects/missing_object", query_string="source=true&format=json")

    assert status == 404
    assert payload["status"] == "error"
    assert payload["error"] == "Object source not found: missing_object"


def test_source_update_is_disabled_by_default(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.delenv("DBBASIC_ENABLE_SOURCE_WRITES", raising=False)
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"code": "def GET(request):\n    return {'count': 1}\n"}).encode(),
        headers=[("authorization", "Token local-dev-token")],
    )

    assert status == 403
    assert payload["status"] == "error"
    assert payload["error"].startswith("Source writes are disabled")
    assert source_path.read_text() == "def GET(request):\n    return {}\n"


def test_source_update_requires_admin_token_configuration(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"code": "def GET(request):\n    return {'count': 1}\n"}).encode(),
    )

    assert status == 403
    assert payload == {"status": "error", "error": "Source writes require DBBASIC_ADMIN_TOKEN."}
    assert source_path.read_text() == "def GET(request):\n    return {}\n"


def test_source_update_requires_authorization_header(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"code": "def GET(request):\n    return {'count': 1}\n"}).encode(),
    )

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}
    assert source_path.read_text() == "def GET(request):\n    return {}\n"


def test_source_update_rejects_invalid_json_body(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=b"{",
        headers=[("authorization", "Token local-dev-token")],
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid JSON body"}
    assert source_path.read_text() == "def GET(request):\n    return {}\n"


def test_source_update_requires_code_string(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"source": "wrong field"}).encode(),
        headers=[("authorization", "Token local-dev-token")],
    )

    assert status == 400
    assert payload == {
        "status": "error",
        "error": "Request JSON field 'code' must be a string",
    }


def test_source_update_versions_source_and_immediately_runs_new_code(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(
        root / "basics" / "counter.py",
        "def GET(request):\n    return {'count': 1}\n",
    )
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")
    new_code = "def GET(request):\n    return {'count': 2}\n"

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps(
            {
                "code": new_code,
                "author": "test-api",
                "message": "Update counter",
            }
        ).encode(),
        headers=[("authorization", "Token local-dev-token")],
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "message": "Code updated to version 1",
        "version_id": 1,
        "object_id": "basics_counter",
    }
    assert source_path.read_text() == new_code

    manager = object_versions.VersionManager(data_dir)
    saved = manager.get_version("basics_counter", 1)
    assert saved is not None
    assert saved["content"] == new_code
    assert saved["author"] == "test-api"
    assert saved["message"] == "Update counter"

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"count": 2}


def test_object_execution_runs_get_method(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"count": 1}


def test_object_execution_passes_query_params_as_payload(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "echo.py",
        "def GET(request):\n    return {'query': request}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_echo", query_string="name=dan&mode=test")

    assert status == 200
    assert payload == {"query": {"name": "dan", "mode": "test"}}


def test_object_execution_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))

    status, _, payload = request("/objects/missing_object")

    assert status == 404
    assert payload == {"status": "error", "error": "Object not found: missing_object"}


def test_object_execution_returns_json_error_for_object_exception(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_broken")

    assert status == 500
    assert payload["status"] == "error"
    assert payload["error"].startswith("Execution failed: GET failed for object basics_broken")
    assert "RuntimeError: boom" in payload["error"]


def test_object_execution_returns_405_when_get_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "poster.py", "def POST(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_poster")

    assert status == 405
    assert payload["status"] == "error"
    assert payload["error"].startswith("Execution failed: Method GET not supported")


def test_non_get_methods_are_rejected_for_now():
    status, _, payload = request("/objects", method="POST", body=b"{}")

    assert status == 405
    assert payload == {"status": "error", "error": "Method not allowed"}


def test_unknown_path_returns_json_404():
    status, _, payload = request("/missing")

    assert status == 404
    assert payload == {"status": "error", "error": "Not found"}


def test_websocket_scope_closes_until_realtime_contract_is_added():
    async def run():
        messages = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            messages.append(message)

        await object_server.app({"type": "websocket", "path": "/ws/basics_counter"}, receive, send)
        return messages

    messages = asyncio.run(run())

    assert messages == [{"type": "websocket.close", "code": 1003}]


def test_lifespan_startup_and_shutdown_complete():
    async def run():
        messages = []
        incoming = iter(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )

        async def receive():
            return next(incoming)

        async def send(message):
            messages.append(message)

        await object_server.app({"type": "lifespan"}, receive, send)
        return messages

    messages = asyncio.run(run())

    assert messages == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
