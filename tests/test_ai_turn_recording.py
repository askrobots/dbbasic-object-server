"""Every AI turn is recorded by the server, stamped and session-grouped.

The client used to write its own history row after the reply,
fire-and-forget. Three failures followed, all found in one evening of
live debugging: a turn whose page died before the write vanished
entirely (billed in ai_usage, absent from history); no row carried a
timestamp, so "what happened today" was unanswerable and a date-filtered
query reported zero turns that existed; and nothing grouped a
conversation, so askrobots' resume-a-session had no substrate to port
onto.

Same posture as the cost write beside it: server-authoritative, because
a record the client is trusted to write is a record that sometimes does
not exist.
"""

import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def server_source():
    return (REPO / "object_server.py").read_text()


@pytest.fixture(scope="module")
def chat_section(server_source):
    start = server_source.index("async def _handle_ai_chat")
    return server_source[start:start + 24000]


def test_the_turn_is_written_inside_the_chat_handler(chat_section):
    assert '"shell_commands", turn_record' in chat_section
    assert '"created_at": _utc_now_iso()' in chat_section


def test_the_write_happens_before_the_reply_is_sent(chat_section):
    """History must not depend on anything that happens after the
    response starts -- that dependency is exactly the old bug."""
    write_at = chat_section.index('"shell_commands", turn_record')
    reply_at = chat_section.rindex("await _send_json")
    assert write_at < reply_at


def test_a_failed_history_write_never_fails_the_chat(chat_section):
    """The reply matters more than the record of it."""
    tail = chat_section[chat_section.index('"shell_commands", turn_record'):]
    assert "except Exception:" in tail[:400]


def test_the_turn_carries_its_evidence(chat_section):
    """session_id groups it, source names the surface, duration_ms
    answers 'why was that slow', tool_calls says what it actually did --
    each one a question that was unanswerable from the old flat row."""
    for field in ('"session_id"', '"source"', '"duration_ms"',
                  '"tool_calls"', '"model"'):
        assert field in chat_section, field


def test_the_schema_grew_the_same_fields():
    schema = json.loads((REPO / "packages/app-shell/schemas/shell_commands.json")
                        .read_text())
    names = {f["name"] for f in schema["fields"]}
    assert {"session_id", "source", "model", "duration_ms",
            "tool_calls", "created_at"} <= names
    assert schema["version"] >= 3


@pytest.mark.parametrize("page", ["talk", "shell"])
def test_the_clients_no_longer_write_ai_history(page):
    source = (REPO / f"packages/app-shell/objects/site/{page}.py").read_text()
    assert 'record(input, replyText, "ai")' not in source
    assert 'record(input, ok ? body.reply : body.error, "ai")' not in source


def test_shell_still_records_its_non_ai_commands():
    """Command turns (note/link/search) never touch the chat endpoint, so
    the server cannot record them -- deleting record() wholesale would
    have silently dropped that history."""
    source = (REPO / "packages/app-shell/objects/site/shell.py").read_text()
    assert 'record(input, "link saved", "link")' in source


@pytest.mark.parametrize("page,source_tag", [("talk", '"talk"'), ("shell", '"shell"')])
def test_the_clients_mint_and_send_a_session_id(page, source_tag):
    source = (REPO / f"packages/app-shell/objects/site/{page}.py").read_text()
    assert "const SESSION_ID" in source
    assert "session_id: SESSION_ID" in source
    assert f"source: {source_tag}" in source


def test_talk_shows_a_moving_elapsed_counter_while_the_model_works():
    """"we don't know because after 10 seconds..." -- a tool-using model
    legitimately takes that long, and a frozen ellipsis for ten seconds
    reads as dead. A counter that is moving is a page that is alive."""
    source = (REPO / "packages/app-shell/objects/site/talk.py").read_text()
    assert "startThinking()" in source
    assert 'thinking\\\\u2026 " + s + "s"' in source
    assert "stopThinking();" in source


def test_talk_acknowledges_the_utterance_audibly():
    """Voice-first: the ear should know the utterance fired without
    looking at the screen. The blip reuses the audio context the iOS
    unlock already opened -- no new permission, no new stream."""
    source = (REPO / "packages/app-shell/objects/site/talk.py").read_text()
    assert "function blip()" in source
    assert "__dbbasicTalkUnlockCtx" in source[source.index("function blip()"):]


def test_the_filtered_list_never_renders_a_uuid_as_a_row_title():
    """The other half of tonight: "show me my notes" rendered seven
    UUIDs, because renderFilteredList fell back title||name||ID. The
    fallback now walks the record for the first human-shaped string and
    skips id-shaped and date-shaped values BY SHAPE."""
    source = (REPO / "packages/app-views/objects/site/view_render.py").read_text()
    assert "r.title || r.name || r.id" not in source
    assert "function rowLabel(r)" in source
    assert "idish" in source
