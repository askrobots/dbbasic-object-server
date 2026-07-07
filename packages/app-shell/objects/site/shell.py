"""The shell: talk to the whole system from one input.

Prefixes are instant record operations against the collection APIs the
visitor is already authorized for; anything else goes to the AI, which
answers with the user's own model, key, and MCP tool subset:

    $50 lunch          quick note
    .fix the header    quick task
    ^https://x title   save a link
    ~flywheel          global search
    /help              built-ins
    anything else      AI chat with tools
"""

_STYLE = """
:root { color-scheme: dark; --bg: #0b0b10; --panel: #17171f; --line: #2b2b37;
        --text: #f4f4f7; --muted: #a2a2ad; --blue: #5aa7ff; --green: #52d273;
        --amber: #f1b747; --red: #ff6b6b; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
.wrap { max-width: 860px; margin: 0 auto; padding: 1.25rem; display: flex;
        flex-direction: column; min-height: 100vh; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 0.75rem;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header h1 { font-size: 1.05rem; margin: 0; }
header .who { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
a { color: var(--blue); text-decoration: none; }
#log { flex: 1; overflow-y: auto; padding-bottom: 1rem; }
.entry { margin-bottom: 0.75rem; }
.entry .in { color: var(--green); white-space: pre-wrap; word-break: break-word; }
.entry .in::before { content: "> "; color: var(--muted); }
.entry .out { color: var(--text); white-space: pre-wrap; word-break: break-word; }
.entry .out.err { color: var(--red); }
.entry .tools { color: var(--amber); font-size: 0.78rem; }
.entry .pending { color: var(--muted); }
form#prompt { display: flex; gap: 0.5rem; border-top: 1px solid var(--line);
              padding-top: 0.75rem; }
form#prompt input { flex: 1; background: var(--panel); color: var(--text);
                    border: 1px solid var(--line); border-radius: 6px;
                    padding: 0.55rem 0.7rem; font: inherit; }
.hint { color: var(--muted); background: var(--panel); border: 1px solid var(--line);
        border-radius: 8px; padding: 0.9rem 1rem;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
"""

_HELP = (
    "$text        quick note\\n"
    ".title       quick task\\n"
    "^url title   save a link\\n"
    "~query       global search\\n"
    "/model x     set AI model (service:model)\\n"
    "/tools a,b   set AI tool subset\\n"
    "/help        this text\\n"
    "anything else goes to the AI with your tools"
)

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const log = document.getElementById("log");
let prefs = {id: OWNER_ID, ai_model: "anthropic:claude-haiku-4-5",
             tools: "global_search,list_records,get_record,create_record"};
let aiHistory = [];

