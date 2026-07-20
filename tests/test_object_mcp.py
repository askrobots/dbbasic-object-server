"""Tests for the MCP endpoint and tool routing."""

import json
import urllib.parse

import pytest

import object_mcp
import object_server

from test_object_server import (
    TEST_ADMIN_TOKEN,
    auth_headers,
    create_identity_session,
    enable_admin_token,
    request,
    raw_request,
    write_records,
    write_source,
)


def mcp_request(message, headers=None):
    return request(
        "/api/mcp",
        method="POST",
        body=json.dumps(message).encode(),
        headers=headers if headers is not None else auth_headers(),
    )


def call_tool(name, arguments=None, headers=None, request_id=1):
    status, _, payload = mcp_request(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers=headers,
    )
    assert status == 200
    result = payload["result"]
    body = json.loads(result["content"][0]["text"])
    return result.get("isError", False), body


def test_mcp_requires_admin_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, payload = mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=[],
    )

    assert status == 401
    assert payload["error"]["code"] == -32000
    assert payload["error"]["message"] == "Unauthorized"


def test_mcp_initialize_negotiates_protocol_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, headers, payload = raw_request(
        "/api/mcp",
        method="POST",
        body=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        ).encode(),
        headers=auth_headers(),
    )
    parsed = json.loads(payload)

    assert status == 200
    assert parsed["id"] == 7
    assert parsed["result"]["protocolVersion"] == "2024-11-05"
    assert parsed["result"]["serverInfo"]["name"] == "dbbasic-object-server-mcp"
    assert headers[b"mcp-session-id"]


def test_mcp_rejects_unsupported_protocol_header(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, payload = mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=[*auth_headers(), ("mcp-protocol-version", "1999-01-01")],
    )

    assert status == 400
    assert "Unsupported MCP protocol version" in payload["error"]["message"]


