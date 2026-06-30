import json

import pytest

import object_events
import object_state


def test_publish_event_writes_daemon_compatible_state_and_lists_newest_first(
    tmp_path,
    monkeypatch,
):
    data_dir = tmp_path / "data"
    timestamps = iter([100, 100, 101, 101])
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))

    first = object_events.publish_event(
        "collection.record.created",
        payload={"id": "c1", "nested": {"ok": True}},
        source="records",
        actor="admin",
        base_dir=data_dir,
    )
    second = object_events.publish_event(
        "collection.record.updated",
        payload={"id": "c1"},
        source="records",
        actor="admin",
        base_dir=data_dir,
    )

    state_file = data_dir / "state" / "events" / "state.tsv"
    rows = state_file.read_text().splitlines()

    assert len(rows) == 2
    assert all(row.startswith("event_") for row in rows)
    assert json.loads(rows[0].split("\t")[1]) == first
    assert json.loads(rows[1].split("\t")[1]) == second

    payload = object_events.list_events(base_dir=data_dir)

    assert payload["count"] == 2
    assert payload["total"] == 2
    assert [event["event_type"] for event in payload["events"]] == [
        "collection.record.updated",
        "collection.record.created",
    ]


def test_list_events_filters_event_type_since_and_paginates(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    timestamps = iter([100, 100, 200, 200, 300, 300])
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))

    object_events.publish_event("invoice.created", payload={"id": "i1"}, base_dir=data_dir)
    object_events.publish_event("invoice.paid", payload={"id": "i1"}, base_dir=data_dir)
    object_events.publish_event("invoice.created", payload={"id": "i2"}, base_dir=data_dir)

    payload = object_events.list_events(
        event_type="invoice.created",
        since=100,
        base_dir=data_dir,
        limit=1,
        offset=1,
    )

    assert payload["event_type"] == "invoice.created"
    assert payload["since"] == 100
    assert payload["count"] == 1
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert payload["events"][0]["payload"] == {"id": "i1"}


def test_subscribe_list_and_delete_subscription(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    timestamps = iter([100, 100, 101])
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))

    subscription = object_events.subscribe_event(
        "collection.record.updated",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        actor="admin",
        base_dir=data_dir,
    )

    state_file = data_dir / "state" / "events" / "state.tsv"
    assert state_file.read_text().split("\t", 1)[0] == "sub_collection.record.updated_scroll"
    assert subscription["last_event_id"] is None

    payload = object_events.list_subscriptions(
        event_type="collection.record.updated",
        base_dir=data_dir,
    )

    assert payload["count"] == 1
    assert payload["subscriptions"] == [subscription]

    deleted = object_events.delete_subscription(
        "collection.record.updated",
        "scroll",
        base_dir=data_dir,
    )

    assert deleted == subscription
    assert object_events.list_subscriptions(base_dir=data_dir)["subscriptions"] == []


def test_prune_events_removes_old_overflow_and_corrupt_rows_but_keeps_cursor(
    tmp_path,
    monkeypatch,
):
    data_dir = tmp_path / "data"
    timestamps = iter([100, 100, 200, 200, 300, 300, 400, 400, 500, 500, 600])
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))

    protected = object_events.publish_event("invoice.created", payload={"id": "old"}, base_dir=data_dir)
    old_unprotected = object_events.publish_event(
        "invoice.created",
        payload={"id": "old_unprotected"},
        base_dir=data_dir,
    )
    newest = object_events.publish_event("invoice.created", payload={"id": "newest"}, base_dir=data_dir)
    subscription = object_events.subscribe_event(
        "invoice.created",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=data_dir,
    )

    manager = object_state.ObjectStateManager(object_events.EVENTS_OBJECT_ID, base_dir=data_dir)
    subscription["last_event_id"] = protected["id"]
    manager.set("sub_invoice.created_scroll", json.dumps(subscription))
    manager.set("event_bad", "not-json")

    result = object_events.prune_events(
        base_dir=data_dir,
        keep_count=1,
        keep_seconds=150,
        now=400,
    )

    assert result == {
        "deleted": 2,
        "kept": 2,
        "scanned": 4,
        "protected": 1,
        "corrupt_deleted": 1,
        "keep_count": 1,
        "keep_seconds": 150,
    }

    events = object_events.list_events(base_dir=data_dir)["events"]
    assert [event["id"] for event in events] == [newest["id"], protected["id"]]
    assert old_unprotected["id"] not in {event["id"] for event in events}
    assert object_events.list_subscriptions(base_dir=data_dir)["subscriptions"][0]["last_event_id"] == protected["id"]


def test_publish_event_can_prune_with_retention_options(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    timestamps = iter([100, 100, 200, 200, 300, 300])
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))

    first = object_events.publish_event("invoice.created", payload={"id": "i1"}, base_dir=data_dir)
    second = object_events.publish_event(
        "invoice.created",
        payload={"id": "i2"},
        base_dir=data_dir,
        keep_count=1,
        keep_seconds=0,
    )

    events = object_events.list_events(base_dir=data_dir)["events"]

    assert [event["id"] for event in events] == [second["id"]]
    assert first["id"] not in {event["id"] for event in events}


def test_event_helpers_reject_unsafe_inputs(tmp_path):
    data_dir = tmp_path / "data"

    with pytest.raises(object_events.InvalidEventTypeError):
        object_events.publish_event("bad event", base_dir=data_dir)

    with pytest.raises(object_events.InvalidEventTypeError):
        object_events.publish_event(7, base_dir=data_dir)

    with pytest.raises(object_events.InvalidSubscriberIdError):
        object_events.subscribe_event("invoice.created", subscriber_id="bad/id", base_dir=data_dir)

    with pytest.raises(object_events.InvalidSubscriberIdError):
        object_events.subscribe_event("invoice.created", subscriber_id=7, base_dir=data_dir)

    with pytest.raises(ValueError, match="callback_url"):
        object_events.subscribe_event(
            "invoice.created",
            callback_url="ftp://example.com/hook",
            base_dir=data_dir,
        )

    with pytest.raises(ValueError, match="limit must be at most"):
        object_events.list_events(base_dir=data_dir, limit=1001)

    with pytest.raises(ValueError, match="keep_count must be at least"):
        object_events.prune_events(base_dir=data_dir, keep_count=-1)

    with pytest.raises(ValueError, match="keep_seconds must be at least"):
        object_events.prune_events(base_dir=data_dir, keep_seconds=-1)


def test_event_helpers_ignore_corrupt_state_rows(tmp_path):
    data_dir = tmp_path / "data"
    state_file = data_dir / "state" / "events" / "state.tsv"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        'event_1_evt_ok\t{"id":"evt_ok","event_type":"invoice.created","timestamp":1}\n'
        'event_2_evt_time\t{"id":"evt_time","event_type":"invoice.created","timestamp":"bad"}\n'
        "event_2_evt_bad\tnot-json\n"
        'sub_invoice.created_scroll\t{"id":"scroll","event_type":"invoice.created","created_at":1}\n'
    )

    assert object_events.list_events(base_dir=data_dir, since=1)["events"] == [
        {"id": "evt_ok", "event_type": "invoice.created", "timestamp": 1}
    ]
    assert object_events.list_subscriptions(base_dir=data_dir)["subscriptions"] == [
        {"id": "scroll", "event_type": "invoice.created", "created_at": 1}
    ]
