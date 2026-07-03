"""Tests for the operational event log."""

import pytest

import object_ops_log


def test_append_and_read_events(tmp_path):
    object_ops_log.append_event(
        object_ops_log.EXECUTION_ERROR,
        {"object_id": "site_home", "error_type": "ValueError", "message": "boom"},
        base_dir=tmp_path,
    )
    object_ops_log.append_event(
        object_ops_log.AUTH,
        {"event": "login_failed", "identifier": "alice@example.com", "auth_method": "password"},
        base_dir=tmp_path,
    )

    events = object_ops_log.read_events(base_dir=tmp_path)

    assert len(events) == 2
    assert events[0]["kind"] == "auth"
    assert events[0]["event"] == "login_failed"
    assert events[1]["kind"] == "execution_error"
    assert events[1]["object_id"] == "site_home"
    assert events[0]["timestamp"].endswith("Z")


def test_filters_by_kind_event_and_identifier(tmp_path):
    for index in range(3):
        object_ops_log.append_event(
            object_ops_log.AUTH,
            {"event": "login_failed", "identifier": f"user-{index}"},
            base_dir=tmp_path,
        )
    object_ops_log.append_event(
        object_ops_log.AUTH,
        {"event": "login_succeeded", "user_id": "user-1"},
        base_dir=tmp_path,
    )

    failures = object_ops_log.read_events(base_dir=tmp_path, kind="auth", event="login_failed")
    one_user = object_ops_log.read_events(base_dir=tmp_path, identifier="user-1")

    assert len(failures) == 3
    assert all(entry["event"] == "login_failed" for entry in failures)
    assert len(one_user) == 1
    assert one_user[0]["identifier"] == "user-1"


def test_events_are_capped(tmp_path):
    for index in range(7):
        object_ops_log.append_event(
            object_ops_log.AUTH,
            {"event": "logout", "user_id": str(index)},
            base_dir=tmp_path,
            max_rows=4,
        )

    events = object_ops_log.read_events(base_dir=tmp_path, limit=100)

    assert len(events) == 4
    assert [entry["user_id"] for entry in events] == ["6", "5", "4", "3"]


def test_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        object_ops_log.append_event("mystery", {}, base_dir=tmp_path)
    with pytest.raises(ValueError):
        object_ops_log.read_events(base_dir=tmp_path, kind="mystery")
    assert object_ops_log.read_events(base_dir=tmp_path) == []
