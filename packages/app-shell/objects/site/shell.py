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

# Terminal-specific layout only; palette, chrome, and inputs come from /style,
# so the shell reskins with the active theme like every other page.
_STYLE = """
.wrap { display: flex; flex-direction: column; min-height: calc(100vh - 3.5rem); }
#log { flex: 1; overflow-y: auto; padding-bottom: 1rem;
       font-family: var(--font-mono); font-size: 0.9rem; }
.entry { margin-bottom: 0.75rem; }
.entry .in { color: var(--positive); white-space: pre-wrap; word-break: break-word; }
.entry .in::before { content: "> "; color: var(--muted); }
.entry .out { color: var(--text); white-space: pre-wrap; word-break: break-word; }
.entry .out.err { color: var(--danger); }
.entry .tools { color: var(--warning); font-size: 0.78rem; }
.entry .pending { color: var(--muted); }
form#prompt { display: flex; gap: 0.5rem; border-top: 1px solid var(--line);
              padding-top: 0.75rem; }
form#prompt input { flex: 1; font-family: var(--font-mono); }
#sessbar { border-top: 1px solid var(--line); padding: 0.3rem 0.6rem; }
#sessbar select { max-width: 100%; font-size: 0.8rem; opacity: 0.85; }
#attachbar { display: flex; gap: 0.4rem; flex-wrap: wrap; padding: 0.3rem 0.6rem;
  border-top: 1px solid var(--line); font-size: 0.8rem; }
#attachbar .chip { border: 1px solid var(--line); border-radius: 10px;
  padding: 0 0.5rem; display: inline-flex; gap: 0.35rem; align-items: center; }
#attachbar .chip button { border: 0; background: none; cursor: pointer; color: inherit; }
body.dragover::after { content: "drop files to attach"; position: fixed; inset: 0;
  display: grid; place-items: center; font-size: 1.2rem;
  background: rgba(20, 20, 28, 0.7);
  border: 2px dashed var(--accent, #b5713a); pointer-events: none; }
#mic { font-family: var(--font-mono); font-size: 0.78rem; color: var(--muted);
       background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius-sm);
       padding: 0 0.7rem; cursor: pointer; }
#mic.listening { color: var(--danger); border-color: var(--danger); }
/* Rendered-markdown AI output (theme-tokened) */
.entry .out.md { white-space: normal; }
.entry .out.md p { margin: 0.35rem 0; }
.entry .out.md a { color: var(--accent-strong); text-decoration: underline; }
.entry .out.md strong { color: var(--text); font-weight: 700; }
.entry .out.md code { background: var(--panel-2); color: var(--accent-strong);
                      padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }
.entry .out.md pre { background: var(--panel-2); padding: 0.6rem 0.8rem;
                     border-radius: var(--radius-sm); overflow-x: auto; margin: 0.4rem 0; }
.entry .out.md pre code { background: none; padding: 0; }
.entry .out.md ul, .entry .out.md ol { padding-left: 1.4rem; margin: 0.3rem 0; }
.entry .out.md h1, .entry .out.md h2, .entry .out.md h3 { font-size: 1em; margin: 0.4rem 0 0.2rem; }
/* Materialized /views page, embedded compact under the reply that made it. */
.viewembed { margin: 0.4rem 0 0.75rem; }
.viewembed iframe { width: 100%; max-height: 360px; border: 1px solid var(--line);
                     border-radius: var(--radius-md); }
.viewembed a { display: inline-block; margin-top: 0.25rem; font-size: 0.78rem;
               color: var(--accent-strong); }
"""

_HELP = (
    "$text        quick note\\n"
    ".title       quick task\\n"
    "^url title   save a link\\n"
    "~query       global search\\n"
    "/key anthropic sk-...   store your AI key (masked, not logged)\\n"
    "/keys        which services have keys\\n"
    "/model x     set AI model (service:model)\\n"
    "/tools a,b   set AI tool subset\\n"
    "/voice [on|off]   toggle spoken replies + mic input\\n"
    "/help        this text\\n"
    "anything else goes to the AI with your tools"
)

