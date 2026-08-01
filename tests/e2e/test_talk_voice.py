"""The talk page, actually run: WebKit, fake voice, intercepted network.

Every /talk regression this week shipped past a green suite, because the
suite proved the page's source CONTAINS the code, never that the code
RUNS. A source-grep test cannot see a runtime error kill the script, a
function called before it exists, or a submit that never fires -- and
each of those reached a real iPad in turn. "you need to better figure
out how to test the various parts and actually test it" is a verbatim
user instruction, and this file is the answer.

The harness: WebKit (the iPad's actual engine), the page's real rendered
HTML+JS, a fake webkitSpeechRecognition the test drives line by line, a
getUserMedia that REJECTS (the exclusive-microphone case every fix
orbited), and every network call intercepted -- prefs, history, chat,
TTS. Hermetic: no droplet, no provider, no key, no sound card.

Lives in the e2e lane beside test_generated_ui.py, but deliberately does
NOT use its real-server fixture: the questions here are about the PAGE's
runtime behaviour (does the gate strip, does the submit fire, does the
unlocked element play), and intercepting the network makes each answer
exact -- the chat payload asserted verbatim, the empty-reply case forced
at will -- where a real provider would make them flaky and cost money.
The uvicorn lane covers the server half; between them the seam is the
HTTP contract, which tests/test_ai_turn_recording.py pins.
"""

import importlib.util
import json
import pathlib

import pytest

pytestmark = pytest.mark.e2e

REPO = pathlib.Path(__file__).resolve().parents[2]
TALK = REPO / "packages" / "app-shell" / "objects" / "site" / "talk.py"

# A valid, near-empty WAV for the TTS route.
SILENT_WAV = (b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
              b'"V\x00\x00D\xac\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00')

FAKE_VOICE = """
// A SpeechRecognition the TEST drives. The page must not be able to tell
// the difference, and the test must see everything: instances register on
// window.__recos, and __speak(text, isFinal) feeds the newest one.
window.__recos = [];
window.__speak = (text, isFinal) => {
  const r = window.__recos[window.__recos.length - 1];
  if (!r || !r.onresult) return;
  r.onresult({results: [{0: {transcript: text}, isFinal: !!isFinal,
                         length: 1}], resultIndex: 0,
              // the page iterates event.results
              });
};
window.__endReco = () => {
  const r = window.__recos[window.__recos.length - 1];
  if (r && r.onend) r.onend();
};
class FakeRecognition {
  constructor() { window.__recos.push(this); this.started = 0; }
  start() { this.started++; window.__recoStarts = (window.__recoStarts || 0) + 1; }
  stop() { if (this.onend) setTimeout(() => this.onend(), 0); }
}
window.SpeechRecognition = FakeRecognition;
window.webkitSpeechRecognition = FakeRecognition;

// The iPad case: the microphone is exclusive and the meter's second
// stream is refused.
if (navigator.mediaDevices) {
  navigator.mediaDevices.getUserMedia = () => Promise.reject(new Error("NotAllowedError"));
}

// Spy on media playback -- a headless browser has no speaker, so the
// PROOF of speech is that play() was called on the unlocked element.
window.__plays = [];
const realPlay = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () {
  window.__plays.push(this.src ? this.src.slice(0, 40) : "(no src)");
  const p = realPlay.apply(this);
  if (p && p.catch) p.catch(() => {});
  // A spied play never really plays, so it must really END -- otherwise
  // the page's `speaking` flag sticks and blocks everything after the
  // first reply, which is exactly the stuck state the page now guards.
  setTimeout(() => this.dispatchEvent(new Event("ended")), 40);
  return Promise.resolve();
};
// speechSynthesis exists headless but must be observable too.
window.__spoken = [];
if (window.speechSynthesis) {
  const realSpeak = window.speechSynthesis.speak.bind(window.speechSynthesis);
  window.speechSynthesis.speak = (u) => {
    window.__spoken.push(u.text);
    setTimeout(() => u.dispatchEvent(new Event("end")), 20);
  };
}
"""


