"""Talk: the shell as a stage instead of a transcript.

Same brain as /shell -- one POST /api/ai/chat, the same prefs/model/tools,
the same shell_commands table -- projected differently. The shell renders
a scrolling keyboard log; Talk fills the screen with whatever the
conversation just produced (a spoken answer or a materialized /views page)
and reduces the transcript to a single caption strip. No server route is
new here: this object is a second window onto the same conversation.

Every turn is still recorded to shell_commands (Talk and the shell share
that record), but Talk does NOT replay old shell_commands rows into the
model's context on load. A model given a transcript of *any* age of
in-context examples will imitate the habits those examples demonstrate
even when the current instructions say otherwise -- concretely, replaying
turns recorded before the [[view:<id>]] marker convention existed taught
the model to keep ignoring the marker instruction, no matter how the
system prompt was reworded. So each Talk page load starts aiHistory empty
and it only ever accumulates turns from *this* session.

_BASE_CAPABILITIES below is copied verbatim from shell.py's system prompt
(including the views MATERIALIZE PAGES block) so the two stay in sync --
edit both together. Talk adds one addendum on top: short spoken replies,
and never speaking ids/urls/paths aloud.
"""

import json

# Page-unique layout only; palette, chrome, and inputs come from /style, so
# Talk reskins with the active theme like every other page.
_STYLE = """
/* The page must FIT the viewport: the mic, the captions and the text box
   are one control surface, and a layout where you scroll between the nav
   and the input is one you cannot operate. The stage takes what is left
   after the fixed-height rows rather than claiming 85vh of its own --
   which is what pushed the input off-screen on a tall tablet.
   dvh where supported (iOS Safari's toolbars shrink the visible area and
   100vh lies about it); -webkit-fill-available for older iPadOS that has
   neither dvh nor svh; plain vh last for everything else. */
.talkwrap { display: flex; flex-direction: column;
            height: calc(100vh - 3.5rem);
            height: -webkit-fill-available;
            height: calc(100dvh - 3.5rem);
            max-height: calc(100dvh - 3.5rem); overflow: hidden; }
.stage { flex: 1 1 auto; min-height: 0; display: flex; align-items: center;
         justify-content: center; padding: 1rem; overflow: hidden; }
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
.endpointhint { font-size: 0.78rem; opacity: 0.75; padding: 0 0.2rem 0.2rem; }
#sessionpick { max-width: 11rem; font-size: 0.78rem; opacity: 0.85; }
.cap.user { color: var(--muted); }
.cap.assistant { color: var(--text); font-weight: 600; }
.controls { display: flex; align-items: center; gap: 0.75rem; }
.micwrap { position: relative; flex: 0 0 auto; }
#mic { width: 4.5rem; height: 4.5rem; border-radius: 50%; font-size: 0.75rem;
       background: var(--panel-2); border: 1px solid var(--line); color: var(--muted); cursor: pointer; }
#mic.on { color: var(--danger); border-color: var(--danger); background: var(--panel); }
/* Level meter ring: a privacy indicator as much as a VU meter -- it must
   read as visibly dead (opacity 0, no shadow) whenever the mic is off. */
.miclevel { position: absolute; inset: -6px; border-radius: 50%; pointer-events: none;
            opacity: 0; box-shadow: 0 0 0 0 var(--danger); transition: opacity 80ms linear; }
form#prompt { flex: 1; display: flex; gap: 0.5rem; }
form#prompt input { flex: 1; }
.backlink { color: var(--muted); font-size: 0.82rem; white-space: nowrap; }
/* Caption states: armed (waiting for the wake word), active (live capture
   or the assistant's reply), sent (what was just submitted, muted). */
.cap.user.armed { font-style: italic; opacity: 0.7; }
.cap.user.active { color: var(--text); opacity: 1; font-style: normal; }
.cap.user.sent { color: var(--muted); opacity: 1; font-style: normal; }
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
    "Prefer a count block above a list block for status-style pages. "
    "You do NOT know from memory which collections exist. Call list_collections "
    "and read its result before answering; if the collection the user named "
    "appears in that result, USE it -- never say it is missing when it is in "
    "the list. The user's tasks live in the collection named tasks. "
    "To show one specific record on screen, create a view whose blocks contain a "
    "detail block for it. Never claim something is on screen unless you created "
    "or updated a views record in this same turn. "
    "Whenever the screen should show a view -- newly created OR one that already "
    "exists -- end your reply with the marker [[view:<record id>]] alone on the "
    "last line. The marker is machine-read; it is never displayed or spoken, so "
    "it does not violate the no-ids-aloud rule. "
    'Example reply: "Here are your open tasks. '
    '[[view:26b247ed-3b1a-4206-b060-1d92847194de]]"'
    " You can also READ WEB PAGES with the read_page tool when the user gives a "
    "URL or asks you to read/summarize a page: it returns the page text and its "
    'links numbered in order. Offer the links as "link 1, link 2, ..." so the '
    'user can say "open link N" and you read_page that link\'s url next.'
)

_TALK_ADDENDUM = (
    " You are in voice mode. Reply in one or two short spoken sentences. When the "
    "user asks to see, list, or track anything, create (or update) a views record "
    "and say you have put it on screen. NEVER read ids, urls, uuids, or paths aloud."
)

TALK_SYSTEM = _BASE_CAPABILITIES + _TALK_ADDENDUM

_SCRIPT = """
// One conversation per page load. The server stamps this on every turn it
// records, which is what makes a session listable and resumable later.
let SESSION_ID = freshSessionId();
function freshSessionId() {
  return crypto.randomUUID ? crypto.randomUUID()
       : "s-" + Math.random().toString(36).slice(2);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const stage = document.getElementById("stage");
const capUser = document.getElementById("capUser");
const capAssistant = document.getElementById("capAssistant");
let prefs = {id: OWNER_ID, ai_model: "anthropic:claude-sonnet-5",
             tools: "global_search,list_collections,list_records,get_record,create_record,update_record,read_page",
             talk_wake_word: "computer", talk_end_word: "over",
             talk_endpoint: "silence", talk_silence_ms: "1400", talk_tts: "auto",
             talk_tts_engine: "local", talk_stt_engine: "browser"};
let aiHistory = [];
const TTS_MAX_CHARS = 800;
const VIEWS_PATH_RE = /\\/views\\/[A-Za-z0-9_-]+/;

// Preference reads all go through pref() rather than raw prefs.x access:
// loadPrefs() replaces `prefs` wholesale with whatever record comes back
// from the server, and a record written before a field existed in the
// schema simply won't have that key. pref() falls back to the shipped
// default only when the key is *missing* -- an explicit empty string (the
// wake/end word's "off" setting) is a real value, not a gap, and must be
// left alone.
const DEFAULT_WAKE_WORD = "computer";
const DEFAULT_END_WORD = "over";
const DEFAULT_ENDPOINT = "silence";
const DEFAULT_SILENCE_MS = 1400;
const DEFAULT_TOOLS = "global_search,list_collections,list_records,get_record,create_record,update_record,read_page";
const DEFAULT_MODEL = "anthropic:claude-sonnet-5";
const DEFAULT_TALK_TTS = "auto";
const DEFAULT_TALK_TTS_ENGINE = "local";
const DEFAULT_TALK_STT_ENGINE = "browser";

function pref(name, fallback) {
  const v = prefs[name];
  return v === undefined || v === null ? fallback : v;
}
function wakeWord() { return String(pref("talk_wake_word", DEFAULT_WAKE_WORD)).trim(); }
function endWord() { return String(pref("talk_end_word", DEFAULT_END_WORD)).trim(); }
function endpointMode() {
  const m = String(pref("talk_endpoint", DEFAULT_ENDPOINT)).trim();
  return (m === "word" || m === "manual" || m === "silence") ? m : DEFAULT_ENDPOINT;
}
function talkTtsMode() {
  const m = String(pref("talk_tts", DEFAULT_TALK_TTS)).trim();
  return (m === "server" || m === "browser") ? m : DEFAULT_TALK_TTS;
}
// ?tts=openai|local and ?stt=openai|browser override the stored preference
// for THIS page load only -- nothing is saved back to shell_preferences.
// Exists so a link (e.g. "/talk?stt=openai") can be tried or shared
// without visiting settings first; the stored preference underneath is
// exactly what a plain "/talk" load still honors.
function urlOverride(param, allowed) {
  const v = new URLSearchParams(location.search).get(param);
  return allowed.includes(v) ? v : null;
}
function talkTtsEngine() {
  const override = urlOverride("tts", ["local", "openai"]);
  if (override) return override;
  const m = String(pref("talk_tts_engine", DEFAULT_TALK_TTS_ENGINE)).trim();
  return m === "openai" ? "openai" : DEFAULT_TALK_TTS_ENGINE;
}
// "openai" here is deliberately opt-in only, never an automatic fallback
// for browsers lacking SpeechRecognition -- a Firefox user who never
// touched this setting (or the URL override) and has no stored OpenAI key
// must keep seeing today's exact behavior (mic hidden), not a confusing
// "no key stored" error from a feature they never asked for. See
// initMic()/initCloudMic().
function talkSttEngine() {
  const override = urlOverride("stt", ["browser", "openai"]);
  if (override) return override;
  const m = String(pref("talk_stt_engine", DEFAULT_TALK_STT_ENGINE)).trim();
  return m === "openai" ? "openai" : DEFAULT_TALK_STT_ENGINE;
}
function silenceMs() {
  const n = Number(pref("talk_silence_ms", DEFAULT_SILENCE_MS));
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_SILENCE_MS;
}

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
    .replace(/\\[\\[view:[^\\]]*\\]\\]/g, " ")
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
  stage.innerHTML = `<iframe src="${esc(path)}?embed=1" class="stageframe"></iframe>`;
}

