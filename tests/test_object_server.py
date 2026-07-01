import asyncio
import json
import tarfile
from pathlib import Path

import object_correlation
import object_execution
import object_events
import object_ids
import object_logs
import object_package_changes
import object_packages
import object_permission_audit
import object_permission_store
import object_server
import object_state
import object_versions

TEST_ADMIN_TOKEN = "unit-test-only-admin-token"


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_records(data_dir, collection, content):
    path = data_dir / "collections" / collection / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def request(
    path,
    method="GET",
    query_string="",
    body=b"",
    headers=None,
    body_chunks=None,
    client=None,
):
    status, headers, payload = raw_request(
        path,
        method=method,
        query_string=query_string,
        body=body,
        headers=headers,
        body_chunks=body_chunks,
        client=client,
    )
    return status, headers, json.loads(payload.decode("utf-8"))


def raw_request(
    path,
    method="GET",
    query_string="",
    body=b"",
    headers=None,
    body_chunks=None,
    client=None,
):
    return asyncio.run(
        asgi_request(
            path,
            method=method,
            query_string=query_string,
            body=body,
            headers=headers,
            body_chunks=body_chunks,
            client=client,
        )
    )


async def asgi_request(
    path,
    method="GET",
    query_string="",
    body=b"",
    headers=None,
    body_chunks=None,
    client=None,
):
    messages = []
    chunks = body_chunks if body_chunks is not None else [body]
    chunk_index = 0

    async def receive():
        nonlocal chunk_index
        if chunk_index < len(chunks):
            chunk = chunks[chunk_index]
            chunk_index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": chunk_index < len(chunks),
            }
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
        "client": client or ("127.0.0.1", 12345),
    }
    await object_server.app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body_parts = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ]
    payload = b"".join(body_parts)
    return start["status"], dict(start["headers"]), payload


def enable_source_writes(monkeypatch, root, data_dir):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", TEST_ADMIN_TOKEN)


def auth_headers():
    return [("authorization", f"Token {TEST_ADMIN_TOKEN}")]


def enable_admin_token(monkeypatch):
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", TEST_ADMIN_TOKEN)


def save_permission_policy(data_dir, policy):
    object_permission_store.replace_policy(policy, data_dir)


def write_package(root, package_id, payload, files=()):
    package_dir = root / package_id
    package_dir.mkdir(parents=True)
    (package_dir / object_packages.MANIFEST_FILE).write_text(json.dumps(payload))
    for relative_path, content in files:
        write_source(package_dir / relative_path, content)
    return package_dir


def claim_limit_slot(limiter, limit):
    token = limiter.try_acquire(limit)
    assert token is not None
    return token


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
    correlation_id = headers[b"x-dbbasic-correlation-id"].decode("latin-1")
    assert object_correlation.normalize_correlation_id(correlation_id) == correlation_id
    assert payload == {"status": "ok"}


def test_response_preserves_valid_correlation_header():
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

    status, headers, payload = request(
        "/health",
        headers=[("x-dbbasic-correlation-id", correlation_id)],
    )

    assert status == 200
    assert payload == {"status": "ok"}
    assert headers[b"x-dbbasic-correlation-id"].decode("latin-1") == correlation_id


def test_response_replaces_invalid_correlation_header():
    status, headers, payload = request(
        "/health",
        headers=[("x-dbbasic-correlation-id", "not-a-uuid")],
    )

    assert status == 200
    assert payload == {"status": "ok"}
    correlation_id = headers[b"x-dbbasic-correlation-id"].decode("latin-1")
    assert correlation_id != "not-a-uuid"
    assert object_correlation.normalize_correlation_id(correlation_id) == correlation_id


