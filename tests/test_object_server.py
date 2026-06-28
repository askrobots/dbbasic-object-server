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


def enable_source_writes(monkeypatch, root, data_dir):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")


def auth_headers():
    return [("authorization", "Token local-dev-token")]


def update_source(object_id, code, *, author="test-api", message="Update source"):
    return request(
        f"/objects/{object_id}",
        method="PUT",
        query_string="source=true",
        body=json.dumps(
            {
                "code": code,
                "author": author,
                "message": message,
            }
        ).encode(),
        headers=auth_headers(),
    )


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


def test_get_state_returns_empty_state_for_object_without_state_file(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/basics_counter", query_string="state=true")

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "state": {},
    }


def test_get_state_reads_tsv_state(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    state_file = data_dir / "state" / "basics_counter" / "state.tsv"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        "key\tvalue\ttimestamp\n"
        "count\t3\t1710000000.1\n"
        "rate\t2.5\t1710000000.2\n"
        "name\tcounter\t1710000000.3\n"
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/basics_counter", query_string="state=true")

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "state": {
            "count": 3,
            "rate": 2.5,
            "name": "counter",
        },
    }


def test_get_state_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/missing_object", query_string="state=true")

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_state_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/bad.id", query_string="state=true")

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


def test_get_logs_returns_empty_logs_for_object_without_log_file(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/basics_counter", query_string="logs=true")

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "logs": [],
        "count": 0,
    }


def test_get_logs_reads_tsv_logs_with_level_and_limit(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    log_file = data_dir / "logs" / "basics_counter" / "log.tsv"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "entry_id\ttimestamp\tlevel\tmessage\tmethod\n"
        "a1\t2026-01-01T00:00:00\tINFO\tstarted\tGET\n"
        "a2\t2026-01-01T00:00:01\tERROR\tboom\tGET\n"
        "a3\t2026-01-01T00:00:02\tERROR\tstill bad\tGET\n"
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true&level=ERROR&limit=1",
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "logs": [
            {
                "entry_id": "a2",
                "timestamp": "2026-01-01T00:00:01",
                "level": "ERROR",
                "message": "boom",
                "method": "GET",
            }
        ],
        "count": 1,
    }


def test_get_logs_rejects_bad_limit(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/basics_counter", query_string="logs=true&limit=0")

    assert status == 400
    assert payload == {
        "status": "error",
        "error": "Query parameter 'limit' must be at least 1",
    }


def test_get_logs_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/missing_object", query_string="logs=true")

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_logs_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/bad.id", query_string="logs=true")

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


def test_get_metadata_summarizes_object_storage(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    source = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    state_file = data_dir / "state" / "basics_counter" / "state.tsv"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("key\tvalue\ttimestamp\ncount\t3\t1710000000.1\n")
    log_file = data_dir / "logs" / "basics_counter" / "log.tsv"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "a1\t2026-01-01T00:00:00\tINFO\tstarted\n"
    )
    object_versions.VersionManager(data_dir).save_version(
        "basics_counter",
        "def GET(request):\n    return {}\n",
        author="test",
        message="first",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/basics_counter", query_string="metadata=true")

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "metadata": {
            "object_id": "basics_counter",
            "source_path": "basics/counter.py",
            "owner": "system",
            "kind": "system",
            "last_modified": source.stat().st_mtime,
            "state_count": 1,
            "state_keys": ["count"],
            "log_count": 1,
            "version_count": 1,
        },
    }


def test_get_metadata_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/missing_object", query_string="metadata=true")

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_metadata_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))

    status, _, payload = request("/objects/bad.id", query_string="metadata=true")

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


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


def test_versions_endpoint_lists_history_newest_first_without_content(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    data_dir = tmp_path / "data"
    enable_source_writes(monkeypatch, root, data_dir)

    update_source(
        "basics_counter",
        "def GET(request):\n    return {'count': 1}\n",
        message="first",
    )
    update_source(
        "basics_counter",
        "def GET(request):\n    return {'count': 2}\n",
        message="second",
    )

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="versions=true&limit=10",
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["object_id"] == "basics_counter"
    assert payload["count"] == 2
    assert [version["version_id"] for version in payload["versions"]] == [2, 1]
    assert [version["message"] for version in payload["versions"]] == ["second", "first"]
    assert all("content" not in version for version in payload["versions"])

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="versions=true&limit=1",
    )

    assert status == 200
    assert payload["count"] == 1
    assert [version["version_id"] for version in payload["versions"]] == [2]