// The machine channel: the model ends a reply with [[view:<id>]] when the
// stage should show that view (new OR already-existing) -- explicit
// signaling instead of path-sniffing, since spoken replies never carry
// paths. The marker is stripped from everything displayed and spoken.
const VIEW_MARKER_RE = /\\[\\[view:([A-Za-z0-9-]+)\\]\\]/;

function stripViewMarker(text) {
  return String(text ?? "").replace(/\\[\\[view:[^\\]]*\\]\\]/g, " ").trim();
}

function viewPathFromReply(text) {
  const marker = String(text || "").match(VIEW_MARKER_RE);
  if (marker) return "/views/" + marker[1];
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

let speaking = false;

// Voice picking for the browser speechSynthesis engine: prefer a
// natural-sounding named voice, then the first on-device (localService)
// English voice, else leave utter.voice unset and let the browser use its
// own default.
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
// and as the fallback when "server"/"auto" server TTS fails. Carries the
// exact same speaking-flag/listen-pause contract as the server path below:
// set before speaking, cleared and resumed only once the utterance ends.
async function speakBrowser(text) {
  if (!window.speechSynthesis) { speaking = false; resumeListeningIfNeeded(); return; }
  speaking = true;
  await stopListeningAndWaitForRelease();
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(text);
  const voice = pickVoice();
  if (voice) utter.voice = voice;
  utter.addEventListener("end", () => { speaking = false; resumeListeningIfNeeded(); });
  utter.addEventListener("error", () => { speaking = false; resumeListeningIfNeeded(); });
  window.speechSynthesis.speak(utter);
}

// Speak one assistant reply, routed by the talk_tts preference:
//   "server"  -- always server TTS (POST /api/tts), speechSynthesis on failure.
//   "browser" -- always speechSynthesis, no server round-trip.
//   "auto"    -- speechSynthesis if an on-device voice is available, else
//                server-with-fallback, same as "server".
// Whichever path runs, the mic is kept stopped for the whole span and only
// resumed once playback actually ends, so the recognizer never hears the
// machine talking to itself.
async function speak(text) {
  const spoken = stripForSpeech(text);
  if (!spoken) { resumeListeningIfNeeded(); return; }

  const mode = talkTtsMode();
  if (mode === "browser") { speakBrowser(spoken); return; }
  if (mode === "auto" && window.speechSynthesis && hasLocalVoice()) {
    speakBrowser(spoken);
    return;
  }

  speaking = true;
  await stopListeningAndWaitForRelease();
  try {
    const res = await fetch("/api/tts", {
      method: "POST", credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "audio/*"},
      body: JSON.stringify({text: spoken, engine: talkTtsEngine()}),
    });
    if (!res.ok) throw new Error("tts endpoint failed");
    const url = URL.createObjectURL(await res.blob());
    // The SAME element every time, never a fresh one. iOS allows play()
    // outside a user gesture only on an element that has already played
    // inside one -- primeAudioForIOS() plays this element silent during
    // the mic tap, and changing src afterwards keeps the unlock. `new
    // Audio(url)` here was born locked, its play() rejected, and the
    // catch fell back to speechSynthesis... which the mute switch
    // silences. Server voice configured, server WAV fetched, and still
    // no sound: this line is why.
    const player = sharedTtsAudio();
    player.pause();
    player.src = url;
    // EVERY exit clears `speaking`, not just the happy one. It gates the
    // mic restart AND the transcript-stability submit, so a playback that
    // ends without an `ended` event -- a bad WAV, a decode error, an
    // interrupted element -- used to leave the page deaf and mute
    // forever: mic never resumed, submits silently blocked, and nothing
    // on screen said why. Found by the harness, whose spied play() never
    // completed, which is exactly the shape of a real playback failure.
    const done = () => {
      player.onended = player.onerror = player.onstalled = null;
      URL.revokeObjectURL(url);
      speaking = false;
      resumeListeningIfNeeded();
    };
    player.onended = done;
    player.onerror = done;
    player.onstalled = done;
    await player.play();
  } catch (e) {
    speakBrowser(spoken);
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

// --- Radio protocol: wake word / end word / VAD silence endpointing ------
//
// `buffer` holds finalized transcript from earlier recognizer sessions in
// the current utterance; `sessionLive` holds the current (possibly still
// interim) recognizer session's transcript. Both are already wake-word-
// stripped once armed flips false, so most readers just concatenate them.
//
// Word-boundary matching is done by tokenizing on whitespace rather than
// regex word-boundary escapes -- this file has already had one bug from a
// backslash escape that didn't survive its Python-string layer, so the
// word-match helpers below avoid backslash metacharacters entirely.
let buffer = "";
let sessionLive = "";
let armed = true;
let finalDebounceTimer = null;
let stabilityTimer = null;

function tokenize(text) {
  return String(text ?? "").trim().split(/\\s+/).filter(Boolean);
}

// Strip leading/trailing punctuation from one token for comparison --
// "computer," or "over." still match "computer" / "over".
function wordKey(token) {
  return token.replace(/[.,!?;:]+$/, "").replace(/^[.,!?;:]+/, "").toLowerCase();
}

// If `word` appears as a whole token in `text`, return everything after
// its first occurrence (joined back with spaces); otherwise null.
// Bounded edit distance for wake-word tolerance. Recognition mishears:
// an iPad delivered "compture" for "computer", and an exact match then
// silently discarded everything the user said after it -- armed forever,
// capturing nothing, with the transcript ON SCREEN making it look heard.
// The budget scales with length (short words stay exact: "hey" must not
// match "they"), and it is capped at 2 because a wake word is a gate, not
// a search. Worst case of a false arm: the mic captures a sentence the
// user then sees -- recoverable. Worst case of a strict miss: everything
// said is thrown away invisibly -- not recoverable, and that asymmetry is
// the whole argument for leniency here.
function editDistanceAtMost(a, b, budget) {
  if (Math.abs(a.length - b.length) > budget) return false;
  // Damerau (OSA), not plain Levenshtein, and the difference is the
  // whole point: a transposition costs 1. The observed mishear was
  // "compture" for "computer" -- two adjacent swaps, Damerau distance 2,
  // plain distance 3 -- so plain Levenshtein would have rejected the
  // exact case this function exists for. Swapped letters are what
  // recognition mishears LOOK like.
  let prevPrev = null;
  let prev = [];
  for (let j = 0; j <= b.length; j++) prev.push(j);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let v = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        v = Math.min(v, prevPrev[j - 2] + 1);
      }
      row.push(v);
      if (v < best) best = v;
    }
    if (best > budget) return false;   // the whole row is over budget
    prevPrev = prev;
    prev = row;
  }
  return prev[b.length] <= budget;
}