def test_identity_endpoint_returns_anonymous_subject(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DBBASIC_PERMISSION_TRUST_HEADERS", raising=False)

    status, _, payload = request("/identity")

    assert status == 200
    assert payload == {
        "status": "ok",
        "subject": {
            "user_id": None,
            "account_id": None,
            "roles": [],
            "subscriptions": [],
            "authenticated": False,
        },
        "auth": {
            "method": "anonymous",
            "trusted_headers_enabled": False,
            "trusted_headers_present": False,
        },
        "permissions": {
            "enforcement_enabled": False,
            "enforcement_requested": False,
            "enforcement_blocked": False,
            "audit_enabled": False,
        },
    }


def test_identity_endpoint_reports_admin_token_subject(monkeypatch):
    enable_admin_token(monkeypatch)

    status, _, payload = request("/identity", headers=auth_headers())

    assert status == 200
    assert payload["subject"] == {
        "user_id": "admin",
        "account_id": None,
        "roles": ["admin"],
        "subscriptions": [],
        "authenticated": True,
    }
    assert payload["auth"]["method"] == "admin_token"


def test_identity_accounts_create_list_and_get(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, created = request(
        "/identity/accounts",
        method="POST",
        body=json.dumps(
            {
                "account_id": "acme",
                "name": "Acme Corp",
                "subscriptions": ["pro"],
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert created["status"] == "ok"
    assert created["account"]["account_id"] == "acme"
    assert created["account"]["subscriptions"] == ["pro"]

    status, _, listed = request("/identity/accounts", headers=auth_headers())

    assert status == 200
    assert listed["accounts"] == [created["account"]]
    assert listed["count"] == 1

    status, _, fetched = request("/identity/accounts/acme", headers=auth_headers())

    assert status == 200
    assert fetched["account"] == created["account"]


def test_identity_users_create_list_filter_and_get(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    request(
        "/identity/accounts",
        method="POST",
        body=json.dumps({"account_id": "acme"}).encode(),
        headers=auth_headers(),
    )
    status, _, created = request(
        "/identity/users",
        method="POST",
        body=json.dumps(
            {
                "user_id": "u_7",
                "account_id": "acme",
                "email": "alice@example.com",
                "roles": ["sales"],
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert created["status"] == "ok"
    assert created["user"]["user_id"] == "u_7"
    assert created["user"]["account_id"] == "acme"
    assert created["user"]["roles"] == ["sales"]

    status, _, listed = request(
        "/identity/users",
        query_string="account_id=acme",
        headers=auth_headers(),
    )

    assert status == 200
    assert listed["users"] == [created["user"]]
    assert listed["count"] == 1

    status, _, fetched = request("/identity/users/u_7", headers=auth_headers())

    assert status == 200
    assert fetched["user"] == created["user"]


def test_identity_account_and_user_routes_are_admin_gated(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, account_payload = request("/identity/accounts")
    status_user, _, user_payload = request("/identity/users")

    assert status == 403
    assert account_payload == {
        "status": "error",
        "error": "Identity accounts require DBBASIC_ADMIN_TOKEN.",
    }
    assert status_user == 403
    assert user_payload == {
        "status": "error",
        "error": "Identity users require DBBASIC_ADMIN_TOKEN.",
    }


def test_identity_session_uses_registered_user_profile(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    request(
        "/identity/accounts",
        method="POST",
        body=json.dumps(
            {
                "account_id": "acme",
                "subscriptions": ["pro"],
            }
        ).encode(),
        headers=auth_headers(),
    )
    request(
        "/identity/users",
        method="POST",
        body=json.dumps(
            {
                "user_id": "u_7",
                "account_id": "acme",
                "roles": ["sales"],
                "subscriptions": ["team"],
            }
        ).encode(),
        headers=auth_headers(),
    )

    status, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "u_7", "label": "scroll"}).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert created["session"]["account_id"] == "acme"
    assert created["session"]["roles"] == ["sales"]
    assert created["session"]["subscriptions"] == ["team", "pro"]

    status, _, identity = request(
        "/identity",
        headers=[("authorization", f"Bearer {created['token']}")],
    )

    assert status == 200
    assert identity["subject"]["account_id"] == "acme"
    assert identity["subject"]["roles"] == ["sales"]
    assert identity["subject"]["subscriptions"] == ["team", "pro"]


def test_identity_session_strict_known_user_mode(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.REQUIRE_KNOWN_IDENTITY_USERS_ENV, "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "missing"}).encode(),
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {"status": "error", "error": "User not found: missing"}


def test_identity_session_create_and_use(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps(
            {
                "user_id": "7",
                "account_id": "acme",
                "roles": ["sales", "manager"],
                "subscriptions": ["pro"],
                "label": "scroll",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert created["status"] == "ok"
    assert created["token"]
    assert created["session"]["user_id"] == "7"
    assert created["session"]["account_id"] == "acme"
    assert created["session"]["roles"] == ["sales", "manager"]
    assert "token_hash" not in created["session"]

    status, _, identity = request(
        "/identity",
        headers=[("authorization", f"Bearer {created['token']}")],
    )

    assert status == 200
    assert identity["auth"]["method"] == "session_token"
    assert identity["subject"] == {
        "user_id": "7",
        "account_id": "acme",
        "roles": ["sales", "manager"],
        "subscriptions": ["pro"],
        "authenticated": True,
    }


def test_identity_sessions_are_admin_gated(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/identity/sessions")

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Identity sessions require DBBASIC_ADMIN_TOKEN.",
    }


def test_identity_session_list_and_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    _, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "7", "roles": ["sales"]}).encode(),
        headers=auth_headers(),
    )
    session_id = created["session"]["session_id"]
    token = created["token"]

    status, _, listing = request("/identity/sessions", headers=auth_headers())

    assert status == 200
    assert listing["count"] == 1
    assert listing["sessions"][0]["session_id"] == session_id
    assert "token" not in listing["sessions"][0]

    status, _, revoked = request(
        f"/identity/sessions/{session_id}",
        method="DELETE",
        headers=auth_headers(),
    )

    assert status == 200
    assert revoked["session"]["active"] is False
    assert revoked["session"]["revoked_at"] is not None

    status, _, identity = request("/identity", headers=[("authorization", f"Token {token}")])

    assert status == 200
    assert identity["auth"]["method"] == "anonymous"
    assert identity["subject"]["authenticated"] is False


def test_identity_current_session_returns_active_session_without_admin_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    _, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps(
            {
                "user_id": "u_7",
                "account_id": "acme",
                "roles": ["sales"],
                "subscriptions": ["pro"],
                "label": "scroll",
            }
        ).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/identity/session",
        headers=[("authorization", f"Bearer {created['token']}")],
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["session"]["session_id"] == created["session"]["session_id"]
    assert payload["session"]["user_id"] == "u_7"
    assert payload["session"]["account_id"] == "acme"
    assert payload["session"]["roles"] == ["sales"]
    assert payload["session"]["subscriptions"] == ["pro"]
    assert payload["session"]["label"] == "scroll"
    assert payload["session"]["active"] is True
    assert "token" not in payload["session"]
    assert "token_hash" not in payload["session"]


def test_identity_current_session_rejects_missing_invalid_and_admin_tokens(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    missing_status, _, missing_payload = request("/identity/session")
    invalid_status, _, invalid_payload = request(
        "/identity/session",
        headers=[("authorization", "Token not-a-session-token")],
    )
    admin_status, _, admin_payload = request("/identity/session", headers=auth_headers())

    assert missing_status == 401
    assert invalid_status == 401
    assert admin_status == 401
    assert missing_payload == {"status": "error", "error": "Active session token required"}
    assert invalid_payload == {"status": "error", "error": "Active session token required"}
    assert admin_payload == {"status": "error", "error": "Active session token required"}


def test_identity_current_session_can_revoke_itself(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    _, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "u_7", "roles": ["sales"]}).encode(),
        headers=auth_headers(),
    )
    token_headers = [("authorization", f"Token {created['token']}")]

    status, _, payload = request("/identity/session", method="DELETE", headers=token_headers)

    assert status == 200
    assert payload["session"]["session_id"] == created["session"]["session_id"]
    assert payload["session"]["active"] is False
    assert payload["session"]["revoked_at"] is not None

    identity_status, _, identity = request("/identity", headers=token_headers)
    session_status, _, session_payload = request("/identity/session", headers=token_headers)

    assert identity_status == 200
    assert identity["auth"]["method"] == "anonymous"
    assert identity["subject"]["authenticated"] is False
    assert session_status == 401
    assert session_payload == {"status": "error", "error": "Active session token required"}


def test_identity_current_session_rejects_non_get_delete_methods(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    _, _, created = request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "u_7"}).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/identity/session",
        method="POST",
        headers=[("authorization", f"Token {created['token']}")],
    )

    assert status == 405
    assert payload == {"status": "error", "error": "Method not allowed"}


def test_identity_endpoint_ignores_untrusted_identity_headers(monkeypatch):
    monkeypatch.delenv("DBBASIC_PERMISSION_TRUST_HEADERS", raising=False)
    headers = [
        ("x-dbbasic-user-id", "7"),
        ("x-dbbasic-account-id", "acme"),
        ("x-dbbasic-roles", "sales"),
    ]

    status, _, payload = request("/identity", headers=headers)

    assert status == 200
    assert payload["subject"]["user_id"] is None
    assert payload["subject"]["account_id"] is None
    assert payload["subject"]["roles"] == []
    assert payload["auth"] == {
        "method": "anonymous",
        "trusted_headers_enabled": False,
        "trusted_headers_present": True,
    }


def test_identity_endpoint_reports_trusted_header_subject(monkeypatch):
    monkeypatch.setenv("DBBASIC_PERMISSION_TRUST_HEADERS", "true")
    headers = [
        ("x-dbbasic-user-id", "7"),
        ("x-dbbasic-account-id", "acme"),
        ("x-dbbasic-roles", "sales,manager,sales"),
        ("x-dbbasic-subscriptions", "pro,temporary"),
    ]

    status, _, payload = request("/identity", headers=headers)

    assert status == 200
    assert payload["subject"] == {
        "user_id": "7",
        "account_id": "acme",
        "roles": ["sales", "manager"],
        "subscriptions": ["pro", "temporary"],
        "authenticated": True,
    }
    assert payload["auth"] == {
        "method": "trusted_headers",
        "trusted_headers_enabled": True,
        "trusted_headers_present": True,
    }


def test_identity_endpoint_rejects_non_get_methods():
    status, _, payload = request("/identity", method="POST")

    assert status == 405
    assert payload == {"status": "error", "error": "Method not allowed"}


def test_health_endpoint_bypasses_request_concurrency_limit(monkeypatch):
    monkeypatch.setenv(object_server.MAX_CONCURRENT_REQUESTS_ENV, "1")
    token = claim_limit_slot(object_server._request_limiter, 1)
    try:
        status, _, payload = request("/health")
    finally:
        token.release()

    assert status == 200
    assert payload == {"status": "ok"}


def test_health_capacity_requires_admin_token_configuration(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/health", query_string="capacity=true")

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Health capacity requires DBBASIC_ADMIN_TOKEN.",
    }


def test_health_capacity_requires_authorization_header(monkeypatch):
    enable_admin_token(monkeypatch)

    status, _, payload = request("/health", query_string="capacity=true")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_health_capacity_reports_version_config_objects_and_slots(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "2048")
    monkeypatch.setenv(object_server.MAX_CONCURRENT_REQUESTS_ENV, "2")
    monkeypatch.setenv(object_server.MAX_CONCURRENT_EXECUTIONS_ENV, "1")
    monkeypatch.setenv(object_server.TRUSTED_IN_PROCESS_OBJECTS_ENV, "site_home, basics_counter")
    enable_admin_token(monkeypatch)

    token = claim_limit_slot(object_server._request_limiter, 2)
    try:
        status, _, payload = request(
            "/health",
            query_string="capacity=true",
            headers=auth_headers(),
        )
    finally:
        token.release()

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["version"] == "0.0.1"
    assert payload["station_id"] == "standalone"
    assert payload["objects"] == {"count": 1}
    assert payload["checks"]["storage"] == {"status": "ok"}
    assert payload["config"]["max_request_bytes"] == 2048
    assert payload["config"]["max_concurrent_requests"] == 2
    assert payload["config"]["max_concurrent_executions"] == 1
    assert payload["config"]["trusted_in_process_objects"] == ["basics_counter", "site_home"]
    assert payload["config"]["permission_enforcement_enabled"] is False
    assert payload["config"]["permission_audit_enabled"] is False
    assert payload["config"]["permission_trust_headers"] is False
    assert payload["capacity"]["requests"] == {
        "in_flight": 1,
        "max": 2,
        "available": 1,
        "limited": True,
    }
    assert payload["capacity"]["object_executions"] == {
        "in_flight": 0,
        "max": 1,
        "available": 1,
        "limited": True,
    }
    assert "metrics" not in payload


def test_health_metrics_keeps_old_dashboard_shape(monkeypatch):
    enable_admin_token(monkeypatch)
    request("/missing-before-health-metrics")

    status, _, payload = request(
        "/health",
        query_string="metrics=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["requests"] >= 1
    assert payload["errors"] >= 0
    assert payload["rps"] >= 0
    assert set(payload["response_time_ms"]) == {"avg", "p50", "p95", "p99"}
    assert payload["metrics"]["total_requests"] >= 1
    assert payload["metrics"]["total_4xx"] >= 1
    assert "top_paths" in payload["metrics"]


def test_admin_status_requires_admin_token(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/admin/status", headers=auth_headers())

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Admin status requires DBBASIC_ADMIN_TOKEN.",
    }


def test_admin_status_reports_inventory_capabilities_and_package_posture(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    packages_root = tmp_path / "packages"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    schema_file = data_dir / "schemas" / "dbbasic_probe.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "title": "Probe",
                "fields": [{"name": "note", "type": "text"}],
            }
        )
    )
    write_records(data_dir, "dbbasic_probe", "id\tnote\nprobe_001\talready installed\n")
    write_package(
        packages_root,
        "probe-pack",
        {
            "id": "probe-pack",
            "name": "Probe Pack",
            "version": "0.1.0",
            "description": "Status fixture",
            "objects": [{"id": "site_home", "path": "objects/site/home.py"}],
            "schemas": [{"collection": "dbbasic_probe", "path": "schemas/dbbasic_probe.json"}],
            "permissions": [],
            "seed": [],
            "migrations": [],
        },
        files=(
            ("objects/site/home.py", "def GET(request): return {'ok': True}\n"),
            (
                "schemas/dbbasic_probe.json",
                json.dumps({"title": "Probe", "fields": [{"name": "note", "type": "text"}]}),
            ),
        ),
    )
    object_package_changes.append_package_change(
        package_id="probe-pack",
        action="dry_run",
        package_version="0.1.0",
        actor="unit-test",
        base_dir=data_dir,
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "2048")
    monkeypatch.setenv(object_server.MAX_CONCURRENT_REQUESTS_ENV, "3")
    monkeypatch.setenv(object_server.MAX_CONCURRENT_EXECUTIONS_ENV, "2")
    enable_admin_token(monkeypatch)

    status, _, payload = request("/admin/status", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["version"] == "0.0.1"
    assert payload["health"]["status"] == "ok"
    assert "metrics" in payload["health"]
    assert payload["inventory"] == {
        "objects": 1,
        "collections": 2,
        "schemas": 2,
        "packages": 1,
    }
    assert payload["capabilities"]["source_writes"] == {
        "enabled": False,
        "env": "DBBASIC_ENABLE_SOURCE_WRITES",
    }
    assert payload["capabilities"]["package_installs"] == {
        "enabled": False,
        "env": "DBBASIC_ENABLE_PACKAGE_INSTALLS",
    }
    assert payload["capabilities"]["limits"]["max_request_bytes"] == 2048
    assert payload["capabilities"]["limits"]["max_concurrent_requests"] == 3
    assert payload["capabilities"]["limits"]["max_concurrent_executions"] == 2
    assert payload["permissions"]["enforcement_enabled"] is False
    assert payload["permissions"]["audit_enabled"] is False
    assert payload["packages"][0]["id"] == "probe-pack"
    assert payload["packages"][0]["status"] == "installed"
    assert payload["packages"][0]["install"]["installed_count"] == 2
    assert payload["packages"][0]["install"]["installable_count"] == 2
    assert payload["packages"][0]["install"]["install_enabled"] is False
    assert payload["packages"][0]["changes"]["total"] == 1
    assert payload["packages"][0]["changes"]["latest"]["action"] == "dry_run"


def test_daemon_status_requires_admin_token(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/daemon/status", headers=auth_headers())

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Daemon status requires DBBASIC_ADMIN_TOKEN.",
    }


def test_daemon_status_reports_scheduler_queue_and_event_state(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "triggers" / "scheduler.py", "def POST(request):\n    return {'ok': True}\n")
    write_source(root / "triggers" / "queue.py", "def POST(request):\n    return {'ok': True}\n")
    write_source(root / "triggers" / "events.py", "def POST(request):\n    return {'ok': True}\n")

    scheduler = object_state.ObjectStateManager("scheduler", base_dir=data_dir)
    scheduler.set("task_due", json.dumps({"id": "due", "status": "active", "next_run": 1}))
    queue = object_state.ObjectStateManager("queue", base_dir=data_dir)
    queue.set("msg_ready", json.dumps({"id": "ready", "status": "pending", "visible_after": 1}))
    events = object_state.ObjectStateManager("events", base_dir=data_dir)
    events.set(
        "event_1_evt",
        json.dumps(
            {
                "id": "evt",
                "event_type": "collection.record.created",
                "payload": {"hidden": True},
                "timestamp": 1,
            }
        ),
    )

    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/daemon/status", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert set(payload) == {"status", "timestamp", "daemon", "scheduler", "queue", "events", "cleanup"}
    assert payload["daemon"]["triggers"]["scheduler"]["source_present"] is True
    assert payload["scheduler"]["tasks"]["active"] == 1
    assert payload["queue"]["messages"]["pending_visible"] == 1
    assert payload["events"]["events"]["latest"]["id"] == "evt"
    assert "payload" not in payload["events"]["events"]["latest"]


def test_daemon_scheduler_task_api_creates_lists_updates_and_deletes(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, created = request(
        "/daemon/scheduler/tasks",
        method="POST",
        body=json.dumps(
            {
                "object_id": "system_dashboard",
                "method": "POST",
                "type": "onetime",
                "schedule": "2026-07-01T12:00:00Z",
                "payload": {"refresh": True},
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert created["status"] == "ok"
    task = created["task"]
    assert object_ids.is_uuid4(task["id"])
    assert task["object_id"] == "system_dashboard"
    assert task["payload_present"] is True
    assert "payload" not in task

    status, _, listed = request("/daemon/scheduler/tasks", headers=auth_headers())

    assert status == 200
    assert listed["status"] == "ok"
    assert listed["count"] == 1
    assert listed["tasks"] == [task]

    status, _, updated = request(
        f"/daemon/scheduler/tasks/{task['id']}",
        method="PATCH",
        body=json.dumps({"status": "paused"}).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert updated["task"]["id"] == task["id"]
    assert updated["task"]["status"] == "paused"

    status, _, deleted = request(
        f"/daemon/scheduler/tasks/{task['id']}",
        method="DELETE",
        headers=auth_headers(),
    )

    assert status == 200
    assert deleted["deleted"] is True
    assert deleted["task"]["id"] == task["id"]


def test_daemon_queue_message_api_enqueues_lists_retries_and_deletes(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, created = request(
        "/daemon/queue/messages",
        method="POST",
        body=json.dumps(
            {
                "object_id": "system_dashboard",
                "queue_name": "default",
                "priority_level": 7,
                "payload": {"refresh": True},
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    message = created["message"]
    assert object_ids.is_uuid4(message["id"])
    assert message["status"] == "pending"
    assert message["message"]["object_id"] == "system_dashboard"
    assert message["message"]["payload_present"] is True
    assert "payload" not in message["message"]

    status, _, listed = request(
        "/daemon/queue/messages",
        query_string="status=pending&queue_name=default",
        headers=auth_headers(),
    )

    assert status == 200
    assert listed["count"] == 1
    assert listed["messages"] == [message]

    status, _, cancelled = request(
        f"/daemon/queue/messages/{message['id']}",
        method="PATCH",
        body=json.dumps({"action": "cancel"}).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert cancelled["message"]["status"] == "cancelled"

    status, _, retried = request(
        f"/daemon/queue/messages/{message['id']}",
        method="PATCH",
        body=json.dumps({"action": "retry"}).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert retried["message"]["status"] == "pending"

    status, _, deleted = request(
        f"/daemon/queue/messages/{message['id']}",
        method="DELETE",
        headers=auth_headers(),
    )

    assert status == 200
    assert deleted["deleted"] is True
    assert deleted["message"]["id"] == message["id"]


def test_daemon_control_routes_require_admin_token(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    scheduler_status, _, scheduler_payload = request("/daemon/scheduler/tasks")
    queue_status, _, queue_payload = request("/daemon/queue/messages")

    assert scheduler_status == 403
    assert scheduler_payload == {
        "status": "error",
        "error": "Daemon scheduler controls require DBBASIC_ADMIN_TOKEN.",
    }
    assert queue_status == 403
    assert queue_payload == {
        "status": "error",
        "error": "Daemon queue controls require DBBASIC_ADMIN_TOKEN.",
    }


def test_request_concurrency_limit_returns_503_when_full(monkeypatch):
    monkeypatch.setenv(object_server.MAX_CONCURRENT_REQUESTS_ENV, "1")
    token = claim_limit_slot(object_server._request_limiter, 1)
    try:
        status, _, payload = request("/missing")
    finally:
        token.release()

    assert status == 503
    assert payload == {
        "status": "error",
        "error": "Server is busy",
        "limit": "requests",
        "max_concurrent": 1,
    }


def test_request_concurrency_limit_releases_after_error(monkeypatch):
    monkeypatch.setenv(object_server.MAX_CONCURRENT_REQUESTS_ENV, "1")

    first_status, _, first_payload = request("/missing")
    second_status, _, second_payload = request("/missing")

    assert first_status == 404
    assert first_payload == {"status": "error", "error": "Not found"}
    assert second_status == 404
    assert second_payload == {"status": "error", "error": "Not found"}


def test_rate_limit_returns_429_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(object_server.RATE_LIMIT_REQUESTS_ENV, "1")
    monkeypatch.setenv(object_server.RATE_LIMIT_WINDOW_SECONDS_ENV, "60")
    client = ("198.51.100.10", 54321)

    first_status, _, first_payload = request("/missing", client=client)
    second_status, second_headers, second_payload = request("/missing", client=client)

    assert first_status == 404
    assert first_payload == {"status": "error", "error": "Not found"}
    assert second_status == 429
    assert second_headers[b"retry-after"].isdigit()
    assert second_payload["status"] == "error"
    assert second_payload["error"] == "Rate limit exceeded"
    assert second_payload["limit"] == 1
    assert second_payload["window_seconds"] == 60


def test_plain_health_bypasses_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(object_server.RATE_LIMIT_REQUESTS_ENV, "1")
    client = ("198.51.100.10", 54321)

    assert request("/health", client=client)[2] == {"status": "ok"}
    assert request("/health", client=client)[2] == {"status": "ok"}


def test_admin_token_uses_separate_rate_limit_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(object_server.RATE_LIMIT_REQUESTS_ENV, "1")
    monkeypatch.setenv(object_server.RATE_LIMIT_WINDOW_SECONDS_ENV, "60")
    enable_admin_token(monkeypatch)
    client = ("198.51.100.10", 54321)

    assert request("/missing", client=client)[0] == 404
    status, _, payload = request(
        "/health",
        query_string="capacity=true",
        headers=auth_headers(),
        client=client,
    )

    assert status == 200
    assert payload["status"] == "ok"


def test_rate_limit_can_trust_proxy_headers_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(object_server.RATE_LIMIT_REQUESTS_ENV, "1")
    monkeypatch.setenv(object_server.RATE_LIMIT_TRUST_PROXY_HEADERS_ENV, "true")
    proxy_client = ("127.0.0.1", 54321)

    first_status = request(
        "/missing",
        headers=[("x-forwarded-for", "198.51.100.10")],
        client=proxy_client,
    )[0]
    second_status = request(
        "/missing",
        headers=[("x-forwarded-for", "198.51.100.11")],
        client=proxy_client,
    )[0]

    assert first_status == 404
    assert second_status == 404


def test_object_list_returns_existing_contract_shape(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    write_source(root / "users" / "42" / "deals.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects", query_string="format=json", headers=auth_headers())

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
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects", query_string="format=json", headers=auth_headers())

    assert status == 200
    assert payload == {"status": "ok", "objects": [], "count": 0}


def test_object_list_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/objects", query_string="format=json")

    assert status == 403
    assert payload == {"status": "error", "error": "Object listing requires DBBASIC_ADMIN_TOKEN."}


def test_object_list_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects", query_string="format=json")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_collection_list_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/collections")

    assert status == 403
    assert payload == {"status": "error", "error": "Collection listing requires DBBASIC_ADMIN_TOKEN."}


def test_collection_list_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_collection_list_returns_derived_collection_summaries(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    write_source(root / "site" / "about.py", "def GET(request):\n    return {'ok': True}\n")
    write_source(root / "users" / "42" / "deals.py", "def GET(request):\n    return {}\n")
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:admin",
                    "actions": ["read", "execute"],
                    "collection": "site",
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["count"] == 2
    assert [item["name"] for item in payload["collections"]] == ["deals", "site"]
    site = payload["collections"][1]
    assert site["object_count"] == 2
    assert site["has_records"] is False
    assert site["owners"] == ["system"]
    assert site["kinds"] == {"system": 2}
    assert site["permission"]["principals"] == ["role:admin"]


def test_collection_detail_returns_objects(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "apps" / "widget_counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/apps", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["collection"]["name"] == "apps"
    assert payload["collection"]["objects"] == [
        {
            "object_id": "apps_widget_counter",
            "path": "apps/widget_counter.py",
            "owner": "system",
            "kind": "system",
            "state_count": 0,
            "has_logs": False,
            "file_count": 0,
        }
    ]


def test_collection_detail_rejects_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/bad.name", headers=auth_headers())

    assert status == 400
    assert payload["status"] == "error"
    assert payload["error"] == "Invalid collection name: bad.name"


def test_collection_detail_rejects_missing_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/missing", headers=auth_headers())

    assert status == 404
    assert payload == {"status": "error", "error": "Collection not found: missing"}


def test_collection_routes_reject_non_get_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    list_status, _, list_payload = request("/collections", method="POST", headers=auth_headers())
    detail_status, _, detail_payload = request(
        "/collections/site",
        method="POST",
        headers=auth_headers(),
    )

    assert list_status == 405
    assert detail_status == 405
    assert list_payload == {"status": "error", "error": "Method not allowed"}
    assert detail_payload == {"status": "error", "error": "Method not allowed"}


def test_collection_records_require_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/collections/contacts/records")

    assert status == 403
    assert payload == {"status": "error", "error": "Collection records require DBBASIC_ADMIN_TOKEN."}


def test_collection_records_require_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/contacts/records")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_collection_records_return_paginated_tsv_rows(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tfirst_name\tlast_name\n"
        "c1\tAda\tLovelace\n"
        "c2\tGrace\tHopper\n"
        "c3\tKatherine\tJohnson\n",
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/collections/contacts/records",
        query_string="limit=2&offset=1",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "collection": "contacts",
        "records": [
            {"id": "c2", "first_name": "Grace", "last_name": "Hopper"},
            {"id": "c3", "first_name": "Katherine", "last_name": "Johnson"},
        ],
        "count": 2,
        "total": 3,
        "limit": 2,
        "offset": 1,
        "has_more": False,
    }


def test_collection_record_detail_returns_one_tsv_row(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/contacts/records/c2", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "collection": "contacts",
        "record": {"id": "c2", "name": "Grace"},
    }


def test_collection_records_return_empty_for_schema_backed_collection(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json.dumps({"fields": [{"name": "id"}]}))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/collections/invoices/records", headers=auth_headers())

    assert status == 200
    assert payload["records"] == []
    assert payload["total"] == 0


def test_collection_records_reject_bad_limit(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/collections/contacts/records",
        query_string="limit=0",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Query parameter 'limit' must be at least 1"}


def test_collection_records_reject_invalid_and_missing_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    invalid_status, _, invalid_payload = request(
        "/collections/bad.name/records",
        headers=auth_headers(),
    )
    missing_status, _, missing_payload = request(
        "/collections/missing/records",
        headers=auth_headers(),
    )

    assert invalid_status == 400
    assert invalid_payload == {"status": "error", "error": "Invalid collection name: bad.name"}
    assert missing_status == 404
    assert missing_payload == {"status": "error", "error": "Collection not found: missing"}


def test_collection_record_detail_rejects_invalid_and_missing_record(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    invalid_status, _, invalid_payload = request(
        "/collections/contacts/records/bad.name",
        headers=auth_headers(),
    )
    missing_status, _, missing_payload = request(
        "/collections/contacts/records/missing",
        headers=auth_headers(),
    )

    assert invalid_status == 400
    assert invalid_payload == {"status": "error", "error": "Invalid record id: bad.name"}
    assert missing_status == 404
    assert missing_payload == {
        "status": "error",
        "error": "Record not found: contacts/missing",
    }


def test_collection_record_routes_reject_non_get_methods(tmp_path, monkeypatch):
    enable_admin_token(monkeypatch)

    list_status, _, list_payload = request(
        "/collections/contacts/records",
        method="PUT",
        headers=auth_headers(),
    )
    detail_status, _, detail_payload = request(
        "/collections/contacts/records/c1",
        method="POST",
        headers=auth_headers(),
    )

    assert list_status == 405
    assert detail_status == 405
    assert list_payload == {"status": "error", "error": "Method not allowed"}
    assert detail_payload == {"status": "error", "error": "Method not allowed"}


def test_collection_record_create_requires_admin_token_by_default(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_file = data_dir / "schemas" / "contacts.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json.dumps({"fields": [{"name": "id"}]}))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    missing_auth_status, _, missing_auth_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c1", "name": "Ada"}).encode("utf-8"),
    )
    create_status, _, create_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c1", "name": "Ada"}).encode("utf-8"),
        headers=auth_headers(),
    )

    assert missing_auth_status == 401
    assert missing_auth_payload == {"status": "error", "error": "Unauthorized"}
    assert create_status == 201
    assert create_payload == {
        "status": "ok",
        "collection": "contacts",
        "record": {"id": "c1", "name": "Ada"},
    }


def test_collection_record_create_uses_schema_validation(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "id"},
                    {"name": "invoice_date", "type": "date", "required": True},
                    {"name": "status", "type": "enum", "enum": ["draft", "sent"], "default": "draft"},
                    {"name": "total", "type": "computed", "computed": "sum(line_items)"},
                ]
            }
        )
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    missing_required_status, _, missing_required_payload = request(
        "/collections/invoices/records",
        method="POST",
        body=json.dumps({"id": "i1", "status": "draft"}).encode("utf-8"),
        headers=auth_headers(),
    )
    invalid_computed_status, _, invalid_computed_payload = request(
        "/collections/invoices/records",
        method="POST",
        body=json.dumps({"id": "i1", "invoice_date": "2026-04-08", "total": "100"}).encode("utf-8"),
        headers=auth_headers(),
    )
    create_status, _, create_payload = request(
        "/collections/invoices/records",
        method="POST",
        body=json.dumps({"id": "i1", "invoice_date": "2026-04-08"}).encode("utf-8"),
        headers=auth_headers(),
    )

    assert missing_required_status == 400
    assert missing_required_payload == {
        "status": "error",
        "error": "Record field 'invoice_date' is required",
    }
    assert invalid_computed_status == 400
    assert invalid_computed_payload == {
        "status": "error",
        "error": "Record field 'total' is computed or read-only and cannot be written",
    }
    assert create_status == 201
    assert create_payload == {
        "status": "ok",
        "collection": "invoices",
        "record": {"id": "i1", "invoice_date": "2026-04-08", "status": "draft"},
    }


def test_collection_record_create_rejects_duplicate_and_invalid_payload(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    duplicate_status, _, duplicate_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c1", "name": "Again"}).encode("utf-8"),
        headers=auth_headers(),
    )
    generated_status, _, generated_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"name": "Server ID"}).encode("utf-8"),
        headers=auth_headers(),
    )
    invalid_status, _, invalid_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"name": {"nested": "bad"}}).encode("utf-8"),
        headers=auth_headers(),
    )

    assert duplicate_status == 409
    assert duplicate_payload == {
        "status": "error",
        "error": "Record already exists: contacts/c1",
    }
    assert generated_status == 201
    assert generated_payload["record"]["name"] == "Server ID"
    assert invalid_status == 400
    assert invalid_payload == {
        "status": "error",
        "error": "Record field value must be scalar or null: name",
    }


def test_collection_record_update_and_delete_require_admin_token_by_default(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    denied_status, _, denied_payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"name": "Ada Lovelace"}).encode("utf-8"),
    )
    update_status, _, update_payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"name": "Ada Lovelace", "email": "ada@example.com"}).encode("utf-8"),
        headers=auth_headers(),
    )
    delete_status, _, delete_payload = request(
        "/collections/contacts/records/c2",
        method="DELETE",
        headers=auth_headers(),
    )
    list_status, _, list_payload = request("/collections/contacts/records", headers=auth_headers())

    assert denied_status == 401
    assert denied_payload == {"status": "error", "error": "Unauthorized"}
    assert update_status == 200
    assert update_payload == {
        "status": "ok",
        "collection": "contacts",
        "record": {
            "id": "c1",
            "name": "Ada Lovelace",
            "email": "ada@example.com",
        },
    }
    assert delete_status == 200
    assert delete_payload == {
        "status": "ok",
        "collection": "contacts",
        "record": {"id": "c2", "name": "Grace", "email": ""},
        "deleted": True,
    }
    assert list_status == 200
    assert list_payload["records"] == [
        {"id": "c1", "name": "Ada Lovelace", "email": "ada@example.com"},
    ]


def test_collection_record_mutations_append_change_history(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    create_status, _, create_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c3", "name": "Katherine"}).encode("utf-8"),
        headers=auth_headers(),
    )
    update_status, _, update_payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"name": "Ada Lovelace"}).encode("utf-8"),
        headers=auth_headers(),
    )
    delete_status, _, delete_payload = request(
        "/collections/contacts/records/c2",
        method="DELETE",
        headers=auth_headers(),
    )

    assert create_status == 201
    assert create_payload["record"] == {"id": "c3", "name": "Katherine"}
    assert update_status == 200
    assert update_payload["record"] == {"id": "c1", "name": "Ada Lovelace"}
    assert delete_status == 200
    assert delete_payload["deleted"] is True

    history_status, _, history_payload = request(
        "/collections/contacts/changes",
        query_string="limit=10",
        headers=auth_headers(),
    )
    record_status, _, record_payload = request(
        "/collections/contacts/records/c1/changes",
        headers=auth_headers(),
    )

    assert history_status == 200
    assert history_payload["collection"] == "contacts"
    assert history_payload["count"] == 3
    assert [change["action"] for change in history_payload["changes"]] == [
        "delete",
        "update",
        "create",
    ]
    assert all(change["actor"] == "admin" for change in history_payload["changes"])

    update_change = history_payload["changes"][1]
    assert update_change["record_id"] == "c1"
    assert update_change["changed_fields"] == ["name"]
    assert update_change["before"] == {"id": "c1", "name": "Ada"}
    assert update_change["after"] == {"id": "c1", "name": "Ada Lovelace"}

    delete_change = history_payload["changes"][0]
    assert delete_change["record_id"] == "c2"
    assert delete_change["after"] is None

    assert record_status == 200
    assert record_payload["record_id"] == "c1"
    assert record_payload["count"] == 1
    assert record_payload["changes"][0]["action"] == "update"


def test_collection_record_mutations_publish_metadata_events(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    create_status, _, _ = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c3", "name": "Katherine"}).encode("utf-8"),
        headers=auth_headers(),
    )
    update_status, _, _ = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"name": "Ada Lovelace"}).encode("utf-8"),
        headers=auth_headers(),
    )
    delete_status, _, _ = request(
        "/collections/contacts/records/c2",
        method="DELETE",
        headers=auth_headers(),
    )

    assert create_status == 201
    assert update_status == 200
    assert delete_status == 200

    status, _, payload = request(
        "/events",
        query_string="limit=10",
        headers=auth_headers(),
    )

    assert status == 200
    events_by_type = {event["event_type"]: event for event in payload["events"]}
    assert set(events_by_type) == {
        "collection.record.created",
        "collection.record.updated",
        "collection.record.deleted",
    }

    update_event = events_by_type["collection.record.updated"]
    assert update_event["source"] == "record_changes"
    assert update_event["actor"] == "admin"
    assert update_event["payload"]["collection"] == "contacts"
    assert update_event["payload"]["record_id"] == "c1"
    assert update_event["payload"]["action"] == "update"
    assert update_event["payload"]["changed_fields"] == ["name"]
    assert "before" not in update_event["payload"]
    assert "after" not in update_event["payload"]

    state_file = data_dir / "state" / "events" / "state.tsv"
    rows = state_file.read_text().splitlines()
    assert len(rows) == 3
    assert all(row.startswith("event_") for row in rows)


