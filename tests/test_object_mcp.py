"""Tests for the MCP endpoint and tool routing."""

import json

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


def test_mcp_tool_errors_are_marked(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    unknown_error, unknown = call_tool("launch_missiles", {})
    missing_error, missing = call_tool("get_object_source", {"object_id": "no_such_object"})
    unsafe_error, unsafe = call_tool("get_object_source", {"object_id": "../etc/passwd"})

    assert unknown_error and "Unknown tool" in unknown["error"]
    assert missing_error and missing["http_status"] == 404
    assert unsafe_error and "unsafe characters" in unsafe["error"]


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