function wakeBudget(target) {
  if (target.length >= 7) return 2;
  if (target.length >= 4) return 1;
  return 0;
}

function matchesWakeWord(tokenKey, target) {
  if (tokenKey === target) return true;
  return editDistanceAtMost(tokenKey, target, wakeBudget(target));
}

function findWakeSplit(text, word) {
  const target = word.trim().toLowerCase();
  if (!target) return null;
  const tokens = tokenize(text);
  for (let i = 0; i < tokens.length; i++) {
    const key = wordKey(tokens[i]);
    if (matchesWakeWord(key, target)) return tokens.slice(i + 1).join(" ");
    // A no-space transcript fuses the wake word onto what follows
    // ("comptureshowmemynotes"). If a long token's PREFIX matches the
    // wake word, the remainder of the token is the start of the command
    // -- unspaced, but the model reads unspaced text far better than
    // this page reads a discarded utterance. Prefix lengths of the
    // target and one either side, so a mishear inside the fused prefix
    // still arms.
    if (key.length > target.length + wakeBudget(target)) {
      for (const len of [target.length, target.length + 1, target.length - 1]) {
        if (len < 3 || len >= key.length) continue;
        if (matchesWakeWord(key.slice(0, len), target)) {
          const rest = [key.slice(len)].concat(tokens.slice(i + 1));
          return rest.join(" ").trim();
        }
      }
    }
  }
  return null;
}

// If `text`'s last token is `word`, return everything before it (joined
// back with spaces); otherwise null. Tolerant of trailing punctuation on
// the last token ("...turn it on, over." still matches "over").
function tailMatchesEndWord(text, word) {
  const target = word.trim().toLowerCase();
  if (!target) return null;
  const tokens = tokenize(text);
  if (!tokens.length) return null;
  if (wordKey(tokens[tokens.length - 1]) !== target) return null;
  return tokens.slice(0, -1).join(" ");
}

function resetTalkState() {
  buffer = "";
  sessionLive = "";
  armed = true;
  if (finalDebounceTimer) { clearTimeout(finalDebounceTimer); finalDebounceTimer = null; }
  utteranceStarted = false;
  silenceStartTs = null;
  speechHoldStart = null;
}

function updateCaptionArmed() {
  const w = wakeWord();
  capUser.textContent = w ? `say "${w}" to address me` : "";
  capUser.classList.remove("active", "sent");
  capUser.classList.add("armed");
}

