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
  return Promise.resolve();
};
// speechSynthesis exists headless but must be observable too.
window.__spoken = [];
if (window.speechSynthesis) {
  const realSpeak = window.speechSynthesis.speak.bind(window.speechSynthesis);
  window.speechSynthesis.speak = (u) => { window.__spoken.push(u.text); };
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


@pytest.fixture(scope="module")
def talk_page():
    from playwright.sync_api import sync_playwright

    html = render_talk_html()
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        ctx = browser.new_context(viewport={"width": 810, "height": 1080})
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
        browser.close()


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