def test_versions_endpoint_rejects_bad_limit(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="versions=true&limit=0",
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Query parameter 'limit' must be at least 1"}


def test_specific_version_endpoint_returns_content(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    data_dir = tmp_path / "data"
    enable_source_writes(monkeypatch, root, data_dir)
    v1_code = "def GET(request):\n    return {'count': 1}\n"

    update_source("basics_counter", v1_code, message="first")

    status, _, payload = request("/objects/basics_counter", query_string="version=1")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["object_id"] == "basics_counter"
    assert payload["version"]["version_id"] == 1
    assert payload["version"]["content"] == v1_code
    assert payload["version"]["message"] == "first"


def test_specific_version_endpoint_returns_404_for_missing_version(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    data_dir = tmp_path / "data"
    enable_source_writes(monkeypatch, root, data_dir)

    status, _, payload = request("/objects/basics_counter", query_string="version=99")

    assert status == 404
    assert payload == {
        "status": "error",
        "error": "Version 99 not found for object basics_counter",
    }


def test_specific_version_endpoint_rejects_bad_version_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_counter", query_string="version=bad")

    assert status == 400
    assert payload == {"status": "error", "error": "Query parameter 'version' must be an integer"}


def test_rollback_requires_source_write_gate(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(
        root / "basics" / "counter.py",
        "def GET(request):\n    return {'count': 0}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ENABLE_SOURCE_WRITES", raising=False)
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "local-dev-token")

    status, _, payload = request(
        "/objects/basics_counter",
        method="POST",
        body=json.dumps({"action": "rollback", "version_id": 1}).encode(),
        headers=auth_headers(),
    )

    assert status == 403
    assert payload["status"] == "error"
    assert payload["error"].startswith("Source writes are disabled")
    assert source_path.read_text() == "def GET(request):\n    return {'count': 0}\n"


def test_rollback_versions_source_and_immediately_runs_old_code(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(
        root / "basics" / "counter.py",
        "def GET(request):\n    return {'count': 0}\n",
    )
    data_dir = tmp_path / "data"
    enable_source_writes(monkeypatch, root, data_dir)
    v1_code = "def GET(request):\n    return {'count': 1}\n"
    v2_code = "def GET(request):\n    return {'count': 2}\n"

    update_source("basics_counter", v1_code, message="first")
    update_source("basics_counter", v2_code, message="second")

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"count": 2}

    status, _, payload = request(
        "/objects/basics_counter",
        method="POST",
        body=json.dumps(
            {
                "action": "rollback",
                "version_id": 1,
                "author": "test-api",
                "message": "Rollback to first",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "message": "Rolled back to version 1",
        "version_id": 1,
        "new_version_id": 3,
        "object_id": "basics_counter",
    }
    assert source_path.read_text() == v1_code

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"count": 1}

    manager = object_versions.VersionManager(data_dir)
    assert [row["version_id"] for row in manager.get_history("basics_counter")] == [3, 2, 1]
    latest = manager.get_version("basics_counter")
    assert latest is not None
    assert latest["content"] == v1_code
    assert latest["message"] == "Rollback to first"


def test_rollback_returns_404_for_missing_version(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    data_dir = tmp_path / "data"
    enable_source_writes(monkeypatch, root, data_dir)

    status, _, payload = request(
        "/objects/basics_counter",
        method="POST",
        body=json.dumps({"action": "rollback", "version_id": 99}).encode(),
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {
        "status": "error",
        "error": "Version 99 not found for object basics_counter",
    }


def test_post_object_execution_runs_post_method_with_json_body_and_query_merge(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "echo.py",
        "def POST(request):\n    return {'request': request}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_echo",
        method="POST",
        query_string="name=query&mode=test",
        body=json.dumps({"name": "body", "count": 2}).encode(),
    )

    assert status == 200
    assert payload == {"request": {"name": "body", "count": 2, "mode": "test"}}


def test_post_object_execution_passes_raw_body_for_non_json(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "raw.py",
        "def POST(request):\n"
        "    return {'body_size': len(request['body']), 'mode': request['mode']}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_raw",
        method="POST",
        query_string="mode=raw",
        body=b"not-json",
    )

    assert status == 200
    assert payload == {"body_size": 8, "mode": "raw"}


def test_post_object_execution_rejects_non_object_json(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "poster.py", "def POST(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_poster",
        method="POST",
        body=json.dumps(["not", "an", "object"]).encode(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "JSON body must be an object"}


def test_post_object_execution_returns_405_when_post_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "getter.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_getter", method="POST", body=b"{}")

    assert status == 405
    assert payload["status"] == "error"
    assert payload["error"].startswith("Execution failed: Method POST not supported")


def test_put_object_execution_runs_put_method_with_json_body(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "profile.py",
        "def PUT(request):\n    return {'request': request}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_profile",
        method="PUT",
        query_string="query_value=ignored",
        body=json.dumps({"name": "Alice"}).encode(),
    )

    assert status == 200
    assert payload == {"request": {"name": "Alice"}}


def test_put_object_execution_uses_query_params_when_body_is_empty(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "profile.py",
        "def PUT(request):\n    return {'request': request}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_profile",
        method="PUT",
        query_string="name=Alice",
    )

    assert status == 200
    assert payload == {"request": {"name": "Alice"}}


def test_put_object_execution_rejects_invalid_json(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "profile.py", "def PUT(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_profile", method="PUT", body=b"{")

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid JSON body"}


def test_delete_object_execution_runs_delete_method_with_json_body(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "items.py",
        "def DELETE(request):\n    return {'deleted': request['id']}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request(
        "/objects/basics_items",
        method="DELETE",
        body=json.dumps({"id": "item-1"}).encode(),
    )

    assert status == 200
    assert payload == {"deleted": "item-1"}


def test_delete_object_execution_rejects_invalid_json(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "items.py", "def DELETE(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_items", method="DELETE", body=b"{")

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid JSON body"}


def test_object_execution_runs_get_method(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"count": 1}


def test_object_execution_appends_success_log(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/basics_counter")
    assert status == 200
    assert payload == {"count": 1}

    status, _, payload = request("/objects/basics_counter", query_string="logs=true")

    assert status == 200
    assert payload["count"] == 1
    log = payload["logs"][0]
    assert log["level"] == "DEBUG"
    assert log["message"] == "GET completed successfully"
    assert log["method"] == "GET"
    assert log["status"] == "success"
    assert float(log["duration_ms"]) >= 0


def test_object_execution_state_manager_persists_state(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(
        root / "basics" / "counter.py",
        "_state_manager = None\n"
        "def GET(request):\n"
        "    count = _state_manager.get('count', 0) + 1\n"
        "    _state_manager.set('count', count)\n"
        "    return {'count': count}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    assert request("/objects/basics_counter")[2] == {"count": 1}
    assert request("/objects/basics_counter")[2] == {"count": 2}

    status, _, payload = request("/objects/basics_counter", query_string="state=true")

    assert status == 200
    assert payload == {
        "status": "ok",
        "object_id": "basics_counter",
        "state": {"count": 2},
    }


def test_object_execution_logger_writes_object_logs(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(
        root / "basics" / "counter.py",
        "_logger = None\n"
        "def GET(request):\n"
        "    _logger.info('Counter was read', event='counter_read')\n"
        "    return {'ok': True}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/basics_counter")

    assert status == 200
    assert payload == {"ok": True}

    status, _, payload = request("/objects/basics_counter", query_string="logs=true")

    assert status == 200
    messages = [entry["message"] for entry in payload["logs"]]
    assert "Counter was read" in messages
    assert "GET completed successfully" in messages


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


def test_object_execution_appends_error_log(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(
        root / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/basics_broken")
    assert status == 500
    assert "RuntimeError: boom" in payload["error"]

    status, _, payload = request("/objects/basics_broken", query_string="logs=true")

    assert status == 200
    assert payload["count"] == 1
    log = payload["logs"][0]
    assert log["level"] == "ERROR"
    assert log["message"].startswith("GET failed:")
    assert log["method"] == "GET"
    assert log["status"] == "error"
    assert log["error_type"] == "ObjectMethodExecutionError"
    assert "RuntimeError: boom" in log["error"]


def test_object_execution_returns_405_when_get_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "poster.py", "def POST(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, _, payload = request("/objects/basics_poster")

    assert status == 405
    assert payload["status"] == "error"
    assert payload["error"].startswith("Execution failed: Method GET not supported")


def test_collection_non_get_methods_are_rejected():
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