function updateCaptionActive(text) {
  capUser.textContent = text || "…";
  capUser.classList.remove("armed", "sent");
  capUser.classList.add("active");
}

// What would be submitted right now, with wake word and end word both
// stripped if present -- used by VAD endpointing and by manual send/Enter,
// which fold in whatever voice buffer exists regardless of protocol.
function pendingText() {
  let text = ((buffer ? buffer + " " : "") + sessionLive).trim();
  const w = wakeWord();
  if (w) {
    const split = findWakeSplit(text, w);
    if (split !== null) text = split;
  }
  const ew = endWord();
  if (ew) {
    const stripped = tailMatchesEndWord(text, ew);
    if (stripped !== null) text = stripped;
  }
  return text;
}

function bufferHasContent() {
  return ((buffer ? buffer + " " : "") + sessionLive).trim().length > 0;
}

// The single funnel for every voice-triggered submission (end word, VAD
// silence, isFinal-debounce fallback): clear/re-arm first so new speech
// during the in-flight chat call starts a fresh utterance, then hand off
// to the same submitTurn() the text box and Enter use.
function finalizeVoiceSubmit(text) {
  if (finalDebounceTimer) { clearTimeout(finalDebounceTimer); finalDebounceTimer = null; }
  if (stabilityTimer) { clearTimeout(stabilityTimer); stabilityTimer = null; }
  const clean = String(text ?? "").trim();
  resetTalkState();
  if (!clean) { if (conversationMode) updateCaptionArmed(); return; }
  // The wake word ALONE is a knock, not a question. One leaked through a
  // gate seam on an iPad and bought a 2.4-second provider round-trip
  // that answered "" -- money and silence for a turn nobody asked. The
  // check is fuzzy like the gate itself, so a misheard bare "compture"
  // is also swallowed.
  const w = wakeWord();
  if (w) {
    const only = tokenize(clean);
    if (only.length === 1 && matchesWakeWord(wordKey(only[0]), w.toLowerCase())) {
      if (conversationMode) updateCaptionArmed();
      return;
    }
  }
  submitTurn(clean);
}

// Called on every recognizer result (interim and final). Handles the wake
// gate, live caption, end-word override, and -- on a final result -- folds
// the session transcript into `buffer` and (endpoint "word" with no end
// word configured) arms the isFinal-debounce fallback.
function processTranscript(isFinal) {
  const combinedRaw = ((buffer ? buffer + " " : "") + sessionLive).trim();
  const w = wakeWord();

  // The split runs on EVERY event, not once at arming. Recognition
  // transcripts are cumulative -- each interim restates the utterance
  // from the beginning -- so a split applied only at arm time is undone
  // by the very next event, and the wake word rides into the submitted
  // command. Not hypothetical: the server's recorded turns include
  // "Show me my notes computer show me my notes", the wake word twice,
  // from exactly this. Splitting per event is idempotent: once buffer
  // holds only command text, findWakeSplit finds nothing and passes it
  // through untouched.
  const split = w ? findWakeSplit(combinedRaw, w) : null;
  if (armed) {
    if (w && split === null) {
      updateCaptionArmed();
      if (isFinal) sessionLive = "";
      return;
    }
    armed = false;   // empty wake word: capture everything while the mic is on
  }

  const active = (split !== null ? split : combinedRaw).trim();
  updateCaptionActive(active);

  const endpoint = endpointMode();
  const ew = endWord();
  if (ew && (endpoint === "word" || endpoint === "silence")) {
    const stripped = tailMatchesEndWord(active, ew);
    if (stripped !== null) { finalizeVoiceSubmit(stripped); return; }
  }

  // TRANSCRIPT-STABILITY endpointing: on a device where the level meter
  // cannot run (the iPad's exclusive microphone), end-of-utterance used
  // to wait for iOS to volunteer an isFinal -- which it does 2-6 seconds
  // after you stop, sometimes never. But the recognizer's own interims
  // ARE a silence detector: while you speak the text keeps changing, and
  // when it stops changing for the silence window, you have finished.
  // Same signal the RMS meter derived from amplitude, taken from the
  // transcript instead -- no second microphone stream, no contention.
  // Backstop even when the meter runs: a mis-calibrated meter (started
  // mid-speech, threshold learned from the voice itself) fails SILENT --
  // no error, no submit, ever. The transcript stalling is evidence the
  // meter cannot fake, so this fires a beat after the VAD would have; a
  // healthy VAD always wins first, and finalizeVoiceSubmit clears both
  // timers so the loser never double-fires.
  if (!armed && endpointMode() === "silence" && active) {
    if (stabilityTimer) clearTimeout(stabilityTimer);
    // Close over THIS event's already-split text. Rebuilding from the
    // raw sessionLive here would resurrect the wake word the split just
    // removed -- the timer is reset by every event, so the latest
    // event's `active` is by definition the whole utterance so far.
    stabilityTimer = setTimeout(() => {
      stabilityTimer = null;
      if (active && conversationMode && !speaking) finalizeVoiceSubmit(active);
    }, silenceMs() + (meterRunning ? 500 : 0));
  }

  if (isFinal) {
    buffer = active;
    sessionLive = "";
    // `silence` mode with no working meter has no other way to end an
    // utterance, so isFinal becomes the endpoint. Without this the mode
    // is a dead end on any device where the mic cannot be opened twice.
    if ((endpoint === "word" && !ew) || (endpoint === "silence" && !meterRunning)) {
      // No end word configured for word mode: fall back to the original
      // isFinal-triggered submit, debounced so a recognizer that fires
      // several quick finals in a row (common near silence) only submits
      // once.
      if (finalDebounceTimer) clearTimeout(finalDebounceTimer);
      finalDebounceTimer = setTimeout(() => {
        finalDebounceTimer = null;
        finalizeVoiceSubmit(buffer);
      }, 300);
    }
  }
}

// --- Mic level meter + VAD silence endpointing ----------------------------
//
// A separate getUserMedia stream (SpeechRecognition exposes no raw audio)
// feeds an AnalyserNode; a rAF loop computes RMS to both draw the level
// ring around the mic button and, in "silence" endpoint mode, decide when
// an utterance has ended. The stream is only open while conversation mode
// is on -- it doubles as the privacy indicator, so it must go fully dead
// (tracks stopped, context closed) the instant the mode turns off.
const MIN_SPEECH_THRESHOLD = 0.015;
const CALIBRATION_MS = 800;
const SPEECH_HOLD_MS = 150;

