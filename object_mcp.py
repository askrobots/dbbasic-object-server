"""MCP (Model Context Protocol) surface for the DBBASIC object server.

This module holds the protocol constants, the tool catalog, and the mapping
from tool calls to internal HTTP routes. The server dispatches each tool call
back through its own routing with the caller's original credentials, so MCP
tools inherit exactly the same gates, permission checks, audit trail, and
correlation ids as the admin HTTP surface. Nothing new runs underneath.

Protocol: JSON-RPC 2.0 over HTTP POST, MCP Streamable HTTP transport.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Mapping

MCP_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(
    {
        "2024-11-05",
        "2025-03-26",
        "2025-11-25",
    }
)
SERVER_INFO = {
    "name": "dbbasic-object-server-mcp",
    "version": "1.0.0",
}

_OBJECT_ID_ARG = {"type": "string", "description": "Object id, like site_home"}
_COLLECTION_ARG = {"type": "string", "description": "Collection name, like contacts"}
_RECORD_ID_ARG = {"type": "string", "description": "Record id"}
_LIMIT_ARG = {"type": "integer", "description": "Max rows (default 100)", "default": 100}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_admin_status",
        "description": "Server health, inventory, capability flags, package posture, and permission readiness",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_objects",
        "description": "List all objects with source metadata",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_object_source",
        "description": "Read one object's Python source",
        "inputSchema": {
            "type": "object",
            "properties": {"object_id": _OBJECT_ID_ARG},
            "required": ["object_id"],
        },
    },
    {
        "name": "get_object_state",
        "description": "Read one object's persistent state",
        "inputSchema": {
            "type": "object",
            "properties": {"object_id": _OBJECT_ID_ARG},
            "required": ["object_id"],
        },
    },
    {
        "name": "get_object_logs",
        "description": "Read one object's recent log entries",
        "inputSchema": {
            "type": "object",
            "properties": {"object_id": _OBJECT_ID_ARG, "limit": _LIMIT_ARG},
            "required": ["object_id"],
        },
    },
    {
        "name": "get_object_metadata",
        "description": "Read one object's metadata summary",
        "inputSchema": {
            "type": "object",
            "properties": {"object_id": _OBJECT_ID_ARG},
            "required": ["object_id"],
        },
    },
    {
        "name": "get_object_changes",
        "description": "Read one object's source and file change timeline",
        "inputSchema": {
            "type": "object",
            "properties": {"object_id": _OBJECT_ID_ARG, "limit": _LIMIT_ARG},
            "required": ["object_id"],
        },
    },
    {
        "name": "create_object",
        "description": "Create a new object from Python source (requires source writes enabled)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_id": _OBJECT_ID_ARG,
                "code": {"type": "string", "description": "Python source with GET/POST/PUT/DELETE methods"},
                "message": {"type": "string", "description": "Change message"},
            },
            "required": ["object_id", "code"],
        },
    },
    {
        "name": "update_object_source",
        "description": "Replace one object's source; saves a version and change history (requires source writes enabled)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_id": _OBJECT_ID_ARG,
                "code": {"type": "string", "description": "New Python source"},
                "message": {"type": "string", "description": "Change message"},
            },
            "required": ["object_id", "code"],
        },
    },
    {
        "name": "execute_object",
        "description": "Run one object method and return its response, like a request would",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_id": _OBJECT_ID_ARG,
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE"],
                    "default": "GET",
                },
                "payload": {"type": "object", "description": "Request payload for the object"},
            },
            "required": ["object_id"],
        },
    },
    {
        "name": "list_collections",
        "description": "List collections with record, schema, and permission summaries",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_records",
        "description": "List records in one collection",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": _COLLECTION_ARG,
                "limit": _LIMIT_ARG,
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["collection"],
        },
    },
    {
        "name": "get_record",
        "description": "Read one record",
        "inputSchema": {
            "type": "object",
            "properties": {"collection": _COLLECTION_ARG, "record_id": _RECORD_ID_ARG},
            "required": ["collection", "record_id"],
        },
    },
    {
        "name": "create_record",
        "description": "Create one record (validated against the collection schema)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": _COLLECTION_ARG,
                "record": {"type": "object", "description": "Record fields including id"},
            },
            "required": ["collection", "record"],
        },
    },
    {
        "name": "update_record",
        "description": "Update fields on one record",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": _COLLECTION_ARG,
                "record_id": _RECORD_ID_ARG,
                "changes": {"type": "object", "description": "Fields to change"},
            },
            "required": ["collection", "record_id", "changes"],
        },
    },
    {
        "name": "delete_record",
        "description": "Delete one record",
        "inputSchema": {
            "type": "object",
            "properties": {"collection": _COLLECTION_ARG, "record_id": _RECORD_ID_ARG},
            "required": ["collection", "record_id"],
        },
    },
    {
        "name": "get_schema",
        "description": "Read one collection schema",
        "inputSchema": {
            "type": "object",
            "properties": {"collection": _COLLECTION_ARG},
            "required": ["collection"],
        },
    },
    {
        "name": "update_schema",
        "description": "Replace one collection schema; records a schema version",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": _COLLECTION_ARG,
                "schema": {"type": "object", "description": "Schema payload with fields list"},
                "message": {"type": "string", "description": "Change message"},
            },
            "required": ["collection", "schema"],
        },
    },
    {
        "name": "rollback_schema",
        "description": "Roll one collection schema back to a previous version",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": _COLLECTION_ARG,
                "version_id": {"type": "integer", "description": "Schema version to restore"},
            },
            "required": ["collection", "version_id"],
        },
    },
    {
        "name": "global_search",
        "description": "Search records across all collections whose schema declares search fields",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms; all must match"},
                "limit": {"type": "integer", "description": "Max results per collection", "default": 10},
                "collections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional collection names to restrict the search to",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_ops_events",
        "description": "Recent operational events: object execution errors and auth activity (login/logout/session mints)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": _LIMIT_ARG,
                "kind": {"type": "string", "enum": ["execution_error", "auth"]},
                "event": {"type": "string", "description": "Auth event filter, like login_failed"},
            },
        },
    },
    {
        "name": "list_changes",
        "description": "Unified source/file/record/package change history, filterable",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": _LIMIT_ARG,
                "kind": {"type": "string", "enum": ["source", "file", "record", "package"]},
                "object_id": {"type": "string"},
                "collection": {"type": "string"},
            },
        },
    },
    {
        "name": "read_page",
        "description": (
            "Fetch a URL server-side and return it stripped to readable text: "
            "{title, text, links: [{n, label, href}], final_url, truncated}. "
            "Links are numbered in document order so they work as speakable "
            "navigation targets. Gated by DBBASIC_ENABLE_READER; refuses "
            "non-http(s) schemes and requests aimed at private/internal "
            "addresses (SSRF gate)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http(s) URL to fetch"}},
            "required": ["url"],
        },
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def jsonrpc_response(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_initialize(params: Mapping[str, Any]) -> dict[str, Any]:
    client_version = params.get("protocolVersion")
    negotiated = (
        client_version
        if client_version in SUPPORTED_MCP_PROTOCOL_VERSIONS
        else MCP_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {}},
        "serverInfo": dict(SERVER_INFO),
    }


def handle_tools_list() -> dict[str, Any]:
    return {"tools": TOOLS}


def tool_route(name: str, arguments: Mapping[str, Any]) -> tuple[str, str, str, bytes]:
    """Map one tool call to (method, path, query_string, body) on the admin surface."""
    if name not in TOOL_NAMES:
        raise ValueError(f"Unknown tool: {name}")

    args = dict(arguments or {})

    if name == "get_admin_status":
        return ("GET", "/admin/status", "", b"")
    if name == "list_objects":
        return ("GET", "/admin/objects", "", b"")
    if name == "get_object_source":
        return ("GET", _object_path(args), "source=true&format=json", b"")
    if name == "get_object_state":
        return ("GET", _object_path(args), "state=true", b"")
    if name == "get_object_logs":
        return ("GET", _object_path(args), f"logs=true&limit={_limit(args)}", b"")
    if name == "get_object_metadata":
        return ("GET", _object_path(args), "metadata=true", b"")
    if name == "get_object_changes":
        return ("GET", _object_path(args), f"changes=true&limit={_limit(args)}", b"")
    if name == "create_object":
        body = {
            "object_id": _required_str(args, "object_id"),
            "code": _required_str(args, "code"),
            "message": args.get("message") or "Created via MCP",
            "author": "mcp",
        }
        return ("POST", "/admin/objects", "", _json_bytes(body))
    if name == "update_object_source":
        body = {
            "code": _required_str(args, "code"),
            "message": args.get("message") or "Updated via MCP",
            "author": "mcp",
        }
        return ("PUT", _object_path(args), "source=true", _json_bytes(body))
    if name == "execute_object":
        body = {
            "method": args.get("method") or "GET",
            "payload": args.get("payload") or {},
        }
        return ("POST", f"{_object_path(args)}/execute", "", _json_bytes(body))
    if name == "list_collections":
        return ("GET", "/admin/collections", "", b"")
    if name == "list_records":
        query = f"limit={_limit(args)}&offset={_offset(args)}"
        return ("GET", f"{_collection_path(args)}/records", query, b"")
    if name == "get_record":
        return ("GET", _record_path(args), "", b"")
    if name == "create_record":
        record = args.get("record")
        if not isinstance(record, dict):
            raise ValueError("record must be an object")
        return ("POST", f"{_collection_path(args)}/records", "", _json_bytes(record))
    if name == "update_record":
        changes = args.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        return ("PUT", _record_path(args), "", _json_bytes(changes))
    if name == "delete_record":
        return ("DELETE", _record_path(args), "", b"")
    if name == "get_schema":
        return ("GET", _schema_path(args), "format=json", b"")
    if name == "update_schema":
        schema = args.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("schema must be an object")
        body = {
            "schema": schema,
            "author": "mcp",
            "message": args.get("message") or "Updated schema via MCP",
        }
        return ("PUT", _schema_path(args), "", _json_bytes(body))
    if name == "rollback_schema":
        version_id = args.get("version_id")
        if isinstance(version_id, bool) or not isinstance(version_id, int):
            raise ValueError("version_id must be an integer")
        body = {"action": "rollback", "version_id": version_id}
        return ("POST", _schema_path(args), "", _json_bytes(body))
    if name == "global_search":
        search_limit = _bounded_int(args.get("limit"), default=10, minimum=1, maximum=100, name="limit")
        pairs = [("q", _required_str(args, "query")), ("limit", str(search_limit))]
        collections = args.get("collections")
        if collections is not None:
            if not isinstance(collections, list) or not all(
                isinstance(item, str) and item.strip() for item in collections
            ):
                raise ValueError("collections must be a list of collection names")
            pairs.append(("collections", ",".join(item.strip() for item in collections)))
        return ("GET", "/api/search", urllib.parse.urlencode(pairs), b"")
    if name == "list_ops_events":
        pairs = [("limit", str(_limit(args)))]
        for key in ("kind", "event"):
            value = args.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} must be a non-empty string")
                pairs.append((key, value.strip()))
        return ("GET", "/admin/ops", urllib.parse.urlencode(pairs), b"")
    if name == "list_changes":
        pairs = [("limit", str(_limit(args)))]
        for key in ("kind", "object_id", "collection"):
            value = args.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} must be a non-empty string")
                pairs.append((key, value.strip()))
        return ("GET", "/admin/changes", urllib.parse.urlencode(pairs), b"")
    if name == "read_page":
        body = {"url": _required_str(args, "url")}
        return ("POST", "/api/read", "", _json_bytes(body))

    raise ValueError(f"Unknown tool: {name}")


def tool_result_content(status: int, payload: Any) -> dict[str, Any]:
    """Wrap one internal route response as an MCP tool result."""
    body = {"http_status": status, "response": payload}
    return {
        "content": [{"type": "text", "text": json.dumps(body, default=str, indent=2)}],
        "isError": status >= 400,
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _required_str(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _safe_segment(args: Mapping[str, Any], key: str) -> str:
    value = _required_str(args, key)
    if "/" in value or "\\" in value or ".." in value or "?" in value or "#" in value:
        raise ValueError(f"{key} contains unsafe characters")
    return value


def _object_path(args: Mapping[str, Any]) -> str:
    return f"/admin/objects/{_safe_segment(args, 'object_id')}"


def _collection_path(args: Mapping[str, Any]) -> str:
    return f"/admin/collections/{_safe_segment(args, 'collection')}"


def _record_path(args: Mapping[str, Any]) -> str:
    return f"{_collection_path(args)}/records/{_safe_segment(args, 'record_id')}"


def _schema_path(args: Mapping[str, Any]) -> str:
    return f"/admin/schemas/{_safe_segment(args, 'collection')}"


def _limit(args: Mapping[str, Any]) -> int:
    return _bounded_int(args.get("limit"), default=100, minimum=1, maximum=1000, name="limit")


def _offset(args: Mapping[str, Any]) -> int:
    return _bounded_int(args.get("offset"), default=0, minimum=0, maximum=1_000_000, name="offset")


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value
