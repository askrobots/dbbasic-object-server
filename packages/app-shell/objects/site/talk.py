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
    "Use the list_collections tool to discover what collections exist before "
    "saying something is unavailable. "
    "To show one specific record on screen, create a view whose blocks contain a "
    "detail block for it. Never claim something is on screen unless you created "
    "or updated a views record in this same turn. "
    "Whenever the screen should show a view -- newly created OR one that already "
    "exists -- end your reply with the marker [[view:<record id>]] alone on the "
    "last line. The marker is machine-read; it is never displayed or spoken, so "
    "it does not violate the no-ids-aloud rule."
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
             tools: "global_search,list_collections,list_records,get_record,create_record,update_record",
             talk_wake_word: "computer", talk_end_word: "over",
             talk_endpoint: "silence", talk_silence_ms: "1400"};
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
const DEFAULT_TOOLS = "global_search,list_records,get_record,create_record,update_record";
const DEFAULT_MODEL = "anthropic:claude-haiku-4-5";

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
function findWakeSplit(text, word) {
  const target = word.trim().toLowerCase();
  if (!target) return null;
  const tokens = tokenize(text);
  for (let i = 0; i < tokens.length; i++) {
    if (wordKey(tokens[i]) === target) return tokens.slice(i + 1).join(" ");
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
  const clean = String(text ?? "").trim();
  resetTalkState();
  if (!clean) { if (conversationMode) updateCaptionArmed(); return; }
  submitTurn(clean);
}

// Called on every recognizer result (interim and final). Handles the wake
// gate, live caption, end-word override, and -- on a final result -- folds
// the session transcript into `buffer` and (endpoint "word" with no end
// word configured) arms the isFinal-debounce fallback.
function processTranscript(isFinal) {
  const combinedRaw = ((buffer ? buffer + " " : "") + sessionLive).trim();
  const w = wakeWord();

  if (armed) {
    if (w) {
      const split = findWakeSplit(combinedRaw, w);
      if (split === null) {
        updateCaptionArmed();
        if (isFinal) sessionLive = "";
        return;
      }
      armed = false;
      buffer = "";
      sessionLive = split;
    } else {
      armed = false; // empty wake word: capture everything while the mic is on
    }
  }

  const active = ((buffer ? buffer + " " : "") + sessionLive).trim();
  updateCaptionActive(active);

  const endpoint = endpointMode();
  const ew = endWord();
  if (ew && (endpoint === "word" || endpoint === "silence")) {
    const stripped = tailMatchesEndWord(active, ew);
    if (stripped !== null) { finalizeVoiceSubmit(stripped); return; }
  }

  if (isFinal) {
    buffer = active;
    sessionLive = "";
    if (endpoint === "word" && !ew) {
      // No end word configured for word mode: fall back to the original
      // isFinal-triggered submit, debounced so a recognizer that fires
      // several quick finals in a row (common near silence) only submits
      // once.
      if (finalDebounceTimer) clearTimeout(finalDebounceTimer);
      finalDebounceTimer = setTimeout(() => {
        finalDebounceTimer = null;
        finalizeVoiceSubmit(buffer);
      }, 800);
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
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) { return; }
  if (!conversationMode) { stream.getTracks().forEach((t) => t.stop()); return; }
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) { stream.getTracks().forEach((t) => t.stop()); return; }
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
  if (!SpeechRecognitionCtor || !mic) {
    if (mic) mic.hidden = true;
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
    let text = "";
    for (let i = 0; i < event.results.length; i++) text += event.results[i][0].transcript;
    sessionLive = text;
    const isFinal = event.results[event.results.length - 1].isFinal;
    processTranscript(isFinal);
    if (isFinal) stopListening();
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
    mic.textContent = conversationMode ? "listening…" : "mic";
    if (conversationMode) {
      resetTalkState();
      updateCaptionArmed();
      startListening();
      startMeter();
    } else {
      stopListening();
      stopMeter();
      resetTalkState();
      capUser.textContent = "";
      capUser.classList.remove("armed", "active", "sent");
    }
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
  capUser.classList.remove("armed", "active");
  capUser.classList.add("sent");
  capAssistant.textContent = "\\u2026";
  stopListening();

  // pref(), not raw prefs.tools -- a shell_preferences record written
  // before this field existed in the schema won't have it, and
  // undefined.split() would throw here and silently kill the turn.
  const tools = String(pref("tools", DEFAULT_TOOLS)).split(",").map((t) => t.trim()).filter(Boolean);
  const [ok, body] = await api("POST", "/api/ai/chat",
    {message: input, model: pref("ai_model", DEFAULT_MODEL), tools, history: aiHistory.slice(-20),
     system: TALK_SYSTEM + " Current local date/time: " + new Date().toString() + "."});

  const rawReply = ok ? body.reply : (body.error || "Something went wrong.");
  const replyText = stripViewMarker(rawReply);
  capAssistant.textContent = stripForSpeech(replyText) || replyText;

  if (ok) {
    aiHistory.push({role: "user", content: input});
    aiHistory.push({role: "assistant", content: body.reply});
    const viewPath = viewPathFromReply(rawReply) || viewPathFromToolCalls(body.tool_calls);
    if (viewPath) renderStageView(viewPath); else renderCard(replyText);
  } else {
    renderCard(replyText);
  }
  speak(replyText);
  record(input, replyText, "ai");
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
<div class="micwrap">
<button type="button" id="mic" hidden aria-label="toggle conversation mode">mic</button>
<span id="miclevel" class="miclevel" hidden></span>
</div>
<form id="prompt" autocomplete="off">
<input name="line" placeholder="or type..." autofocus>
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