// Did the VAD meter actually come up? On iOS the microphone is EXCLUSIVE:
// SpeechRecognition and getUserMedia contend for it, so the second one to
// ask simply fails. Silence endpointing depends entirely on that second
// stream, and when it is absent nothing in `silence` mode ever submits --
// the user speaks, sees their words, and has to tap the mic again to make
// anything happen. Reported from an older iPad, and the fallback below is
// the fix: fall back to the recognizer's own isFinal, which every browser
// that supports recognition at all still gives us.
let meterRunning = false;
// Set the first time recognition returns a result. Until then the meter
// must not open a competing microphone stream.
let recognitionProven = false;
// The deferred meter start above is NOT sufficient on its own, and an
// iPad proved it: recognition delivered ONE result ("computer"), that
// proof started the meter, and the meter's stream then took the
// microphone back -- first word on screen, then nothing, which is the
// original bug moved one result later. So the meter must be able to
// LOSE, and it convicts itself with its own evidence: if it can hear
// voice (RMS above the speech threshold) while recognition has produced
// nothing for CONTENTION_MS, the two are not sharing the microphone --
// on a working platform, results flow continuously while you speak.
// Convicted once, it stays off for the session and isFinal endpointing
// takes over.
let meterContended = false;
let lastResultTs = 0;
const CONTENTION_MS = 2000;
let micStream = null;
let audioCtx = null;
let analyser = null;
let meterRafId = null;
let noiseFloor = 0;
let speechThreshold = MIN_SPEECH_THRESHOLD;
let calibrating = false;
let calibrationStart = 0;
let calibrationSamples = [];
let utteranceStarted = false;
let silenceStartTs = null;
let speechHoldStart = null;

// Absent capability, stated -- never a stub that pretends to work. The
// hint is the only way a user can tell "it is waiting for me to stop
// talking" from "it is waiting for me to tap".
function noteEndpointFallback() {
  meterRunning = false;
  const hint = document.getElementById("endpointhint");
  if (hint && endpointMode() === "silence") {
    hint.textContent = "No level meter on this device, so it sends when it "
      + "hears you finish rather than after a silence.";
    hint.hidden = false;
  }
}


function updateMeterVisual(rms) {
  const ring = document.getElementById("miclevel");
  if (!ring) return;
  // Force the ring to idle during TTS playback -- the stream is still
  // open (so the mic doesn't visibly "go dead" mid-conversation), but a
  // level driven by the machine's own voice bleeding into the mic would
  // be a misleading, distracting reading.
  const level = speaking ? 0 : Math.min(1, rms * 6);
  ring.style.opacity = String(Math.min(0.65, level * 1.3));
  ring.style.boxShadow = "0 0 0 " + (level * 14).toFixed(1) + "px var(--danger)";
}

function evaluateVAD(rms, now) {
  if (speaking || !conversationMode) { silenceStartTs = null; speechHoldStart = null; return; }
  if (endpointMode() !== "silence") return;
  const above = rms >= speechThreshold;
  if (above) {
    silenceStartTs = null;
    if (speechHoldStart === null) speechHoldStart = now;
    if (!utteranceStarted && now - speechHoldStart >= SPEECH_HOLD_MS) utteranceStarted = true;
    return;
  }
  speechHoldStart = null;
  if (!utteranceStarted || !bufferHasContent()) return;
  if (silenceStartTs === null) { silenceStartTs = now; return; }
  if (now - silenceStartTs < silenceMs()) return;
  silenceStartTs = null;
  utteranceStarted = false;
  if (armed) {
    // Never woken -- silence ended the attempt, discard rather than submit.
    resetTalkState();
    updateCaptionArmed();
  } else {
    finalizeVoiceSubmit(pendingText());
  }
}

async function startMeter() {
  meterRunning = false;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    noteEndpointFallback(); return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) { noteEndpointFallback(); return; }
  if (!conversationMode) { stream.getTracks().forEach((t) => t.stop()); return; }
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) {
    stream.getTracks().forEach((t) => t.stop()); noteEndpointFallback(); return;
  }
  meterRunning = true;
  lastResultTs = performance.now();
  micStream = stream;
  audioCtx = new Ctor();
  const source = audioCtx.createMediaStreamSource(micStream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);

  calibrating = true;
  calibrationStart = performance.now();
  calibrationSamples = [];
  utteranceStarted = false;
  silenceStartTs = null;
  speechHoldStart = null;
  const ring = document.getElementById("miclevel");
  if (ring) ring.hidden = false;

  function frame() {
    if (!analyser) return; // meter was stopped
    analyser.getByteTimeDomainData(data);
    let sumSquares = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sumSquares += v * v;
    }
    const rms = Math.sqrt(sumSquares / data.length);
    const now = performance.now();

    if (calibrating) {
      calibrationSamples.push(rms);
      if (now - calibrationStart >= CALIBRATION_MS) {
        const avg = calibrationSamples.reduce((a, b) => a + b, 0) / calibrationSamples.length;
        noiseFloor = avg;
        speechThreshold = Math.max(avg * 3, MIN_SPEECH_THRESHOLD);
        calibrating = false;
      }
    } else {
      // The contention check runs BEFORE the VAD: the meter hearing
      // voice while recognition says nothing is proof the microphone is
      // exclusive and the meter took it. Stopping the meter frees the
      // mic; stopListening() kicks the starved recognizer, whose onend
      // handler restarts it fresh -- and this time nothing competes.
      if (rms >= speechThreshold && now - lastResultTs > CONTENTION_MS) {
        meterContended = true;
        stopMeter();
        noteEndpointFallback();
        stopListening();
        return;
      }
      evaluateVAD(rms, now);
    }

    updateMeterVisual(rms);
    meterRafId = requestAnimationFrame(frame);
  }
  meterRafId = requestAnimationFrame(frame);
}