def render_talk_html():
    spec = importlib.util.spec_from_file_location("talk_under_test", TALK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Null:
        def info(self, *a, **k): pass
    module._logger = _Null()
    return module.GET({"_identity": {"user_id": "dan"}})["body"]


# FUNCTION-scoped, matching the house e2e convention (conftest.py's own
# `page` fixture) and not incidentally: a module-scoped page was tried
# first here and it leaked. A timer armed near the end of one test
# (the stability endpointer, ~1.4s) was still pending when that test
# returned, fired mid-WAY THROUGH THE NEXT TEST, and submitted a stale
# utterance that made an unrelated assertion fail in a way that looked
# exactly like the guard it was supposed to be proving. One page per
# test is slightly slower and is the only way this suite's own timers
# cannot contaminate each other -- the failure mode this harness exists
# to catch, caught in itself.
@pytest.fixture(scope="module")
def talk_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        yield browser
        browser.close()


@pytest.fixture
def talk_page(talk_browser):
    html = render_talk_html()
    ctx = talk_browser.new_context(viewport={"width": 810, "height": 1080})
    page = ctx.new_page()
    page.add_init_script(FAKE_VOICE)

    state = {"chat_calls": [], "reply": "Here are your notes.",
             "errors": []}
    page.on("pageerror", lambda e: state["errors"].append(str(e)))

    def route_all(route):
        url = route.request.url
        if url.endswith("/talk") or "/talk?" in url:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        elif "/api/ai/chat" in url:
            # NEVER time.sleep() here to simulate network latency: a
            # sync-API route handler runs on Playwright's one shared
            # dispatch thread, and blocking it stalls every OTHER
            # in-flight call too -- including whatever a concurrency
            # test fires "shortly after". Race conditions in this
            # harness are proven by firing calls in the SAME JS tick
            # (see test_a_turn_in_flight_blocks_a_second_submit), never
            # by an artificial server-side delay.
            state["chat_calls"].append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"status": "ok", "reply": state["reply"],
                                           "tool_calls": []}))
        elif "/api/tts" in url:
            route.fulfill(status=200, content_type="audio/wav", body=SILENT_WAV)
        elif "shell_preferences" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"record": {"id": "dan", "talk_tts": "server"}}))
        elif "shell_commands" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"records": []}))
        else:
            route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/*", route_all)
    page.goto("http://talk.test/talk", wait_until="networkidle")
    yield page, state
    ctx.close()


def fresh_turn(page, state):
    state["chat_calls"].clear()
    state["errors"].clear()
    page.evaluate("""() => {
      window.__plays = []; window.__spoken = [];
      const mic = document.getElementById('mic');
      // ensure conversation mode is ON exactly once for this turn
      if (!mic.classList.contains('on')) mic.click();
    }""")
    page.wait_for_timeout(120)


def say(page, text, *, final=True):
    page.evaluate("([t, f]) => window.__speak(t, f)", [text, final])


def test_the_page_runs_without_a_single_error(talk_page):
    page, state = talk_page
    assert state["errors"] == []
    assert page.evaluate("() => typeof submitTurn") == "function"


def test_the_full_voice_loop_speaks_the_reply(talk_page):
    """Wake word -> command -> auto-submit -> reply caption -> SERVER TTS
    played through the unlocked element. The whole pipeline, in a real
    WebKit, with the exclusive-microphone case active."""
    page, state = talk_page
    fresh_turn(page, state)

    say(page, "computer show me my notes", final=True)
    page.wait_for_timeout(1400)   # isFinal debounce (800ms) + settle

    assert len(state["chat_calls"]) == 1, state["errors"]
    sent = state["chat_calls"][0]
    assert sent["message"] == "show me my notes"     # wake word stripped
    assert sent["source"] == "talk"
    assert sent["session_id"]

    caption = page.text_content("#capAssistant")
    assert "Here are your notes." in caption

    plays = page.evaluate("() => window.__plays")
    assert plays, "server TTS was fetched but never played"
    assert state["errors"] == []


def test_the_bare_wake_word_never_reaches_the_api(talk_page):
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "computer", final=True)
    page.wait_for_timeout(1200)
    assert state["chat_calls"] == []


def test_a_fused_no_space_utterance_still_submits(talk_page):
    """"comptureshowmemynotes", the literal iPad transcript: misheard
    wake word fused onto the command with no spaces."""
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "comptureshowmemynotes", final=True)
    page.wait_for_timeout(1400)
    assert len(state["chat_calls"]) == 1
    assert state["chat_calls"][0]["message"] == "showmemynotes"


def test_an_empty_reply_becomes_words_on_screen(talk_page):
    page, state = talk_page
    fresh_turn(page, state)
    state["reply"] = ""
    try:
        say(page, "computer what is two plus two", final=True)
        page.wait_for_timeout(1400)
        caption = page.text_content("#capAssistant")
        assert "came back empty" in caption
    finally:
        state["reply"] = "Here are your notes."


def test_the_endpoint_fallback_hint_is_shown(talk_page):
    """getUserMedia rejects in this harness (the iPad case), so the page
    must SAY it switched to send-when-you-finish."""
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "computer hello there", final=True)
    page.wait_for_timeout(1200)
    hint = page.text_content("#endpointhint")
    assert "level meter" in (hint or "")


def test_interim_results_render_a_live_caption(talk_page):
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "computer make a", final=False)
    page.wait_for_timeout(150)
    live = page.text_content("#capUser")
    assert "make a" in (live or "")
    # finish it so the module-scoped page is not left mid-utterance
    say(page, "computer make a note", final=True)
    page.wait_for_timeout(1300)


def test_desktop_with_a_dead_vad_still_submits(talk_page):
    """THE iMac regression, encoded. On desktop the meter RUNS -- and a
    meter started mid-speech calibrates on the voice itself, so its VAD
    never fires: transcript on screen, listening forever, nothing sent,
    no turn recorded (verified against the live box's shell_commands).
    The stability endpointer must backstop even a running meter, because
    a mis-calibrated one fails SILENT and the stalling transcript is the
    one signal it cannot fake.

    Here the meter is simulated as running-but-useless, which is exactly
    what a bad calibration is."""
    page, state = talk_page
    fresh_turn(page, state)
    page.evaluate("() => { meterRunning = true; }")
    try:
        say(page, "computer list my tasks", final=False)   # interims only
        page.wait_for_timeout(2500)                        # window + meter margin
        assert len(state["chat_calls"]) == 1, "backstop did not fire past the meter"
        assert state["chat_calls"][0]["message"] == "list my tasks"
    finally:
        page.evaluate("() => { meterRunning = false; }")


def test_the_meter_starts_at_the_click_so_it_calibrates_in_silence(talk_page):
    """The root cause, pinned at the source level: calibration measures
    the noise floor, so it must run BEFORE speech. Deferring the meter to
    the first recognition result put calibration mid-utterance and
    taught the threshold the user's own voice."""
    source = TALK.read_text()
    click = source[source.index('mic.addEventListener("click"'):]
    handler = click[:click.index("} else {")]
    assert "startMeter()" in handler
    onresult = source[source.index("recognizer.onresult"):]
    onresult = onresult[:onresult.index("recognizer.onerror")]
    assert "startMeter()" not in onresult


def test_an_utterance_submits_even_if_a_final_never_comes(talk_page):
    """THE latency fix, and the regression it repairs. With the meter
    convicted, end-of-utterance waited on iOS volunteering an isFinal --
    2-6 seconds after you stop, sometimes never, and strictly WORSE than
    the meter it replaced. The recognizer's interims are themselves a
    silence detector: text that stops changing IS the silence. Here the
    device sends interims only, never a final, and the turn still fires
    after the silence window."""
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "computer what time is it", final=False)   # interim only
    page.wait_for_timeout(2100)                          # silence window (1400ms) + margin
    assert len(state["chat_calls"]) == 1, "no submit without a final"
    assert state["chat_calls"][0]["message"] == "what time is it"


def test_continued_speech_keeps_postponing_the_submit(talk_page):
    """Stability means STABLE: as long as the interim keeps growing, the
    timer must keep resetting -- otherwise mid-sentence pauses truncate."""
    page, state = talk_page
    fresh_turn(page, state)
    say(page, "computer remind me", final=False)
    page.wait_for_timeout(700)
    say(page, "computer remind me to call the supplier", final=False)
    page.wait_for_timeout(700)          # 1400ms since FIRST, 700 since latest
    assert state["chat_calls"] == []    # still talking; must not have fired
    page.wait_for_timeout(1100)         # now quiet past the window
    assert len(state["chat_calls"]) == 1
    assert state["chat_calls"][0]["message"] == "remind me to call the supplier"


def test_talk_prefers_its_own_model_when_set(talk_page):
    """Voice tolerates latency far worse than a terminal: a fast model
    that answers in 3 seconds beats a smarter one that leaves 10 seconds
    of silence. talk_model overrides ai_model for this surface only."""
    page, state = talk_page
    page.evaluate("() => { prefs.talk_model = 'anthropic:claude-haiku-4-5'; }")
    try:
        fresh_turn(page, state)
        say(page, "computer hello", final=True)
        page.wait_for_timeout(900)
        assert state["chat_calls"], "turn did not fire"
        assert state["chat_calls"][0]["model"] == "anthropic:claude-haiku-4-5"
    finally:
        page.evaluate("() => { delete prefs.talk_model; }")


def test_a_turn_in_flight_blocks_a_second_submit(talk_page):
    """The real bug, off a real session: "showed me my notes" and "show
    me my notes" reached the server ONE SECOND apart -- two provider
    calls billed for one utterance, the reply spoken twice. Nothing
    serialized the paths that can produce an utterance (voice
    endpointer, manual send), so a second one arriving while the first
    is still in flight was never refused.

    Both calls fire in ONE evaluate, back to back, with no wait between
    them -- deliberately not an artificial network delay (a Python-side
    time.sleep() inside a sync-API route handler blocks Playwright's one
    shared dispatch thread, which stalls every OTHER call including the
    "second" one, and the two land far enough apart that the guard's
    absence goes unnoticed; that false pass is what this comment now
    warns off). submitTurn is async but sets the guard synchronously
    before its first await, so two calls in the same JS tick already
    race for real -- no delay required.

    Calling finalizeVoiceSubmit directly, twice, isolates the guarantee
    from the choreography of WHICH timer or recognizer instance produced
    the duplicate -- that cause matters less than the guarantee holding
    regardless of cause."""
    page, state = talk_page
    fresh_turn(page, state)
    page.evaluate("""() => {
      finalizeVoiceSubmit('show me my notes');
      finalizeVoiceSubmit('show me my notes');
    }""")
    page.wait_for_timeout(400)
    assert len(state["chat_calls"]) == 1, state["chat_calls"]


def test_a_failed_turn_releases_the_guard_for_the_next_one(talk_page):
    """The guard must not survive its own failure -- a dropped connection
    on turn one must not wedge the page shut for the rest of the
    session."""
    page, state = talk_page
    fresh_turn(page, state)

    def fail_once(route):
        route.abort()
    page.route("**/api/ai/chat", fail_once, times=1)
    page.evaluate("() => finalizeVoiceSubmit('this one fails')")
    page.wait_for_timeout(400)

    page.evaluate("() => finalizeVoiceSubmit('this one should work')")
    page.wait_for_timeout(400)
    assert len(state["chat_calls"]) == 1
    assert state["chat_calls"][0]["message"] == "this one should work"