def test_collection_record_events_can_be_disabled(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_RECORD_EVENTS", "false")
    enable_admin_token(monkeypatch)

    status, _, _ = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c2", "name": "Grace"}).encode("utf-8"),
        headers=auth_headers(),
    )
    events_status, _, events_payload = request("/events", headers=auth_headers())

    assert status == 201
    assert events_status == 200
    assert events_payload["events"] == []


def test_collection_record_event_publish_failure_does_not_break_mutation(
    tmp_path,
    monkeypatch,
):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    def fail_publish(*args, **kwargs):
        raise OSError("event state unavailable")

    monkeypatch.setattr(object_server.object_events, "publish_event", fail_publish)

    status, _, payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c2", "name": "Grace"}).encode("utf-8"),
        headers=auth_headers(),
    )
    history_status, _, history_payload = request(
        "/collections/contacts/changes",
        headers=auth_headers(),
    )

    assert status == 201
    assert payload["record"] == {"id": "c2", "name": "Grace"}
    assert history_status == 200
    assert history_payload["changes"][0]["action"] == "create"


def test_collection_change_history_requires_admin_token(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    collection_status, _, collection_payload = request("/collections/contacts/changes")
    record_status, _, record_payload = request("/collections/contacts/records/c1/changes")

    assert collection_status == 401
    assert collection_payload == {"status": "error", "error": "Unauthorized"}
    assert record_status == 401
    assert record_payload == {"status": "error", "error": "Unauthorized"}


def test_collection_change_history_rejects_bad_queries_and_missing_collection(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    bad_limit_status, _, bad_limit_payload = request(
        "/collections/contacts/changes",
        query_string="limit=0",
        headers=auth_headers(),
    )
    bad_record_status, _, bad_record_payload = request(
        "/collections/contacts/records/bad.name/changes",
        headers=auth_headers(),
    )
    missing_status, _, missing_payload = request(
        "/collections/missing/changes",
        headers=auth_headers(),
    )

    assert bad_limit_status == 400
    assert bad_limit_payload == {
        "status": "error",
        "error": "Query parameter 'limit' must be at least 1",
    }
    assert bad_record_status == 400
    assert bad_record_payload == {"status": "error", "error": "Invalid record id: bad.name"}
    assert missing_status == 404
    assert missing_payload == {"status": "error", "error": "Collection not found: missing"}


def test_collection_record_update_rejects_id_change(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"id": "c2", "name": "Grace"}).encode("utf-8"),
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Record id cannot be changed"}


def test_collection_record_create_enforcement_uses_row_filter(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_file = data_dir / "schemas" / "contacts.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json.dumps({"fields": [{"name": "id"}]}))
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["create"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    denied_status, _, denied_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c1", "name": "Bob", "owner_id": "8"}).encode("utf-8"),
        headers=headers,
    )
    allowed_status, _, allowed_payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c2", "name": "Ada", "owner_id": "7"}).encode("utf-8"),
        headers=headers,
    )

    assert denied_status == 403
    assert denied_payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}
    assert allowed_status == 201
    assert allowed_payload["record"] == {"id": "c2", "name": "Ada", "owner_id": "7"}