function stopMeter() {
  if (meterRafId) cancelAnimationFrame(meterRafId);
  meterRafId = null;
  analyser = null;
  if (audioCtx) { try { audioCtx.close(); } catch (e) { /* already closed */ } audioCtx = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  calibrating = false;
  utteranceStarted = false;
  silenceStartTs = null;
  speechHoldStart = null;
  const ring = document.getElementById("miclevel");
  if (ring) { ring.style.opacity = "0"; ring.style.boxShadow = "none"; ring.hidden = true; }
}

function stopListening() {
  listening = false;
  if (recognizer) { try { recognizer.stop(); } catch (e) { /* already stopped */ } }
}

// The mic and the speaker share ONE audio session on iOS, and
// recognizer.stop() is a REQUEST, not an instant release -- teardown
// completes asynchronously and onend fires later. Starting playback
// before that teardown finishes was Dan's own diagnosis: "listening
// while talking, maybe not allowed" -- iOS can leave the session
// configured for recording through the transition, and audio played
// into a still-recording session can come out silent or ducked to
// nothing, with no error anywhere to say so. speak() now WAITS for the
// release rather than racing it.
let _listeningStoppedWaiters = [];
function _drainListeningStopped() {
  const waiters = _listeningStoppedWaiters;
  _listeningStoppedWaiters = [];
  waiters.forEach((resolve) => resolve());
}
function stopListeningAndWaitForRelease() {
  stopListening();
  if (!recognizer) return Promise.resolve();
  return new Promise((resolve) => {
    // A safety net, not the expected path: some browsers/recognizer
    // states never fire onend for a stop() that was already a no-op
    // (already stopped, never started). Real iOS teardown is much
    // faster than this; it only pays the timeout when there was
    // nothing to wait for in the first place.
    const timer = setTimeout(resolve, 400);
    _listeningStoppedWaiters.push(() => { clearTimeout(timer); resolve(); });
  });
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
  const ring = document.getElementById("miclevel");
  if (!mic) return;

  // talk_stt_engine "openai" is an explicit opt-in (see talkSttEngine's
  // comment) -- it never silently activates for a browser that merely
  // lacks SpeechRecognition, so someone who never touched this setting
  // keeps seeing exactly today's behavior either way.
  if (talkSttEngine() === "openai") {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      mic.hidden = true;
      if (ring) ring.hidden = true;
      return;
    }
    mic.hidden = false;
    // No live level meter here: it is driven by SpeechRecognition's own
    // partial-result timing (see startMeter's callers), which push-to-talk
    // cloud capture has no equivalent signal for.
    if (ring) ring.hidden = true;
    initCloudMic(mic);
    return;
  }

  if (!SpeechRecognitionCtor) {
    mic.hidden = true;
    if (ring) ring.hidden = true;
    return;
  }
  mic.hidden = false;
  recognizer = new SpeechRecognitionCtor();
  recognizer.continuous = false;
  recognizer.interimResults = true;
  recognizer.lang = "en-US";

  // Interim and final results both feed the same processTranscript(), which
  // is the single funnel for the wake gate, live caption, and end-word/
  // isFinal submission -- there is no separate "submit" path for voice.
  recognizer.onresult = (event) => {
    lastResultTs = performance.now();
    // Feeds the contention verdict and the no-recognition hint; the
    // meter no longer waits on this (it must calibrate BEFORE speech).
    recognitionProven = true;
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
    sessionLive = text;
    const isFinal = event.results[event.results.length - 1].isFinal;
    processTranscript(isFinal);
    if (isFinal) stopListening();
  };
  recognizer.onerror = () => { listening = false; };
  recognizer.onend = () => {
    _drainListeningStopped();
    // CRITICAL: stop recognition before playing TTS (done in speak()) and
    // only resume after playback ends -- if mode is still on AND we are
    // not currently speaking, restart here too, so a recognizer that ends
    // for any other reason (silence timeout, browser quirk) still keeps
    // conversation mode going instead of going quiet.
    listening = false;
    if (conversationMode && !speaking) startListening();
  };

  // If recognition has produced nothing at all a few seconds after the
  // mic went on, the device has not given us speech recognition -- say so
  // instead of leaving a listening button that will never do anything.
  let provenTimer = null;
  function watchForRecognition() {
    if (provenTimer) clearTimeout(provenTimer);
    provenTimer = setTimeout(() => {
      provenTimer = null;
      if (!conversationMode || recognitionProven) return;
      const hint = document.getElementById("endpointhint");
      if (hint) {
        hint.textContent = "This browser is not returning speech recognition."
          + " Type below instead \u2014 the mic cannot hear you here.";
        hint.hidden = false;
      }
    }, 6000);
  }

  mic.addEventListener("click", () => {
    // iOS will not speak or play audio that was not started by a user
    // gesture, and a reply arrives asynchronously long after this click.
    // Priming both engines HERE -- inside the gesture -- is what makes the
    // later programmatic speak() audible. Silent by construction: an empty
    // utterance and a zero-length resume.
    primeAudioForIOS();
    conversationMode = !conversationMode;
    mic.classList.toggle("on", conversationMode);
    mic.textContent = conversationMode ? "listening…" : "mic";
    if (conversationMode) {
      resetTalkState();
      updateCaptionArmed();
      // Recognition and the meter BOTH start here, and the ordering war
      // between them is settled by evidence, not scheduling. History of
      // this line: starting both stole the mic from recognition on iOS
      // (exclusive microphone -- ring animated, no transcript), so the
      // meter was deferred until recognition's first result. That fix
      // broke every DESKTOP: a meter started mid-speech CALIBRATES ON
      // SPEECH, sets its threshold at 3x the user's own voice, and the
      // VAD never fires again -- transcript on screen, listening
      // forever, nothing sent. Calibration must happen in the pre-speech
      // silence, which means the meter must start at the click.
      //
      // The iOS case is owned by the CONVICTION instead: a meter that
      // can hear voice while recognition produces nothing for two
      // seconds took the microphone, and is stopped for the session --
      // recognition restarts and the stability endpointer takes over.
      // Each platform loses the component it cannot support, on proof.
      startListening();
      if (endpointMode() === "silence" && !meterContended) startMeter();
      watchForRecognition();
    } else {
      recognitionProven = false;
      stopListening();
      stopMeter();
      resetTalkState();
      capUser.textContent = "";
      capUser.classList.remove("armed", "active", "sent");
    }
  });
}

