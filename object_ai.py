"""Provider-neutral AI chat with MCP tool calling.

This is the shell's brain: one conversation turn goes to an AI provider
using the caller's own stored service key, the model may call a small,
caller-chosen subset of the server's MCP tools, and every tool call is
dispatched back through the server's own routing so permission checks
and audit apply exactly as if the caller made the request directly.

Models are named ``service:model`` (``anthropic:claude-haiku-4-5``,
``openai:gpt-5-mini``). Handing the model only a few named tools keeps
the context small enough for fast, inexpensive models — the tool subset
is configuration, not code.

The module is pure: HTTP is injected by the caller, so tests run without
a network and the server owns timeouts and threading.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

DEFAULT_MAX_ROUNDS = 6
MAX_ROUNDS_LIMIT = 12
DEFAULT_MAX_TOKENS = 1024

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SUPPORTED_SERVICES = ("anthropic", "openai")

# send_http(url, headers, body_bytes) -> (status, response_bytes)
SendHttp = Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]
# dispatch_tool(name, arguments) -> {"http_status": int, "response": Any}
DispatchTool = Callable[[str, dict[str, Any]], dict[str, Any]]


class AIProviderError(RuntimeError):
    """Raised when the provider returns an unusable response."""


class InvalidChatRequestError(ValueError):
    """Raised when a chat request payload is not usable."""


def split_model(model: str) -> tuple[str, str]:
    """Split 'service:model' and validate the service."""
    if not isinstance(model, str) or ":" not in model:
        raise InvalidChatRequestError(
            "model must be 'service:model', like anthropic:claude-haiku-4-5"
        )
    service, _, name = model.partition(":")
    if service not in SUPPORTED_SERVICES or not name:
        supported = ", ".join(SUPPORTED_SERVICES)
        raise InvalidChatRequestError(f"model service must be one of: {supported}")
    return service, name


def mcp_tools_as_provider_tools(
    tool_names: list[str],
    catalog: list[dict[str, Any]],
    *,
    service: str,
) -> list[dict[str, Any]]:
    """Convert named MCP catalog entries into the provider's tool format."""
    by_name = {tool["name"]: tool for tool in catalog}
    unknown = [name for name in tool_names if name not in by_name]
    if unknown:
        raise InvalidChatRequestError(f"Unknown tools: {', '.join(sorted(unknown))}")

    tools = []
    for name in tool_names:
        tool = by_name[name]
        if service == "anthropic":
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema", {"type": "object"}),
                }
            )
        else:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", {"type": "object"}),
                    },
                }
            )
    return tools


