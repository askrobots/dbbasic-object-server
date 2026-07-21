"""12 notify: record-change events -> notifications, declaratively.

Covers object_notify's pure logic (event-pattern grammar, transition-aware
match, the four recipient modes, template rendering, suppress_self) and the
daemon pass end-to-end (assign a task -> notify the assignee; idempotent;
change-log-driven, no event-handler dependency).
"""

import json
from pathlib import Path

import object_daemon
import object_identity
import object_notify
import object_packages
import object_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


# ---- event-pattern grammar ------------------------------------------------

def test_event_pattern_wildcards_and_action_mapping():
    m = object_notify.event_pattern_matches
    assert m("tasks.record.updated", "tasks", "updated")
    assert m("tasks.record.*", "tasks", "created")
    assert m("*.record.created", "invoices", "created")
    assert m("*.record.*", "anything", "deleted")
    assert not m("tasks.record.updated", "tasks", "created")
    assert not m("tasks.record.updated", "orders", "updated")
    assert not m("tasks.record.updated", "tasks", "update")   # present tense is not an event action
    assert not m("garbage", "tasks", "updated")


def test_change_action_present_tense_maps_to_event_action():
    """The change log stores create/update/delete; the grammar uses -ed. The
    engine maps one to the other so a `tasks.record.updated` rule fires on a
    stored `update` change."""
    rule = {"event_pattern": "tasks.record.updated", "recipients": json.dumps({"mode": "owner"}),
            "channels": json.dumps([{"channel": "in_app", "body_template": "hi"}])}
    change = {"collection": "tasks", "action": "update", "record_id": "t1",
              "changed_fields": ["status"], "after": {"owner_id": "9", "status": "x"}, "actor": "u"}
    notes = object_notify.notifications_for_change(rule, change, base_dir="/nonexistent")
    assert len(notes) == 1 and notes[0]["user_id"] == "9"


# ---- transition-aware match ----------------------------------------------

def _rule(**over):
    base = {"enabled": "true", "event_pattern": "tasks.record.updated",
            "match": json.dumps({"status": "assigned"}),
            "recipients": json.dumps({"mode": "field", "field": "assigned_to"}),
            "suppress_self": "true",
            "channels": json.dumps([{"channel": "in_app", "body_template": "You got {title}"}])}
    base.update(over)
    return base


def test_match_fires_only_on_the_transition_not_later_edits():
    rule = _rule()
    # status became assigned in THIS write -> matches
    transition = {"collection": "tasks", "action": "update", "record_id": "t1",
                  "changed_fields": ["status", "assigned_to"],
                  "after": {"status": "assigned", "assigned_to": "7", "title": "A"}, "actor": "1"}
    assert len(object_notify.notifications_for_change(rule, transition, base_dir="/x")) == 1
    # a later title edit on an already-assigned task -> status NOT in changed_fields -> no re-fire
    later = {"collection": "tasks", "action": "update", "record_id": "t1",
             "changed_fields": ["title"],
             "after": {"status": "assigned", "assigned_to": "7", "title": "B"}, "actor": "1"}
    assert object_notify.notifications_for_change(rule, later, base_dir="/x") == []


def test_suppress_self_skips_the_actor_only():
    # actor assigns themselves -> suppressed
    self_assign = {"collection": "tasks", "action": "update", "record_id": "t1",
                   "changed_fields": ["status", "assigned_to"],
                   "after": {"status": "assigned", "assigned_to": "7", "title": "A"}, "actor": "7"}
    assert object_notify.notifications_for_change(_rule(), self_assign, base_dir="/x") == []
    # someone else assigns 7 -> not suppressed
    other = dict(self_assign, actor="1")
    assert len(object_notify.notifications_for_change(_rule(), other, base_dir="/x")) == 1