// Cloud STT is push-to-talk, not continuous wake-word listening: one tap
// starts a real MediaRecorder capture, a second tap stops it and uploads
// the clip to /api/stt for transcription. Raw audio has no equivalent to
// SpeechRecognition's interimResults, so there is no live partial
// transcript to drive a wake word, a VAD silence endpoint, or a
// continuous conversation mode the way the browser path has -- building
// real audio-level VAD to replicate that is separate, real work (see
// bench/README.md's ASR section). This is deliberately a smaller, honest
// feature: it promises only "record what you say between two taps,
// transcribe it, send it" -- and unlike SpeechRecognition, it works on
// every browser with a microphone, including ones the Web Speech API
// never supported (Firefox).
let cloudRecorder = null;
let cloudChunks = [];
let cloudStream = null;
let cloudRecording = false;

function initCloudMic(mic) {
  mic.addEventListener("click", async () => {
    primeAudioForIOS();
    if (cloudRecording) { stopCloudRecording(); return; }
    await startCloudRecording(mic);
  });
}

function showEndpointHint(message) {
  const hint = document.getElementById("endpointhint");
  if (hint) { hint.textContent = message; hint.hidden = false; }
}

async function startCloudRecording(mic) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) {
    showEndpointHint("Microphone access was denied or unavailable.");
    return;
  }
  const candidates = ["audio/webm", "audio/mp4", "audio/ogg"];
  const mimeType = window.MediaRecorder && MediaRecorder.isTypeSupported
    ? candidates.find((t) => MediaRecorder.isTypeSupported(t))
    : undefined;
  try {
    cloudRecorder = mimeType ? new MediaRecorder(stream, {mimeType}) : new MediaRecorder(stream);
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    showEndpointHint("This browser cannot record audio for cloud speech recognition.");
    return;
  }
  cloudStream = stream;
  cloudChunks = [];
  cloudRecorder.ondataavailable = (e) => { if (e.data && e.data.size) cloudChunks.push(e.data); };
  cloudRecorder.onstop = () => onCloudRecordingStopped(mic);
  cloudRecorder.start();
  cloudRecording = true;
  mic.classList.add("on");
  mic.textContent = "recording… tap to stop";
  capUser.textContent = "";
  capUser.classList.add("armed");
}

function stopCloudRecording() {
  if (cloudRecorder && cloudRecorder.state !== "inactive") cloudRecorder.stop();
  cloudRecording = false;
}

async function onCloudRecordingStopped(mic) {
  if (cloudStream) { cloudStream.getTracks().forEach((t) => t.stop()); cloudStream = null; }
  mic.classList.remove("on");
  mic.textContent = "transcribing…";
  const blobType = (cloudRecorder && cloudRecorder.mimeType) || "audio/webm";
  const blob = new Blob(cloudChunks, {type: blobType});
  cloudChunks = [];
  if (!blob.size) {
    mic.textContent = "mic";
    return;
  }
  try {
    const res = await fetch("/api/stt", {
      method: "POST", credentials: "same-origin",
      headers: {"content-type": blobType, accept: "application/json"},
      body: blob,
    });
    const data = await res.json().catch(() => ({}));
    mic.textContent = "mic";
    if (!res.ok || data.status !== "ok" || typeof data.text !== "string") {
      showEndpointHint(data.error || "Transcription failed.");
      return;
    }
    const text = data.text.trim();
    if (text) submitTurn(text);
  } catch (e) {
    mic.textContent = "mic";
    showEndpointHint("Transcription request failed.");
  }
}

// One-time unlock, inside a user gesture. Safari on iOS refuses both
// speechSynthesis and HTMLAudioElement.play() outside one, and gives no
// error worth reading when it refuses -- the reply simply never becomes
// sound, which is exactly what an older iPad reported.
let ttsAudioEl = null;
function sharedTtsAudio() {
  if (!ttsAudioEl) ttsAudioEl = new Audio();
  return ttsAudioEl;
}

let audioPrimed = false;
function primeAudioForIOS() {
  if (audioPrimed) return;
  audioPrimed = true;
  try {
    if (window.speechSynthesis) {
      const warm = new SpeechSynthesisUtterance("");
      warm.volume = 0;
      window.speechSynthesis.speak(warm);
      // getVoices() is empty until the engine loads on iOS; asking inside
      // the gesture is what populates it in time for hasLocalVoice().
      window.speechSynthesis.getVoices();
    }
  } catch (e) { /* nothing to unlock */ }
  try {
    // Unlock the ONE audio element every server-TTS reply will reuse: a
    // zero-length silent play inside the gesture is what licenses every
    // later programmatic play() on the same element.
    const player = sharedTtsAudio();
    player.muted = true;
    player.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA=";
    const attempt = player.play();
    if (attempt && attempt.catch) attempt.catch(() => {});
    setTimeout(() => { player.muted = false; }, 150);
  } catch (e) { /* nothing to unlock */ }
  try {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (Ctor) {
      const ctx = new Ctor();
      if (ctx.state === "suspended" && ctx.resume) ctx.resume();
      // Kept open deliberately: closing it re-locks playback on iOS.
      window.__dbbasicTalkUnlockCtx = ctx;
    }
  } catch (e) { /* nothing to unlock */ }
}


async function api(method, path, payload) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  return [res.ok, await res.json()];
}

