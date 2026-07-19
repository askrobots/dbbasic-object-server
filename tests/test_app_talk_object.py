"""Tests for packages/app-shell's Talk surface (site_talk / /talk).

Talk is a second projection of the same conversation as the shell: same
POST /api/ai/chat, same prefs/model/tools, same shell_commands history --
just staged as voice instead of transcript. These tests mirror the
package/renderer testing conventions used in tests/test_app_views_package.py
and add coverage specific to Talk's conversation-mode mic loop and the
places shell.py was patched to stay in sync with it.
"""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import object_execution
import object_packages
import object_permissions
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_SHELL_DIR = PACKAGES_ROOT / "app-shell"
TALK_PATH = APP_SHELL_DIR / "objects" / "site" / "talk.py"
TALK_SOURCE = TALK_PATH.read_text()
SHELL_SOURCE = (APP_SHELL_DIR / "objects" / "site" / "shell.py").read_text()
SHELL_PREFS_SCHEMA = json.loads((APP_SHELL_DIR / "schemas" / "shell_preferences.json").read_text())


def _render_talk_html(user_id="u1"):
    """Import talk.py fresh and render its GET() body for a signed-in user,
    the same way the object runtime would -- used by tests that need the
    *actual evaluated* inline <script>, not just the Python source text."""
    spec = importlib.util.spec_from_file_location("site_talk_under_test", TALK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _NullLogger:
        def info(self, *args, **kwargs):
            pass

    module._logger = _NullLogger()
    return module.GET({"_identity": {"user_id": user_id}})["body"]


def _extract_inline_script(html):
    """Pull the content of the one bare `<script>...</script>` block (the
    page's own inline script, as opposed to the `<script src=...>` tags)."""
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline <script> block found in rendered Talk HTML"
    return blocks[0]


def test_get_package_lists_site_talk_alongside_site_shell():
    package = object_packages.get_package("app-shell", root=PACKAGES_ROOT)

    assert package["id"] == "app-shell"
    assert {obj["id"] for obj in package["objects"]} == {"site_shell", "site_talk"}
    ids_to_paths = {obj["id"]: obj["path"] for obj in package["objects"]}
    assert ids_to_paths["site_talk"] == "objects/site/talk.py"


def test_dry_run_app_shell_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-shell",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_install_app_shell_package_writes_talk_object(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-shell",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert (object_root / "site" / "talk.py").is_file()
    # Convention routing (/talk -> site_talk, no site_routes record needed)
    # is exactly what lets a page-object addition create its own URL, the
    # same way /shell -> site_shell already works.
    import object_site_routes

    assert object_site_routes.convention_object_id("/talk") == "site_talk"
    assert object_site_routes.convention_object_id("/shell") == "site_shell"


def _app_shell_policy():
    payload = json.loads((APP_SHELL_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_talk_page_execute_is_public_like_shell():
    policy = _app_shell_policy()

    for object_id in ("site_shell", "site_talk"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True, object_id


def test_talk_object_serves_sign_in_prompt_when_anonymous(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package(
        "app-shell", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_talk", payload={"_identity": {}}),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert "Sign in" in body
    assert "/login?next=/talk" in body


def test_talk_object_serves_stage_scaffolding_when_signed_in(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package(
        "app-shell", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_talk", payload={"_identity": {"user_id": "u1"}}
        ),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert result.result["content_type"] == "text/html; charset=utf-8"
    # Stage/caption/mic scaffolding.
    assert 'id="stage"' in body
    assert 'id="capUser"' in body and 'id="capAssistant"' in body
    assert 'id="mic"' in body
    assert 'id="prompt"' in body
    assert 'href="/shell"' in body
    # Same brain: it is the /api/ai/chat call, not a new endpoint.
    assert "/api/ai/chat" in body
    assert "/collections/shell_commands/records" in body
    # The talk-mode system prompt marker, plus the base capabilities text
    # copied verbatim from shell.py (proves the two are still in sync).
    assert "You are in voice mode" in body
    assert "NEVER read ids, urls, uuids, or paths aloud" in body
    assert "MATERIALIZE PAGES" in body
    assert "u1" in body


def test_talk_source_pauses_recognition_around_tts_playback():
    """The core anti-feedback-loop requirement: the mic must not hear the
    machine talk. Assert the shape directly on source since it's an
    interaction between async callbacks that's easiest to prove by
    inspecting the wiring: speak() marks `speaking` and stops listening
    before playback, both playback-completion paths clear it and resume,
    and onend / startListening both respect the `speaking` flag.
    """
    assert "speaking = true" in TALK_SOURCE
    assert "stopListening();" in TALK_SOURCE

    speak_fn = re.search(r"async function speak\(text\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert speak_fn, "speak() not found in talk.py"
    body = speak_fn.group(1)
    # speaking flips true and recognition stops before either playback path.
    assert body.index("speaking = true") < body.index('await currentAudio.play()')
    assert body.index("stopListening();") < body.index('await currentAudio.play()')
    # Both the server-TTS path and the speechSynthesis fallback clear
    # `speaking` and resume listening once playback actually ends.
    assert "speaking = false; resumeListeningIfNeeded();" in body or (
        "speaking = false;" in body and "resumeListeningIfNeeded();" in body
    )
    assert body.count("resumeListeningIfNeeded()") >= 2

    # startListening() itself refuses to start while speaking, and the
    # recognizer's own onend only auto-restarts when not speaking.
    assert "if (!recognizer || listening || speaking) return;" in TALK_SOURCE
    assert "if (conversationMode && !speaking) startListening();" in TALK_SOURCE


def test_talk_source_conversation_mode_auto_restarts_on_end():
    assert "conversationMode" in TALK_SOURCE
    assert "recognizer.onend = () => {" in TALK_SOURCE
    assert "resumeListeningIfNeeded" in TALK_SOURCE


def test_talk_stripforspeech_strips_urls_and_view_paths():
    fn = re.search(r"function stripForSpeech\(text\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert fn, "stripForSpeech() not found in talk.py"
    body = fn.group(1)
    assert "https?" in body
    assert "/views" in body


def test_talk_base_capabilities_match_shell_verbatim():
    """The views MATERIALIZE PAGES block must be copied verbatim into
    talk.py so the two prompts stay in sync, per the module docstring."""
    marker = "You can also MATERIALIZE PAGES"
    assert marker in TALK_SOURCE
    assert marker in SHELL_SOURCE

    talk_block = TALK_SOURCE[TALK_SOURCE.index(marker):TALK_SOURCE.index(marker) + 900]
    shell_block = SHELL_SOURCE[SHELL_SOURCE.index(marker):SHELL_SOURCE.index(marker) + 900]
    # Normalize away Python vs. JS string-literal concatenation syntax so
    # this compares the actual prose, not the quoting style.
    def normalize(s):
        s = re.sub(r'["\'+]|\\+', "", s)
        return re.sub(r"\s+", " ", s).strip()

    assert normalize(talk_block)[:400] == normalize(shell_block)[:400]


def test_talk_falls_back_to_tool_calls_for_the_view_path():
    assert "viewPathFromToolCalls" in TALK_SOURCE
    assert "create_record" in TALK_SOURCE
    assert "update_record" in TALK_SOURCE
    assert 'args.collection !== "views"' in TALK_SOURCE


# --- shell.py patches (linkify + embed, extended stripForSpeech) ---------


def test_shell_finish_linkifies_and_embeds_views_paths():
    assert "function linkifyViews(html)" in SHELL_SOURCE
    assert "VIEWS_PATH_RE" in SHELL_SOURCE
    assert "viewembed" in SHELL_SOURCE
    assert 'target="_blank" rel="noopener">open ↗' in SHELL_SOURCE

    finish_fn = re.search(r"function finish\(out, text,.*?\n\}\n", SHELL_SOURCE, re.S)
    assert finish_fn, "finish() not found in shell.py"
    body = finish_fn.group(0)
    assert "linkifyViews(" in body
    assert "insertAdjacentElement(\"afterend\", embed)" in body


def test_shell_stripforspeech_strips_urls_and_view_paths():
    fn = re.search(r"function stripForSpeech\(text\) \{(.*?)\n\}\n", SHELL_SOURCE, re.S)
    assert fn, "stripForSpeech() not found in shell.py"
    body = fn.group(1)
    assert "https?" in body
    assert "/views" in body


# --- bug fixes: placeholder escaping + the dead submit path --------------
#
# The placeholder bug was a context mismatch: `…` is a JS/Python
# unicode escape, but the placeholder lives in plain HTML markup (the
# `body` string), not inside the `<script>` tag -- so the browser never
# gets a chance to interpret it and shows the six literal characters
# `…` instead of an ellipsis. `node --check` on the *actual evaluated*
# script is the strongest guard against this whole class of Python-string/
# JS-string escaping fault recurring; it is skipped (not failed) when node
# isn't on PATH, falling back to a source assertion that the placeholder
# is clean.


def test_talk_placeholder_has_no_backslash_escape():
    assert 'placeholder="or type...' in TALK_SOURCE
    assert "or type\\u2026" not in TALK_SOURCE
    assert "or type\\\\u2026" not in TALK_SOURCE

    html = _render_talk_html()
    assert 'placeholder="or type...' in html
    input_tag = re.search(r'<input name="line"[^>]*>', html)
    assert input_tag, "line input not found in rendered Talk HTML"
    assert "\\u2026" not in input_tag.group(0)
    assert "\\" not in input_tag.group(0)


def test_talk_evaluated_script_passes_node_check(tmp_path):
    html = _render_talk_html()
    script = _extract_inline_script(html)

    node = shutil.which("node")
    if not node:
        # No node on PATH in this environment -- fall back to the source
        # assertion above, which is the part node --check would have caught
        # for this specific bug class anyway.
        assert 'placeholder="or type...' in html
        return

    script_path = tmp_path / "talk_inline.js"
    script_path.write_text(script)
    result = subprocess.run(
        [node, "--check", str(script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


def test_talk_submit_handler_reads_line_input_and_calls_submitturn():
    """The dead-send-button symptom: verify the actual wiring end to end --
    the form's submit listener is attached (not just present as dead code),
    it prevents default navigation, reads the named `line` input, and always
    calls submitTurn() so voice and typed input share one code path."""
    handler = re.search(
        r'document\.getElementById\("prompt"\)\.addEventListener\("submit", \(event\) => \{(.*?)\n\}\);',
        TALK_SOURCE,
        re.S,
    )
    assert handler, "submit listener not found in talk.py"
    body = handler.group(1)
    assert "event.preventDefault();" in body
    assert 'event.target.elements["line"]' in body
    assert "submitTurn(input)" in body
    # Manual submit folds in the voice buffer regardless of protocol state.
    assert "pendingText()" in body
    assert "resetTalkState();" in body


def test_talk_recognition_final_results_feed_the_same_submit_function():
    onresult = re.search(r"recognizer\.onresult = \(event\) => \{(.*?)\n  \};", TALK_SOURCE, re.S)
    assert onresult, "recognizer.onresult not found in talk.py"
    body = onresult.group(1)
    assert "processTranscript(isFinal)" in body
    # processTranscript() is the one funnel that can call submitTurn (via
    # finalizeVoiceSubmit) -- both the mic path and the form path resolve
    # to the same submitTurn() used for /api/ai/chat.
    assert "async function submitTurn(input)" in TALK_SOURCE
    assert "finalizeVoiceSubmit" in TALK_SOURCE
    finalize = re.search(r"function finalizeVoiceSubmit\(text\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert finalize
    assert "submitTurn(clean)" in finalize.group(1)


def test_talk_prefs_reads_go_through_pref_helper_not_raw_field_access():
    """A shell_preferences record written before a field existed in the
    schema won't have that key; `prefs.tools.split(",")` on undefined would
    throw and silently kill the turn (send button/voice submit both call
    submitTurn). pref() with a fallback closes that hole."""
    submit_turn = re.search(r"async function submitTurn\(input\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert submit_turn, "submitTurn() not found in talk.py"
    body = submit_turn.group(1)
    assert "prefs.tools.split" not in body
    assert 'pref("tools", DEFAULT_TOOLS)' in body
    assert 'pref("ai_model", DEFAULT_MODEL)' in body


# --- radio protocol: wake word / end word / VAD endpointing --------------


def test_talk_schema_bumped_with_radio_protocol_fields():
    assert SHELL_PREFS_SCHEMA["version"] == 3

    fields = {f["name"]: f for f in SHELL_PREFS_SCHEMA["fields"]}
    assert fields["talk_wake_word"]["default"] == "computer"
    assert fields["talk_end_word"]["default"] == "over"
    assert fields["talk_endpoint"]["type"] == "enum"
    assert fields["talk_endpoint"]["default"] == "silence"
    assert set(fields["talk_endpoint"]["enum"]) == {"silence", "word", "manual"}
    assert fields["talk_silence_ms"]["default"] == "1400"

    # The defaults are mirrored client-side so a first-ever load (before
    # loadPrefs() resolves) already behaves per spec.
    assert 'talk_wake_word: "computer"' in TALK_SOURCE
    assert 'talk_end_word: "over"' in TALK_SOURCE
    assert 'talk_endpoint: "silence"' in TALK_SOURCE
    assert 'talk_silence_ms: "1400"' in TALK_SOURCE


def test_talk_wake_word_gating_discards_until_heard():
    process = re.search(r"function processTranscript\(isFinal\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert process, "processTranscript() not found in talk.py"
    body = process.group(1)
    assert "if (armed) {" in body
    assert "findWakeSplit(combinedRaw, w)" in body
    # Nothing found yet -> stays armed and shows the dim hint, does not fold
    # into the active buffer.
    assert "if (split === null) {" in body
    assert "updateCaptionArmed();" in body

    armed_caption = re.search(r"function updateCaptionArmed\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert armed_caption
    assert "to address me" in armed_caption.group(1)
    assert '.add("armed")' in armed_caption.group(1)


def test_talk_wake_word_match_is_case_insensitive_word_boundary():
    find_split = re.search(r"function findWakeSplit\(text, word\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert find_split, "findWakeSplit() not found in talk.py"
    body = find_split.group(1)
    assert ".toLowerCase()" in body
    assert "tokenize(text)" in body
    assert "wordKey(tokens[i]) === target" in body  # whole-token compare, not substring


def test_talk_end_word_tail_match_and_strip():
    tail_match = re.search(r"function tailMatchesEndWord\(text, word\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert tail_match, "tailMatchesEndWord() not found in talk.py"
    body = tail_match.group(1)
    assert ".toLowerCase()" in body
    assert "tokens[tokens.length - 1]" in body  # only the tail token
    assert "tokens.slice(0, -1).join" in body  # strips it from the remainder

    word_key = re.search(r"function wordKey\(token\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert word_key, "wordKey() not found in talk.py"
    assert "[.,!?;:]" in word_key.group(1)  # tolerant of trailing punctuation

    process = re.search(r"function processTranscript\(isFinal\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert process
    body = process.group(1)
    assert 'endpoint === "word" || endpoint === "silence"' in body
    assert "tailMatchesEndWord(active, ew)" in body
    assert "finalizeVoiceSubmit(stripped)" in body


def test_talk_empty_wake_word_captures_everything():
    process = re.search(r"function processTranscript\(isFinal\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert process
    body = process.group(1)
    assert "armed = false; // empty wake word: capture everything while the mic is on" in body


def test_talk_empty_end_word_falls_back_to_debounced_isfinal():
    process = re.search(r"function processTranscript\(isFinal\) \{(.*?)\n\}\n", TALK_SOURCE, re.S)
    assert process
    body = process.group(1)
    assert 'endpoint === "word" && !ew' in body
    assert "setTimeout(() => {" in body
    assert "}, 800);" in body
    assert "finalizeVoiceSubmit(buffer);" in body


def test_talk_buffer_cleared_and_rearmed_after_any_submit():
    finalize = re.search(r"function finalizeVoiceSubmit\(text\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert finalize, "finalizeVoiceSubmit() not found in talk.py"
    body = finalize.group(1)
    assert "resetTalkState();" in body

    reset = re.search(r"function resetTalkState\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert reset, "resetTalkState() not found in talk.py"
    body = reset.group(1)
    assert 'buffer = "";' in body
    assert 'sessionLive = "";' in body
    assert "armed = true;" in body
    assert "clearTimeout(finalDebounceTimer);" in body

    # Manual submit also resets before/around submitTurn, per "after any
    # submit: clear the buffer and return to armed state".
    handler = re.search(
        r'document\.getElementById\("prompt"\)\.addEventListener\("submit", \(event\) => \{(.*?)\n\}\);',
        TALK_SOURCE,
        re.S,
    )
    assert handler
    assert "resetTalkState();" in handler.group(1)


def test_talk_manual_submit_bypasses_arming_and_joins_voice_plus_typed():
    handler = re.search(
        r'document\.getElementById\("prompt"\)\.addEventListener\("submit", \(event\) => \{(.*?)\n\}\);',
        TALK_SOURCE,
        re.S,
    )
    assert handler
    body = handler.group(1)
    assert "const typed = box.value.trim();" in body
    assert "const voice = conversationMode ? pendingText() : " in body
    assert "[voice, typed].filter(Boolean).join" in body


def test_talk_tts_pause_keeps_buffer_from_accumulating_machine_speech():
    """Recognition is already stopped for the whole speaking span (asserted
    elsewhere); this just confirms the VAD path independently refuses to run
    while speaking, since its getUserMedia stream is not gated by the
    recognizer's own stop/start."""
    vad = re.search(r"function evaluateVAD\(rms, now\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert vad, "evaluateVAD() not found in talk.py"
    assert "if (speaking || !conversationMode) {" in vad.group(1)

    meter = re.search(r"function updateMeterVisual\(rms\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert meter, "updateMeterVisual() not found in talk.py"
    assert "speaking ? 0 :" in meter.group(1)  # ring idles during playback


# --- mic level metering + VAD silence endpointing -------------------------


def test_talk_meter_uses_getusermedia_and_analysernode():
    start_meter = re.search(r"async function startMeter\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert start_meter, "startMeter() not found in talk.py"
    body = start_meter.group(1)
    assert "navigator.mediaDevices.getUserMedia({audio: true})" in body
    assert "createMediaStreamSource(micStream)" in body
    assert "createAnalyser()" in body
    assert "requestAnimationFrame(frame)" in body


def test_talk_noise_floor_calibration_then_threshold():
    start_meter = re.search(r"async function startMeter\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert start_meter
    body = start_meter.group(1)
    assert "CALIBRATION_MS" in body
    assert "calibrationSamples.push(rms)" in body
    assert "noiseFloor = avg;" in body
    assert "speechThreshold = Math.max(avg * 3, MIN_SPEECH_THRESHOLD);" in body
    assert "const CALIBRATION_MS = 800;" in TALK_SOURCE


def test_talk_silence_timer_submits_buffer_respecting_wake_gate():
    vad = re.search(r"function evaluateVAD\(rms, now\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert vad
    body = vad.group(1)
    assert "utteranceStarted" in body
    assert "bufferHasContent()" in body
    assert "now - silenceStartTs < silenceMs()) return;" in body
    assert "if (armed) {" in body
    assert "resetTalkState();" in body  # discard, not submit, if never woken
    assert "finalizeVoiceSubmit(pendingText());" in body  # submit once woken


def test_talk_meter_stream_released_when_conversation_mode_turns_off():
    stop_meter = re.search(r"function stopMeter\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert stop_meter, "stopMeter() not found in talk.py"
    body = stop_meter.group(1)
    assert "cancelAnimationFrame(meterRafId)" in body
    assert "audioCtx.close()" in body
    assert "micStream.getTracks().forEach((t) => t.stop())" in body
    assert 'ring.hidden = true;' in body  # visibly dead -- the privacy indicator

    mic_click = re.search(r'mic\.addEventListener\("click", \(\) => \{(.*?)\n  \}\);', TALK_SOURCE, re.S)
    assert mic_click, "mic click handler not found in talk.py"
    body = mic_click.group(1)
    assert "startMeter();" in body
    assert "stopMeter();" in body


def test_talk_endpoint_mode_helper_validates_and_defaults():
    fn = re.search(r"function endpointMode\(\) \{(.*?)\n\}", TALK_SOURCE, re.S)
    assert fn, "endpointMode() not found in talk.py"
    body = fn.group(1)
    assert '"word"' in body and '"manual"' in body and '"silence"' in body
    assert "DEFAULT_ENDPOINT" in body


def test_talk_helper_functions_behave_correctly_under_node():
    """Behavioral (not just source-string) coverage for the pure word-match
    helpers, run against the actual evaluated script when node is available."""
    node = shutil.which("node")
    if not node:
        return

    html = _render_talk_html()
    script = _extract_inline_script(html)
    start = script.index("function tokenize(text)")
    end = script.index("function resetTalkState()")
    helpers = script[start:end]

    probe = (
        helpers
        + "\nconsole.log(JSON.stringify({"
        + 'wake: findWakeSplit("hey computer turn on the lights", "computer"),'
        + 'noWake: findWakeSplit("turn on the lights", "computer"),'
        + 'wakeCasePunct: findWakeSplit("Computer, whats the time", "computer"),'
        + 'endStrip: tailMatchesEndWord("turn on the lights over", "over"),'
        + 'endStripPunct: tailMatchesEndWord("turn on the lights, over.", "over"),'
        + 'noEnd: tailMatchesEndWord("turn on the lights", "over"),'
        + "}));\n"
    )
    result = subprocess.run([node, "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, f"node probe failed:\n{result.stderr}"
    out = json.loads(result.stdout)
    assert out["wake"] == "turn on the lights"
    assert out["noWake"] is None
    assert out["wakeCasePunct"] == "whats the time"
    assert out["endStrip"] == "turn on the lights"
    assert out["endStripPunct"] == "turn on the lights,"
    assert out["noEnd"] is None