function entry(input) {
  const div = document.createElement("div");
  div.className = "entry";
  div.innerHTML = `<div class="in">${esc(input)}</div><div class="out pending">&hellip;</div>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div.querySelector(".out");
}

function finish(out, text, {err = false, tools = null} = {}) {
  out.classList.remove("pending");
  out.classList.toggle("err", err);
  out.textContent = text;
  if (tools && tools.length) {
    const info = document.createElement("div");
    info.className = "tools";
    info.textContent = "tools: " + tools.map((t) => `${t.name}(${t.http_status})`).join(" ");
    out.parentNode.insertBefore(info, out);
  }
  log.scrollTop = log.scrollHeight;
}

async function api(method, path, payload) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  return [res.ok, await res.json()];
}

async function record(input, output, kind) {
  api("POST", "/collections/shell_commands/records",
      {id: crypto.randomUUID(), input, output: String(output).slice(0, 4000),
       kind, owner_id: OWNER_ID});
}

async function loadPrefs() {
  const res = await fetch(`/collections/shell_preferences/records/${OWNER_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (res.ok) { const body = await res.json(); prefs = body.record || prefs; }
}

async function loadHistory() {
  const res = await fetch("/collections/shell_commands/records?limit=1000",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  for (const row of (body.records || []).slice(-30)) {
    const out = entry(row.input);
    finish(out, row.output || "");
    if (row.kind === "ai" && row.output) {
      aiHistory.push({role: "user", content: row.input});
      aiHistory.push({role: "assistant", content: row.output});
    }
  }
}

async function savePrefs(changes) {
  Object.assign(prefs, changes);
  const [ok] = await api("PUT", `/collections/shell_preferences/records/${OWNER_ID}`, changes);
  if (!ok) await api("POST", "/collections/shell_preferences/records", prefs);
}

async function run(input) {
  const out = entry(input);
  const first = input[0];

  if (first === "$" || first === ".") {
    const isNote = first === "$";
    const text = input.slice(1).trim();
    const path = isNote ? "/collections/notes/records" : "/collections/tasks/records";
    const payload = isNote
      ? {id: crypto.randomUUID(), content: text, is_public: "false", owner_id: OWNER_ID}
      : {id: crypto.randomUUID(), title: text, owner_id: OWNER_ID};
    const [ok, body] = await api("POST", path, payload);
    finish(out, ok ? (isNote ? "note saved" : "task created") : body.error, {err: !ok});
    if (ok) record(input, isNote ? "note saved" : "task created", isNote ? "note" : "task");
    return;
  }

  if (first === "^") {
    const rest = input.slice(1).trim();
    const [url, ...titleParts] = rest.split(/\\s+/);
    const [ok, body] = await api("POST", "/collections/links/records",
      {id: crypto.randomUUID(), url, title: titleParts.join(" ") || url, owner_id: OWNER_ID});
    finish(out, ok ? "link saved" : body.error, {err: !ok});
    if (ok) record(input, "link saved", "link");
    return;
  }

  if (first === "~") {
    const query = input.slice(1).trim();
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=5`,
                            {credentials: "same-origin", headers: {accept: "application/json"}});
    const body = await res.json();
    const lines = [];
    for (const [collection, hits] of Object.entries(body.results || {})) {
      for (const hit of hits) {
        const summary = Object.entries(hit).filter(([k]) => k !== "id")
          .map(([, v]) => v).join(" \\u00b7 ").slice(0, 100);
        lines.push(`${collection}/${hit.id}  ${summary}`);
      }
    }
    finish(out, lines.join("\\n") || "no matches");
    record(input, lines.join("\\n") || "no matches", "search");
    return;
  }

  if (first === "/") {
    const [cmd, ...rest] = input.slice(1).split(/\\s+/);
    if (cmd === "help") { finish(out, HELP); return; }
    if (cmd === "model" && rest.length) {
      await savePrefs({ai_model: rest[0]});
      finish(out, `model set to ${rest[0]}`);
      return;
    }
    if (cmd === "tools" && rest.length) {
      await savePrefs({tools: rest.join("")});
      finish(out, `tools set to ${prefs.tools}`);
      return;
    }
    if (cmd === "time") { finish(out, new Date().toString()); return; }
    finish(out, "unknown command; /help", {err: true});
    return;
  }

  const tools = prefs.tools.split(",").map((t) => t.trim()).filter(Boolean);
  const [ok, body] = await api("POST", "/api/ai/chat",
    {message: input, model: prefs.ai_model, tools, history: aiHistory.slice(-20)});
  finish(out, ok ? body.reply : body.error, {err: !ok, tools: ok ? body.tool_calls : null});
  if (ok) {
    aiHistory.push({role: "user", content: input});
    aiHistory.push({role: "assistant", content: body.reply});
  }
  record(input, ok ? body.reply : body.error, "ai");
}

document.getElementById("prompt").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = event.target.elements["line"];
  const input = box.value.trim();
  if (!input) return;
  box.value = "";
  run(input);
});
loadPrefs();
loadHistory();
</script>
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_shell served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/shell">Sign in</a> to use the shell.</p>'
        script = ""
    else:
        body = """
<div id="log"><div class="entry"><div class="out">type /help for commands, or just talk</div></div></div>
<form id="prompt" autocomplete="off">
<input name="line" placeholder="&gt;_" autofocus>
</form>
"""
        script = (
            f"<script>const OWNER_ID = {user_id!r}; const HELP = \"{_HELP}\";"
            f"{_SCRIPT}"
        )

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/shell">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shell</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header><h1>Shell</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
