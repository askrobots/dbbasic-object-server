"""Files dropped into the chat: classified, capped, and refused by name.

askrobots had attach-a-file; the object server had the attachments
CAPABILITY (files on records) but no way to hand a file to the AI chat.
The split of labour that makes this testable: the server resolves file
ids to bytes (ownership, blob reads — all I/O), and object_ai classifies
and formats — pure, so everything below the endpoint runs with no data
directory and no network.

THE GATE IS THE POINT. A binary the model cannot see must be refused
loudly at send time, not arrive as mojibake the model hallucinates an
interpretation of. That silent path is why classification is a gate
rather than a best-effort decoder.
"""

import base64
import json

import pytest

import object_ai


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# --- classification (pure) ------------------------------------------------------

def test_an_image_passes_through_as_base64():
    part = object_ai.attachment_part("chart.png", "image/png", PNG)
    assert part["kind"] == "image"
    assert part["media_type"] == "image/png"
    assert base64.b64decode(part["data_b64"]) == PNG


def test_text_is_inlined_and_keeps_its_name():
    part = object_ai.attachment_part("notes.md", "text/markdown", b"# Plan\nship it")
    assert part == {"kind": "text", "name": "notes.md", "text": "# Plan\nship it"}


def test_an_honest_text_file_under_a_lazy_mime_type_still_inlines():
    """Uploads arrive as application/octet-stream constantly. Bytes that
    decode as UTF-8 are text whatever the header claims."""
    part = object_ai.attachment_part("data.txt", "application/octet-stream",
                                     b"plain enough")
    assert part["kind"] == "text"


def test_a_binary_is_refused_naming_what_is_supported():
    """THE gate. The refusal must name the supported set, because the
    commonest next question is 'well what CAN I attach'."""
    with pytest.raises(object_ai.InvalidChatRequestError) as err:
        object_ai.attachment_part("report.pdf", "application/pdf",
                                  b"%PDF-1.7\x00\xff\xfe garbage")
    message = str(err.value)
    assert "report.pdf" in message
    assert "image/png" in message
    assert "extracted first" in message


def test_an_oversized_image_is_refused_with_both_numbers():
    big = b"x" * (object_ai.MAX_IMAGE_BYTES + 1)
    with pytest.raises(object_ai.InvalidChatRequestError) as err:
        object_ai.attachment_part("huge.png", "image/png", big)
    assert str(object_ai.MAX_IMAGE_BYTES // 1024) in str(err.value)


def test_an_oversized_text_file_is_refused_suggesting_an_excerpt():
    big = b"a" * (object_ai.MAX_TEXT_BYTES + 1)
    with pytest.raises(object_ai.InvalidChatRequestError) as err:
        object_ai.attachment_part("dump.txt", "text/plain", big)
    assert "excerpt" in str(err.value)


def test_the_per_turn_count_cap_is_enforced_in_run_chat():
    parts = [{"kind": "text", "name": f"f{i}", "text": "x"}
             for i in range(object_ai.MAX_ATTACHMENTS_PER_TURN + 1)]
    with pytest.raises(object_ai.InvalidChatRequestError) as err:
        object_ai.run_chat(
            send_http=lambda *a: (_ for _ in ()).throw(AssertionError("no call")),
            dispatch_tool=lambda *a: None,
            service="anthropic", model="m", key="k",
            message="hello", attachments=parts)
    assert str(object_ai.MAX_ATTACHMENTS_PER_TURN) in str(err.value)


# --- provider payload shapes ----------------------------------------------------

def capture_one_turn(service, attachments):
    """Run one chat turn against a fake provider, returning the outbound
    request body — the thing that must be shaped right, since the provider
    is the one party we cannot test against."""
    captured = {}

    def send_http(url, headers, body):
        captured["body"] = json.loads(body)
        reply = ({"content": [{"type": "text", "text": "seen"}], "usage": {}}
                 if service == "anthropic" else
                 {"choices": [{"message": {"content": "seen"}}], "usage": {}})
        return 200, json.dumps(reply).encode()

    object_ai.run_chat(
        send_http=send_http, dispatch_tool=lambda *a: None,
        service=service, model="test-model", key="k",
        message="what is in this file?", attachments=attachments)
    return captured["body"]


def test_anthropic_gets_a_content_array_with_the_message_last():
    image = object_ai.attachment_part("pic.png", "image/png", PNG)
    note = object_ai.attachment_part("note.txt", "text/plain", b"context here")
    body = capture_one_turn("anthropic", [image, note])

    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "text", "text"]
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["text"].startswith("[file: note.txt]\n")
    assert content[-1]["text"] == "what is in this file?"


def test_openai_gets_data_urls():
    image = object_ai.attachment_part("pic.png", "image/png", PNG)
    body = capture_one_turn("openai", [image])
    content = body["messages"][-1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[-1] == {"type": "text", "text": "what is in this file?"}


def test_a_chat_with_no_attachments_sends_a_plain_string():
    """The outbound payload of a text-only chat must be byte-identical to
    what it always was — a content array where a string used to be is the
    kind of change that breaks a provider-side cache or a logged-payload
    consumer silently."""
    body = capture_one_turn("anthropic", None)
    assert body["messages"][0]["content"] == "what is in this file?"


# --- the endpoint's ownership gate ----------------------------------------------

def test_the_ownership_refusal_says_why_sharing_does_not_apply():
    """v1 is strict owner-only, and the refusal explains the decision
    rather than reciting 'forbidden': a share grants READ of a record, and
    whether that extends to feeding the blob to an AI provider is a real
    question deliberately not yet answered. Pinned as a source assertion
    because the reasoning IS the contract."""
    import pathlib
    source = pathlib.Path("object_server.py").read_text()
    assert "Chat " in source and "attachments are owner-only for now" in source
    assert "deliberately not yet" in source
    assert "attachments=attachment_parts or None" in source


def test_the_shell_script_still_encodes_to_utf8():
    """Caught live, not by the original tests: emoji written as \\ud83d
    \\udcce escape pairs inside the shell's plain triple-quoted _SCRIPT
    string become LONE SURROGATES in the Python str — which compile fine,
    pass ast checks, pass every JS-shape assertion, and then crash with
    UnicodeEncodeError the moment the server encodes the page for HTTP.
    A page that 500s on encode is invisible to every test that only
    parses the source."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "shellmod", "packages/app-shell/objects/site/shell.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCRIPT.encode("utf-8")          # raises if a surrogate survives
    assert "pendingAttach" in module._SCRIPT
    assert "attachments: attachIds.length" in module._SCRIPT