_SCRIPT = """
let SESSION_ID = freshSessionId();
function freshSessionId() {
  return crypto.randomUUID ? crypto.randomUUID()
       : "s-" + Math.random().toString(36).slice(2);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const log = document.getElementById("log");
let prefs = {id: OWNER_ID, ai_model: "anthropic:claude-sonnet-5",
             tools: "global_search,list_collections,list_records,get_record,create_record,update_record,read_page",
             voice_enabled: "false", talk_tts: "auto"};
let aiHistory = [];
const TTS_MAX_CHARS = 800;
const DEFAULT_TALK_TTS = "auto";
const voiceOn = () => prefs.voice_enabled === "true";
function talkTtsMode() {
  const m = String(prefs.talk_tts || DEFAULT_TALK_TTS).trim();
  return (m === "server" || m === "browser") ? m : DEFAULT_TALK_TTS;
}

function entry(input) {
  const div = document.createElement("div");
  div.className = "entry";
  div.innerHTML = `<div class="in">${esc(input)}</div><div class="out pending">&hellip;</div>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div.querySelector(".out");
}

// Matches a /views/{id} path in already-rendered HTML, but not one that is
// already the href of a link (dbbasicMarkdown may have linkified it itself
// from markdown link syntax) -- so linkifyViews() never nests an <a> inside
// an existing href attribute.
const VIEWS_PATH_RE = /(?<!href=")\/views\/[A-Za-z0-9_-]+/g;

function linkifyViews(html) {
  return html.replace(VIEWS_PATH_RE, (path) => `<a href="${path}">${path}</a>`);
}

function finish(out, text, {err = false, tools = null, markdown = false} = {}) {
  out.classList.remove("pending");
  out.classList.toggle("err", err);
  // Markdown rendering is the shared /markdown utility (window.dbbasicMarkdown),
  // defined once. If it is unavailable, degrade to escaped plain text — never a
  // second markdown implementation.
  if (markdown) {
    out.classList.add("md");
    // [[view:<id>]] is the machine channel: extract it for the embed, then
    // strip it so it is never rendered.
    const marker = String(text ?? "").match(/\[\[view:([A-Za-z0-9-]+)\]\]/);
    text = String(text ?? "").replace(/\[\[view:[^\]]*\]\]/g, " ").trim();
    out.innerHTML = linkifyViews(window.dbbasicMarkdown ? window.dbbasicMarkdown(text) : esc(text));
    // A materialized page is worth more than a link: embed it right under
    // the reply, small, with an escape hatch to the full page.
    const viewMatch = marker ? ["/views/" + marker[1]] : String(text ?? "").match(/\/views\/[A-Za-z0-9_-]+/);
    if (viewMatch) {
      const path = viewMatch[0].replace(/[.,;:)]+$/, "");
      const embed = document.createElement("div");
      embed.className = "viewembed";
      embed.innerHTML = `<iframe src="${path}?embed=1"></iframe><a href="${path}" target="_blank" rel="noopener">open ↗</a>`;
      out.insertAdjacentElement("afterend", embed);
    }
  } else { out.textContent = text; }
  if (tools && tools.length) {
    const info = document.createElement("div");
    info.className = "tools";
    info.textContent = "tools: " + tools.map((t) => `${t.name}(${t.http_status})`).join(" ");
    out.parentNode.insertBefore(info, out);
  }
  log.scrollTop = log.scrollHeight;
}

// Strip markdown down to sentences worth speaking: fenced/inline code and
// tool noise never reach TTS, only the assistant's prose does.
function stripForSpeech(text) {
  return String(text ?? "")
    .replace(/```[\\s\\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!\\[[^\\]]*\\]\\([^)]*\\)/g, " ")
    .replace(/\\[([^\\]]*)\\]\\([^)]*\\)/g, "$1")
    .replace(/https?:\\/\\/\\S+/g, " ")
    .replace(/\\/views\\/\\S+/g, " ")
    .replace(/\\[\\[view:[^\\]]*\\]\\]/g, " ")
    .replace(/[*_#>~]/g, " ")
    .replace(/\\s+/g, " ")
    .trim()
    .slice(0, TTS_MAX_CHARS);
}

let currentAudio = null;

// Voice picking for the browser speechSynthesis engine -- kept identical to
// talk.py's pickVoice()/hasLocalVoice(): prefer a natural-sounding named
// voice, then the first on-device (localService) English voice, else leave
// utter.voice unset and let the browser use its own default.
const PREFERRED_VOICE_RE = /Samantha|Ava|Karen|Daniel/;
function getVoices() {
  return (window.speechSynthesis && window.speechSynthesis.getVoices()) || [];
}
function pickVoice() {
  const voices = getVoices();
  if (!voices.length) return null;
  const byName = voices.find((v) => PREFERRED_VOICE_RE.test(v.name));
  if (byName) return byName;
  const localEn = voices.find((v) => v.localService && /^en/i.test(v.lang));
  return localEn || null;
}
// The "auto" engine gate: is there at least one on-device voice at all
// (any language) -- if so, speaking never has to leave the device.
function hasLocalVoice() {
  return getVoices().some((v) => v.localService);
}

// speechSynthesis-only speaking path, used both when talk_tts is "browser"
// and as the fallback when "server"/"auto" server TTS fails.
function speakBrowser(text) {
  if (!window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(text);
  const voice = pickVoice();
  if (voice) utter.voice = voice;
  window.speechSynthesis.speak(utter);
}

// Speak one assistant reply, routed by the talk_tts preference:
//   "server"  -- always server TTS (POST /api/tts), speechSynthesis on failure.
//   "browser" -- always speechSynthesis, no server round-trip.
//   "auto"    -- speechSynthesis if an on-device voice is available, else
//                server-with-fallback, same as "server".
// Any failure -- flag off, no engine, network -- falls back to the
// browser's own speechSynthesis so voice mode never just goes silent.
async function speak(text) {
  const spoken = stripForSpeech(text);
  if (!spoken) return;

  const mode = talkTtsMode();
  if (mode === "browser") { speakBrowser(spoken); return; }
  if (mode === "auto" && window.speechSynthesis && hasLocalVoice()) {
    speakBrowser(spoken);
    return;
  }

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
    currentAudio.addEventListener("ended", () => URL.revokeObjectURL(url));
    await currentAudio.play();
  } catch (e) {
    speakBrowser(spoken);
  }
}

// Push-to-talk mic. Absent the browser API the button stays hidden -- no
// polyfills, no fallback recorder, voice input just isn't offered.
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null;
let listening = false;

function stopListening() {
  listening = false;
  const mic = document.getElementById("mic");
  if (mic) { mic.classList.remove("listening"); mic.textContent = "mic"; }
  if (recognizer) { try { recognizer.stop(); } catch (e) { /* already stopped */ } }
}

function startListening() {
  const input = document.querySelector('#prompt input[name="line"]');
  if (!recognizer || listening || !input) return;
  listening = true;
  const mic = document.getElementById("mic");
  if (mic) { mic.classList.add("listening"); mic.textContent = "listening"; }
  input.value = "";
  try { recognizer.start(); } catch (e) { stopListening(); }
}

function initMic() {
  const mic = document.getElementById("mic");
  if (!SpeechRecognitionCtor || !mic) return;
  mic.hidden = false;
  recognizer = new SpeechRecognitionCtor();
  recognizer.continuous = false;
  recognizer.interimResults = true;
  recognizer.lang = "en-US";

  recognizer.onresult = (event) => {
    const input = document.querySelector('#prompt input[name="line"]');
    if (!input) return;
    let text = "";
    // Segments are joined with an explicit space. Chrome includes leading
    // spaces on continuation segments; iOS does NOT, and concatenating
    // raw produced "comptureshowmemynotes" from an iPad -- one giant
    // token the whitespace tokenizer cannot split, so the wake gate could
    // never match and every utterance was invisibly discarded. Trimming
    // then joining is correct on both: the tokenizer collapses runs of
    // whitespace anyway, so a doubled space costs nothing.
    for (let i = 0; i < event.results.length; i++) {
      const seg = event.results[i][0].transcript.trim();
      if (seg) text += (text ? " " : "") + seg;
    }
    input.value = text;
    if (event.results[event.results.length - 1].isFinal) {
      stopListening();
      // Final transcript goes through the *existing* submit path unchanged.
      const form = document.getElementById("prompt");
      if (input.value.trim() && form.requestSubmit) form.requestSubmit();
    }
  };
  recognizer.onerror = () => stopListening();
  recognizer.onend = () => stopListening();

  mic.addEventListener("click", () => (listening ? stopListening() : startListening()));
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

// === sessions =================================================================
//
// A session is a FOLD over the server-recorded turns (grouped by the
// session_id the server stamps on every /api/ai/chat turn), not a second
// table -- q9's QuerySession kept messages as one growing JSON blob per
// row and derived its title in three duplicated places; here the title
// is simply the first input, computed in exactly one. The q9 behaviours
// worth keeping ARE kept: resume the latest conversation automatically
// when it is fresh (their 30-minute window), deep-link with ?session=,
// mark the current one, and exactly ONE way to start a new session --
// their own dropdown had two differently-wired "new" affordances, which
// the port audit flagged as the thing not to copy.
let sessions = [];
const RESUME_WINDOW_MS = 30 * 60 * 1000;

function foldSessions(rows) {
  const by = {};
  for (const row of rows) {
    const sid = row.session_id || "";
    if (!sid) continue;         // pre-session history stays queryable, not replayed
    (by[sid] = by[sid] || []).push(row);
  }
  return Object.entries(by).map(([id, rs]) => {
    rs.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    const first = rs.find((r) => r.input) || rs[0];
    return {id, rows: rs, count: rs.length,
            title: String((first && first.input) || "(untitled)").slice(0, 40),
            last: String(rs[rs.length - 1].created_at || "")};
  }).sort((a, b) => b.last.localeCompare(a.last)).slice(0, 10);
}

function agoShort(iso) {
  const ms = Date.now() - Date.parse(iso || 0);
  if (!isFinite(ms) || ms < 0) return "";
  const m = Math.round(ms / 60000);
  if (m < 1) return "now";
  if (m < 60) return m + "m";
  const h = Math.round(m / 60);
  if (h < 24) return h + "h";
  return Math.round(h / 24) + "d";
}

function renderSessionPicker() {
  const sel = document.getElementById("sessionpick");
  if (!sel) return;
  const opts = ['<option value="__new">\u2795 new session</option>'];
  for (const s of sessions) {
    const label = s.title + (s.last ? " \u2014 " + agoShort(s.last) : "");
    opts.push('<option value="' + esc(s.id) + '"'
              + (s.id === SESSION_ID ? " selected" : "") + ">"
              + esc(label) + "</option>");
  }
  sel.innerHTML = opts.join("");
  if (!sessions.some((s) => s.id === SESSION_ID)) sel.value = "__new";
}

function adoptSession(s) {
  SESSION_ID = s.id;
  aiHistory = [];
  const log = document.getElementById("log");
  if (log) log.innerHTML = "";
  for (const row of s.rows.slice(-30)) {
    const out = entry(row.input);
    finish(out, row.output || "", {markdown: row.kind === "ai"});
    if (row.kind === "ai" && row.output) {
      aiHistory.push({role: "user", content: row.input});
      aiHistory.push({role: "assistant", content: row.output});
    }
  }
  renderSessionPicker();
}

async function loadHistory() {
  const res = await fetch("/collections/shell_commands/records?limit=1000",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const rows = (await res.json()).records || [];
  sessions = foldSessions(rows);

  const wanted = new URLSearchParams(location.search).get("session");
  const deep = wanted && sessions.find((s) => s.id === wanted);
  const latest = sessions[0];
  const fresh = latest && (Date.now() - Date.parse(latest.last || 0) < RESUME_WINDOW_MS);
  if (deep) adoptSession(deep);
  else if (fresh) adoptSession(latest);   // q9's lazy resume, same window
  else renderSessionPicker();

  const sel = document.getElementById("sessionpick");
  if (sel) sel.addEventListener("change", () => {
    if (sel.value === "__new") {
      SESSION_ID = freshSessionId();
      aiHistory = [];
      const log = document.getElementById("log");
      if (log) log.innerHTML = '<div class="entry"><div class="out">new session</div></div>';
      history.replaceState(null, "", location.pathname);
      renderSessionPicker();
    } else {
      const s = sessions.find((x) => x.id === sel.value);
      if (s) { adoptSession(s); history.replaceState(null, "", "?session=" + s.id); }
    }
  });
}

async function savePrefs(changes) {
  Object.assign(prefs, changes);
  const [ok] = await api("PUT", `/collections/shell_preferences/records/${OWNER_ID}`, changes);
  if (!ok) await api("POST", "/collections/shell_preferences/records", prefs);
}

async function run(input) {
  const display = input.startsWith("/key ")
    ? input.split(/\\s+/).slice(0, 2).join(" ") + " \\u2022\\u2022\\u2022\\u2022"
    : input;
  const out = entry(display);
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
    if (cmd === "key" && rest.length >= 2) {
      const [service, ...keyParts] = rest;
      const [ok, body] = await api("PUT", `/identity/users/${OWNER_ID}/service-keys`,
                                   {service, key: keyParts.join("")});
      finish(out, ok ? `${service} key stored (never logged, never readable back)` : body.error,
             {err: !ok});
      return;
    }
    if (cmd === "keys") {
      const [ok, body] = await api("GET", `/identity/users/${OWNER_ID}/service-keys`);
      const lines = ok ? (body.services || []).map((s) => `${s.service}  set ${s.updated_at}`) : [];
      finish(out, ok ? (lines.join("\\n") || "no keys stored; /key anthropic sk-...") : body.error,
             {err: !ok});
      return;
    }
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
    if (cmd === "voice") {
      const arg = (rest[0] || "").toLowerCase();
      const next = arg === "on" ? "true" : arg === "off" ? "false" : voiceOn() ? "false" : "true";
      await savePrefs({voice_enabled: next});
      if (next !== "true" && currentAudio) currentAudio.pause();
      if (next !== "true" && window.speechSynthesis) window.speechSynthesis.cancel();
      finish(out, `voice ${next === "true" ? "on" : "off"}`);
      return;
    }
    if (cmd === "time") { finish(out, new Date().toString()); return; }
    finish(out, "unknown command; /help", {err: true});
    return;
  }

  const tools = prefs.tools.split(",").map((t) => t.trim()).filter(Boolean);
  // Attachments ride THIS turn only. History keeps a text marker instead of
  // re-sending the bytes -- images are big, and the model's own reply
  // already carries what it saw in them.
  const attachIds = pendingAttach.map((f) => f.id);
  const attachNote = pendingAttach.length
    ? " [attached: " + pendingAttach.map((f) => f.name).join(", ") + "]" : "";
  const [ok, body] = await api("POST", "/api/ai/chat",
    {message: input, model: prefs.ai_model, tools, history: aiHistory.slice(-20),
     attachments: attachIds.length ? attachIds : undefined,
     session_id: SESSION_ID, source: "shell",
     system: "You are the shell of this user's object server. Answer in plain terminal text " +
             "with no markdown formatting. Be concise. Use your tools when the question is " +
             "about the user's records. " +
             "You can also MATERIALIZE PAGES: the views collection turns records into live " +
             "pages. When the user asks for a page/dashboard/view (or an answer clearly worth " +
             "keeping as one), create a views record: fields title, layout 'single', " +
             "owner_id (the user), pinned 'false', is_public 'false', and blocks = a JSON " +
             "string of a list of block objects. Block kinds: " +
             "{kind:'count', collection, filters:{field:value}, label, warn_over?} | " +
             "{kind:'list', collection, filters?, sort?:'newest'|'oldest', title?} | " +
             "{kind:'form', collection, record_id?} | " +
             "{kind:'detail', collection, record_id} | " +
             "{kind:'markdown', text}. " +
             "After creating it, tell the user the page is at /views/{id} (the record id). " +
             "Prefer a count block above a list block for status-style pages. " +
             "You do NOT know from memory which collections exist. Call list_collections " +
             "and read its result before answering; if the collection the user named " +
             "appears in that result, USE it -- never say it is missing when it is in " +
             "the list. The user's tasks live in the collection named tasks. " +
             "To show one specific record on screen, create a view whose blocks contain a " +
             "detail block for it. Never claim something is on screen unless you created " +
             "or updated a views record in this same turn. " +
             "Whenever the screen should show a view -- newly created OR one that already " +
             "exists -- end your reply with the marker [[view:<record id>]] alone on the " +
             "last line. The marker is machine-read; it is never displayed or spoken, so " +
             "it does not violate the no-ids-aloud rule. " +
             "Example reply: \\"Here are your open tasks. " +
             "[[view:26b247ed-3b1a-4206-b060-1d92847194de]]\\"" +
             " You can also READ WEB PAGES with the read_page tool when the user gives a " +
             "URL or asks you to read/summarize a page: it returns the page text and its " +
             "links numbered in order. Offer the links as \\"link 1, link 2, ...\\" so the " +
             "user can say \\"open link N\\" and you read_page that link's url next." +
             " Current local date/time: " + new Date().toString() + "."});
  finish(out, ok ? body.reply : body.error,
         {err: !ok, tools: ok ? body.tool_calls : null, markdown: ok});
  if (ok && pendingAttach.length) { pendingAttach = []; renderAttachBar(); }
  if (ok) {
    aiHistory.push({role: "user", content: input + attachNote});
    aiHistory.push({role: "assistant", content: body.reply});
    // Only the final assistant text is ever spoken -- never tool-call noise.
    if (voiceOn()) speak(body.reply);
  }
  // AI turns are recorded by the server inside /api/ai/chat -- stamped
  // and session-grouped. Command turns (note/link/search) stay client-
  // written below: they never touch the chat endpoint.
}

document.getElementById("prompt").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = event.target.elements["line"];
  const input = box.value.trim();
  if (!input) return;
  box.value = "";
  run(input);
});
initMic();
loadPrefs();
loadHistory();
</script>

// === drag a file in, talk about it ==========================================
//
// Drop anywhere on the page. Each file is uploaded to the EXISTING
// /api/files surface (owner-stamped server-side, the same endpoint the
// record-attachments capability uses -- no parent, so it is simply the
// user's file), and its id rides the NEXT AI message as `attachments`.
// The server turns ids into provider content: images become vision input,
// text files are inlined. Binaries the model cannot see are refused by
// name at send time rather than arriving as mojibake.
let pendingAttach = [];
function renderAttachBar() {
  const bar = document.getElementById("attachbar");
  if (!bar) return;
  bar.hidden = !pendingAttach.length;
  bar.innerHTML = pendingAttach.map((f, i) =>
    '<span class="chip">[file] ' + esc(f.name)
    + '<button type="button" data-detach="' + i + '" aria-label="remove">x</button></span>'
  ).join("");
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-detach]");
  if (!btn) return;
  pendingAttach.splice(Number(btn.getAttribute("data-detach")), 1);
  renderAttachBar();
});
function attachNoteLine(text) {
  const log = document.getElementById("log");
  if (!log) return;
  const div = document.createElement("div");
  div.className = "entry";
  div.innerHTML = '<div class="out">' + esc(text) + "</div>";
  log.appendChild(div); log.scrollTop = log.scrollHeight;
}
async function uploadDropped(file) {
  const data = new FormData();
  data.append("file", file);
  const res = await fetch("/api/files", {method: "POST", credentials: "same-origin", body: data});
  let resp = null; try { resp = await res.json(); } catch (x) {}
  if (!res.ok || !resp || !resp.file) {
    attachNoteLine("could not attach " + file.name + ": " + ((resp && resp.error) || res.status));
    return;
  }
  pendingAttach.push({id: resp.file.id, name: resp.file.filename || file.name});
  renderAttachBar();
  attachNoteLine("attached: " + (resp.file.filename || file.name)
    + " -- it rides your next message");
}
let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  const types = (e.dataTransfer && e.dataTransfer.types) || [];
  if (![].some.call(types, (t) => t === "Files")) return;
  e.preventDefault(); dragDepth++; document.body.classList.add("dragover");
});
document.addEventListener("dragover", (e) => { e.preventDefault(); });
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragover"); }
});
document.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dragover");
  if (typeof OWNER_ID === "undefined" || !OWNER_ID) {
    attachNoteLine("sign in to attach files"); return;
  }
  const files = (e.dataTransfer && e.dataTransfer.files) || [];
  for (const file of files) uploadDropped(file);
});

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
<div id="attachbar" hidden></div>
<div id="sessbar"><select id="sessionpick" aria-label="conversation">
<option value="__new">&#10133; new session</option></select></div>
<form id="prompt" autocomplete="off">
<input name="line" placeholder="&gt;_" autofocus>
<button type="submit" class="btn primary" aria-label="send">send</button>
<button type="button" id="mic" hidden aria-label="voice input">mic</button>
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
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Shell</h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/markdown"></script>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
