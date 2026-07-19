"""Tests for the /api/read endpoint and the read_page MCP tool.

Mirrors tests/test_object_tts.py's gating tests (flag off -> 403, no
session -> 401) and tests/test_object_mcp.py's session-token-through-MCP
pattern for the SESSION_ADMIN_GATES_ENV case. object_reader.read_page
itself is unit-tested in tests/test_object_reader.py -- these tests
monkeypatch it out so the endpoint/tool wiring is what's under test here.
"""

import json

import object_mcp
import object_reader
import object_server

from test_object_mcp import call_tool
from test_object_server import (
    auth_headers,
    create_identity_session,
    enable_admin_token,
    request,
)


def reader_env(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.READER_ENABLED_ENV, "true")
    enable_admin_token(monkeypatch)


def signed_in_bearer():
    token, _ = create_identity_session({"user_id": "dan"})
    return [("authorization", f"Bearer {token}")]


def test_read_endpoint_requires_flag_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, disabled = request(
        "/api/read", method="POST", body=json.dumps({"url": "https://example.com"}).encode()
    )
    assert status == 403 and "disabled" in disabled["error"]

    monkeypatch.setenv(object_server.READER_ENABLED_ENV, "true")
    status, _, anonymous = request(
        "/api/read", method="POST", body=json.dumps({"url": "https://example.com"}).encode()
    )
    assert status == 401


def test_read_endpoint_requires_url(tmp_path, monkeypatch):
    reader_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    status, _, body = request("/api/read", method="POST", body=json.dumps({}).encode(), headers=bearer)
    assert status == 400
    assert "url" in body["error"]


def test_read_endpoint_maps_reader_error_to_502(tmp_path, monkeypatch):
    reader_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    def fake_read_page(url, *, timeout=10, max_bytes=2_000_000):
        raise object_reader.ReaderError("Refusing to fetch 'internal.example': resolves to internal address 10.0.0.1")

    monkeypatch.setattr(object_server.object_reader, "read_page", fake_read_page)

    status, _, body = request(
        "/api/read",
        method="POST",
        body=json.dumps({"url": "http://internal.example/"}).encode(),
        headers=bearer,
    )
    assert status == 502
    assert "internal address" in body["error"]


def test_read_endpoint_happy_path_returns_structured_result(tmp_path, monkeypatch):
    reader_env(tmp_path, monkeypatch)
    bearer = signed_in_bearer()

    fake_result = {
        "title": "Example",
        "text": "Hello there.",
        "links": [{"n": 1, "label": "About", "href": "https://example.com/about"}],
        "final_url": "https://example.com/",
        "truncated": False,
    }

    def fake_read_page(url, *, timeout=10, max_bytes=2_000_000):
        assert url == "https://example.com/"
        return dict(fake_result)

    monkeypatch.setattr(object_server.object_reader, "read_page", fake_read_page)

    status, _, body = request(
        "/api/read",
        method="POST",
        body=json.dumps({"url": "https://example.com/"}).encode(),
        headers=bearer,
    )
    assert status == 200
    assert body["status"] == "ok"
    assert body["title"] == "Example"
    assert body["links"] == fake_result["links"]


def test_mcp_read_page_tool_is_in_the_catalog():
    names = [tool["name"] for tool in object_mcp.TOOLS]
    assert "read_page" in names


def test_read_page_tool_route_maps_to_api_read():
    method, path, query_string, body = object_mcp.tool_route("read_page", {"url": "https://example.com"})
    assert (method, path, query_string) == ("POST", "/api/read", "")
    assert json.loads(body) == {"url": "https://example.com"}


def test_read_page_tool_route_requires_url():
    import pytest

    with pytest.raises(ValueError, match="url"):
        object_mcp.tool_route("read_page", {})


def test_mcp_read_page_tool_is_gated_by_the_flag(tmp_path, monkeypatch):
    """Flag off: the endpoint the tool routes to refuses, and that refusal
    comes back through MCP as an isError result -- the tool isn't silently
    dropped from the catalog, it just fails closed like every other gated
    surface here."""
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
        "read_page",
        {"url": "https://example.com"},
        headers=[("authorization", f"Bearer {token}")],
    )

    assert is_error
    assert body["http_status"] == 403
    assert "disabled" in body["response"]["error"]


def test_mcp_read_page_tool_happy_path_through_session_token(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.SESSION_ADMIN_GATES_ENV, "true")
    monkeypatch.setenv(object_server.READER_ENABLED_ENV, "true")
    enable_admin_token(monkeypatch)
    request(
        "/identity/users",
        method="POST",
        body=json.dumps({"user_id": "agent-claude", "roles": ["admin"]}).encode(),
        headers=auth_headers(),
    )
    token, _ = create_identity_session({"user_id": "agent-claude", "label": "mcp claude-code"})

    def fake_read_page(url, *, timeout=10, max_bytes=2_000_000):
        return {
            "title": "Example",
            "text": "Body text.",
            "links": [],
            "final_url": url,
            "truncated": False,
        }

    monkeypatch.setattr(object_server.object_reader, "read_page", fake_read_page)

    is_error, body = call_tool(
        "read_page",
        {"url": "https://example.com/"},
        headers=[("authorization", f"Bearer {token}")],
    )

    assert not is_error
    assert body["http_status"] == 200
    assert body["response"]["title"] == "Example"