def test_no_in_app_channel_writes_no_in_app_notification():
    # email-only rule: no in_app notification record...
    rule = _rule(channels=json.dumps([{"channel": "email", "subject_template": "s {title}", "body_template": "b"}]))
    change = {"collection": "tasks", "action": "update", "record_id": "t1", "changed_fields": ["status"],
              "after": {"status": "assigned", "assigned_to": "7", "title": "T"}, "actor": "1"}
    assert object_notify.notifications_for_change(rule, change, base_dir="/x") == []
    # ...but the email channel DOES produce an intent
    intents = object_notify.email_intents_for_change(rule, change, base_dir="/x")
    assert intents == [{"user_id": "7", "subject": "s T", "body": "b", "target": "tasks/t1"}]


def test_email_intents_render_per_recipient_and_gate_on_match():
    rule = _rule(channels=json.dumps([
        {"channel": "in_app", "body_template": "in-app {title}"},
        {"channel": "email", "subject_template": "Task: {title}", "body_template": "You got {title} ({urgency})"},
    ]))
    fires = {"collection": "tasks", "action": "update", "record_id": "t1",
             "changed_fields": ["status", "assigned_to"],
             "after": {"status": "assigned", "assigned_to": "7", "title": "Fix bug", "urgency": "high"}, "actor": "1"}
    intents = object_notify.email_intents_for_change(rule, fires, base_dir="/x")
    assert intents[0]["user_id"] == "7" and intents[0]["subject"] == "Task: Fix bug"
    assert intents[0]["body"] == "You got Fix bug (high)"
    # a later edit that no longer satisfies the transition doesn't re-fire email
    later = {"collection": "tasks", "action": "update", "record_id": "t1", "changed_fields": ["title"],
             "after": {"status": "assigned", "assigned_to": "7", "title": "renamed"}, "actor": "1"}
    assert object_notify.email_intents_for_change(rule, later, base_dir="/x") == []


def test_email_intents_empty_without_email_channel():
    change = {"collection": "tasks", "action": "update", "record_id": "t1", "changed_fields": ["status", "assigned_to"],
              "after": {"status": "assigned", "assigned_to": "7", "title": "T"}, "actor": "1"}
    assert object_notify.email_intents_for_change(_rule(), change, base_dir="/x") == []  # _rule() is in_app only


def test_recipient_modes_users_and_owner_and_field():
    change = {"collection": "tasks", "action": "create", "record_id": "t1", "changed_fields": [],
              "after": {"owner_id": "3", "assigned_to": "7"}, "actor": "z"}
    ch = dict(event_pattern="tasks.record.created",
              channels=json.dumps([{"channel": "in_app", "body_template": "x"}]), match="", suppress_self="false")
    owner = object_notify.notifications_for_change(dict(ch, recipients=json.dumps({"mode": "owner"})), change, base_dir="/x")
    assert owner[0]["user_id"] == "3"
    field = object_notify.notifications_for_change(dict(ch, recipients=json.dumps({"mode": "field", "field": "assigned_to"})), change, base_dir="/x")
    assert field[0]["user_id"] == "7"
    users = object_notify.notifications_for_change(dict(ch, recipients=json.dumps({"mode": "users", "user_ids": ["a", "b"]})), change, base_dir="/x")
    assert {n["user_id"] for n in users} == {"a", "b"}


def test_template_renders_fields_missing_is_blank_not_error():
    assert object_notify.render_template("You got {title} ({urgency})", {"title": "T", "urgency": "hi"}) == "You got T (hi)"
    assert object_notify.render_template("due {due_date}", {}) == "due "  # missing -> blank, no KeyError


# ---- daemon pass end-to-end ----------------------------------------------

def _install(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for pkg in ("app-tasks", "app-collab", "app-notify", "app-projects"):
        object_packages.install_package(pkg, root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root], allow_replace=True)
    return data_dir


def _notes(data_dir):
    return object_records.read_collection_records("notifications", base_dir=data_dir)


