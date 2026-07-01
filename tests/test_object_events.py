import json
from uuid import UUID

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
    assert UUID(first["id"]).version == 4
    assert UUID(second["id"]).version == 4
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
    assert subscription["delivery"]["status"] == "idle"
    assert subscription["delivery"]["attempts"] == 0

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


def test_subscribe_event_generates_uuid_id_when_missing(tmp_path):
    subscription = object_events.subscribe_event(
        "collection.record.updated",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=tmp_path / "data",
    )

    parsed = UUID(subscription["id"])
    assert parsed.version == 4


def test_record_subscription_delivery_tracks_success_and_failure(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    subscription = object_events.subscribe_event(
        "collection.record.updated",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=data_dir,
    )
    event = {"id": "evt_001", "event_type": "collection.record.updated"}

    failed = object_events.record_subscription_delivery(
        subscription,
        event,
        success=False,
        status_code=500,
        error="callback failed",
        now=100,
    )

    assert failed["last_event_id"] is None
    assert failed["delivery"]["status"] == "failed"
    assert failed["delivery"]["attempts"] == 1
    assert failed["delivery"]["failures"] == 1
    assert failed["delivery"]["last_failure_event_id"] == "evt_001"
    assert failed["delivery"]["last_error"] == "callback failed"

    succeeded = object_events.record_subscription_delivery(
        failed,
        event,
        success=True,
        status_code=204,
        now=101,
    )

    assert succeeded["last_event_id"] == "evt_001"
    assert succeeded["delivery"]["status"] == "ok"
    assert succeeded["delivery"]["attempts"] == 2
    assert succeeded["delivery"]["successes"] == 1
    assert succeeded["delivery"]["last_success_event_id"] == "evt_001"
    assert succeeded["delivery"]["last_status_code"] == 204
    assert succeeded["delivery"]["last_error"] is None


def test_list_event_deliveries_summarizes_pending_and_failed_without_payloads(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    timestamps = iter(range(100, 120))
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))
    first_event = object_events.publish_event(
        "collection.record.updated",
        payload={"secret": "do-not-list"},
        base_dir=data_dir,
    )
    second_event = object_events.publish_event(
        "collection.record.updated",
        payload={"secret": "also-hidden"},
        base_dir=data_dir,
    )
    subscription = object_events.subscribe_event(
        "collection.record.updated",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=data_dir,
    )
    subscription = object_events.record_subscription_delivery(
        subscription,
        first_event,
        success=False,
        status_code=500,
        error="callback down",
        now=100,
    )
    manager = object_state.ObjectStateManager(object_events.EVENTS_OBJECT_ID, base_dir=data_dir)
    manager.set("sub_collection.record.updated_scroll", json.dumps(subscription))

    payload = object_events.list_event_deliveries(
        event_type="collection.record.updated",
        delivery_status="failed",
        pending=True,
        base_dir=data_dir,
    )

    assert payload["count"] == 1
    delivery = payload["deliveries"][0]
    assert delivery["subscriber_id"] == "scroll"
    assert delivery["callback_url_present"] is True
    assert "callback_url" not in delivery
    assert delivery["pending"] is True
    assert delivery["pending_count"] == 2
    assert delivery["next_pending_event"]["id"] == first_event["id"]
    assert delivery["latest_pending_event"]["id"] == second_event["id"]
    assert "payload" not in delivery["next_pending_event"]
    assert delivery["delivery"]["status"] == "failed"
    assert delivery["delivery"]["last_error"] == "callback down"


def test_list_event_deliveries_can_include_callback_and_limited_event_summaries(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    timestamps = iter(range(100, 120))
    monkeypatch.setattr(object_events.time, "time", lambda: next(timestamps))
    first_event = object_events.publish_event("invoice.created", payload={"id": "i1"}, base_dir=data_dir)
    object_events.publish_event("invoice.created", payload={"id": "i2"}, base_dir=data_dir)
    subscription = object_events.subscribe_event(
        "invoice.created",
        subscriber_id="billing",
        callback_url="https://example.com/hooks/billing",
        base_dir=data_dir,
    )
    subscription = object_events.record_subscription_delivery(
        subscription,
        first_event,
        success=True,
        status_code=204,
        now=100,
    )
    manager = object_state.ObjectStateManager(object_events.EVENTS_OBJECT_ID, base_dir=data_dir)
    manager.set("sub_invoice.created_billing", json.dumps(subscription))

    payload = object_events.list_event_deliveries(
        event_type="invoice.created",
        pending=True,
        include_callback_url=True,
        include_events=True,
        event_limit=1,
        base_dir=data_dir,
    )

    assert payload["count"] == 1
    delivery = payload["deliveries"][0]
    assert delivery["callback_url"] == "https://example.com/hooks/billing"
    assert delivery["pending_count"] == 1
    assert len(delivery["pending_events"]) == 1
    assert "payload" not in delivery["pending_events"][0]
    assert delivery["pending_events"][0]["id"] != first_event["id"]


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


def test_prune_events_keeps_failed_delivery_attempt(tmp_path):
    data_dir = tmp_path / "data"

    failed_event = object_events.publish_event("invoice.created", payload={"id": "failed"}, base_dir=data_dir)
    old_unprotected = object_events.publish_event("invoice.created", payload={"id": "old"}, base_dir=data_dir)
    subscription = object_events.subscribe_event(
        "invoice.created",
        subscriber_id="scroll",
        callback_url="https://example.com/hooks/dbbasic",
        base_dir=data_dir,
    )
    subscription = object_events.record_subscription_delivery(
        subscription,
        failed_event,
        success=False,
        error="callback down",
        now=102,
    )
    manager = object_state.ObjectStateManager(object_events.EVENTS_OBJECT_ID, base_dir=data_dir)
    manager.set("sub_invoice.created_scroll", json.dumps(subscription))

    result = object_events.prune_events(
        base_dir=data_dir,
        keep_count=None,
        keep_seconds=1,
        now=max(failed_event["timestamp"], old_unprotected["timestamp"]) + 10,
    )

    assert result["protected"] == 1
    events = object_events.list_events(base_dir=data_dir)["events"]
    assert [event["id"] for event in events] == [failed_event["id"]]
    assert old_unprotected["id"] not in {event["id"] for event in events}


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
    subscriptions = object_events.list_subscriptions(base_dir=data_dir)["subscriptions"]
    assert len(subscriptions) == 1
    assert subscriptions[0]["id"] == "scroll"
    assert subscriptions[0]["event_type"] == "invoice.created"
    assert subscriptions[0]["created_at"] == 1
    assert subscriptions[0]["last_event_id"] is None
    assert subscriptions[0]["delivery"]["status"] == "idle"
