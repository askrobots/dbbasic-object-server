"""Structural tests for packages/app-messaging (message_threads, messages,
message_recipients, message_drafts).

Mirrors the package/schema/permission testing conventions used for
packages/app-invoices and packages/app-worker in
tests/test_app_invoices_package.py and tests/test_app_worker_package.py.
app-messaging has no HANDLES handler and no behavior module, so this is
the whole suite.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_MESSAGING_DIR = PACKAGES_ROOT / "app-messaging"


def _threads_schema():
    return json.loads((APP_MESSAGING_DIR / "schemas" / "message_threads.json").read_text())


def _messages_schema():
    return json.loads((APP_MESSAGING_DIR / "schemas" / "messages.json").read_text())


def _recipients_schema():
    return json.loads((APP_MESSAGING_DIR / "schemas" / "message_recipients.json").read_text())


def _drafts_schema():
    return json.loads((APP_MESSAGING_DIR / "schemas" / "message_drafts.json").read_text())


def test_get_package_normalizes_app_messaging_manifest():
    package = object_packages.get_package("app-messaging", root=PACKAGES_ROOT)

    assert package["id"] == "app-messaging"
    assert package["name"] == "Messaging"
    assert {schema["collection"] for schema in package["schemas"]} == {
        "message_threads",
        "messages",
        "message_recipients",
        "message_drafts",
    }
    assert {obj["id"] for obj in package["objects"]} == {
        "site_inbox",
        "site_message_thread",
    }
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert {entry["collection"] for entry in package["seed"]} == {
        "message_threads",
        "messages",
        "message_recipients",
        "message_drafts",
    }
    assert package["dependencies"] == []


def test_dry_run_app_messaging_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-messaging",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {
        "message_threads",
        "messages",
        "message_recipients",
        "message_drafts",
    }


def test_install_app_messaging_package_loads_schemas(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-messaging",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    threads_schema = object_schemas.get_schema("message_threads", base_dir=data_dir)
    messages_schema = object_schemas.get_schema("messages", base_dir=data_dir)
    recipients_schema = object_schemas.get_schema("message_recipients", base_dir=data_dir)
    drafts_schema = object_schemas.get_schema("message_drafts", base_dir=data_dir)

    assert threads_schema["name"] == "message_threads"
    assert messages_schema["name"] == "messages"
    assert recipients_schema["name"] == "message_recipients"
    assert drafts_schema["name"] == "message_drafts"
    assert (object_root / "site" / "inbox.py").is_file()
    assert (object_root / "site" / "message_thread.py").is_file()


def test_schema_json_files_are_valid_and_versioned():
    for name in ("message_threads", "messages", "message_recipients", "message_drafts"):
        payload = json.loads((APP_MESSAGING_DIR / "schemas" / f"{name}.json").read_text())
        assert payload["name"] == name
        assert payload["version"] == 1


def test_four_collections_present_with_expected_field_names():
    thread_fields = [f["name"] for f in _threads_schema()["fields"]]
    assert thread_fields == [
        "id", "thread_type", "subject", "participant_summary", "last_message_at",
        "message_count", "is_read", "is_starred", "is_archived", "is_trashed",
        "labels", "owner_id", "created_at",
    ]

    message_fields = [f["name"] for f in _messages_schema()["fields"]]
    assert message_fields == [
        "id", "thread_id", "direction", "from_address", "subject", "body_text",
        "is_read", "sent_at", "received_at", "in_reply_to", "external_message_id",
        "owner_id", "created_at",
    ]

    recipient_fields = [f["name"] for f in _recipients_schema()["fields"]]
    assert recipient_fields == [
        "id", "message_id", "kind", "address", "owner_id", "created_at",
    ]

    draft_fields = [f["name"] for f in _drafts_schema()["fields"]]
    assert draft_fields == [
        "id", "thread_id", "to_addresses", "cc_addresses", "subject", "body_text",
        "owner_id", "created_at",
    ]


def test_no_money_fields_anywhere():
    """SCOPE: the source messaging model carried no monetary fields. No
    *_cents field, and no float/currency-typed field, exists anywhere in
    this package -- unlike app-invoices, money was never part of this
    model.
    """
    for schema in (_threads_schema(), _messages_schema(), _recipients_schema(), _drafts_schema()):
        for field in schema["fields"]:
            assert "cents" not in field["name"]
            assert field.get("type") not in {"float", "currency"}


def test_message_threads_carries_the_source_flags():
    by_name = {f["name"]: f for f in _threads_schema()["fields"]}
    assert by_name["thread_type"]["type"] == "enum"
    assert by_name["thread_type"]["enum"] == ["internal", "email"]
    assert by_name["thread_type"]["default"] == "email"

    for name in ("is_read", "is_starred", "is_archived", "is_trashed"):
        assert by_name[name]["type"] == "boolean"

    assert by_name["message_count"]["default"] == "0"
    assert by_name["labels"]["type"] == "text"
    assert "relation" not in by_name["labels"]


def test_message_threads_participant_summary_is_free_text_not_a_relation():
    """SCOPE RULE: the source's real participants M2M collapses to a
    denormalized display snapshot here, same trade-off app-invoices'
    customer_name makes for its customer relation -- no participants
    collection or M2M join is built.
    """
    by_name = {f["name"]: f for f in _threads_schema()["fields"]}
    assert by_name["participant_summary"]["type"] == "text"
    assert "relation" not in by_name["participant_summary"]


def test_message_threads_forms_and_views():
    schema = _threads_schema()
    assert schema["forms"]["default"]["fields"] == ["thread_type", "subject", "labels"]
    # Moderation-style flags (star/archive/trash/read) are toggled by
    # dedicated buttons in objects/site/message_thread.py, not through the
    # generic form -- same posture as forum_topics keeping
    # is_pinned/is_locked/is_solved out of its own forms.default.
    for flag in ("is_read", "is_starred", "is_archived", "is_trashed"):
        assert flag not in schema["forms"]["default"]["fields"]


def test_messages_direction_enum_and_relation_to_thread():
    by_name = {f["name"]: f for f in _messages_schema()["fields"]}
    assert by_name["direction"]["type"] == "enum"
    assert by_name["direction"]["enum"] == ["inbound", "outbound"]
    assert by_name["direction"]["required"] is True

    assert by_name["thread_id"]["type"] == "relation"
    assert by_name["thread_id"]["relation"]["collection"] == "message_threads"
    assert by_name["thread_id"]["required"] is True


def test_messages_deferred_transport_linkage_fields_present():
    """Fields carried for the deferred IMAP linkage (task brief + source
    audit), never populated by anything in this package.
    """
    by_name = {f["name"]: f for f in _messages_schema()["fields"]}
    assert "in_reply_to" in by_name
    assert "external_message_id" in by_name
    assert "sent_at" in by_name
    assert "received_at" in by_name


def test_messages_storage_is_append_and_other_collections_are_classic():
    """The storage choice tied to the audit: a message's content is
    write-once (only is_read flips after creation) -- the same profile
    that put app-thread's thread_comments and app-catalog's stock_moves on
    append storage. message_threads/message_recipients/message_drafts are
    smaller, more frequently flag-toggled containers and stay classic
    (default), matching forum_topics' choice.
    """
    assert _messages_schema()["storage"] == "append"
    assert "storage" not in _threads_schema()
    assert "storage" not in _recipients_schema()
    assert "storage" not in _drafts_schema()


def test_message_recipients_normalized_not_flattened_onto_message():
    """SCOPE RULE: the source normalized recipients into their own
    MessageRecipient table rather than flattening a To/Cc/Bcc address list
    onto Message -- this package follows that same split. messages.json
    itself carries no to_addresses/cc field.
    """
    message_field_names = {f["name"] for f in _messages_schema()["fields"]}
    assert "to_addresses" not in message_field_names
    assert "cc" not in message_field_names
    assert "cc_addresses" not in message_field_names

    by_name = {f["name"]: f for f in _recipients_schema()["fields"]}
    assert by_name["message_id"]["relation"]["collection"] == "messages"
    assert by_name["message_id"]["required"] is True
    assert by_name["kind"]["type"] == "enum"
    assert by_name["kind"]["enum"] == ["to", "cc", "bcc"]
    assert by_name["address"]["required"] is True


def test_message_drafts_is_its_own_collection_not_a_status_on_messages():
    """SCOPE RULE: the source kept drafts as their own model (MessageDraft),
    not a status value on Message -- this package follows that split.
    messages.json's direction enum has no "draft" value, and there is no
    status field on messages at all.
    """
    by_name = {f["name"]: f for f in _messages_schema()["fields"]}
    assert "status" not in by_name
    assert "draft" not in by_name["direction"]["enum"]

    draft_by_name = {f["name"]: f for f in _drafts_schema()["fields"]}
    assert draft_by_name["thread_id"]["relation"]["collection"] == "message_threads"
    assert draft_by_name["thread_id"].get("required") is not True  # optional: new compose has no thread yet


def test_no_ai_triage_or_transport_fields_invented():
    """SCOPE RULE: dove's AI classification (state/summary/commitments/
    draft_reply), sender reputation, a rules engine, and mailcow/Twilio
    fields are all separate, infra/AI-bound, and out of scope -- none of
    their vocabulary should leak into this package's schemas as invented
    fields.
    """
    banned_field_names = {
        "ai_summary", "is_ai_summarized", "classification", "sender_trust",
        "twilio_sid", "call_transcript", "mailcow_id", "imap_uid",
    }
    for schema in (_threads_schema(), _messages_schema(), _recipients_schema(), _drafts_schema()):
        field_names = {f["name"] for f in schema["fields"]}
        assert not (field_names & banned_field_names), (
            f"{schema['name']} carries an invented out-of-scope field"
        )


def _app_messaging_policy():
    payload = json.loads((APP_MESSAGING_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_records_on_every_collection():
    policy = _app_messaging_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    records = {
        "message_threads": {"owner_id": "7", "subject": "Hello"},
        "messages": {"owner_id": "7", "thread_id": "t1", "direction": "inbound", "from_address": "a@example.com"},
        "message_recipients": {"owner_id": "7", "message_id": "m1", "address": "a@example.com"},
        "message_drafts": {"owner_id": "7", "subject": "Draft"},
    }

    for collection, record in records.items():
        for action in (
            object_permissions.CREATE,
            object_permissions.READ,
            object_permissions.UPDATE,
            object_permissions.DELETE,
        ):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record
            )
            assert decision.allowed is True, f"owner {action} should be allowed on {collection}"


def test_others_cannot_read_or_write_someone_elses_records():
    """Cross-owner denial: a different signed-in user gets nothing on any
    collection -- a mailbox is private, not just write-protected.
    """
    policy = _app_messaging_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    records = {
        "message_threads": {"owner_id": "7", "subject": "Hello"},
        "messages": {"owner_id": "7", "thread_id": "t1", "direction": "inbound", "from_address": "a@example.com"},
        "message_recipients": {"owner_id": "7", "message_id": "m1", "address": "a@example.com"},
        "message_drafts": {"owner_id": "7", "subject": "Draft"},
    }

    for collection, record in records.items():
        for action in (
            object_permissions.READ,
            object_permissions.UPDATE,
            object_permissions.DELETE,
        ):
            decision = object_permissions.check_permission(
                subject, action, policy=policy, collection=collection, record=record
            )
            assert decision.allowed is False, f"non-owner {action} should be denied on {collection}"


def test_anonymous_cannot_read_any_collection():
    """No public read rule is granted on any collection in this package --
    a mailbox is private, unlike app-forum/app-worker's public-read
    collections.
    """
    policy = _app_messaging_policy()
    records = {
        "message_threads": {"owner_id": "7", "subject": "Hello"},
        "messages": {"owner_id": "7", "thread_id": "t1", "direction": "inbound", "from_address": "a@example.com"},
        "message_recipients": {"owner_id": "7", "message_id": "m1", "address": "a@example.com"},
        "message_drafts": {"owner_id": "7", "subject": "Draft"},
    }

    for collection, record in records.items():
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection, record=record
        )
        assert decision.allowed is False, f"anonymous read should be denied on {collection}"


def test_anonymous_cannot_create_on_any_collection():
    policy = _app_messaging_policy()
    records = {
        "message_threads": {"subject": "Hello"},
        "messages": {"thread_id": "t1", "direction": "inbound", "from_address": "a@example.com"},
        "message_recipients": {"message_id": "m1", "address": "a@example.com"},
        "message_drafts": {"subject": "Draft"},
    }

    for collection, record in records.items():
        decision = object_permissions.check_permission(
            None, object_permissions.CREATE, policy=policy, collection=collection, record=record
        )
        assert decision.allowed is False, f"anonymous create should be denied on {collection}"


def test_messaging_pages_are_publicly_executable():
    """Public execute on the *page objects* (they show a sign-in prompt to
    visitors), never public read on the *collections* -- same split
    app-invoices uses for site_invoices/site_invoice_view.
    """
    policy = _app_messaging_policy()

    for object_id in ("site_inbox", "site_message_thread"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id
        )
        assert decision.allowed is True


def test_seed_tsvs_have_no_data_rows_and_match_schema_field_order():
    """Header-only seeds, matching the established precedent (app-tasks,
    app-notes, app-invoices, app-forum, app-worker all ship header-only
    seeds).
    """
    for name, schema in (
        ("message_threads", _threads_schema()),
        ("messages", _messages_schema()),
        ("message_recipients", _recipients_schema()),
        ("message_drafts", _drafts_schema()),
    ):
        path = APP_MESSAGING_DIR / "seed" / f"{name}.tsv"
        lines = path.read_text().splitlines()
        assert len(lines) == 1, f"{name}.tsv should be header-only"
        header = lines[0].split("\t")
        assert header == [f["name"] for f in schema["fields"]]


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source, including in any source-doc path reference
    (this package describes its source only as "a private
    predecessor-system audit, not part of this repo").
    """
    # Built from fragments so this guard file itself stays clean of the
    # very internal names it forbids.
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_MESSAGING_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


def test_package_does_not_touch_app_shell_or_app_theme():
    """This package's brief explicitly forbids editing packages/app-shell
    or packages/app-theme (nav/home wiring is the main loop's job). This
    test only asserts app-messaging's own manifest declares no dependency
    on them; it cannot detect a stray edit elsewhere in the repo, but
    keeps the intent on record.
    """
    manifest = json.loads((APP_MESSAGING_DIR / "dbbasic-package.json").read_text())
    assert "app-shell" not in manifest.get("dependencies", [])
    assert "app-theme" not in manifest.get("dependencies", [])