def test_daemon_pass_notifies_on_assignment_and_is_idempotent(tmp_path):
    data_dir = _install(tmp_path)
    # first run stamps the cursor at "now" -> never backfills history
    assert object_daemon.process_notifications(base_dir=data_dir) is None
    assert _notes(data_dir) == []

    t = object_records.create_collection_record(
        "tasks", {"title": "Fix bug", "status": "open", "owner_id": "1", "urgency": "high", "due_date": "2026-02-01"},
        base_dir=data_dir, actor="user:1")
    object_records.update_collection_record("tasks", t["id"], {"status": "assigned", "assigned_to": "7"}, base_dir=data_dir, actor="user:1")

    result = object_daemon.process_notifications(base_dir=data_dir)
    assert result["notifications"] == 1
    notes = _notes(data_dir)
    assert notes[0]["user_id"] == "7"
    assert notes[0]["kind"] == "notify"
    assert notes[0]["target"] == f"tasks/{t['id']}"
    assert "Fix bug" in notes[0]["body"] and "high" in notes[0]["body"]

    # re-run: cursor has advanced past the change -> nothing new, no duplicate
    assert object_daemon.process_notifications(base_dir=data_dir)["notifications"] == 0
    assert len(_notes(data_dir)) == 1


def _install_with_email(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for pkg in ("app-tasks", "app-collab", "app-notify", "app-projects", "app-email"):
        object_packages.install_package(pkg, root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root], allow_replace=True)
    return data_dir


def test_daemon_queues_email_via_generic_outbox(tmp_path):
    data_dir = _install_with_email(tmp_path)
    dan = object_identity.create_user(
        {"user_id": "usr_dan", "email": "dan@example.com", "display_name": "Dan"}, base_dir=data_dir)
    object_daemon.process_notifications(base_dir=data_dir)  # stamp cursor at now

    t = object_records.create_collection_record(
        "tasks", {"title": "Fix bug", "status": "open", "owner_id": "1", "urgency": "high", "due_date": "2026-02-01"},
        base_dir=data_dir, actor="user:1")
    object_records.update_collection_record(
        "tasks", t["id"], {"status": "assigned", "assigned_to": dan["user_id"]}, base_dir=data_dir, actor="user:1")

    result = object_daemon.process_notifications(base_dir=data_dir)
    assert result["notifications"] == 1 and result["emails"] == 1
    # the seeded rule's email channel enqueued into the GENERIC outbox
    outbox = object_records.read_collection_records("email_outbox", base_dir=data_dir)
    assert len(outbox) == 1
    row = outbox[0]
    assert row["to"] == "dan@example.com" and row["status"] == "queued"
    assert "Fix bug" in row["subject"] and row["source_object_id"] == "notify"


def test_daemon_skips_email_when_recipient_has_no_address(tmp_path):
    data_dir = _install_with_email(tmp_path)
    ghost = object_identity.create_user(
        {"user_id": "usr_ghost", "display_name": "No Email"}, base_dir=data_dir)  # no email
    object_daemon.process_notifications(base_dir=data_dir)

    t = object_records.create_collection_record(
        "tasks", {"title": "T", "status": "open", "owner_id": "1"}, base_dir=data_dir, actor="user:1")
    object_records.update_collection_record(
        "tasks", t["id"], {"status": "assigned", "assigned_to": ghost["user_id"]}, base_dir=data_dir, actor="user:1")

    result = object_daemon.process_notifications(base_dir=data_dir)
    # in_app still fires; email is skipped (no deliverable address)
    assert result["notifications"] == 1 and result["emails"] == 0
    assert object_records.read_collection_records("email_outbox", base_dir=data_dir) == []


def test_daemon_pass_returns_none_when_no_rules(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    # app-collab (notifications) + app-tasks, but NOT app-notify -> no notify_rules
    for pkg in ("app-tasks", "app-collab", "app-projects"):
        object_packages.install_package(pkg, root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root], allow_replace=True)
    assert object_daemon.process_notifications(base_dir=data_dir) is None
