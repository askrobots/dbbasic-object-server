"""Talk: the shell as a stage instead of a transcript.

Same brain as /shell -- one POST /api/ai/chat, the same prefs/model/tools,
the same shell_commands history -- projected differently. The shell renders
a scrolling keyboard log; Talk fills the screen with whatever the
conversation just produced (a spoken answer or a materialized /views page)
and reduces the transcript to a single caption strip. No server route is
new here: this object is a second window onto the same conversation.

_BASE_CAPABILITIES below is copied verbatim from shell.py's system prompt
(including the views MATERIALIZE PAGES block) so the two stay in sync --
edit both together. Talk adds one addendum on top: short spoken replies,
and never speaking ids/urls/paths aloud.
"""

import json

# Page-unique layout only; palette, chrome, and inputs come from /style, so
# Talk reskins with the active theme like every other page.
_STYLE = """
.talkwrap { display: flex; flex-direction: column; min-height: calc(100vh - 3.5rem); }
.stage { flex: 0 0 85vh; height: 85vh; display: flex; align-items: center; justify-content: center;
         padding: 1rem; overflow: hidden; }
.stage .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: 2rem; max-width: 640px; max-height: 100%; overflow-y: auto;
               font-size: 1.4rem; line-height: 1.5; text-align: center; }
.stage .card.placeholder { color: var(--muted); font-size: 1.1rem; }
.stage .card a { color: var(--accent-strong); }
.stage .card code { background: var(--panel-2); color: var(--accent-strong);
                     padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }
.stageframe { width: 100%; height: 100%; border: 1px solid var(--line); border-radius: var(--radius-md); }
.bar { border-top: 1px solid var(--line); padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
.captions { min-height: 2.6rem; font-size: 0.95rem; }
.cap.user { color: var(--muted); }
.cap.assistant { color: var(--text); font-weight: 600; }
.controls { display: flex; align-items: center; gap: 0.75rem; }
#mic { width: 4.5rem; height: 4.5rem; flex: 0 0 auto; border-radius: 50%; font-size: 0.75rem;
       background: var(--panel-2); border: 1px solid var(--line); color: var(--muted); cursor: pointer; }
#mic.on { color: var(--danger); border-color: var(--danger); background: var(--panel); }
form#prompt { flex: 1; display: flex; gap: 0.5rem; }
form#prompt input { flex: 1; }
.backlink { color: var(--muted); font-size: 0.82rem; white-space: nowrap; }
"""

# Copied verbatim from shell.py's /api/ai/chat system prompt so the two
# stay in sync. Edit both together.
_BASE_CAPABILITIES = (
    "You are the shell of this user's object server. Answer in plain terminal text "
    "with no markdown formatting. Be concise. Use your tools when the question is "
    "about the user's records. "
    "You can also MATERIALIZE PAGES: the views collection turns records into live "
    "pages. When the user asks for a page/dashboard/view (or an answer clearly worth "
    "keeping as one), create a views record: fields title, layout 'single', "
    "owner_id (the user), pinned 'false', is_public 'false', and blocks = a JSON "
    "string of a list of block objects. Block kinds: "
    "{kind:'count', collection, filters:{field:value}, label, warn_over?} | "
    "{kind:'list', collection, filters?, sort?:'newest'|'oldest', title?} | "
    "{kind:'form', collection, record_id?} | "
    "{kind:'detail', collection, record_id} | "
    "{kind:'markdown', text}. "
    "After creating it, tell the user the page is at /views/{id} (the record id). "
    "Prefer a count block above a list block for status-style pages."
)

_TALK_ADDENDUM = (
    " You are in voice mode. Reply in one or two short spoken sentences. When the "
    "user asks to see, list, or track anything, create (or update) a views record "
    "and say you have put it on screen. NEVER read ids, urls, uuids, or paths aloud."
)

TALK_SYSTEM = _BASE_CAPABILITIES + _TALK_ADDENDUM

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const stage = document.getElementById("stage");
const capUser = document.getElementById("capUser");
const capAssistant = document.getElementById("capAssistant");
let prefs = {id: OWNER_ID, ai_model: "anthropic:claude-haiku-4-5",
             tools: "global_search,list_records,get_record,create_record,update_record"};
