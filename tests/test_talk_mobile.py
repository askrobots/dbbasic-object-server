"""/talk on a tablet: three failures reported from an older iPad.

All three are the same shape -- a capability the page silently assumed,
on a device that does not have it -- and all three were invisible on a
desktop browser where the assumption holds.

1. **It never sent.** Speech showed on screen, but nothing happened until
   the mic was tapped a second time. Silence endpointing depends on a
   SECOND microphone stream (getUserMedia feeding an AnalyserNode, since
   SpeechRecognition exposes no raw audio). On iOS the microphone is
   EXCLUSIVE: the second consumer to ask simply fails. With no meter,
   nothing in `silence` mode ever calls finalizeVoiceSubmit.

2. **It never spoke.** iOS refuses speechSynthesis and audio playback
   that was not started by a user gesture, and a reply arrives long after
   the click that started listening.

3. **The controls did not fit.** The stage claimed 85vh of its own on top
   of captions, controls and nav, so the page was taller than the screen
   by construction -- you could see the nav or the text box, never both.
"""

import pathlib

import pytest

TALK = (pathlib.Path(__file__).resolve().parents[1] / "packages" / "app-shell"
        / "objects" / "site" / "talk.py")


@pytest.fixture(scope="module")
def source():
    return TALK.read_text()


# --- 1. endpointing without a level meter ---------------------------------------

def test_silence_mode_falls_back_to_isFinal_when_the_meter_is_absent(source):
    """THE bug. Without this, `silence` mode is a dead end on any device
    where the microphone cannot be opened twice: the user speaks, sees
    their words, and has to tap the mic to make anything happen."""
    assert 'endpoint === "silence" && !meterRunning' in source


def test_the_meter_reports_whether_it_actually_came_up(source):
    """startMeter returns early on four separate failures. Each one has to
    say so, or the fallback above can never fire."""
    assert "meterRunning = false" in source
    assert "meterRunning = true" in source
    assert source.count("noteEndpointFallback()") >= 4


def test_the_fallback_is_visible_to_the_user(source):
    """Absent capability, STATED -- the house rule. The hint is the only
    way to tell 'waiting for you to stop talking' from 'waiting for you to
    tap', and both look identical on screen."""
    assert 'id="endpointhint"' in source
    assert "hint.hidden = false" in source


# --- 2. iOS will not make sound without a gesture --------------------------------

def test_audio_is_primed_inside_the_mic_click(source):
    """Not at load, not when the reply arrives -- inside the gesture. iOS
    gives no useful error when it refuses; the reply just never becomes
    sound."""
    click = source[source.index('mic.addEventListener("click"'):]
    handler = click[:click.index("conversationMode = !conversationMode")]
    assert "primeAudioForIOS();" in handler, \
        "priming must run BEFORE the mode toggles, so it is inside the " \
        "gesture even on the click that turns listening off"


def test_priming_is_silent(source):
    """A warm-up that made a noise would be worse than the bug."""
    prime = source[source.index("function primeAudioForIOS"):]
    prime = prime[:prime.index("async function api")]
    assert "volume = 0" in prime
    assert 'SpeechSynthesisUtterance("")' in prime


def test_priming_also_warms_the_voice_list(source):
    """getVoices() is empty on iOS until the engine loads, and
    hasLocalVoice() routes on it -- asking inside the gesture is what
    makes the answer true by the time a reply arrives."""
    prime = source[source.index("function primeAudioForIOS"):]
    assert "getVoices()" in prime[:900]


def test_the_unlocked_context_is_not_closed(source):
    """Closing it re-locks playback on iOS, which would undo the fix on
    the second reply rather than the first."""
    assert "__dbbasicTalkUnlockCtx" in source


# --- 3. the page has to fit the screen ------------------------------------------

def test_the_stage_no_longer_claims_a_fixed_share_of_the_viewport(source):
    """85vh of stage plus captions plus controls plus nav is taller than
    the screen by construction."""
    assert "flex: 0 0 85vh" not in source
    assert "flex: 1 1 auto; min-height: 0" in source


def test_the_wrapper_is_bounded_by_the_viewport(source):
    assert "overflow: hidden" in source
    assert "max-height: calc(100dvh" in source


def test_the_height_ladder_degrades_for_older_ipados(source):
    """dvh where supported (iOS toolbars shrink the visible area and 100vh
    lies about it), -webkit-fill-available for older iPadOS that has
    neither dvh nor svh, plain vh last."""
    wrap = source[source.index(".talkwrap {"):]
    wrap = wrap[:wrap.index("}")]
    assert "100vh" in wrap
    assert "-webkit-fill-available" in wrap
    assert "100dvh" in wrap
    assert wrap.index("100vh") < wrap.index("-webkit-fill-available") < wrap.index("100dvh")


def test_the_page_still_encodes_and_compiles(source):
    """The lone-surrogate lesson from the shell: a page that dies on
    encode passes every source-parsing test."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("talkmod", TALK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for value in vars(module).values():
        if isinstance(value, str):
            value.encode("utf-8")
