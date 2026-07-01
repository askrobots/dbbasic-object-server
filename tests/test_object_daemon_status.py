import json

import object_daemon_status
import object_state


def test_daemon_status_summarizes_scheduler_queue_and_events(tmp_path):
    objects_dir = tmp_path / "objects"
    triggers_dir = objects_dir / "triggers"
    data_dir = tmp_path / "data"
    rate_limit_dir = data_dir / "ratelimit"
    triggers_dir.mkdir(parents=True)
    rate_limit_dir.mkdir(parents=True)
    (triggers_dir / "scheduler.py").write_text("def POST(request):\n    return {'ok': True}\n")
    (triggers_dir / "queue.py").write_text("def POST(request):\n    return {'ok': True}\n")
    (triggers_dir / "events.py").write_text("def POST(request):\n    return {'ok': True}\n")
    (rate_limit_dir / "127.0.0.1.txt").write_text("200\n")

    scheduler = object_state.ObjectStateManager("scheduler", base_dir=data_dir)
    scheduler.set(
        "task_due",
        json.dumps({"id": "due", "status": "active", "next_run": 190, "type": "onetime"}),
    )
    scheduler.set(
        "task_future",
        json.dumps({"id": "future", "status": "active", "next_run": 250, "type": "onetime"}),
    )
    scheduler.set("task_bad", "{")

    queue = object_state.ObjectStateManager("queue", base_dir=data_dir)
    queue.set(
        "msg_ready",
        json.dumps(
            {
                "id": "ready",
                "status": "pending",
                "visible_after": 100,
                "expires_at": 500,
                "priority_level": 5,
            }
        ),
    )
    queue.set(
        "msg_later",
        json.dumps({"id": "later", "status": "pending", "visible_after": 250}),
    )

    events = object_state.ObjectStateManager("events", base_dir=data_dir)
    events.set(
        "event_180_evt_1",
        json.dumps(
            {
                "id": "evt_1",
                "event_type": "collection.record.created",
                "payload": {"redacted_from_status": True},
                "source": "records",
                "actor": "unit-test",
                "timestamp": 180,
                "created_at": "2026-01-01T00:03:00+00:00",
            }
        ),
    )
    events.set(
        "sub_collection.record.created_scroll",
        json.dumps(
            {
                "id": "scroll",
                "event_type": "collection.record.created",
                "callback_url": "https://example.test/hook",
                "last_event_id": None,
                "delivery": {"status": "failed", "attempts": 1},
            }
        ),
    )

    payload = object_daemon_status.daemon_status(
        base_dir=data_dir,
        object_roots=[objects_dir],
        rate_limit_dir=rate_limit_dir,
        event_keep_count=500,
        event_keep_seconds=3600,
        now=200,
    )

    assert payload["status"] == "degraded"
    assert payload["daemon"]["triggers"]["scheduler"]["source_present"] is True
    assert payload["scheduler"]["tasks"]["active"] == 2
    assert payload["scheduler"]["tasks"]["due"] == 1
    assert payload["scheduler"]["tasks"]["future"] == 1
    assert payload["scheduler"]["tasks"]["invalid"] == 1
    assert payload["scheduler"]["tasks"]["next_run"] == 190
    assert payload["queue"]["messages"]["pending_visible"] == 1
    assert payload["queue"]["messages"]["pending_delayed"] == 1
    assert payload["queue"]["messages"]["next_visible_at"] == 250
    assert payload["events"]["events"]["by_event_type"] == {"collection.record.created": 1}
    assert payload["events"]["events"]["latest"]["id"] == "evt_1"
    assert "payload" not in payload["events"]["events"]["latest"]
    assert payload["events"]["subscriptions"]["pending_deliveries"] == 1
    assert payload["events"]["subscriptions"]["by_delivery_status"] == {"failed": 1}
    assert payload["cleanup"]["event_retention"] == {
        "keep_count": 500,
        "keep_seconds": 3600,
    }
    assert payload["cleanup"]["rate_limit_files"] == 1