let aiHistory = [];
const TTS_MAX_CHARS = 800;
const VIEWS_PATH_RE = /\\/views\\/[A-Za-z0-9_-]+/;

// Strip markdown -- and, unlike the shell's stripForSpeech, urls and
// /views paths too -- down to sentences worth speaking. The talk-mode
// system prompt tells the model never to read an id/url/path aloud; this
// is the belt to that suspenders in case it slips one in anyway.
function stripForSpeech(text) {
  return String(text ?? "")
    .replace(/```[\\s\\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!\\[[^\\]]*\\]\\([^)]*\\)/g, " ")
    .replace(/\\[([^\\]]*)\\]\\([^)]*\\)/g, "$1")
    .replace(/https?:\\/\\/\\S+/g, " ")
    .replace(/\\/views\\/\\S+/g, " ")
    .replace(/[*_#>~]/g, " ")
    .replace(/\\s+/g, " ")
    .trim()
    .slice(0, TTS_MAX_CHARS);
}

function renderCard(text) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = window.dbbasicMarkdown ? window.dbbasicMarkdown(text) : esc(text);
  stage.innerHTML = "";
  stage.appendChild(card);
}

function renderStageView(path) {
  stage.innerHTML = `<iframe src="${esc(path)}" class="stageframe"></iframe>`;
}

function viewPathFromReply(text) {
  const match = String(text || "").match(VIEWS_PATH_RE);
  return match ? match[0].replace(/[.,;:)]+$/, "") : null;
}

// Fallback for when the model materialized a page but its spoken reply
// never mentions the path -- inspect the tool calls the chat turn made
// (that's what /api/ai/chat actually returns: name/arguments/http_status
// per call) for a create_record or update_record on the views collection.
function viewPathFromToolCalls(toolCalls) {
  if (!Array.isArray(toolCalls)) return null;
  for (let i = toolCalls.length - 1; i >= 0; i--) {
    const call = toolCalls[i];
    const args = call && call.arguments;
    if (!args || args.collection !== "views") continue;
    const id = (call.name === "create_record" && args.record && args.record.id)
      || (call.name === "update_record" && args.record_id);
    if (id) return "/views/" + id;
  }
  return null;
}

let currentAudio = null;
let speaking = false;

// Speak one assistant reply. Server TTS first (POST /api/tts, played as an
// object URL); any failure falls back to the browser's own speechSynthesis.
// The mic is kept stopped for the whole span (see stopListening() calls
// around this) and only resumed once playback actually ends, so the
// recognizer never hears the machine talking to itself.
async function speak(text) {
  const spoken = stripForSpeech(text);
  if (!spoken) { resumeListeningIfNeeded(); return; }
  speaking = true;
  stopListening();
  try {
    const res = await fetch("/api/tts", {
      method: "POST", credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "audio/wav"},
      body: JSON.stringify({text: spoken}),
    });
    if (!res.ok) throw new Error("tts endpoint failed");
    const url = URL.createObjectURL(await res.blob());
    if (currentAudio) currentAudio.pause();
    currentAudio = new Audio(url);
    currentAudio.addEventListener("ended", () => {
      URL.revokeObjectURL(url);
      speaking = false;
      resumeListeningIfNeeded();
    });
    await currentAudio.play();
  } catch (e) {
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(spoken);
      utter.addEventListener("end", () => { speaking = false; resumeListeningIfNeeded(); });
      window.speechSynthesis.speak(utter);
    } else {
      speaking = false;
      resumeListeningIfNeeded();
    }
  }
}

// Conversation-mode mic: the button toggles a mode, not a single listen.
// While the mode is on, onend auto-restarts recognition -- unless we are
// currently speaking (playing TTS), in which case resumeListeningIfNeeded()
// (called from speak()'s completion handlers above) is what restarts it.
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null;
let conversationMode = false;
let listening = false;

function stopListening() {
  listening = false;
  if (recognizer) { try { recognizer.stop(); } catch (e) { /* already stopped */ } }
}

function startListening() {
  if (!recognizer || listening || speaking) return;
  listening = true;
  try { recognizer.start(); } catch (e) { listening = false; }
}

function resumeListeningIfNeeded() {
  if (conversationMode && !speaking && !listening) startListening();
}

function initMic() {
  const mic = document.getElementById("mic");
  if (!SpeechRecognitionCtor || !mic) { if (mic) mic.hidden = true; return; }
  mic.hidden = false;
  recognizer = new SpeechRecognitionCtor();
  recognizer.continuous = false;
  recognizer.interimResults = true;
  recognizer.lang = "en-US";

  recognizer.onresult = (event) => {
    let text = "";
    for (let i = 0; i < event.results.length; i++) text += event.results[i][0].transcript;
    capUser.textContent = text;
    if (event.results[event.results.length - 1].isFinal) {
      stopListening();
      if (text.trim()) submitTurn(text.trim());
    }
  };
  recognizer.onerror = () => { listening = false; };
  recognizer.onend = () => {
    // CRITICAL: stop recognition before playing TTS (done in speak()) and
    // only resume after playback ends -- if mode is still on AND we are
    // not currently speaking, restart here too, so a recognizer that ends
    // for any other reason (silence timeout, browser quirk) still keeps
    // conversation mode going instead of going quiet.
    listening = false;
    if (conversationMode && !speaking) startListening();
  };

  mic.addEventListener("click", () => {
    conversationMode = !conversationMode;
    mic.classList.toggle("on", conversationMode);
    mic.textContent = conversationMode ? "listening\\u2026" : "mic";
    if (conversationMode) startListening(); else stopListening();
  });
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

// Talk shares shell_commands with the shell -- one conversation, two
// projections -- so recent shell turns give this page's chat call
// continuity too. Nothing is rendered from it; only the caption strip and
// the stage reflect the current turn.
async function loadHistory() {
  const res = await fetch("/collections/shell_commands/records?limit=1000",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  for (const row of (body.records || []).slice(-30)) {
    if (row.kind === "ai" && row.output) {
      aiHistory.push({role: "user", content: row.input});
      aiHistory.push({role: "assistant", content: row.output});
    }
  }
}

async function submitTurn(input) {
  capUser.textContent = input;
  capAssistant.textContent = "\\u2026";
  stopListening();

  const tools = prefs.tools.split(",").map((t) => t.trim()).filter(Boolean);
  const [ok, body] = await api("POST", "/api/ai/chat",
    {message: input, model: prefs.ai_model, tools, history: aiHistory.slice(-20), system: TALK_SYSTEM});

  const replyText = ok ? body.reply : (body.error || "Something went wrong.");
  capAssistant.textContent = stripForSpeech(replyText) || replyText;

  if (ok) {
    aiHistory.push({role: "user", content: input});
    aiHistory.push({role: "assistant", content: body.reply});
    const viewPath = viewPathFromReply(body.reply) || viewPathFromToolCalls(body.tool_calls);
    if (viewPath) renderStageView(viewPath); else renderCard(body.reply);
  } else {
    renderCard(replyText);
  }
  speak(replyText);
  record(input, replyText, "ai");
}

document.getElementById("prompt").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = event.target.elements["line"];
  const input = box.value.trim();
  if (!input) return;
  box.value = "";
  submitTurn(input);
});
initMic();
loadPrefs();
loadHistory();
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_talk served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/talk">Sign in</a> to use Talk.</p>'
        script = ""
    else:
        body = """
<div class="talkwrap">
<div id="stage" class="stage"><div class="card placeholder">Tap the mic and talk, or type below.</div></div>
<div class="bar">
<div class="captions">
<div id="capUser" class="cap user"></div>
<div id="capAssistant" class="cap assistant"></div>
</div>
<div class="controls">
<button type="button" id="mic" hidden aria-label="toggle conversation mode">mic</button>
<form id="prompt" autocomplete="off">
<input name="line" placeholder="or type\\u2026" autofocus>
<button type="submit" class="btn primary" aria-label="send">send</button>
</form>
<a class="backlink" href="/shell">back to shell</a>
</div>
</div>
</div>
"""
        script = f"<script>const OWNER_ID = {user_id!r}; const TALK_SYSTEM = {json.dumps(TALK_SYSTEM)};{_SCRIPT}</script>"

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/talk">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Talk</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Talk</h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/markdown"></script>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