def test_collection_record_update_enforcement_checks_existing_and_candidate(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\towner_id\nc1\tAda\t7\nc2\tBob\t8\n")
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["update"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    allowed_status, _, allowed_payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"name": "Ada Lovelace"}).encode("utf-8"),
        headers=headers,
    )
    steal_status, _, steal_payload = request(
        "/collections/contacts/records/c1",
        method="PUT",
        body=json.dumps({"owner_id": "8"}).encode("utf-8"),
        headers=headers,
    )
    other_status, _, other_payload = request(
        "/collections/contacts/records/c2",
        method="PUT",
        body=json.dumps({"name": "Robert"}).encode("utf-8"),
        headers=headers,
    )

    assert allowed_status == 200
    assert allowed_payload["record"] == {"id": "c1", "name": "Ada Lovelace", "owner_id": "7"}
    assert steal_status == 403
    assert steal_payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}
    assert other_status == 403
    assert other_payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}


def test_collection_record_delete_enforcement_uses_row_filter(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\towner_id\nc1\tAda\t7\nc2\tBob\t8\n")
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["delete"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    denied_status, _, denied_payload = request(
        "/collections/contacts/records/c2",
        method="DELETE",
        headers=headers,
    )
    allowed_status, _, allowed_payload = request(
        "/collections/contacts/records/c1",
        method="DELETE",
        headers=headers,
    )
    list_status, _, list_payload = request("/collections/contacts/records", headers=auth_headers())

    assert denied_status == 403
    assert denied_payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}
    assert allowed_status == 200
    assert allowed_payload["deleted"] is True
    assert allowed_payload["record"] == {"id": "c1", "name": "Ada", "owner_id": "7"}
    assert list_status == 200
    assert list_payload["records"] == [{"id": "c2", "name": "Bob", "owner_id": "8"}]


def test_collection_record_writes_in_audit_mode_still_require_admin_token(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_AUDIT_ENV, "true")
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

    status, _, payload = request(
        "/collections/contacts/records",
        method="POST",
        body=json.dumps({"id": "c2", "name": "Grace"}).encode("utf-8"),
        headers=[("x-dbbasic-correlation-id", correlation_id)],
    )

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Collection record writes require DBBASIC_ADMIN_TOKEN.",
    }
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["action"] == "create"
    assert entry["enforced"] is False
    assert entry["correlation_id"] == correlation_id


def test_collection_records_enforcement_request_with_default_policy_stays_in_shadow_mode(
    tmp_path,
    monkeypatch,
):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\n1\tAlice\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")

    status, _, payload = request("/collections/contacts/records")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["collection"] == "contacts"
    assert payload["records"] == [{"id": "1", "name": "Alice"}]
    entries = object_permission_audit.get_permission_audit(data_dir)
    assert entries[-1]["action"] == "read"
    assert entries[-1]["collection"] == "contacts"
    assert entries[-1]["object_id"] is None
    assert entries[-1]["decision"]["allowed"] is False
    assert entries[-1]["enforced"] is False
    assert entries[-1]["enforcement_requested"] is True
    assert entries[-1]["enforcement_blocked"] is True


def test_collection_records_enforcement_can_force_default_policy(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\n1\tAlice\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_UNREADY_ENFORCEMENT_ENV, "true")

    status, _, payload = request("/collections/contacts/records")

    assert status == 403
    assert payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}
    entries = object_permission_audit.get_permission_audit(data_dir)
    assert entries[-1]["action"] == "read"
    assert entries[-1]["collection"] == "contacts"
    assert entries[-1]["object_id"] is None
    assert entries[-1]["decision"]["allowed"] is False
    assert entries[-1]["enforced"] is True
    assert entries[-1]["enforcement_requested"] is True


def test_collection_records_enforcement_filters_rows_for_trusted_subject(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tname\towner_id\tsecret\n1\tAlice\t7\tred\n2\tBob\t8\tblue\n3\tCarol\t7\tgreen\n",
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["read"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                    "denied_fields": ["secret"],
                    "reason": "sales can read own contacts",
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")

    status, _, payload = request(
        "/collections/contacts/records",
        headers=[
            ("x-dbbasic-user-id", "7"),
            ("x-dbbasic-roles", "sales"),
        ],
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "collection": "contacts",
        "records": [
            {"id": "1", "name": "Alice", "owner_id": "7"},
            {"id": "3", "name": "Carol", "owner_id": "7"},
        ],
        "count": 2,
        "total": 2,
        "limit": 100,
        "offset": 0,
        "has_more": False,
    }
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["decision"]["allowed"] is True
    assert entry["decision"]["row_filter"] == {"owner_id": "$user_id"}


def test_collection_records_enforcement_paginates_after_row_filter(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tname\towner_id\n1\tAlice\t7\n2\tBob\t8\n3\tCarol\t7\n4\tDana\t7\n",
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["read"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")

    status, _, payload = request(
        "/collections/contacts/records",
        query_string="limit=1&offset=1",
        headers=[
            ("x-dbbasic-user-id", "7"),
            ("x-dbbasic-roles", "sales"),
        ],
    )

    assert status == 200
    assert payload["records"] == [{"id": "3", "name": "Carol", "owner_id": "7"}]
    assert payload["count"] == 1
    assert payload["total"] == 3
    assert payload["has_more"] is True


def test_collection_record_detail_enforces_row_filter_and_fields(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tname\towner_id\tsecret\n1\tAlice\t7\tred\n2\tBob\t8\tblue\n",
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["read"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                    "fields": ["id", "name", "owner_id"],
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    allowed_status, _, allowed_payload = request(
        "/collections/contacts/records/1",
        headers=headers,
    )
    denied_status, _, denied_payload = request(
        "/collections/contacts/records/2",
        headers=headers,
    )

    assert allowed_status == 200
    assert allowed_payload == {
        "status": "ok",
        "collection": "contacts",
        "record": {"id": "1", "name": "Alice", "owner_id": "7"},
    }
    assert denied_status == 403
    assert denied_payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}


def test_collection_records_enforcement_applies_schema_field_permissions(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "invoices",
        "id\towner_id\tmemo\tstatus\tcost_price\tmargin\n"
        "i1\t7\tHosting\tdraft\t100\t30\n"
        "i2\t8\tSupport\tsent\t200\t60\n",
    )
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "id"},
                    {"name": "owner_id"},
                    {"name": "memo", "permissions": {"sales": "edit", "admin": "edit"}},
                    {"name": "status", "permissions": {"sales": "read", "admin": "edit"}},
                    {"name": "cost_price", "permissions": {"sales": "hidden", "admin": "edit"}},
                    {"name": "margin", "permissions": {"sales": "hidden", "admin": "edit"}},
                ]
            }
        )
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["read"],
                    "collection": "invoices",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    sales_headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    list_status, _, list_payload = request("/collections/invoices/records", headers=sales_headers)
    detail_status, _, detail_payload = request("/collections/invoices/records/i1", headers=sales_headers)
    admin_status, _, admin_payload = request("/collections/invoices/records/i1", headers=auth_headers())

    assert list_status == 200
    assert list_payload["records"] == [
        {"id": "i1", "owner_id": "7", "memo": "Hosting", "status": "draft"}
    ]
    assert detail_status == 200
    assert detail_payload["record"] == {
        "id": "i1",
        "owner_id": "7",
        "memo": "Hosting",
        "status": "draft",
    }
    assert admin_status == 200
    assert admin_payload["record"] == {
        "id": "i1",
        "owner_id": "7",
        "memo": "Hosting",
        "status": "draft",
        "cost_price": "100",
        "margin": "30",
    }


def test_collection_record_update_enforces_schema_edit_permissions(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "invoices", "id\towner_id\tmemo\tstatus\tmargin\ni1\t7\tHosting\tdraft\t30\n")
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "id"},
                    {"name": "owner_id"},
                    {"name": "memo", "permissions": {"sales": "edit", "admin": "edit"}},
                    {"name": "status", "permissions": {"sales": "read", "admin": "edit"}},
                    {"name": "margin", "permissions": {"sales": "hidden", "admin": "edit"}},
                ]
            }
        )
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["update", "read"],
                    "collection": "invoices",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    sales_headers = [("x-dbbasic-user-id", "7"), ("x-dbbasic-roles", "sales")]

    allowed_status, _, allowed_payload = request(
        "/collections/invoices/records/i1",
        method="PUT",
        body=json.dumps({"memo": "Managed hosting"}).encode("utf-8"),
        headers=sales_headers,
    )
    read_only_status, _, read_only_payload = request(
        "/collections/invoices/records/i1",
        method="PUT",
        body=json.dumps({"status": "sent"}).encode("utf-8"),
        headers=sales_headers,
    )
    hidden_status, _, hidden_payload = request(
        "/collections/invoices/records/i1",
        method="PUT",
        body=json.dumps({"margin": "45"}).encode("utf-8"),
        headers=sales_headers,
    )
    admin_status, _, admin_payload = request(
        "/collections/invoices/records/i1",
        method="PUT",
        body=json.dumps({"status": "sent", "margin": "45"}).encode("utf-8"),
        headers=auth_headers(),
    )

    assert allowed_status == 200
    assert allowed_payload["record"]["memo"] == "Managed hosting"
    assert read_only_status == 403
    assert read_only_payload == {
        "status": "error",
        "error": "Record field 'status' is not editable for this subject",
        "code": "forbidden",
        "denied_fields": ["status"],
    }
    assert hidden_status == 403
    assert hidden_payload == {
        "status": "error",
        "error": "Record field 'margin' is not editable for this subject",
        "code": "forbidden",
        "denied_fields": ["margin"],
    }
    assert admin_status == 200
    assert admin_payload["record"]["status"] == "sent"
    assert admin_payload["record"]["margin"] == "45"