def test_mcp_notifications_return_202(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, body = raw_request(
        "/api/mcp",
        method="POST",
        body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        headers=auth_headers(),
    )

    assert status == 202
    assert body == b""


def test_mcp_tools_list_matches_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, payload = mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert status == 200
    names = [tool["name"] for tool in payload["result"]["tools"]]
    assert names == [tool["name"] for tool in object_mcp.TOOLS]
    assert "execute_object" in names
    assert all(tool["inputSchema"]["type"] == "object" for tool in payload["result"]["tools"])


def test_mcp_unknown_method_and_bad_json(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    unknown_status, _, unknown = mcp_request({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})
    parse_status, _, parse_error = request(
        "/api/mcp",
        method="POST",
        body=b"{not json",
        headers=auth_headers(),
    )
    version_status, _, version_error = mcp_request({"jsonrpc": "1.0", "id": 4, "method": "tools/list"})

    assert unknown_status == 200
    assert unknown["error"]["code"] == -32601
    assert parse_status == 200
    assert parse_error["error"]["code"] == -32700
    assert version_status == 200
    assert version_error["error"]["code"] == -32600


def test_mcp_full_object_build_loop(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    root.mkdir(parents=True)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("DBBASIC_ENABLE_SOURCE_WRITES", "true")
    monkeypatch.setenv(object_server.TRUSTED_IN_PROCESS_OBJECTS_ENV, "tools_greeter")
    enable_admin_token(monkeypatch)

    created_error, created = call_tool(
        "create_object",
        {
            "object_id": "tools_greeter",
            "code": "def GET(request):\n    return {'hello': request.get('name', 'world')}\n",
        },
    )
    executed_error, executed = call_tool(
        "execute_object",
        {"object_id": "tools_greeter", "method": "GET", "payload": {"name": "mcp"}},
    )
    source_error, source = call_tool("get_object_source", {"object_id": "tools_greeter"})
    updated_error, updated = call_tool(
        "update_object_source",
        {
            "object_id": "tools_greeter",
            "code": "def GET(request):\n    return {'hello': 'v2'}\n",
            "message": "second version",
        },
    )
    logs_error, logs = call_tool("get_object_logs", {"object_id": "tools_greeter", "limit": 10})

    assert not created_error and created["http_status"] == 201
    assert not executed_error and executed["response"] == {"hello": "mcp"}
    assert not source_error and "def GET" in source["response"]["source"]
    assert not updated_error and updated["response"]["version_id"] == 2
    assert not logs_error and logs["http_status"] == 200


def test_mcp_record_and_schema_tools(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)

    schema_error, schema_set = call_tool(
        "update_schema",
        {"collection": "contacts", "schema": {"fields": [{"name": "id"}, {"name": "name"}]}},
    )
    created_error, created = call_tool(
        "create_record",
        {"collection": "contacts", "record": {"id": "c2", "name": "Grace"}},
    )
    listed_error, listed = call_tool("list_records", {"collection": "contacts", "limit": 10})
    updated_error, updated = call_tool(
        "update_record",
        {"collection": "contacts", "record_id": "c1", "changes": {"name": "Ada Lovelace"}},
    )
    deleted_error, deleted = call_tool("delete_record", {"collection": "contacts", "record_id": "c2"})
    changes_error, changes = call_tool("list_changes", {"kind": "record", "limit": 10})

    assert not schema_error and schema_set["response"]["version_id"] == 1
    assert not created_error and created["http_status"] == 201
    assert not listed_error and listed["response"]["total"] == 2
    assert not updated_error and updated["response"]["record"]["name"] == "Ada Lovelace"
    assert not deleted_error and deleted["response"]["deleted"] is True
    assert not changes_error and changes["response"]["count"] >= 3


def test_mcp_tool_route_translates_where_eq_shorthand_to_query_param():
    method, path, query, body = object_mcp.tool_route(
        "list_records", {"collection": "contacts", "where": {"lead_status": "hot"}}
    )

    assert method == "GET"
    assert path == "/admin/collections/contacts/records"
    assert dict(urllib.parse.parse_qsl(query))["lead_status"] == "hot"
    assert body == b""


def test_mcp_tool_route_translates_where_explicit_op_to_dotted_param():
    method, path, query, body = object_mcp.tool_route(
        "list_records",
        {"collection": "contacts", "where": {"status": {"op": "in", "value": ["open", "assigned"]}}},
    )

    assert dict(urllib.parse.parse_qsl(query))["status.in"] == "open,assigned"


def test_mcp_tool_route_translates_where_range_query_as_two_params():
    """A range (two conditions on the same field) round-trips as two
    separate query params -- urlencode keeps repeated keys distinct, and
    the HTTP handler ANDs same-field conditions from separate params."""
    method, path, query, body = object_mcp.tool_route(
        "list_records",
        {
            "collection": "postings",
            "where": {
                "posted_at": [
                    {"op": "gte", "value": "2026-07-01"},
                    {"op": "lte", "value": "2026-07-31"},
                ]
            },
        },
    )

    pairs = urllib.parse.parse_qsl(query)
    assert ("posted_at.gte", "2026-07-01") in pairs
    assert ("posted_at.lte", "2026-07-31") in pairs


def test_mcp_tool_route_rejects_non_object_where():
    with pytest.raises(ValueError, match="where must be an object"):
        object_mcp.tool_route("list_records", {"collection": "contacts", "where": "bogus"})


def test_mcp_tool_route_rejects_where_condition_missing_op():
    with pytest.raises(ValueError, match="op"):
        object_mcp.tool_route(
            "list_records", {"collection": "contacts", "where": {"status": {"value": "hot"}}}
        )


def test_mcp_list_records_where_filters_end_to_end(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tname\tlead_status\nc1\tAda\thot\nc2\tGrace\tcold\nc3\tKatherine\thot\n",
    )
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)

    error, result = call_tool(
        "list_records", {"collection": "contacts", "where": {"lead_status": "hot"}}
    )

    assert not error
    assert result["http_status"] == 200
    assert [r["id"] for r in result["response"]["records"]] == ["c1", "c3"]


def test_mcp_list_records_where_explicit_op_end_to_end(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tname\tstatus\nc1\tAda\topen\nc2\tGrace\tclosed\nc3\tKatherine\tassigned\n",
    )
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)

    error, result = call_tool(
        "list_records",
        {"collection": "contacts", "where": {"status": {"op": "in", "value": ["open", "assigned"]}}},
    )

    assert not error
    assert [r["id"] for r in result["response"]["records"]] == ["c1", "c3"]


def test_mcp_list_records_where_unknown_field_surfaces_structured_400(tmp_path, monkeypatch):
    """A bad filter still comes back as the same structured 400 a direct
    HTTP caller gets -- MCP adds no new validation of its own, per this
    module's "nothing new runs underneath" doctrine."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    schema_file = data_dir / "schemas" / "contacts.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text('{"fields": [{"name": "id"}, {"name": "name"}]}')
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)

    error, result = call_tool(
        "list_records", {"collection": "contacts", "where": {"nickname": "Ada"}}
    )

    assert error is True
    assert result["http_status"] == 400
    assert result["response"]["code"] == "invalid_filter"
    assert result["response"]["param"] == "nickname"


def test_mcp_global_search_tool(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\tcontent\nn1\tflywheel growth loop\nn2\tother memo\n")
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)

    call_tool(
        "update_schema",
        {
            "collection": "notes",
            "schema": {
                "fields": [{"name": "id"}, {"name": "content"}],
                "search": {"fields": ["content"]},
            },
        },
    )

    search_error, found = call_tool("global_search", {"query": "flywheel growth"})
    scoped_error, scoped = call_tool(
        "global_search", {"query": "flywheel", "collections": ["notes"], "limit": 5}
    )

    assert not search_error
    assert found["response"]["results"]["notes"] == [
        {"id": "n1", "content": "flywheel growth loop"}
    ]
    assert found["response"]["total_count"] == 1
    assert not scoped_error and scoped["response"]["total_count"] == 1


def test_global_search_tool_route_validates_arguments():
    with pytest.raises(ValueError, match="query"):
        object_mcp.tool_route("global_search", {})
    with pytest.raises(ValueError, match="collections"):
        object_mcp.tool_route("global_search", {"query": "x", "collections": "notes"})

    method, path, query_string, body = object_mcp.tool_route(
        "global_search", {"query": "fly wheel", "collections": ["notes", "tasks"]}
    )
    assert (method, path, body) == ("GET", "/api/search", b"")
    assert query_string == "q=fly+wheel&limit=10&collections=notes%2Ctasks"


def test_mcp_tool_errors_are_marked(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    unknown_error, unknown = call_tool("launch_missiles", {})
    missing_error, missing = call_tool("get_object_source", {"object_id": "no_such_object"})
    unsafe_error, unsafe = call_tool("get_object_source", {"object_id": "../etc/passwd"})

    assert unknown_error and "Unknown tool" in unknown["error"]
    assert missing_error and missing["http_status"] == 404
    assert unsafe_error and "unsafe characters" in unsafe["error"]


def test_admin_gates_accept_session_cookie_when_gates_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.SESSION_ADMIN_GATES_ENV, "true")
    enable_admin_token(monkeypatch)
    request(
        "/identity/users",
        method="POST",
        body=json.dumps({"user_id": "dan", "roles": ["admin"]}).encode(),
        headers=auth_headers(),
    )
    token, _ = create_identity_session({"user_id": "dan", "label": "browser"})

    cookie_status, _, cookie_payload = request(
        "/admin/status",
        headers=[("cookie", f"dbbasic_session={token}")],
    )
    metrics_status, _, _ = request(
        "/health",
        query_string="metrics=true",
        headers=[("cookie", f"dbbasic_session={token}")],
    )

    monkeypatch.setenv(object_server.SESSION_ADMIN_GATES_ENV, "false")
    gates_off_status, _, _ = request(
        "/admin/status",
        headers=[("cookie", f"dbbasic_session={token}")],
    )
    monkeypatch.setenv(object_server.SESSION_ADMIN_GATES_ENV, "true")
    admin_token_cookie_status, _, _ = request(
        "/admin/status",
        headers=[("cookie", f"dbbasic_session={TEST_ADMIN_TOKEN}")],
    )

    assert cookie_status == 200
    assert cookie_payload["status"] == "ok"
    assert metrics_status == 200
    assert gates_off_status == 401
    assert admin_token_cookie_status == 401


def test_mcp_works_with_admin_role_session_token(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.SESSION_ADMIN_GATES_ENV, "true")
    enable_admin_token(monkeypatch)
    request(
        "/identity/users",
        method="POST",
        body=json.dumps({"user_id": "agent-claude", "roles": ["admin"]}).encode(),
        headers=auth_headers(),
    )
    token, _ = create_identity_session({"user_id": "agent-claude", "label": "mcp claude-code"})

    is_error, body = call_tool(
        "get_admin_status",
        headers=[("authorization", f"Bearer {token}")],
    )

    assert not is_error
    assert body["http_status"] == 200
    assert body["response"]["status"] == "ok"
