"""Tests for packages/app-shell's Talk surface (site_talk / /talk).

Talk is a second projection of the same conversation as the shell: same
POST /api/ai/chat, same prefs/model/tools, same shell_commands history --
just staged as voice instead of transcript. These tests mirror the
package/renderer testing conventions used in tests/test_app_views_package.py
and add coverage specific to Talk's conversation-mode mic loop and the
places shell.py was patched to stay in sync with it.
"""

import json
import re
from pathlib import Path

import object_execution
import object_packages
import object_permissions
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_SHELL_DIR = PACKAGES_ROOT / "app-shell"
TALK_SOURCE = (APP_SHELL_DIR / "objects" / "site" / "talk.py").read_text()
SHELL_SOURCE = (APP_SHELL_DIR / "objects" / "site" / "shell.py").read_text()


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
