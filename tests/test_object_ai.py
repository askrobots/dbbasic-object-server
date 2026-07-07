"""Tests for AI chat: provider adapters, the tool loop, and the endpoint."""

import json

import pytest

import object_ai
import object_mcp
import object_server

from test_object_server import (
    create_identity_session,
    enable_admin_token,
    request,
    save_permission_policy,
    write_records,
)


def anthropic_text_response(text):
    return (
        200,
        json.dumps(
            {
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        ).encode(),
    )


def anthropic_tool_response(name, arguments):
    return (
        200,
        json.dumps(
            {
                "content": [
                    {"type": "tool_use", "id": "call-1", "name": name, "input": arguments}
                ],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            }
        ).encode(),
    )


def test_split_model_and_tool_conversion():
    assert object_ai.split_model("anthropic:claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")
    with pytest.raises(object_ai.InvalidChatRequestError):
        object_ai.split_model("claude-haiku-4-5")
    with pytest.raises(object_ai.InvalidChatRequestError):
        object_ai.split_model("mystery:model")

    tools = object_ai.mcp_tools_as_provider_tools(
        ["global_search"], object_mcp.TOOLS, service="anthropic"
    )
    assert tools[0]["name"] == "global_search"
    assert "input_schema" in tools[0]

    openai_tools = object_ai.mcp_tools_as_provider_tools(
        ["global_search"], object_mcp.TOOLS, service="openai"
    )
    assert openai_tools[0]["function"]["name"] == "global_search"

    with pytest.raises(object_ai.InvalidChatRequestError, match="Unknown tools"):
        object_ai.mcp_tools_as_provider_tools(["launch_missiles"], object_mcp.TOOLS, service="anthropic")


def test_run_chat_loops_through_tool_calls():
    responses = [
        anthropic_tool_response("global_search", {"query": "flywheel"}),
        anthropic_text_response("Found one note about the flywheel."),
    ]
    requests_seen = []

    def send_http(url, headers, body):
        requests_seen.append(json.loads(body))
        return responses.pop(0)

    dispatched = []

    def dispatch_tool(name, arguments):
        dispatched.append((name, arguments))
        return {"http_status": 200, "response": {"results": {"notes": [{"id": "n1"}]}}}

    result = object_ai.run_chat(
        send_http=send_http,
        dispatch_tool=dispatch_tool,
        service="anthropic",
        model="claude-haiku-4-5",
        key="sk-test",
        message="find flywheel notes",
        tools=object_ai.mcp_tools_as_provider_tools(
            ["global_search"], object_mcp.TOOLS, service="anthropic"
        ),
    )

    assert result["reply"] == "Found one note about the flywheel."
    assert result["rounds"] == 2
    assert dispatched == [("global_search", {"query": "flywheel"})]
    assert result["tool_calls"] == [
        {"name": "global_search", "arguments": {"query": "flywheel"}, "http_status": 200}
    ]
    assert result["usage"] == {"input_tokens": 30, "output_tokens": 13}
    # The second provider round carries the tool result back.
    followup = requests_seen[1]["messages"]
    assert followup[-1]["content"][0]["type"] == "tool_result"


def test_run_chat_resumes_from_history():
    requests_seen = []

    def send_http(url, headers, body):
        requests_seen.append(json.loads(body))
        return anthropic_text_response("Continuing where we left off.")

    result = object_ai.run_chat(
        send_http=send_http,
        dispatch_tool=lambda name, arguments: {},
        service="anthropic",
        model="claude-haiku-4-5",
        key="sk-test",
        message="and then?",
        history=[
            {"role": "user", "content": "tell me about the flywheel"},
            {"role": "assistant", "content": "It spins."},
        ],
    )
    assert result["reply"] == "Continuing where we left off."
    messages = requests_seen[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "and then?"

    with pytest.raises(object_ai.InvalidChatRequestError, match="history"):
        object_ai.normalize_history([{"role": "system", "content": "x"}])


def test_run_chat_stops_at_round_limit():
    def send_http(url, headers, body):
        return anthropic_tool_response("global_search", {"query": "again"})

    result = object_ai.run_chat(
        send_http=send_http,
        dispatch_tool=lambda name, arguments: {"http_status": 200, "response": {}},
        service="anthropic",
        model="claude-haiku-4-5",
        key="sk-test",
        message="loop forever",
        tools=object_ai.mcp_tools_as_provider_tools(
            ["global_search"], object_mcp.TOOLS, service="anthropic"
        ),
        max_rounds=2,
    )
    assert result["truncated"] is True
    assert len(result["tool_calls"]) == 2


def test_run_chat_raises_on_provider_error():
    def send_http(url, headers, body):
        return 401, json.dumps({"error": {"message": "invalid x-api-key"}}).encode()

    with pytest.raises(object_ai.AIProviderError, match="invalid x-api-key"):
        object_ai.run_chat(
            send_http=send_http,
            dispatch_tool=lambda name, arguments: {},
            service="anthropic",
            model="claude-haiku-4-5",
            key="sk-bad",
            message="hi",
        )


def test_ai_chat_endpoint_runs_tools_with_caller_permissions(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\tcontent\nn1\tflywheel plan\n")
    schema_file = data_dir / "schemas" / "notes.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {"fields": [{"name": "id"}, {"name": "content"}], "search": {"fields": ["content"]}}
        )
    )
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "registered",
                    "actions": ["read"],
                    "collection": "notes",
                    "reason": "signed-in users read notes",
                }
            ],
        },
    )
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.AI_CHAT_ENABLED_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    enable_admin_token(monkeypatch)
    token, _ = create_identity_session({"user_id": "dan"})
    bearer = [("authorization", f"Bearer {token}")]

    responses = [
        anthropic_tool_response("global_search", {"query": "flywheel"}),
        anthropic_text_response("One note matches."),
    ]
    monkeypatch.setattr(
        object_server.object_ai,
        "run_chat",
        object_ai.run_chat,
    )

    def fake_urlopen(request_obj, timeout=None):
        raise AssertionError("network must go through the injected transport")

    # Intercept at the handler's transport: patch urllib inside object_server.
    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_transport(request_obj, timeout=None):
        status, body = responses.pop(0)
        return FakeResponse(status, body)

    monkeypatch.setattr(object_server.urllib.request, "urlopen", fake_transport)

    # No key stored yet: helpful 400.
    status, _, no_key = request(
        "/api/ai/chat",
        method="POST",
        body=json.dumps({"message": "find flywheel notes", "tools": ["global_search"]}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )
    assert status == 400 and "service-keys" in no_key["error"]

    request(
        "/identity/users/dan/service-keys",
        method="PUT",
        body=json.dumps({"service": "anthropic", "key": "sk-test-1"}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )

    status, _, chat = request(
        "/api/ai/chat",
        method="POST",
        body=json.dumps({"message": "find flywheel notes", "tools": ["global_search"]}).encode(),
        headers=bearer + [("content-type", "application/json")],
    )

    assert status == 200, chat
    assert chat["reply"] == "One note matches."
    assert chat["tool_calls"][0]["name"] == "global_search"
    assert chat["tool_calls"][0]["http_status"] == 200
    assert chat["usage"]["output_tokens"] == 13


def test_ai_chat_endpoint_requires_flag_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)

    status, _, disabled = request(
        "/api/ai/chat", method="POST", body=json.dumps({"message": "hi"}).encode()
    )
    assert status == 403 and "disabled" in disabled["error"]

    monkeypatch.setenv(object_server.AI_CHAT_ENABLED_ENV, "true")
    status, _, anonymous = request(
        "/api/ai/chat", method="POST", body=json.dumps({"message": "hi"}).encode()
    )
    assert status == 401