def run_chat(
    *,
    send_http: SendHttp,
    dispatch_tool: DispatchTool,
    service: str,
    model: str,
    key: str,
    message: str,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Run one chat turn, looping through tool calls until a final reply.

    ``history`` is prior conversation turns ({"role": "user"|"assistant",
    "content": ...}) so a caller can resume a conversation it logged —
    the server itself stays stateless about chats.
    """
    if not isinstance(message, str) or not message.strip():
        raise InvalidChatRequestError("message is required")
    if attachments and len(attachments) > MAX_ATTACHMENTS_PER_TURN:
        raise InvalidChatRequestError(
            f"{len(attachments)} attachments; the limit is "
            f"{MAX_ATTACHMENTS_PER_TURN} per message."
        )
    rounds = max(1, min(int(max_rounds), MAX_ROUNDS_LIMIT))

    if service == "anthropic":
        provider = _AnthropicProvider(key, model, system, tools or [], max_tokens)
    else:
        provider = _OpenAIProvider(key, model, system, tools or [], max_tokens)

    for turn in normalize_history(history):
        provider.messages.append(turn)
    provider.start(message.strip(), attachments or None)
    tool_log: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    for round_index in range(rounds):
        status, body = send_http(*provider.request())
        parsed = provider.parse(status, body)
        usage["input_tokens"] += parsed["usage"].get("input_tokens", 0)
        usage["output_tokens"] += parsed["usage"].get("output_tokens", 0)

        if not parsed["tool_calls"]:
            return {
                "reply": parsed["text"],
                "rounds": round_index + 1,
                "tool_calls": tool_log,
                "usage": usage,
            }

        results = []
        for call in parsed["tool_calls"]:
            result = dispatch_tool(call["name"], call["arguments"])
            tool_log.append(
                {
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "http_status": result.get("http_status"),
                }
            )
            results.append((call, result))
        provider.add_tool_results(parsed, results)

    return {
        "reply": parsed["text"] or "(stopped after reaching the tool-call round limit)",
        "rounds": rounds,
        "tool_calls": tool_log,
        "usage": usage,
        "truncated": True,
    }


MAX_HISTORY_TURNS = 40


def normalize_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Validate and trim prior turns to plain user/assistant text messages."""
    if history is None:
        return []
    if not isinstance(history, list):
        raise InvalidChatRequestError("history must be a list of {role, content} turns")
    turns = []
    for item in history:
        if not isinstance(item, dict):
            raise InvalidChatRequestError("history turns must be objects")
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            raise InvalidChatRequestError(
                "history turns need role user|assistant and non-empty content"
            )
        turns.append({"role": role, "content": content.strip()})
    return turns[-MAX_HISTORY_TURNS:]


# === chat attachments ========================================================
#
# A dropped file becomes a provider-neutral PART here, and the split of
# labour is the point: the SERVER resolves file ids to bytes (permissions,
# ownership, reading the blob -- all I/O), while this module classifies and
# formats -- pure, testable without a data directory, like everything else
# in it. run_chat then renders parts per provider, because Anthropic wants
# a content array with base64 image blocks and OpenAI wants data: URLs,
# and neither difference belongs anywhere near the caller.

MAX_ATTACHMENTS_PER_TURN = 8
MAX_IMAGE_BYTES = 5 * 1024 * 1024        # both providers cap around here
MAX_TEXT_BYTES = 200 * 1024              # ~50k tokens of context is plenty

IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXTUAL_CONTENT_TYPES = (
    "application/json", "application/xml", "application/x-yaml",
    "application/csv", "application/javascript", "application/sql",
)


def attachment_part(filename: str, content_type: str, data: bytes) -> dict[str, Any]:
    """Classify one file's bytes into a chat part, or refuse by name.

    Images pass through as base64 for the provider's vision input; anything
    that reads as text is inlined as text. Everything else is refused with
    a message that says what IS supported -- a binary the model cannot see
    must fail here, loudly, rather than arrive as mojibake the model
    hallucinates an interpretation of. That silent path is the whole
    reason this function exists as a gate rather than a best-effort
    decoder.
    """
    import base64

    name = str(filename or "file")
    ctype = str(content_type or "").split(";")[0].strip().lower()

    if ctype in IMAGE_MEDIA_TYPES:
        if len(data) > MAX_IMAGE_BYTES:
            raise InvalidChatRequestError(
                f"{name}: image is {len(data) // 1024}KB; the limit is "
                f"{MAX_IMAGE_BYTES // 1024}KB. Resize it before attaching."
            )
        return {"kind": "image", "name": name, "media_type": ctype,
                "data_b64": base64.b64encode(data).decode("ascii")}

    looks_textual = (
        ctype.startswith("text/")
        or ctype in TEXTUAL_CONTENT_TYPES
        or ctype.endswith("+json") or ctype.endswith("+xml")
    )
    if not looks_textual:
        try:
            data.decode("utf-8")
            looks_textual = True          # honest text under a lazy MIME type
        except UnicodeDecodeError:
            looks_textual = False
    if looks_textual:
        if len(data) > MAX_TEXT_BYTES:
            raise InvalidChatRequestError(
                f"{name}: text file is {len(data) // 1024}KB; the limit is "
                f"{MAX_TEXT_BYTES // 1024}KB. Attach an excerpt instead."
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidChatRequestError(
                f"{name}: declared {ctype or 'text'} but is not valid UTF-8."
            ) from exc
        return {"kind": "text", "name": name, "text": text}

    raise InvalidChatRequestError(
        f"{name}: cannot attach {ctype or 'unknown'} to a chat. Supported: "
        f"images ({', '.join(IMAGE_MEDIA_TYPES)}) and text-like files "
        f"(anything UTF-8). PDFs and other binaries need their text "
        f"extracted first."
    )


def _content_with_parts_anthropic(message: str, parts: list[dict[str, Any]]):
    content: list[dict[str, Any]] = []
    for part in parts:
        if part["kind"] == "image":
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": part["media_type"],
                "data": part["data_b64"]}})
        else:
            content.append({"type": "text",
                            "text": f"[file: {part['name']}]\n{part['text']}"})
    content.append({"type": "text", "text": message})
    return content


def _content_with_parts_openai(message: str, parts: list[dict[str, Any]]):
    content: list[dict[str, Any]] = []
    for part in parts:
        if part["kind"] == "image":
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{part['media_type']};base64,{part['data_b64']}"}})
        else:
            content.append({"type": "text",
                            "text": f"[file: {part['name']}]\n{part['text']}"})
    content.append({"type": "text", "text": message})
    return content