def test_collection_records_enforcement_returns_payment_required(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "reports", "id\ttitle\n1\tRevenue\n")
    save_permission_policy(data_dir, {"access_mode": "subscription"})
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")

    status, _, payload = request(
        "/collections/reports/records",
        headers=[("x-dbbasic-user-id", "7")],
    )

    assert status == 402
    assert payload == {"status": "error", "error": "subscription required", "code": "payment_required"}


def test_collection_records_audit_mode_logs_without_filtering(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\n1\tAlice\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_AUDIT_ENV, "true")

    status, _, payload = request("/collections/contacts/records")

    assert status == 200
    assert payload["records"] == [{"id": "1", "name": "Alice"}]
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["collection"] == "contacts"
    assert entry["action"] == "read"
    assert entry["decision"]["allowed"] is False
    assert entry["enforced"] is False


def test_schema_list_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/schemas")

    assert status == 403
    assert payload == {"status": "error", "error": "Schema listing requires DBBASIC_ADMIN_TOKEN."}


def test_schema_list_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_schema_list_returns_manual_and_derived_summaries(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "contacts" / "directory.py", "def GET(request):\n    return {}\n")
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "title": "Invoices",
                "fields": [
                    {"name": "invoice_date", "type": "date", "required": True},
                    {"name": "total", "type": "computed"},
                ],
            }
        )
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "schemas": [
            {
                "name": "contacts",
                "title": "Contacts",
                "source": "derived",
                "version": 1,
                "field_count": 0,
            },
            {
                "name": "invoices",
                "title": "Invoices",
                "source": "manual",
                "version": 1,
                "field_count": 2,
            },
        ],
        "count": 2,
    }


def test_schema_detail_returns_manual_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_file = data_dir / "schemas" / "invoices.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "title": "Invoices",
                "fields": [
                    {
                        "name": "customer_id",
                        "type": "relation",
                        "relation": {"collection": "contacts"},
                        "required": True,
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/invoices", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "schema": {
            "name": "invoices",
            "title": "Invoices",
            "source": "manual",
            "version": 1,
            "fields": [
                {
                    "name": "customer_id",
                    "type": "relation",
                    "required": True,
                    "relation": {"collection": "contacts"},
                }
            ],
            "field_count": 1,
        },
    }


def test_schema_detail_returns_derived_schema_for_collection(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "contacts" / "directory.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/contacts", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "schema": {
            "name": "contacts",
            "title": "Contacts",
            "source": "derived",
            "version": 1,
            "fields": [],
            "field_count": 0,
        },
    }


def test_schema_put_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/schemas/invoices", method="PUT", body=b"{}")

    assert status == 403
    assert payload == {"status": "error", "error": "Schema writes require DBBASIC_ADMIN_TOKEN."}


def test_schema_put_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/invoices", method="PUT", body=b"{}")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_schema_put_writes_manual_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    body = json.dumps(
        {
            "title": "Invoices",
            "ui": {"default_view": "form"},
            "views": [{"name": "invoice_admin", "type": "form"}],
            "fields": [
                {
                    "name": "invoice_date",
                    "type": "date",
                    "required": True,
                    "layout": {"column": 1},
                },
                {
                    "name": "margin",
                    "type": "currency",
                    "permissions": {"admin": "edit", "sales": "hidden"},
                },
            ],
        }
    ).encode()

    status, _, payload = request(
        "/schemas/invoices",
        method="PUT",
        body=body,
        headers=auth_headers(),
    )
    detail_status, _, detail_payload = request("/schemas/invoices", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "message": "Schema updated to version 1",
        "version_id": 1,
        "collection": "invoices",
        "schema": {
            "name": "invoices",
            "title": "Invoices",
            "source": "manual",
            "version": 1,
            "fields": [
                {
                    "name": "invoice_date",
                    "type": "date",
                    "required": True,
                    "layout": {"column": 1},
                },
                {
                    "name": "margin",
                    "type": "currency",
                    "required": False,
                    "permissions": {"admin": "edit", "sales": "hidden"},
                },
            ],
            "field_count": 2,
            "ui": {"default_view": "form"},
            "views": [{"name": "invoice_admin", "type": "form"}],
        },
    }
    assert detail_status == 200
    assert detail_payload == {"status": "ok", "schema": payload["schema"]}
    assert (data_dir / "schemas" / "invoices.json").exists()
    assert (data_dir / "schema_versions" / "invoices" / "v1.json").exists()


def test_schema_put_accepts_nested_schema_payload(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/schemas/contacts",
        method="PUT",
        body=json.dumps({"schema": {"fields": [{"name": "email", "type": "email"}]}}).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["schema"]["name"] == "contacts"
    assert payload["schema"]["fields"] == [
        {"name": "email", "type": "email", "required": False}
    ]


def test_schema_versions_endpoint_lists_history_newest_first(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps(
            {
                "schema": {"fields": [{"name": "invoice_date", "type": "date"}]},
                "author": "admin",
                "message": "first schema",
            }
        ).encode(),
        headers=auth_headers(),
    )
    request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps(
            {
                "schema": {"fields": [{"name": "invoice_date", "type": "date"}, {"name": "total"}]},
                "author": "admin",
                "message": "second schema",
            }
        ).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/schemas/invoices",
        query_string="versions=true&limit=10",
        headers=auth_headers(),
    )
    limited_status, _, limited_payload = request(
        "/schemas/invoices",
        query_string="versions=true&limit=1",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["collection"] == "invoices"
    assert [version["version_id"] for version in payload["versions"]] == [2, 1]
    assert [version["message"] for version in payload["versions"]] == [
        "second schema",
        "first schema",
    ]
    assert all("content" not in version for version in payload["versions"])
    assert limited_status == 200
    assert [version["version_id"] for version in limited_payload["versions"]] == [2]


def test_schema_versions_endpoint_requires_authorization_header(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "schemas").mkdir(parents=True)
    (data_dir / "schemas" / "invoices.json").write_text('{"fields": []}\n')
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/invoices", query_string="versions=true")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_schema_version_endpoint_returns_content_and_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps(
            {
                "schema": {
                    "title": "Invoices",
                    "fields": [{"name": "invoice_date", "type": "date"}],
                },
                "author": "admin",
                "message": "first schema",
            }
        ).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/schemas/invoices",
        query_string="version=1",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["collection"] == "invoices"
    assert payload["version"]["version_id"] == 1
    assert payload["version"]["message"] == "first schema"
    assert payload["version"]["schema"]["fields"] == [
        {"name": "invoice_date", "type": "date", "required": False}
    ]
    assert '"invoice_date"' in payload["version"]["content"]


def test_schema_rollback_creates_new_latest_version(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps({"fields": [{"name": "invoice_date", "type": "date"}]}).encode(),
        headers=auth_headers(),
    )
    request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps({"fields": [{"name": "invoice_date", "type": "date"}, {"name": "total"}]}).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/schemas/invoices",
        method="POST",
        body=json.dumps(
            {
                "action": "rollback",
                "version_id": 1,
                "author": "admin",
                "message": "restore first",
            }
        ).encode(),
        headers=auth_headers(),
    )
    detail_status, _, detail_payload = request("/schemas/invoices", headers=auth_headers())
    history_status, _, history_payload = request(
        "/schemas/invoices",
        query_string="versions=true&limit=10",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["message"] == "Rolled back schema to version 1"
    assert payload["version_id"] == 1
    assert payload["new_version_id"] == 3
    assert payload["schema"]["fields"] == [
        {"name": "invoice_date", "type": "date", "required": False}
    ]
    assert detail_status == 200
    assert detail_payload["schema"]["fields"] == payload["schema"]["fields"]
    assert history_status == 200
    assert [version["version_id"] for version in history_payload["versions"]] == [3, 2, 1]
    assert history_payload["versions"][0]["message"] == "restore first"


def test_schema_rollback_returns_404_for_missing_version(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/schemas/invoices",
        method="POST",
        body=json.dumps({"action": "rollback", "version_id": 99}).encode(),
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {
        "status": "error",
        "error": "Version 99 not found for schema invoices",
    }


def test_schema_put_rejects_invalid_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    name_status, _, name_payload = request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps({"name": "contacts", "fields": []}).encode(),
        headers=auth_headers(),
    )
    field_status, _, field_payload = request(
        "/schemas/invoices",
        method="PUT",
        body=json.dumps({"fields": [{"name": "bad.name"}]}).encode(),
        headers=auth_headers(),
    )

    assert name_status == 400
    assert name_payload == {
        "status": "error",
        "error": "Schema file name does not match schema collection: invoices",
    }
    assert field_status == 400
    assert field_payload == {
        "status": "error",
        "error": "Schema field has invalid name: invoices",
    }


def test_schema_detail_rejects_invalid_name(tmp_path, monkeypatch):
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/bad.name", headers=auth_headers())

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid schema name: bad.name"}


def test_schema_detail_rejects_missing_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/schemas/missing", headers=auth_headers())

    assert status == 404
    assert payload == {"status": "error", "error": "Schema not found: missing"}


def test_schema_routes_reject_non_get_methods(tmp_path, monkeypatch):
    enable_admin_token(monkeypatch)

    list_status, _, list_payload = request("/schemas", method="POST", headers=auth_headers())
    detail_status, _, detail_payload = request(
        "/schemas/contacts",
        method="POST",
        headers=auth_headers(),
    )

    assert list_status == 405
    assert detail_status == 405
    assert list_payload == {"status": "error", "error": "Method not allowed"}
    assert detail_payload == {"status": "error", "error": "Method not allowed"}


def test_permissions_policy_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/permissions/policy")

    assert status == 403
    assert payload == {"status": "error", "error": "Permissions API requires DBBASIC_ADMIN_TOKEN."}


def test_permissions_policy_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/policy")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_permissions_policy_get_returns_default_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/policy", headers=auth_headers())

    assert status == 200
    assert payload == {
        "status": "ok",
        "policy": {
            "access_mode": "role_based",
            "roles": {},
            "user_roles": {},
            "rules": [],
            "admin_roles": ["admin", "superuser"],
        },
    }


def test_permissions_status_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/permissions/status")

    assert status == 403
    assert payload == {"status": "error", "error": "Permissions API requires DBBASIC_ADMIN_TOKEN."}