def test_playback_waits_for_the_microphone_to_actually_release(talk_page):
    """Dan's own diagnosis, verbatim: "listening while talking, maybe not
    allowed." The mic and speaker share one audio session on iOS, and
    recognizer.stop() is a REQUEST -- teardown finishes asynchronously,
    signalled by onend. Starting playback before that finishes can play
    into a still-recording session and come out silent, with no error
    anywhere. speak() must wait for the release, not race it."""
    page, state = talk_page
    fresh_turn(page, state)
    page.evaluate("() => { window.__recoStarts = 0; }")

    say(page, "computer say something", final=True)
    page.wait_for_timeout(1200)

    plays = page.evaluate("() => window.__plays")
    assert plays, "server TTS never played"


def test_stop_listening_and_wait_resolves_even_if_onend_never_fires(talk_page):
    """The safety timeout: a recognizer that was never actually listening
    (stop() on an idle recognizer) must not hang speak() forever."""
    page, state = talk_page
    fresh_turn(page, state)
    resolved = page.evaluate("""async () => {
      const r = window.__recos[window.__recos.length - 1];
      if (r && r.onend) { r.stop = () => {}; }   // stop() that fires nothing
      const started = performance.now();
      await stopListeningAndWaitForRelease();
      return performance.now() - started;
    }""")
    assert resolved < 1000   # bounded by the safety timeout, not hung