class _AnthropicProvider:
    def __init__(self, key, model, system, tools, max_tokens):
        self.key = key
        self.model = model
        self.system = system
        self.tools = tools
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []

    def start(self, message: str, parts: list[dict[str, Any]] | None = None) -> None:
        # A plain string when there are no parts, so a text-only chat's
        # outbound payload is byte-identical to what it always was.
        content = (_content_with_parts_anthropic(message, parts)
                   if parts else message)
        self.messages.append({"role": "user", "content": content})

    def request(self) -> tuple[str, dict[str, str], bytes]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self.messages,
        }
        if self.system:
            payload["system"] = self.system
        if self.tools:
            payload["tools"] = self.tools
        headers = {
            "x-api-key": self.key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        return ANTHROPIC_URL, headers, json.dumps(payload).encode("utf-8")

    def parse(self, status: int, body: bytes) -> dict[str, Any]:
        payload = _json_or_error(status, body)
        content = payload.get("content") or []
        text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
        tool_calls = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "arguments": item.get("input") or {},
            }
            for item in content
            if item.get("type") == "tool_use"
        ]
        raw_usage = payload.get("usage") or {}
        self._last_content = content
        return {
            "text": "\n".join(part for part in text_parts if part),
            "tool_calls": tool_calls,
            "usage": {
                "input_tokens": raw_usage.get("input_tokens", 0),
                "output_tokens": raw_usage.get("output_tokens", 0),
            },
        }

    def add_tool_results(self, parsed, results) -> None:
        self.messages.append({"role": "assistant", "content": self._last_content})
        result_blocks = []
        for call, result in results:
            result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": json.dumps(result.get("response"), default=str)[:8000],
                }
            )
        self.messages.append({"role": "user", "content": result_blocks})


class _OpenAIProvider:
    def __init__(self, key, model, system, tools, max_tokens):
        self.key = key
        self.model = model
        self.tools = tools
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []
        if system:
            self.messages.append({"role": "system", "content": system})

    def start(self, message: str, parts: list[dict[str, Any]] | None = None) -> None:
        content = (_content_with_parts_openai(message, parts)
                   if parts else message)
        self.messages.append({"role": "user", "content": content})

    def request(self) -> tuple[str, dict[str, str], bytes]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "max_completion_tokens": self.max_tokens,
        }
        if self.tools:
            payload["tools"] = self.tools
        headers = {
            "authorization": f"Bearer {self.key}",
            "content-type": "application/json",
        }
        return OPENAI_URL, headers, json.dumps(payload).encode("utf-8")

    def parse(self, status: int, body: bytes) -> dict[str, Any]:
        payload = _json_or_error(status, body)
        choices = payload.get("choices") or []
        if not choices:
            raise AIProviderError("Provider returned no choices")
        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []
        tool_calls = []
        for item in raw_calls:
            function = item.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {"id": item.get("id"), "name": function.get("name"), "arguments": arguments}
            )
        raw_usage = payload.get("usage") or {}
        self._last_message = message
        return {
            "text": message.get("content") or "",
            "tool_calls": tool_calls,
            "usage": {
                "input_tokens": raw_usage.get("prompt_tokens", 0),
                "output_tokens": raw_usage.get("completion_tokens", 0),
            },
        }

    def add_tool_results(self, parsed, results) -> None:
        self.messages.append(self._last_message)
        for call, result in results:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result.get("response"), default=str)[:8000],
                }
            )


def select_price_row(
    rows: list[dict[str, Any]], *, provider: str, model: str
) -> dict[str, Any] | None:
    """Pick the most recent ai_prices row for (provider, model), or None.

    "Most recent" is the matching row with the greatest ``effective_date``
    (ISO 8601 strings sort lexicographically). Prices are ordinary records —
    editable live, never hardcoded — so a missing row is expected, not an
    error; the caller decides what that means (it records tokens with a
    null cost rather than failing the chat).
    """
    candidates = [
        row for row in rows if row.get("provider") == provider and row.get("model") == model
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.get("effective_date") or "")


def compute_cost_cents(
    tokens_in: int, tokens_out: int, price_row: dict[str, Any] | None
) -> int | None:
    """Integer-cent cost for a turn, or None when there is no price row.

    Money math stays in integer cents throughout: each side is
    ``tokens * per_million_cents // 1_000_000`` (integer division), summed
    after rounding down separately -- never a float division on money.
    """
    if price_row is None:
        return None
    input_rate = int(price_row.get("input_per_million_cents") or 0)
    output_rate = int(price_row.get("output_per_million_cents") or 0)
    return (tokens_in * input_rate // 1_000_000) + (tokens_out * output_rate // 1_000_000)


def _json_or_error(status: int, body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIProviderError(f"Provider returned unreadable response (HTTP {status})") from exc
    if status >= 400:
        detail = payload.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)
        raise AIProviderError(f"Provider error (HTTP {status}): {str(detail)[:300]}")
    if not isinstance(payload, dict):
        raise AIProviderError("Provider returned a non-object response")
    return payload
