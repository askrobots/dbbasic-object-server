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


# --- 0. the meter must never starve recognition ---------------------------------

def test_the_meter_does_not_start_alongside_recognition(source):
    """THE regression, and the sharper version of the original bug.

    The level meter opens a SECOND microphone stream. On iOS the mic is
    exclusive, so two consumers means one silently gets nothing -- and
    when the meter won, the ring animated while recognition sat starved
    and no transcript appeared at all. Recognition is ESSENTIAL; the
    meter is a nicety. Starting them together is the bug.
    """
    click = source[source.index('mic.addEventListener("click"'):]
    handler = click[:click.index("} else {")]
    assert "startListening();" in handler
    assert "startMeter()" not in handler, \
        "the meter must not start in the same breath as recognition"


def test_the_meter_starts_only_once_recognition_has_proven_itself(source):
    """The device tells us which consumer it gave the microphone to; no
    user-agent sniffing required. A first result IS the proof."""
    onresult = source[source.index("recognizer.onresult = (event) =>"):]
    onresult = onresult[:onresult.index("recognizer.onerror")]
    assert "recognitionProven = true" in onresult
    assert "startMeter()" in onresult


def test_a_device_that_returns_no_recognition_says_so(source):
    """A listening button that will never do anything is worse than one
    that admits it cannot hear -- the user can then just type."""
    assert "watchForRecognition" in source
    assert "not returning speech recognition" in source


def test_the_proof_resets_when_listening_stops(source):
    """Otherwise the next session starts the meter immediately and
    reintroduces the contention."""
    assert "recognitionProven = false" in source


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


# --- the meter must be able to LOSE ---------------------------------------------

def test_the_meter_convicts_itself_of_taking_the_microphone(source):
    """The deferred start was not enough, and the same iPad proved it:
    recognition delivered ONE result ("computer"), that proof started the
    meter, and the meter's stream took the microphone back -- first word
    on screen, then nothing, the original bug moved one result later.

    The conviction uses the meter's own evidence: RMS above the speech
    threshold (it can hear voice) while recognition has produced nothing
    for CONTENTION_MS. On a working platform results flow continuously
    while you speak, so the signature cannot fire there."""
    assert "meterContended = true" in source
    assert "CONTENTION_MS" in source
    frame = source[source.index("function frame()"):]
    frame = frame[:frame.index("function stopMeter")]
    assert "rms >= speechThreshold && now - lastResultTs > CONTENTION_MS" in frame


def test_a_convicted_meter_stays_off_for_the_session(source):
    """Restarting it on the next result would re-steal the microphone one
    result later, forever, in a loop the user experiences as 'it hears
    one word per attempt'."""
    onresult = source[source.index("recognizer.onresult"):]
    onresult = onresult[:onresult.index("recognizer.onerror")]
    assert "!meterContended" in onresult


def test_conviction_frees_the_mic_and_kicks_the_starved_recognizer(source):
    frame = source[source.index("function frame()"):]
    conviction = frame[frame.index("meterContended = true"):]
    conviction = conviction[:conviction.index("evaluateVAD")]
    assert "stopMeter();" in conviction
    assert "stopListening();" in conviction   # onend restarts it, mic now free


def test_every_recognition_result_stamps_the_heartbeat(source):
    """lastResultTs is the evidence the conviction reads; a result that
    does not stamp it makes an innocent meter convictable."""
    onresult = source[source.index("recognizer.onresult"):]
    assert "lastResultTs = performance.now();" in onresult[:220]


# --- a misheard wake word must not discard the utterance ------------------------

def test_the_wake_word_tolerates_recognition_mishears(source):
    """"compture" arrived for "computer" -- on screen, visibly heard --
    and the exact match then threw away everything said after it. The
    asymmetry decides the design: a false arm captures a sentence the
    user SEES (recoverable); a strict miss discards it invisibly (not)."""
    assert "function matchesWakeWord" in source
    assert "matchesWakeWord(key, target)" in source


def test_the_tolerance_is_damerau_because_mishears_are_transpositions(source):
    """"compture" is two adjacent swaps: Damerau distance 2, plain
    Levenshtein 3. Plain distance would reject the exact observed case
    this tolerance exists for."""
    assert "prevPrev[j - 2] + 1" in source
    assert "a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]" in source


def test_short_wake_words_stay_exact(source):
    """"hey" must not match "they": the budget scales with length and is
    zero below four letters."""
    budget = source[source.index("function wakeBudget"):]
    budget = budget[:budget.index("function matchesWakeWord")]
    assert "return 0" in budget and "return 2" in budget


# --- iOS joins segments without spaces ------------------------------------------

def test_result_segments_are_joined_with_spaces(source):
    """"comptureshowmemynotes", verbatim off the iPad. Chrome includes
    leading spaces on continuation segments; iOS does not, and the raw
    concatenation produced one giant token the whitespace tokenizer could
    never split -- so the wake gate could never match, and every
    utterance was invisibly discarded WITH the transcript on screen. The
    round that taught the most: recognition was working the whole time;
    the page was destroying its output."""
    onresult = source[source.index("recognizer.onresult"):]
    onresult = onresult[:onresult.index("recognizer.onerror")]
    assert 'text += (text ? " " : "") + seg;' in onresult
    assert "text += event.results[i][0].transcript;" not in onresult


def test_the_shell_gets_the_same_join_fix(source):
    """Same loop, same platform, same bug waiting."""
    shell = (TALK.parent / "shell.py").read_text()
    assert 'text += (text ? " " : "") + seg;' in shell
    assert "text += event.results[i][0].transcript;" not in shell


def test_a_fused_wake_word_still_arms_via_its_prefix(source):
    """Belt beside the braces: if a no-space final still arrives fused,
    a long token whose PREFIX matches the wake word arms, and the
    remainder becomes the command -- unspaced, but a model reads
    unspaced text far better than this page reads a discarded
    utterance."""
    split = source[source.index("function findWakeSplit"):]
    split = split[:split.index("// If `text`")]
    assert "key.slice(0, len)" in split
    assert "key.length > target.length + wakeBudget(target)" in split


# --- a knock is not a question --------------------------------------------------

def test_the_bare_wake_word_is_never_submitted(source):
    """"Computer", alone, reached the API on a live iPad: a 2.4-second
    provider round-trip that answered "" -- money and silence for a turn
    nobody asked. The check is fuzzy like the gate, so a misheard bare
    "compture" is swallowed too."""
    finalize = source[source.index("function finalizeVoiceSubmit"):]
    finalize = finalize[:finalize.index("function processTranscript")]
    assert "matchesWakeWord(wordKey(only[0])" in finalize
    assert finalize.index("matchesWakeWord") < finalize.index("submitTurn(clean)")


def test_an_empty_reply_becomes_words_rather_than_silence(source):
    """A successful round-trip with empty text looks exactly like a hang
    on a voice surface. The recorded turn (out: "") is real."""
    assert 'body.reply || "I came back empty' in source