async function loadPrefs() {
  const res = await fetch(`/collections/shell_preferences/records/${OWNER_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (res.ok) { const body = await res.json(); prefs = body.record || prefs; }
}

// The 10-second silence problem: a tool-using model legitimately takes
// that long, and a page that shows a frozen ellipsis for it reads as
// dead -- "we don't know because after 10 seconds..." is a verbatim
// user report. Two signals, chosen for a voice-first surface: a short
// BLIP the moment the utterance is accepted (the ear knows it fired
// without looking at the screen), and a visible elapsed counter while
// the model works (a counter that is moving is a page that is alive,
// and "thinking... 9s" and "stuck" stop being the same picture).
let thinkingTimer = null;
function startThinking() {
  const started = performance.now();
  capAssistant.textContent = "thinking\\u2026";
  if (thinkingTimer) clearInterval(thinkingTimer);
  thinkingTimer = setInterval(() => {
    const s = Math.round((performance.now() - started) / 1000);
    if (s >= 2) capAssistant.textContent = "thinking\\u2026 " + s + "s";
  }, 1000);
}
function stopThinking() {
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
}
function blip() {
  try {
    const ctx = window.__dbbasicTalkUnlockCtx;
    if (!ctx || ctx.state !== "running") return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.12);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + 0.13);
  } catch (e) { /* a missing blip is not an error */ }
}

// Every path that can produce an utterance -- the voice endpointer
// (VAD, stability backstop, isFinal debounce) and the manual send/Enter
// handler -- funnels here. Nothing serialized them, and a real session
// proved it: "showed me my notes" and "show me my notes" reached the
// server ONE SECOND apart, two provider calls billed for one utterance,
// the reply spoken twice. Cause not fully isolated (a stale recognizer
// instance surviving its own onend restart is the leading suspect), but
// the fix does not need the cause: a turn already in flight must simply
// refuse a second one, for ANY reason it might arrive.
let turnInFlight = false;

async function submitTurn(input) {
  if (turnInFlight) return;   // a second utterance chasing the first is dropped, not queued
  turnInFlight = true;
  capUser.textContent = input;
  capUser.classList.remove("armed", "active");
  capUser.classList.add("sent");
  blip();
  startThinking();
  stopListening();

  let ok, body;
  try {
    // pref(), not raw prefs.tools -- a shell_preferences record written
    // before this field existed in the schema won't have it, and
    // undefined.split() would throw here and silently kill the turn.
    const tools = String(pref("tools", DEFAULT_TOOLS)).split(",").map((t) => t.trim()).filter(Boolean);
    [ok, body] = await api("POST", "/api/ai/chat",
      {message: input,
       model: String(pref("talk_model", "")).trim() || pref("ai_model", DEFAULT_MODEL),
       tools, history: aiHistory.slice(-20),
       session_id: SESSION_ID, source: "talk",
       system: TALK_SYSTEM + " Current local date/time: " + new Date().toString() + "."});
  } finally {
    // Cleared before the reply renders, not after: a fetch that throws
    // must release the gate too, or one failed turn wedges the page shut
    // for the rest of the session.
    turnInFlight = false;
  }

  stopThinking();
  // An empty SUCCESS is the worst reply a voice surface can relay: the
  // round-trip happened, nothing is shown, nothing is spoken, and the
  // user is left staring at a page that looks exactly like a hang. Turn
  // it into words -- one really did come back empty, recorded as out:""
  // on the server.
  const rawReply = ok ? (body.reply || "I came back empty \u2014 ask me again?")
                      : (body.error || "Something went wrong.");
  const replyText = stripViewMarker(rawReply);
  capAssistant.textContent = stripForSpeech(replyText) || replyText;

  if (ok) {
    aiHistory.push({role: "user", content: input});
    aiHistory.push({role: "assistant", content: body.reply});
    // Tool calls are ground truth for what was actually created/updated this
    // turn; the [[view:id]] marker is the model retyping an id and can typo or
    // hallucinate it (pointing the stage at a phantom view). So trust the tool
    // call first, fall back to the marker only for "show an existing view again"
    // turns that touched no records.
    const viewPath = viewPathFromToolCalls(body.tool_calls) || viewPathFromReply(rawReply);
    if (viewPath) renderStageView(viewPath); else renderCard(replyText);
  } else {
    renderCard(replyText);
  }
  speak(replyText);
  // No client-side history write: the SERVER records every AI turn as
  // part of /api/ai/chat itself -- stamped, session-grouped, and immune
  // to this page dying before a fire-and-forget write lands (a real turn
  // was lost exactly that way).
}

// Manual send/Enter always submits whatever is in the voice buffer plus
// whatever is typed, regardless of endpoint protocol or wake-word arming
// -- an explicit click/Enter is its own address signal. pendingText()
// still strips a wake/end word if one happens to be present so a
// half-spoken command doesn't come along for the ride.
document.getElementById("prompt").addEventListener("submit", (event) => {
  event.preventDefault();
  const box = event.target.elements["line"];
  const typed = box.value.trim();
  const voice = conversationMode ? pendingText() : "";
  box.value = "";
  if (finalDebounceTimer) { clearTimeout(finalDebounceTimer); finalDebounceTimer = null; }
  resetTalkState();
  const input = [voice, typed].filter(Boolean).join(" ").trim();
  if (!input) { if (conversationMode) updateCaptionArmed(); return; }
  submitTurn(input);
});
initMic();
loadPrefs();


// === sessions =================================================================
//
// The same fold the shell does over the server-recorded turns, with one
// deliberate difference: Talk NEVER auto-resumes. A voice page that
// silently reloads twenty old turns into model context changes what the
// next answer means without the user hearing anything change -- resume
// here is a hand action on the picker, always. (The shell auto-resumes
// inside a 30-minute window; eyes can see a restored transcript, ears
// cannot.)
let sessions = [];

function foldSessions(rows) {
  const by = {};
  for (const row of rows) {
    const sid = row.session_id || "";
    if (!sid) continue;
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

async function loadSessions() {
  const res = await fetch("/collections/shell_commands/records?limit=1000",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  sessions = foldSessions((await res.json()).records || []);
  renderSessionPicker();

  const sel = document.getElementById("sessionpick");
  if (sel) sel.addEventListener("change", () => {
    if (sel.value === "__new") {
      SESSION_ID = freshSessionId();
      aiHistory = [];
      capAssistant.textContent = "new session";
      renderSessionPicker();
      return;
    }
    const s = sessions.find((x) => x.id === sel.value);
    if (!s) return;
    SESSION_ID = s.id;
    aiHistory = [];
    for (const row of s.rows.slice(-10)) {
      if (row.kind === "ai" && row.output) {
        aiHistory.push({role: "user", content: row.input});
        aiHistory.push({role: "assistant", content: row.output});
      }
    }
    // Say what just happened, on the surface the user is actually using:
    // the context changed, and a voice page must not do that silently.
    capAssistant.textContent = "resumed \u201c" + s.title + "\u201d ("
      + s.count + " turns)";
    renderSessionPicker();
  });
}
loadSessions();

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
<div id="endpointhint" class="endpointhint" hidden></div>
<div class="controls">
<div class="micwrap">
<button type="button" id="mic" hidden aria-label="toggle conversation mode">mic</button>
<span id="miclevel" class="miclevel" hidden></span>
</div>
<form id="prompt" autocomplete="off">
<input name="line" placeholder="or type..." autofocus>
<button type="submit" class="btn primary" aria-label="send">send</button>
</form>
<select id="sessionpick" aria-label="conversation">
<option value="__new">&#10133; new session</option></select>
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
