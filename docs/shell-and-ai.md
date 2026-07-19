# The Shell and AI — Talk to the Whole System

The shell (`/shell`) is one input that reaches everything: instant
record commands, global search, and an AI that operates the server
through the same MCP tools any agent uses — with **your key, your
model choice, and your permissions**.

```mermaid
flowchart TD
    U["You type"] --> D{"prefix?"}
    D -->|"$ . ^"| REC["Create note / task / link<br/>(records API, your session)"]
    D -->|"~"| SRCH["GET /api/search"]
    D -->|"/"| CMD["Built-ins: /help /key /model /tools /voice"]
    D -->|"anything else"| CHAT["POST /api/ai/chat"]
    CHAT --> PROV["AI provider<br/>(your stored key)"]
    PROV -->|"tool calls"| MCP["MCP tool subset<br/>dispatched with YOUR credentials"]
    MCP --> PERM["Permission engine + audit"]
    PERM --> PROV
    PROV --> REPLY["Reply + tool log + token usage + cost"]
```

## Setup: Your Key, Your Model

AI features use each user's own provider key — the server stores it
write-only and calls providers on your behalf. Key material never
appears in any response, record, or backup.

In the shell:

```
/key anthropic sk-ant-...     store a key (masked, never logged)
/keys                          which services have keys
/model anthropic:claude-haiku-4-5     pick a model (service:model)
/model openai:gpt-5-mini              or another provider
```

Or over HTTP: `PUT /identity/users/{you}/service-keys` with your
session (see the [HTTP API contract](http-api-contract.md)).

## Tool Subsets: Small Context, Configurable Power

The AI is offered a **named subset** of the MCP tool catalog, not the
whole thing. Small subsets keep the context small enough for fast,
inexpensive models — and the subset is your safety dial:

```
/tools global_search,list_records,get_record,create_record     conversation mode
/tools global_search,list_objects,get_object_source,create_object,update_object_source,execute_object     builder mode
```

Every tool call the model makes is dispatched through the server's own
routing **with your credentials**: the AI can do exactly what you could
do yourself — row filters, field redaction, and the audit trail apply.
An AI acting for a user is never more powerful than the user.

## Conversation Memory

The shell logs every exchange to the `shell_commands` collection (your
history is just records — searchable and owner-scoped). A new browser
session replays recent history and sends prior AI turns back with each
message, so conversations resume. The server stays stateless about
chats: `POST /api/ai/chat` accepts a `history` list, and what to
remember is the client's choice.

## Cost Recording

Every chat turn is priced server-side, not by the caller. `POST
/api/ai/chat` reads token counts straight from the provider's own response,
looks up the model's price in the `ai_prices` collection (editable records,
never a hardcoded table -- so a price change is a data edit, not a
deploy), and computes the cost in integer cents: `tokens *
per_million_cents // 1_000_000`, input and output added separately, no
float division on money. The result -- `tokens_in`, `tokens_out`,
`cost_cents`, `model`, `provider` -- lands in the `ai_usage` collection,
written by the chat handler itself with the caller's user id as actor, so
the record can't be skipped or forged the way a client-written log could
be. A model with no matching price row still gets its tokens recorded;
only the cost is null. The same numbers come back in the chat response's
`usage` object so a surface can show them immediately.

## Coding Without Coding

With builder-mode tools, the AI can create and edit live objects:

> make an object called site_dice that renders a page rolling two dice

The model writes the source, calls `create_object`, and the page is
live at `/dice` immediately — objects load per execution, so there is
no deploy step between the AI writing code and the code serving
traffic. Edits use `update_object_source`; every version lands in
source history with rollback; and create/update responses report the
methods the code actually exposes, so the model can self-correct.

Object writes ride the admin gate (an admin-role session today), and
source writes require `DBBASIC_ENABLE_SOURCE_WRITES=true` — the same
boundaries that govern humans.

## Voice

The shell doubles as a push-to-talk terminal. Both halves are browser-native
where possible, server-assisted where the browser falls short:

- **Speech in**: the mic button uses `window.SpeechRecognition` (or the
  `webkit` prefix); interim words show live in the input, and the final
  transcript submits through the same form-submit path a typed line does —
  no separate send code. Neither Chrome nor Safari expose the API outside a
  secure context (`https://` or `localhost`), and without it the button
  simply stays hidden.
- **Speech out**: with voice mode on, the shell POSTs each assistant reply
  (markdown and code fences stripped first — only prose is spoken, never
  tool-call noise) to `POST /api/tts` and plays the returned WAV. If that
  endpoint is disabled, has no engine, or the request fails for any reason,
  the browser's own `speechSynthesis.speak` says the line instead, so voice
  mode never goes silent because of a server-side gap.

`/voice`, `/voice on`, `/voice off` toggle it inline (mirrors `/model`); the
setting persists on `shell_preferences.voice_enabled` and is honored on
page load like `ai_model` and `tools`.

### `POST /api/tts`

```http
POST /api/tts
Authorization: Bearer <session-token>   (or the session cookie)

{"text": "one note matches", "voice": "en-us"}
```

Response: `audio/wav` bytes, or the usual `{"status": "error", "error"}`
JSON shape on failure. Requires `DBBASIC_ENABLE_TTS=true` (off by default)
and a signed-in session — same posture as `/api/ai/chat`. `text` is capped
at 800 characters (413 beyond that).

The engine is discovered at call time, first match wins: `espeak-ng`,
`espeak`, then macOS `say` (development convenience — `say` writes AIFF and
the also-stock `afconvert` turns it into WAV; if `afconvert` isn't present
the endpoint returns 501 rather than growing a bespoke audio pipeline). No
engine on `PATH` at all is a 503 with a clear message. Successful audio is
cached at `data/tts-cache/{sha256(engine|voice|text)}.wav` — a repeat of
the same line is a cache read, not a re-synthesis. There's no eviction in
v1; operators who want a bound should prune that directory on their own
schedule.

## The Instant Commands

| Input | Effect |
|---|---|
| `$ pay the hosting bill` | quick note |
| `. fix the header` | quick task |
| `^https://example.com docs` | save a link |
| `~flywheel` | global search across collections |
| `/help` | list commands |

These never touch the AI — they are one permission-checked record
write each, which is why they feel instant.

## For Agents

Everything above is equally available to AI agents connecting over MCP
(`POST /api/mcp`) with their own identities and labeled sessions, and
to headless callers hitting `POST /api/ai/chat` directly. One surface,
many kinds of operator.