def test_permissions_status_reports_default_policy_blocker(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/status", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["permissions"]["enforcement_enabled"] is False
    assert payload["permissions"]["enforcement_requested"] is False
    assert payload["permissions"]["enforcement_blocked"] is False
    assert payload["permissions"]["allow_unready_enforcement"] is False
    assert payload["permissions"]["audit_enabled"] is False
    assert payload["permissions"]["trusted_headers_enabled"] is False
    assert payload["permissions"]["admin_token_configured"] is True
    assert payload["identity"]["accounts"] == {"count": 0, "active": 0, "disabled": 0}
    assert payload["identity"]["users"] == {"count": 0, "active": 0, "disabled": 0}
    assert payload["identity"]["sessions"] == {"count": 0, "active": 0, "revoked": 0}
    assert payload["policy"]["access_mode"] == "role_based"
    assert payload["policy"]["policy_file_exists"] is False
    assert payload["policy"]["rules_count"] == 0
    assert payload["readiness"] == {
        "can_enable_enforcement": False,
        "blockers": ["Role-based policy has no grants; non-admin traffic will be denied."],
    }
    assert "Permission enforcement is off." in payload["warnings"]
    assert "object execution and mutation routes" in payload["coverage"]["policy_checked"]


def test_permissions_status_reports_blocked_enforcement_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/status", headers=auth_headers())

    assert status == 200
    assert payload["permissions"]["enforcement_enabled"] is False
    assert payload["permissions"]["enforcement_requested"] is True
    assert payload["permissions"]["enforcement_blocked"] is True
    assert payload["permissions"]["audit_enabled"] is True
    assert payload["readiness"] == {
        "can_enable_enforcement": False,
        "blockers": ["Role-based policy has no grants; non-admin traffic will be denied."],
    }
    assert (
        "Permission enforcement was requested but readiness checks blocked rollout."
        in payload["warnings"]
    )


def test_permissions_status_reports_identity_counts_and_ready_policy(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_AUDIT_ENV, "true")
    monkeypatch.setenv(object_server.REQUIRE_KNOWN_IDENTITY_USERS_ENV, "true")
    enable_admin_token(monkeypatch)
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "roles": {"sales": {"label": "Sales"}},
            "user_roles": {"u_7": ["sales"]},
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["read"],
                    "collection": "contacts",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
    )
    request(
        "/identity/accounts",
        method="POST",
        body=json.dumps({"account_id": "acme", "name": "Acme"}).encode(),
        headers=auth_headers(),
    )
    request(
        "/identity/users",
        method="POST",
        body=json.dumps({"user_id": "u_7", "account_id": "acme", "roles": ["sales"]}).encode(),
        headers=auth_headers(),
    )
    request(
        "/identity/sessions",
        method="POST",
        body=json.dumps({"user_id": "u_7", "label": "scroll"}).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request("/permissions/status", headers=auth_headers())

    assert status == 200
    assert payload["permissions"]["audit_enabled"] is True
    assert payload["permissions"]["enforcement_enabled"] is False
    assert payload["permissions"]["enforcement_requested"] is False
    assert payload["permissions"]["enforcement_blocked"] is False
    assert payload["permissions"]["require_known_identity_users"] is True
    assert payload["identity"]["accounts"] == {"count": 1, "active": 1, "disabled": 0}
    assert payload["identity"]["users"] == {"count": 1, "active": 1, "disabled": 0}
    assert payload["identity"]["sessions"]["count"] == 1
    assert payload["identity"]["sessions"]["active"] == 1
    assert payload["policy"]["policy_file_exists"] is True
    assert payload["policy"]["rules_count"] == 1
    assert payload["policy"]["allow_rules"] == 1
    assert payload["policy"]["principals"] == ["role:sales"]
    assert payload["policy"]["collections"] == ["contacts"]
    assert payload["readiness"] == {"can_enable_enforcement": True, "blockers": []}


def test_permissions_status_rejects_non_get_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/status", method="POST", headers=auth_headers())

    assert status == 405
    assert payload == {"status": "error", "error": "Method not allowed"}


def test_permissions_policy_put_persists_policy(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)
    policy = {
        "access_mode": "role_based",
        "roles": {"sales": {"label": "Sales"}},
        "user_roles": {"7": ["sales"]},
        "rules": [
            {
                "effect": "allow",
                "principal": "role:sales",
                "actions": ["read"],
                "collection": "contacts",
                "row_filter": {"owner_id": "$user_id"},
                "reason": "sales reps only see own contacts",
            }
        ],
        "admin_roles": ["admin"],
    }

    status, _, payload = request(
        "/permissions/policy",
        method="PUT",
        body=json.dumps({"policy": policy}).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["policy"]["access_mode"] == "role_based"
    assert (data_dir / "permissions" / "policy.json").exists()

    status, _, payload = request("/permissions/policy", headers=auth_headers())

    assert status == 200
    assert payload["policy"]["rules"][0]["principal"] == "role:sales"
    assert payload["policy"]["rules"][0]["row_filter"] == {"owner_id": "$user_id"}


def test_permissions_policy_put_rejects_invalid_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/permissions/policy",
        method="PUT",
        body=json.dumps({"policy": {"access_mode": "unknown"}}).encode(),
        headers=auth_headers(),
    )

    assert status == 400
    assert payload["status"] == "error"
    assert "Permission access_mode must be one of:" in payload["error"]


def test_permissions_check_uses_persisted_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)
    policy = {
        "access_mode": "role_based",
        "rules": [
            {
                "effect": "allow",
                "principal": "role:sales",
                "actions": ["read"],
                "collection": "contacts",
                "row_filter": {"owner_id": "$user_id"},
                "reason": "sales reps only see own contacts",
            }
        ],
    }
    request(
        "/permissions/policy",
        method="PUT",
        body=json.dumps({"policy": policy}).encode(),
        headers=auth_headers(),
    )

    status, _, payload = request(
        "/permissions/check",
        method="POST",
        body=json.dumps(
            {
                "subject": {"user_id": "7", "roles": ["sales"]},
                "action": "read",
                "collection": "contacts",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "decision": {
            "allowed": True,
            "reason": "sales reps only see own contacts",
            "code": "allowed",
            "http_status": 200,
            "row_filter": {"owner_id": "$user_id"},
            "fields": None,
            "denied_fields": [],
        },
    }


def test_permissions_check_accepts_inline_policy_for_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/permissions/check",
        method="POST",
        body=json.dumps(
            {
                "policy": {
                    "access_mode": "role_based",
                    "rules": [
                        {
                            "effect": "allow",
                            "principal": "account:customer-acme",
                            "actions": ["read"],
                            "collection": "invoices",
                            "row_filter": {"customer_account_id": "$account_id"},
                            "fields": ["invoice_id", "total"],
                            "denied_fields": ["internal_notes"],
                        }
                    ],
                },
                "subject": {"user_id": "9", "account_id": "customer-acme"},
                "action": "read",
                "collection": "invoices",
                "record": {"customer_account_id": "customer-acme", "total": 120},
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["decision"]["allowed"] is True
    assert payload["decision"]["fields"] == ["invoice_id", "total"]
    assert payload["decision"]["denied_fields"] == ["internal_notes"]


def test_permissions_check_returns_payment_required_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/permissions/check",
        method="POST",
        body=json.dumps(
            {
                "policy": {"access_mode": "subscription"},
                "subject": {"user_id": "42"},
                "action": "read",
                "collection": "premium_reports",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["decision"]["allowed"] is False
    assert payload["decision"]["code"] == "payment_required"
    assert payload["decision"]["http_status"] == 402


def test_permissions_audit_requires_admin_token_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/permissions/audit")

    assert status == 403
    assert payload == {"status": "error", "error": "Permissions API requires DBBASIC_ADMIN_TOKEN."}


def test_permissions_audit_requires_authorization_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/audit")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_permissions_audit_returns_recent_entries_with_filters(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)
    object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:00Z",
            "action": "execute",
            "object_id": "site_home",
            "collection": "site",
            "enforced": False,
            "decision": {"allowed": False},
        },
        data_dir,
    )
    object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:01Z",
            "action": "source",
            "object_id": "basics_counter",
            "collection": "basics",
            "enforced": True,
            "decision": {"allowed": True},
        },
        data_dir,
    )

    status, _, payload = request(
        "/permissions/audit",
        query_string="action=source&allowed=true&enforced=true&limit=10",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "entries": [
            {
                "timestamp": "2026-06-29T00:00:01Z",
                "action": "source",
                "object_id": "basics_counter",
                "collection": "basics",
                "enforced": True,
                "decision": {"allowed": True},
            }
        ],
        "count": 1,
    }


def test_permissions_audit_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/audit", headers=auth_headers())

    assert status == 200
    assert payload == {"status": "ok", "entries": [], "count": 0}


def test_permissions_audit_rejects_bad_query_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/permissions/audit",
        query_string="allowed=maybe",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {
        "status": "error",
        "error": "Query parameter 'allowed' must be a boolean",
    }


def test_permissions_audit_rejects_non_get_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/permissions/audit", method="POST", headers=auth_headers())

    assert status == 405
    assert payload == {"status": "error", "error": "Method not allowed"}


def test_permission_enforcement_is_disabled_by_default(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    status, _, payload = request("/objects/site_home")

    assert status == 200
    assert payload == {"ok": True}
    assert object_permission_audit.get_permission_audit(data_dir) == []


def test_permission_enforcement_request_with_default_policy_stays_in_shadow_mode(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")

    status, _, payload = request("/objects/site_home")

    assert status == 200
    assert payload == {"ok": True}
    entries = object_permission_audit.get_permission_audit(data_dir)
    assert entries[-1]["action"] == "execute"
    assert entries[-1]["object_id"] == "site_home"
    assert entries[-1]["collection"] == "site"
    assert entries[-1]["decision"]["allowed"] is False
    assert entries[-1]["enforced"] is False
    assert entries[-1]["enforcement_requested"] is True
    assert entries[-1]["enforcement_blocked"] is True


def test_permission_enforcement_can_force_default_policy(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_UNREADY_ENFORCEMENT_ENV, "true")

    status, _, payload = request("/objects/site_home")

    assert status == 403
    assert payload == {"status": "error", "error": "no matching role rule", "code": "forbidden"}
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["decision"]["allowed"] is False
    assert entry["enforced"] is True
    assert entry["enforcement_requested"] is True


def test_permission_enforcement_allows_public_policy_execution(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    save_permission_policy(data_dir, {"access_mode": "public"})
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")

    status, _, payload = request("/objects/site_home")

    assert status == 200
    assert payload == {"ok": True}
    assert object_permission_audit.get_permission_audit(data_dir)[-1]["decision"]["allowed"] is True


def test_permission_enforcement_admin_token_bypasses_policy(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/site_home", headers=auth_headers())

    assert status == 200
    assert payload == {"ok": True}
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["subject"]["roles"] == ["admin"]
    assert entry["decision"]["reason"] == "admin role"


def test_permission_enforcement_uses_trusted_subject_headers(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "role:sales",
                    "actions": ["execute"],
                    "object_id": "site_home",
                    "reason": "sales can execute site home",
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")

    status, _, payload = request(
        "/objects/site_home",
        headers=[
            ("x-dbbasic-user-id", "7"),
            ("x-dbbasic-account-id", "customer-acme"),
            ("x-dbbasic-roles", "sales"),
        ],
    )

    assert status == 200
    assert payload == {"ok": True}
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["subject"]["user_id"] == "7"
    assert entry["subject"]["account_id"] == "customer-acme"
    assert entry["subject"]["roles"] == ["sales"]
    assert entry["decision"]["reason"] == "sales can execute site home"


def test_permission_audit_shadow_mode_logs_without_blocking(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_AUDIT_ENV, "true")

    status, _, payload = request("/objects/site_home")

    assert status == 200
    assert payload == {"ok": True}
    entry = object_permission_audit.get_permission_audit(data_dir)[-1]
    assert entry["decision"]["allowed"] is False
    assert entry["enforced"] is False


def test_get_source_returns_existing_contract_shape(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="source=true&format=json",
        headers=auth_headers(),
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
        "/objects/basics_counter@remote_node",
        query_string="source=true&format=json",
    )

    assert status == 400
    assert payload["status"] == "error"
    assert "Station routing is not available" in payload["error"]


def test_get_source_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/missing_object",
        query_string="source=true&format=json",
        headers=auth_headers(),
    )

    assert status == 404
    assert payload["status"] == "error"
    assert payload["error"] == "Object source not found: missing_object"


def test_get_source_requires_authorization_header(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="source=true&format=json",
    )

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_get_state_returns_empty_state_for_object_without_state_file(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="state=true",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="state=true",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/missing_object",
        query_string="state=true",
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_state_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/bad.id",
        query_string="state=true",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


def test_get_logs_returns_empty_logs_for_object_without_log_file(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true&level=ERROR&limit=1",
        headers=auth_headers(),
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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true&limit=0",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {
        "status": "error",
        "error": "Query parameter 'limit' must be at least 1",
    }


def test_get_logs_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/missing_object",
        query_string="logs=true",
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_logs_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/bad.id",
        query_string="logs=true",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


def test_get_files_lists_object_owned_files(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {}\n")
    file_path = data_dir / "files" / "site_home" / "assets" / "report.txt"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("hello")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/site_home",
        query_string="files=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["object_id"] == "site_home"
    assert payload["count"] == 1
    assert payload["files"][0]["name"] == "assets/report.txt"
    assert payload["files"][0]["size"] == 5


def test_get_files_requires_admin_token(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/site_home", query_string="files=true")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_get_file_downloads_object_owned_file(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {}\n")
    file_path = data_dir / "files" / "site_home" / "image.png"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"\x89PNG")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, headers, body = raw_request(
        "/objects/site_home",
        query_string="file=image.png",
        headers=auth_headers(),
    )

    assert status == 200
    assert headers[b"content-type"] == b"image/png"
    assert headers[b"content-disposition"] == b'inline; filename="image.png"'
    assert body == b"\x89PNG"


def test_get_file_requires_admin_token(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {}\n")
    file_path = data_dir / "files" / "site_home" / "image.png"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"\x89PNG")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/site_home", query_string="file=image.png")

    assert status == 401
    assert payload == {"status": "error", "error": "Unauthorized"}


def test_get_file_rejects_path_traversal(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/site_home",
        query_string="file=../secret.txt",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload["status"] == "error"
    assert payload["error"].startswith("Invalid filename:")


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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="metadata=true",
        headers=auth_headers(),
    )

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
            "file_count": 0,
            "version_count": 1,
        },
    }


def test_get_metadata_returns_404_for_missing_object(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/missing_object",
        query_string="metadata=true",
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {"status": "error", "error": "Object source not found: missing_object"}


def test_get_metadata_rejects_invalid_object_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/bad.id",
        query_string="metadata=true",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid object ID: bad.id"}


def test_source_update_is_disabled_by_default(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.delenv("DBBASIC_ENABLE_SOURCE_WRITES", raising=False)
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"code": "def GET(request):\n    return {'count': 1}\n"}).encode(),
        headers=auth_headers(),
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
    enable_admin_token(monkeypatch)

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
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=b"{",
        headers=auth_headers(),
    )

    assert status == 400
    assert payload == {"status": "error", "error": "Invalid JSON body"}
    assert source_path.read_text() == "def GET(request):\n    return {}\n"


def test_source_update_requires_code_string(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        method="PUT",
        query_string="source=true",
        body=json.dumps({"source": "wrong field"}).encode(),
        headers=auth_headers(),
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
    enable_admin_token(monkeypatch)
    new_code = "def GET(request):\n    return {'count': 2}\n"
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

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
        headers=[*auth_headers(), ("x-dbbasic-correlation-id", correlation_id)],
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "message": "Code updated to version 1",
        "version_id": 1,
        "object_id": "basics_counter",
        "correlation_id": correlation_id,
    }
    assert source_path.read_text() == new_code

    manager = object_versions.VersionManager(data_dir)
    saved = manager.get_version("basics_counter", 1)
    assert saved is not None
    assert saved["content"] == new_code
    assert saved["author"] == "test-api"
    assert saved["message"] == "Update counter"
    assert saved["correlation_id"] == correlation_id

    logs = object_logs.get_object_logs("basics_counter", base_dir=data_dir)
    assert logs[0]["action"] == "source_update"
    assert logs[0]["version_id"] == "1"
    assert logs[0]["correlation_id"] == correlation_id

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
        headers=auth_headers(),
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
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["count"] == 1
    assert [version["version_id"] for version in payload["versions"]] == [2]


def test_versions_endpoint_rejects_bad_limit(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="versions=true&limit=0",
        headers=auth_headers(),
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

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="version=1",
        headers=auth_headers(),
    )

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

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="version=99",
        headers=auth_headers(),
    )

    assert status == 404
    assert payload == {
        "status": "error",
        "error": "Version 99 not found for object basics_counter",
    }


def test_specific_version_endpoint_rejects_bad_version_id(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 0}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="version=bad",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)

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
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

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
        headers=[*auth_headers(), ("x-dbbasic-correlation-id", correlation_id)],
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "message": "Rolled back to version 1",
        "version_id": 1,
        "new_version_id": 3,
        "object_id": "basics_counter",
        "correlation_id": correlation_id,
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
    assert latest["correlation_id"] == correlation_id

    logs = object_logs.get_object_logs("basics_counter", base_dir=data_dir)
    rollback_logs = [entry for entry in logs if entry.get("action") == "source_rollback"]
    assert rollback_logs[-1]["correlation_id"] == correlation_id
    assert rollback_logs[-1]["version_id"] == "3"
    assert rollback_logs[-1]["from_version_id"] == "1"


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


def test_request_body_limit_allows_body_at_configured_limit(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "echo.py",
        "def POST(request):\n    return {'request': request}\n",
    )
    body = json.dumps({"name": "body"}).encode()
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, str(len(body)))

    status, _, payload = request("/objects/basics_echo", method="POST", body=body)

    assert status == 200
    assert payload == {"request": {"name": "body"}}


def test_request_body_limit_rejects_oversized_json_body(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "echo.py", "def POST(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "7")

    status, _, payload = request(
        "/objects/basics_echo",
        method="POST",
        body=json.dumps({"name": "body"}).encode(),
    )

    assert status == 413
    assert payload == {
        "status": "error",
        "error": "Request body too large",
        "max_bytes": 7,
    }


def test_request_body_limit_rejects_large_content_length(monkeypatch):
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "2")

    status, _, payload = request(
        "/objects/basics_echo",
        method="POST",
        body=b"{}",
        headers=[("content-length", "3")],
    )

    assert status == 413
    assert payload == {
        "status": "error",
        "error": "Request body too large",
        "max_bytes": 2,
    }


def test_request_body_limit_rejects_oversized_chunked_body(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "raw.py", "def POST(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "8")

    status, _, payload = request(
        "/objects/basics_raw",
        method="POST",
        body_chunks=[b"not-json", b"-oversized"],
    )

    assert status == 413
    assert payload == {
        "status": "error",
        "error": "Request body too large",
        "max_bytes": 8,
    }


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


def test_object_execution_returns_html_content_type_response(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "site" / "home.py",
        "def GET(request):\n"
        "    return {\n"
        "        'content_type': 'text/html; charset=utf-8',\n"
        "        'body': '<!doctype html><h1>DBBASIC</h1>',\n"
        "    }\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, headers, body = raw_request("/objects/site_home")

    assert status == 200
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    assert body == b"<!doctype html><h1>DBBASIC</h1>"


def test_object_execution_returns_binary_content_type_response(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "image.py",
        "def GET(request):\n"
        "    return {\n"
        "        'content_type': 'image/png',\n"
        "        'body': b'\\x89PNG',\n"
        "    }\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, headers, body = raw_request("/objects/basics_image")

    assert status == 200
    assert headers[b"content-type"] == b"image/png"
    assert body == b"\x89PNG"


def test_object_execution_returns_response_tuple(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "created.py",
        "def POST(request):\n"
        "    return (201, [('Content-Type', 'text/plain'), ('X-Object', 'created')], [b'ok'])\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, headers, body = raw_request("/objects/basics_created", method="POST")

    assert status == 201
    assert headers[b"content-type"] == b"text/plain"
    assert headers[b"x-object"] == b"created"
    assert body == b"ok"


def test_object_execution_returns_plain_string_as_html(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "site" / "home.py", "def GET(request):\n    return '<h1>DBBASIC</h1>'\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    status, headers, body = raw_request("/objects/site_home")

    assert status == 200
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    assert body == b"<h1>DBBASIC</h1>"


def test_object_execution_appends_success_log(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/basics_counter")
    assert status == 200
    assert payload == {"count": 1}

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)

    assert request("/objects/basics_counter")[2] == {"count": 1}
    assert request("/objects/basics_counter")[2] == {"count": 2}

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="state=true",
        headers=auth_headers(),
    )

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
    enable_admin_token(monkeypatch)
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

    status, _, payload = request(
        "/objects/basics_counter",
        headers=[("x-dbbasic-correlation-id", correlation_id)],
    )

    assert status == 200
    assert payload == {"ok": True}

    status, _, payload = request(
        "/objects/basics_counter",
        query_string="logs=true",
        headers=auth_headers(),
    )

    assert status == 200
    messages = [entry["message"] for entry in payload["logs"]]
    assert "Counter was read" in messages
    assert "GET completed successfully" in messages
    assert {
        entry["correlation_id"]
        for entry in payload["logs"]
        if entry["message"] in {"Counter was read", "GET completed successfully"}
    } == {correlation_id}


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
    assert payload["status"] == "error"
    assert payload["error"] == "Object not found: missing_object"
    assert object_correlation.normalize_correlation_id(payload["correlation_id"]) == payload[
        "correlation_id"
    ]


def test_execution_concurrency_limit_returns_503_when_full(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'ok': True}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.MAX_CONCURRENT_EXECUTIONS_ENV, "1")
    token = claim_limit_slot(object_server._execution_limiter, 1)
    try:
        status, _, payload = request("/objects/basics_counter")
    finally:
        token.release()

    assert status == 503
    assert payload == {
        "status": "error",
        "error": "Server is busy",
        "limit": "object_executions",
        "max_concurrent": 1,
    }


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


def test_object_execution_timeout_returns_504_and_logs(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(
        root / "basics" / "slow.py",
        "import time\n\ndef GET(request):\n    time.sleep(5)\n    return {'ok': True}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "0.5")
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/basics_slow")

    assert status == 504
    assert payload["status"] == "error"
    assert payload["error"] == "Execution failed: GET timed out for object basics_slow after 0.5 seconds"
    assert object_correlation.normalize_correlation_id(payload["correlation_id"]) == payload[
        "correlation_id"
    ]

    status, _, payload = request(
        "/objects/basics_slow",
        query_string="logs=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["count"] == 1
    assert payload["logs"][0]["level"] == "ERROR"
    assert payload["logs"][0]["error_type"] == object_execution.TIMEOUT_ERROR_TYPE
    assert payload["logs"][0]["status"] == "error"


def test_timeout_enabled_uses_subprocess_for_untrusted_objects(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "fast.py",
        "def GET(request):\n    return {'mode': 'object_source'}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    calls = []

    def fake_subprocess(request, roots=None, *, timeout_seconds, raise_on_error=False):
        calls.append((request.object_id, timeout_seconds))
        return object_execution.ObjectExecutionResult(
            object_id=request.object_id,
            method=request.normalized_method(),
            path=request.path,
            ok=True,
            result={"mode": "subprocess"},
            error=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:00Z",
            duration_ms=1.0,
        )

    monkeypatch.setattr(
        object_execution,
        "execute_python_object_subprocess",
        fake_subprocess,
    )

    status, _, payload = request("/objects/basics_fast")

    assert status == 200
    assert payload == {"mode": "subprocess"}
    assert calls == [("basics_fast", 5.0)]


def test_trusted_in_process_object_bypasses_subprocess_timeout_path(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "fast.py",
        "def GET(request):\n    return {'mode': 'in_process'}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_server.TRUSTED_IN_PROCESS_OBJECTS_ENV, "basics_fast")

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("trusted object should not use subprocess execution")

    monkeypatch.setattr(
        object_execution,
        "execute_python_object_subprocess",
        fail_subprocess,
    )

    status, _, payload = request("/objects/basics_fast")

    assert status == 200
    assert payload == {"mode": "in_process"}


def test_execution_concurrency_limit_releases_after_object_error(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.MAX_CONCURRENT_EXECUTIONS_ENV, "1")

    first_status, _, first_payload = request("/objects/basics_broken")
    second_status, _, second_payload = request("/objects/basics_broken")

    assert first_status == 500
    assert "RuntimeError: boom" in first_payload["error"]
    assert second_status == 500
    assert "RuntimeError: boom" in second_payload["error"]


def test_object_execution_appends_error_log(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(
        root / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/objects/basics_broken")
    assert status == 500
    assert "RuntimeError: boom" in payload["error"]

    status, _, payload = request(
        "/objects/basics_broken",
        query_string="logs=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["count"] == 1
    log = payload["logs"][0]
    assert log["level"] == "ERROR"
    assert log["message"].startswith("GET failed:")
    assert log["method"] == "GET"
    assert log["status"] == "error"
    assert log["error_type"] == "ObjectMethodExecutionError"
    assert "RuntimeError: boom" in log["error"]


def test_events_api_requires_admin_token(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/events", headers=auth_headers())

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Events API requires DBBASIC_ADMIN_TOKEN.",
    }


def test_events_api_publishes_and_lists_daemon_compatible_events(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/events",
        method="POST",
        body=json.dumps(
            {
                "event_type": "collection.record.created",
                "source": "records",
                "payload": {"collection": "contacts", "record_id": "c1"},
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    assert payload["status"] == "ok"
    event = payload["event"]
    assert event["event_type"] == "collection.record.created"
    assert event["source"] == "records"
    assert event["actor"] == "admin"

    state_file = data_dir / "state" / "events" / "state.tsv"
    row = state_file.read_text().strip().split("\t")
    assert row[0].startswith("event_")
    assert json.loads(row[1]) == event

    status, _, payload = request(
        "/events",
        query_string="event_type=collection.record.created",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["events"] == [event]


def test_events_api_prunes_event_queue(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    for record_id in ("c1", "c2"):
        status, _, payload = request(
            "/events",
            method="POST",
            body=json.dumps(
                {
                    "event_type": "collection.record.created",
                    "source": "records",
                    "payload": {"collection": "contacts", "record_id": record_id},
                }
            ).encode(),
            headers=auth_headers(),
        )
        assert status == 201
        assert payload["status"] == "ok"

    status, _, payload = request(
        "/events",
        method="DELETE",
        query_string="keep_count=1&keep_seconds=0",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["retention"]["deleted"] == 1
    assert payload["retention"]["kept"] == 1

    status, _, payload = request("/events", headers=auth_headers())

    assert status == 200
    assert payload["total"] == 1
    assert payload["events"][0]["payload"]["record_id"] in {"c1", "c2"}


def test_event_subscription_api_creates_lists_and_deletes(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/events/subscriptions",
        method="POST",
        body=json.dumps(
            {
                "event_type": "collection.record.updated",
                "subscriber_id": "scroll",
                "callback_url": "https://example.com/hooks/dbbasic",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 201
    subscription = payload["subscription"]
    assert subscription["id"] == "scroll"
    assert subscription["event_type"] == "collection.record.updated"
    assert subscription["callback_url"] == "https://example.com/hooks/dbbasic"
    assert subscription["last_event_id"] is None

    status, _, payload = request(
        "/events/subscriptions",
        query_string="event_type=collection.record.updated",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["subscriptions"] == [subscription]

    status, _, payload = request(
        "/events/subscriptions",
        method="DELETE",
        query_string="event_type=collection.record.updated&subscriber_id=scroll",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["deleted"] is True
    assert payload["subscription"] == subscription

    status, _, payload = request("/events/subscriptions", headers=auth_headers())

    assert status == 200
    assert payload["subscriptions"] == []


def test_event_delivery_api_lists_pending_failed_subscriptions(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    event = object_events.publish_event(
        "collection.record.updated",
        payload={"secret": "hidden"},
        base_dir=data_dir,
    )
    subscription = object_events.subscribe_event(
        "collection.record.updated",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=data_dir,
    )
    subscription = object_events.record_subscription_delivery(
        subscription,
        event,
        success=False,
        status_code=502,
        error="bad gateway",
        now=100,
    )
    manager = object_state.ObjectStateManager(object_events.EVENTS_OBJECT_ID, base_dir=data_dir)
    manager.set("sub_collection.record.updated_scroll", json.dumps(subscription))

    status, _, payload = request(
        "/events/deliveries",
        query_string="event_type=collection.record.updated&delivery_status=failed&pending=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    delivery = payload["deliveries"][0]
    assert delivery["subscriber_id"] == "scroll"
    assert delivery["pending"] is True
    assert delivery["pending_count"] == 1
    assert delivery["delivery"]["status"] == "failed"
    assert delivery["delivery"]["last_error"] == "bad gateway"
    assert delivery["callback_url_present"] is True
    assert "callback_url" not in delivery
    assert "payload" not in delivery["next_pending_event"]


def test_event_delivery_api_can_include_callback_and_pending_event_summaries(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    event = object_events.publish_event("invoice.created", payload={"id": "i1"}, base_dir=data_dir)
    object_events.subscribe_event(
        "invoice.created",
        subscriber_id="billing",
        callback_url="https://example.com/hooks/billing",
        base_dir=data_dir,
    )

    status, _, payload = request(
        "/events/deliveries",
        query_string="include_callback_url=true&include_events=true&event_limit=1",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["status"] == "ok"
    delivery = payload["deliveries"][0]
    assert delivery["callback_url"] == "https://example.com/hooks/billing"
    assert delivery["pending_events"] == [delivery["next_pending_event"]]
    assert delivery["pending_events"][0]["id"] == event["id"]
    assert "payload" not in delivery["pending_events"][0]


def test_events_api_rejects_bad_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/events",
        method="POST",
        body=json.dumps({"event_type": "bad event"}).encode(),
        headers=auth_headers(),
    )

    assert status == 400
    assert payload["status"] == "error"
    assert "Invalid event type" in payload["error"]

    status, _, payload = request(
        "/events/subscriptions",
        method="POST",
        body=json.dumps(
            {
                "event_type": "invoice.created",
                "callback_url": "ftp://example.com/hook",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 400
    assert "callback_url" in payload["error"]


def test_packages_api_requires_admin_token(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/packages", headers=auth_headers())

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package listing requires DBBASIC_ADMIN_TOKEN.",
    }


def test_packages_api_lists_package_detail_and_dry_run(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_source(object_root / "hello" / "world.py", "def GET(request): return {}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "description": "Safe package fixture",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
            "schemas": [],
            "permissions": [],
            "seed": [],
            "migrations": [],
        },
        files=(("objects/hello/world.py", "def GET(request): return {}\n"),),
    )
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/packages", headers=auth_headers())

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["packages"][0]["id"] == "hello-world"
    assert payload["packages"][0]["object_count"] == 1

    status, _, payload = request("/packages/hello-world", headers=auth_headers())

    assert status == 200
    assert payload["package"]["id"] == "hello-world"
    assert payload["package"]["objects"] == [
        {"id": "hello_world", "path": "objects/hello/world.py"}
    ]

    status, _, payload = request(
        "/packages/hello-world",
        query_string="dry_run=true",
        headers=auth_headers(),
    )

    assert status == 200
    assert payload["dry_run"]["safe_to_install"] is True
    assert payload["dry_run"]["install_enabled"] is False
    assert payload["dry_run"]["objects"][0]["action"] == "replace"
    assert payload["dry_run"]["warnings"] == []
    assert payload["change"]["package_id"] == "hello-world"
    assert payload["change"]["package_version"] == "0.1.0"
    assert payload["change"]["action"] == "dry_run"
    assert payload["change"]["actor"] == "admin"
    assert payload["change"]["details"]["objects"] == {"replace": 1}
    assert payload["change"]["details"]["safe_to_install"] is True

    status, _, history = request(
        "/packages/hello-world/changes",
        headers=auth_headers(),
    )

    assert status == 200
    assert history["package_id"] == "hello-world"
    assert history["count"] == 1
    assert history["total"] == 1
    assert history["changes"][0]["change_id"] == payload["change"]["change_id"]


def test_package_install_api_requires_admin_and_enable_flag(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DBBASIC_ENABLE_PACKAGE_INSTALLS", raising=False)

    status, _, payload = request("/packages/hello-world/install", method="POST", body=b"{}")

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package installs require DBBASIC_ADMIN_TOKEN.",
    }

    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/packages/hello-world/install",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package installs are disabled. Set DBBASIC_ENABLE_PACKAGE_INSTALLS=true.",
    }


def test_package_install_api_installs_and_records_changes(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_source(object_root / "site" / "home.py", "def GET(request): return {'before': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "description": "Safe package fixture",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
            "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
            "permissions": [],
            "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
            "migrations": [],
        },
        files=(
            ("objects/hello/world.py", "def GET(request): return {'status': 'ok'}\n"),
            ("schemas/contacts.json", '{"name":"contacts","fields":[{"name":"id","type":"text"}]}\n'),
            ("seed/contacts.tsv", "id\tname\nc1\tAlice\n"),
        ),
    )
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_INSTALLS", "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/packages/hello-world/install",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 201
    assert payload["status"] == "ok"
    assert payload["install"]["mode"] == "install"
    assert payload["install"]["objects"][0]["destination"] == "hello/world.py"
    assert payload["install"]["restore_point"] == payload["restore_point"]
    assert payload["restore_point"]["path"].endswith("-package-hello-world.tar.gz")
    assert payload["changes"]["requested"]["action"] == "install_requested"
    assert payload["changes"]["installed"]["action"] == "installed"
    assert (object_root / "hello" / "world.py").is_file()
    assert (data_dir / "schemas" / "contacts.json").is_file()
    assert (data_dir / "collections" / "contacts" / "records.tsv").is_file()
    with tarfile.open(payload["restore_point"]["path"], "r:*") as archive:
        names = archive.getnames()
    assert "objects/site/home.py" in names
    assert "objects/hello/world.py" not in names

    status, _, history = request(
        "/packages/hello-world/changes",
        headers=auth_headers(),
    )

    assert status == 200
    assert [change["action"] for change in history["changes"]] == [
        "installed",
        "install_requested",
    ]
    assert history["changes"][0]["details"]["restore_point"]["path"] == payload["restore_point"]["path"]


def test_package_install_api_records_failed_replace(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_source(object_root / "hello" / "world.py", "def GET(request): return {'old': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_INSTALLS", "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/packages/hello-world/install",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 409
    assert "allow_replace=true" in payload["error"]
    assert payload["changes"]["requested"]["action"] == "install_requested"
    assert payload["changes"]["failed"]["action"] == "failed"
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'old': True}\n"
    assert not (data_dir / "backups").exists()

    status, _, history = request(
        "/packages/hello-world/changes",
        headers=auth_headers(),
    )

    assert status == 200
    assert [change["action"] for change in history["changes"]] == [
        "failed",
        "install_requested",
    ]


def test_package_install_api_fails_before_writes_when_restore_point_fails(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    bad_backups_dir = tmp_path / "not-a-directory"
    bad_backups_dir.write_text("not a directory")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    monkeypatch.setenv("DBBASIC_BACKUPS_DIR", str(bad_backups_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_INSTALLS", "true")
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/packages/hello-world/install",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 500
    assert payload["status"] == "error"
    assert "Package install failed" in payload["error"]
    assert payload["changes"]["requested"]["action"] == "install_requested"
    assert payload["changes"]["failed"]["action"] == "failed"
    assert not (object_root / "hello" / "world.py").exists()

    status, _, history = request(
        "/packages/hello-world/changes",
        headers=auth_headers(),
    )

    assert status == 200
    assert [change["action"] for change in history["changes"]] == [
        "failed",
        "install_requested",
    ]


def test_package_restore_api_requires_admin_and_enable_flag(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request(
        "/packages/hello-world/restore",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package restore requires DBBASIC_ADMIN_TOKEN.",
    }

    enable_admin_token(monkeypatch)

    status, _, payload = request(
        "/packages/hello-world/restore",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package restore is disabled. Set DBBASIC_ENABLE_PACKAGE_RESTORE=true.",
    }


def test_package_restore_api_restores_install_restore_point_and_prunes_new_files(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_source(object_root / "site" / "home.py", "def GET(request): return {'before': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
            "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
            "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
        },
        files=(
            ("objects/hello/world.py", "def GET(request): return {'new': True}\n"),
            ("schemas/contacts.json", '{"name":"contacts","fields":[{"name":"id","type":"text"}]}\n'),
            ("seed/contacts.tsv", "id\tname\nc1\tAlice\n"),
        ),
    )
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_INSTALLS", "true")
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_RESTORE", "true")
    enable_admin_token(monkeypatch)

    status, _, install_payload = request(
        "/packages/hello-world/install",
        method="POST",
        body=b"{}",
        headers=auth_headers(),
    )

    assert status == 201
    installed_change_id = install_payload["changes"]["installed"]["change_id"]
    restore_point_path = install_payload["restore_point"]["path"]
    assert (object_root / "hello" / "world.py").is_file()
    assert (data_dir / "schemas" / "contacts.json").is_file()
    assert (data_dir / "collections" / "contacts" / "records.tsv").is_file()

    status, _, restore_payload = request(
        "/packages/hello-world/restore",
        method="POST",
        body=json.dumps(
            {
                "change_id": installed_change_id,
                "confirm": "restore-runtime",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 200
    assert restore_payload["status"] == "ok"
    assert restore_payload["restore_point"]["path"] == restore_point_path
    assert restore_payload["restore"]["overwritten"] is True
    assert restore_payload["restore"]["pruned_files"] >= 3
    assert restore_payload["changes"]["requested"]["action"] == "restore_requested"
    assert restore_payload["changes"]["rolled_back"]["action"] == "rolled_back"
    assert (
        restore_payload["changes"]["rolled_back"]["details"]["from_change"]["change_id"]
        == installed_change_id
    )
    assert not (object_root / "hello" / "world.py").exists()
    assert not (data_dir / "schemas" / "contacts.json").exists()
    assert not (data_dir / "collections" / "contacts" / "records.tsv").exists()
    assert (object_root / "site" / "home.py").is_file()
    assert Path(restore_point_path).exists()

    status, _, history = request(
        "/packages/hello-world/changes",
        headers=auth_headers(),
    )

    assert status == 200
    assert [change["action"] for change in history["changes"]] == [
        "rolled_back",
        "restore_requested",
        "install_requested",
    ]


def test_package_restore_api_rejects_changes_without_restore_points(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_PACKAGE_RESTORE", "true")
    enable_admin_token(monkeypatch)
    change = object_package_changes.append_package_change(
        package_id="hello-world",
        action="dry_run",
        package_version="0.1.0",
        base_dir=data_dir,
    )

    status, _, payload = request(
        "/packages/hello-world/restore",
        method="POST",
        body=json.dumps(
            {
                "change_id": change["change_id"],
                "confirm": "restore-runtime",
            }
        ).encode(),
        headers=auth_headers(),
    )

    assert status == 400
    assert payload["status"] == "error"
    assert "no restore point" in payload["error"]
    assert payload["changes"]["failed"]["action"] == "failed"


def test_package_changes_api_requires_admin_token(monkeypatch):
    monkeypatch.delenv("DBBASIC_ADMIN_TOKEN", raising=False)

    status, _, payload = request("/packages/hello-world/changes", headers=auth_headers())

    assert status == 403
    assert payload == {
        "status": "error",
        "error": "Package change history requires DBBASIC_ADMIN_TOKEN.",
    }


def test_packages_api_rejects_invalid_and_missing_packages(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(packages_root))
    enable_admin_token(monkeypatch)

    status, _, payload = request("/packages/../bad", headers=auth_headers())

    assert status == 400
    assert "Invalid package id" in payload["error"]

    status, _, payload = request("/packages/missing", headers=auth_headers())

    assert status == 404
    assert "Package not found" in payload["error"]

    status, _, payload = request("/packages/bad.name/changes", headers=auth_headers())

    assert status == 400
    assert "Invalid package id" in payload["error"]

    status, _, payload = request(
        "/packages/hello-world/changes",
        query_string="limit=0",
        headers=auth_headers(),
    )

    assert status == 400
    assert "limit" in payload["error"]


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
