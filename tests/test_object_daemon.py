import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import object_record_changes
import object_records

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("object_daemon", ROOT / "object_daemon.py")
object_daemon = importlib.util.module_from_spec(spec)
sys.modules["object_daemon"] = object_daemon
assert spec.loader is not None
spec.loader.exec_module(object_daemon)


def write_append_schema(data_dir: Path, collection: str, fields: list[dict] | None = None) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields or [{"name": "id"}], "storage": "append"}))
    return path


class FakeStateManager:
    def __init__(self):
        self.values = {}

    def reload(self):
        return None

    def get_all(self):
        return dict(self.values)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeObject:
    def __init__(self):
        self.state_manager = FakeStateManager()
        self.calls = []

    def execute(self, method, payload):
        self.calls.append((method, payload))
        return {"status": "ok"}


class FailingObject(FakeObject):
    def execute(self, method, payload):
        self.calls.append((method, payload))
        raise RuntimeError("target failed")


class FakeRuntime:
    def __init__(self):
        self.objects = {}
        self.loaded = []

    def add(self, object_id, obj):
        self.objects[object_id] = obj
        return obj

    def load_object(self, path, object_id=None):
        resolved_id = object_id or Path(path).stem
        self.loaded.append((resolved_id, Path(path)))
        return self.objects[resolved_id]


def write_object(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def GET(request):\n    return {'status': 'ok'}\n")
    return path


def test_daemon_imports_without_runtime_package():
    assert callable(object_daemon.process_queue)
    assert callable(object_daemon.process_events)
    assert callable(object_daemon.cleanup_ratelimit)
    assert callable(object_daemon.cleanup_events)


def test_calculate_onetime_next_run():
    task = {"type": "onetime", "schedule": "2026-01-02T03:04:05Z"}

    next_run = object_daemon._calculate_next_run(task)

    assert isinstance(next_run, int)
    assert next_run > 0


def test_cleanup_ratelimit_removes_expired_and_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ratelimit_dir = tmp_path / "data" / "ratelimit"
    ratelimit_dir.mkdir(parents=True)

    now = time.time()
    expired = ratelimit_dir / "expired.txt"
    recent = ratelimit_dir / "recent.txt"
    corrupt = ratelimit_dir / "corrupt.txt"
    ignored = ratelimit_dir / "ignored.log"

    expired.write_text(f"{now - 500}\n")
    recent.write_text(f"{now}\n")
    corrupt.write_text("not-a-timestamp\n")
    ignored.write_text(f"{now - 500}\n")

    object_daemon.cleanup_ratelimit(max_age=120)

    assert not expired.exists()
    assert recent.exists()
    assert not corrupt.exists()
    assert ignored.exists()


def test_cleanup_events_prunes_event_queue(tmp_path):
    data_dir = tmp_path / "data"
    object_daemon.object_events.publish_event(
        "test.event",
        payload={"id": "first"},
        base_dir=data_dir,
    )
    object_daemon.object_events.publish_event(
        "test.event",
        payload={"id": "second"},
        base_dir=data_dir,
    )

    result = object_daemon.cleanup_events(base_dir=data_dir, keep_count=1, keep_seconds=0)

    assert result["deleted"] == 1
    assert object_daemon.object_events.list_events(base_dir=data_dir)["total"] == 1


def test_process_queue_completes_pending_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_object(tmp_path / "objects" / "triggers" / "queue.py")
    write_object(tmp_path / "objects" / "basics" / "target.py")

    runtime = FakeRuntime()
    queue = runtime.add("queue", FakeObject())
    target = runtime.add("basics_target", FakeObject())

    now = int(time.time())
    key = "msg_tasks_2_1_msg_001"
    queue.state_manager.set(
        key,
        json.dumps(
            {
                "id": "msg_001",
                "queue_name": "tasks",
                "message": {
                    "object_id": "basics_target",
                    "method": "POST",
                    "payload": {"action": "test"},
                },
                "priority_level": 2,
                "status": "pending",
                "created_at": now - 10,
                "visible_after": now - 10,
                "expires_at": now + 3600,
                "attempts": 0,
                "max_attempts": 3,
            }
        ),
    )

    object_daemon.process_queue(runtime)

    updated = json.loads(queue.state_manager.get(key))
    assert updated["status"] == "completed"
    assert target.calls == [("POST", {"action": "test"})]


def test_process_queue_marks_expired_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_object(tmp_path / "objects" / "triggers" / "queue.py")

    runtime = FakeRuntime()
    queue = runtime.add("queue", FakeObject())

    now = int(time.time())
    key = "msg_tasks_2_1_expired"
    queue.state_manager.set(
        key,
        json.dumps(
            {
                "id": "expired",
                "queue_name": "tasks",
                "message": {"object_id": "basics_target"},
                "priority_level": 2,
                "status": "pending",
                "created_at": now - 100,
                "visible_after": now - 100,
                "expires_at": now - 1,
                "attempts": 0,
                "max_attempts": 3,
            }
        ),
    )

    object_daemon.process_queue(runtime)

    updated = json.loads(queue.state_manager.get(key))
    assert updated["status"] == "expired"


def test_find_object_file_honors_configured_objects_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    custom_root = tmp_path / "custom_objects"
    custom_path = write_object(custom_root / "basics" / "counter.py")
    write_object(tmp_path / "objects" / "basics" / "counter.py")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(custom_root))

    found = object_daemon._find_object_file("basics_counter")

    assert found == custom_path


def test_find_object_file_resolves_user_object_from_objects_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    user_path = write_object(tmp_path / "objects" / "users" / "42" / "deals.py")

    found = object_daemon._find_object_file("u_42_deals")

    assert found is not None
    assert found.resolve() == user_path.resolve()


def test_process_queue_requeues_failed_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_object(tmp_path / "objects" / "triggers" / "queue.py")
    write_object(tmp_path / "objects" / "basics" / "target.py")

    runtime = FakeRuntime()
    queue = runtime.add("queue", FakeObject())
    runtime.add("basics_target", FailingObject())

    now = int(time.time())
    key = "msg_tasks_2_1_retry"
    queue.state_manager.set(
        key,
        json.dumps(
            {
                "id": "retry",
                "queue_name": "tasks",
                "message": {"object_id": "basics_target", "method": "POST", "payload": {}},
                "priority_level": 2,
                "status": "pending",
                "created_at": now - 10,
                "visible_after": now - 10,
                "expires_at": now + 3600,
                "attempts": 0,
                "max_attempts": 3,
            }
        ),
    )

    object_daemon.process_queue(runtime)

    updated = json.loads(queue.state_manager.get(key))
    assert updated["status"] == "pending"
    assert updated["attempts"] == 1
    assert updated["visible_after"] > now


def test_process_events_delivers_matching_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_object(tmp_path / "objects" / "triggers" / "events.py")

    runtime = FakeRuntime()
    events = runtime.add("events", FakeObject())

    now = int(time.time())
    events.state_manager.set(
        "sub_test.event_sub_001",
        json.dumps(
            {
                "id": "sub_001",
                "event_type": "test.event",
                "callback_url": "http://127.0.0.1:9999/hook",
                "last_event_id": None,
            }
        ),
    )
    events.state_manager.set(
        f"event_{now}_evt_001",
        json.dumps(
            {
                "id": "evt_001",
                "event_type": "test.event",
                "payload": {"value": 1},
                "timestamp": now,
            }
        ),
    )

    delivered = []

    class FakeResponse:
        def getcode(self):
            return 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout):
        delivered.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(object_daemon.urllib.request, "urlopen", fake_urlopen)

    object_daemon.process_events(runtime)

    assert len(delivered) == 1
    request, timeout = delivered[0]
    assert request.full_url == "http://127.0.0.1:9999/hook"
    assert timeout == 5
    updated = json.loads(events.state_manager.get("sub_test.event_sub_001"))
    assert updated["last_event_id"] == "evt_001"
    assert updated["delivery"]["status"] == "ok"
    assert updated["delivery"]["attempts"] == 1
    assert updated["delivery"]["successes"] == 1
    assert updated["delivery"]["last_status_code"] == 204


def test_process_events_records_failure_without_advancing_cursor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_object(tmp_path / "objects" / "triggers" / "events.py")

    runtime = FakeRuntime()
    events = runtime.add("events", FakeObject())

    now = int(time.time())
    events.state_manager.set(
        "sub_test.event_sub_001",
        json.dumps(
            {
                "id": "sub_001",
                "event_type": "test.event",
                "callback_url": "http://127.0.0.1:9999/hook",
                "last_event_id": None,
            }
        ),
    )
    for index in (1, 2):
        events.state_manager.set(
            f"event_{now + index}_evt_00{index}",
            json.dumps(
                {
                    "id": f"evt_00{index}",
                    "event_type": "test.event",
                    "payload": {"value": index},
                    "timestamp": now + index,
                }
            ),
        )

    attempts = []

    def fake_urlopen(request, timeout):
        attempts.append((request, timeout))
        raise object_daemon.urllib.error.URLError("callback down")

    monkeypatch.setattr(object_daemon.urllib.request, "urlopen", fake_urlopen)

    object_daemon.process_events(runtime)

    assert len(attempts) == 1
    updated = json.loads(events.state_manager.get("sub_test.event_sub_001"))
    assert updated["last_event_id"] is None
    assert updated["delivery"]["status"] == "failed"
    assert updated["delivery"]["attempts"] == 1
    assert updated["delivery"]["failures"] == 1
    assert updated["delivery"]["last_attempted_event_id"] == "evt_001"
    assert "callback down" in updated["delivery"]["last_error"]


# --- Compaction pass (docs/append-only-storage-design.md Compaction) --------


def _seed_bloated_append_collection(data_dir: Path, collection: str, *, live: int = 1, churn: int = 5) -> None:
    """Create `collection` in append storage, then churn one record
    `churn` times so physical rows pile up well past `live`."""
    write_append_schema(data_dir, collection, [{"name": "id"}, {"name": "value"}])
    for i in range(live):
        object_records.create_collection_record(
            collection, {"id": f"r{i}", "value": "v0"}, base_dir=data_dir, roots=[]
        )
    for i in range(churn):
        object_records.update_collection_record(
            collection, "r0", {"value": f"v{i}"}, base_dir=data_dir, roots=[]
        )


def test_process_compactions_compacts_collections_over_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_COMPACTION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DBBASIC_APPEND_COMPACT_MIN_ROWS", "3")
    monkeypatch.setenv("DBBASIC_COMPACTION_BLOAT_RATIO", "1.0")
    data_dir = tmp_path / "data"

    # "hot": 1 live + 5 updates = 6 physical rows, 1 live -> well over both
    # the row floor and the bloat ratio.
    _seed_bloated_append_collection(data_dir, "hot", live=1, churn=5)
    # "cold": 2 live rows, no churn -> under DBBASIC_APPEND_COMPACT_MIN_ROWS
    # even though (trivially) bloat_ratio is 0, so it must be left alone.
    write_append_schema(data_dir, "cold", [{"name": "id"}, {"name": "value"}])
    object_records.create_collection_record("cold", {"id": "c1", "value": "v"}, base_dir=data_dir, roots=[])
    object_records.create_collection_record("cold", {"id": "c2", "value": "v"}, base_dir=data_dir, roots=[])

    hot_path = object_records.collection_records_file("hot", base_dir=data_dir)
    cold_path = object_records.collection_records_file("cold", base_dir=data_dir)
    hot_rows_before = len(hot_path.read_text().splitlines()) - 1
    cold_rows_before = len(cold_path.read_text().splitlines()) - 1
    assert hot_rows_before == 6
    assert cold_rows_before == 2

    result = object_daemon.process_compactions(base_dir=data_dir)

    assert result is not None
    assert [entry["collection"] for entry in result["compacted"]] == ["hot"]

    hot_rows_after = len(hot_path.read_text().splitlines()) - 1
    cold_rows_after = len(cold_path.read_text().splitlines()) - 1
    assert hot_rows_after == 1  # compacted down to its one live record
    assert cold_rows_after == cold_rows_before  # untouched: under the row floor


def test_process_compactions_honors_interval_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_APPEND_COMPACT_MIN_ROWS", "3")
    monkeypatch.setenv("DBBASIC_COMPACTION_BLOAT_RATIO", "1.0")
    monkeypatch.setenv("DBBASIC_COMPACTION_INTERVAL_SECONDS", "3600")
    data_dir = tmp_path / "data"
    _seed_bloated_append_collection(data_dir, "hot", live=1, churn=5)
    path = object_records.collection_records_file("hot", base_dir=data_dir)

    # First call: no marker yet -> runs and compacts.
    first = object_daemon.process_compactions(base_dir=data_dir)
    assert first is not None
    assert [entry["collection"] for entry in first["compacted"]] == ["hot"]
    rows_after_first = len(path.read_text().splitlines()) - 1
    assert rows_after_first == 1

    # Re-bloat the file directly (bypassing the pass) and call again
    # immediately: the marker was just written, so the interval hasn't
    # elapsed and this call must be a pure no-op -- no listing, no compaction.
    for i in range(5):
        object_records.update_collection_record(
            "hot", "r0", {"value": f"w{i}"}, base_dir=data_dir, roots=[]
        )
    rows_before_second = len(path.read_text().splitlines()) - 1
    assert rows_before_second > 1

    second = object_daemon.process_compactions(base_dir=data_dir)
    assert second is None
    rows_after_second = len(path.read_text().splitlines()) - 1
    assert rows_after_second == rows_before_second  # untouched


def test_process_compactions_one_collection_failing_does_not_stop_others(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_COMPACTION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DBBASIC_APPEND_COMPACT_MIN_ROWS", "3")
    monkeypatch.setenv("DBBASIC_COMPACTION_BLOAT_RATIO", "1.0")
    data_dir = tmp_path / "data"
    _seed_bloated_append_collection(data_dir, "bad", live=1, churn=5)
    _seed_bloated_append_collection(data_dir, "good", live=1, churn=5)

    real_compact = object_records.compact_collection

    def flaky_compact(collection, **kwargs):
        if collection == "bad":
            raise RuntimeError("simulated compaction failure")
        return real_compact(collection, **kwargs)

    monkeypatch.setattr(object_records, "compact_collection", flaky_compact)

    result = object_daemon.process_compactions(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 2
    assert [entry["collection"] for entry in result["compacted"]] == ["good"]

    good_path = object_records.collection_records_file("good", base_dir=data_dir)
    bad_path = object_records.collection_records_file("bad", base_dir=data_dir)
    assert len(good_path.read_text().splitlines()) - 1 == 1  # compacted
    assert len(bad_path.read_text().splitlines()) - 1 == 6  # left as-is after its failure


# --- Stale-state auto-transition pass ----------------------------------


def write_tasks_schema(data_dir: Path) -> Path:
    """A trimmed stand-in for packages/app-tasks/schemas/tasks.json: same
    status enum/transitions and the same lone `created_at` timestamp
    field (no `updated_at`), classic (non-append) storage."""
    path = data_dir / "schemas" / "tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fields": [
            {"name": "id"},
            {"name": "title", "type": "text", "required": True},
            {
                "name": "status",
                "type": "enum",
                "default": "open",
                "enum": ["draft", "open", "assigned", "waiting_on_client",
                          "approved", "disputed", "cancelled"],
                "transitions": {
                    "draft": ["open", "assigned", "cancelled"],
                    "open": ["assigned", "cancelled"],
                    "assigned": ["waiting_on_client", "open", "cancelled"],
                    "waiting_on_client": ["approved", "disputed", "assigned"],
                    "disputed": ["assigned", "cancelled"],
                },
            },
            {"name": "created_at", "type": "datetime", "read_only": True},
        ],
    }))
    return path


def _backdate_change_log(data_dir: Path, collection: str, record_id: str, *, hours_ago: float) -> None:
    """Rewrite every record-change log entry for `record_id` to look
    `hours_ago` old, so a record created "now" can still exercise the
    stale-transition age check without sleeping in a test."""
    path = object_record_changes.record_changes_file(collection, base_dir=data_dir)
    past = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("record_id") == record_id:
            entry["timestamp"] = past
        rewritten.append(json.dumps(entry))
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_auto_transition_rules_from_env_defaults_to_tasks_rule(monkeypatch):
    # Unscheduled by default was exactly the predecessor bug (200+ records
    # stranded in waiting_on_client) -- the default here must be a real,
    # working rule, not an empty list a caller has to opt into.
    monkeypatch.delenv("DBBASIC_AUTO_TRANSITION_RULES", raising=False)
    rules = object_daemon.auto_transition_rules_from_env()
    assert rules == [{
        "collection": "tasks",
        "field": "status",
        "from": "waiting_on_client",
        "to": "approved",
        "after_hours": 48.0,
    }]


def test_auto_transition_rules_from_env_empty_string_disables(monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_RULES", "")
    assert object_daemon.auto_transition_rules_from_env() == []
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_RULES", "[]")
    assert object_daemon.auto_transition_rules_from_env() == []


def test_process_stale_transitions_moves_stale_record_with_daemon_actor(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "0")
    data_dir = tmp_path / "data"
    write_tasks_schema(data_dir)
    object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Stale task", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    _backdate_change_log(data_dir, "tasks", "t1", hours_ago=50)

    result = object_daemon.process_stale_transitions(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 1
    assert result["moved"] == 1
    assert result["rules"][0]["collection"] == "tasks"

    current = object_records.get_collection_record("tasks", "t1", base_dir=data_dir)
    assert current["status"] == "approved"

    changes = object_record_changes.list_record_changes("tasks", record_id="t1", base_dir=data_dir)
    latest = changes["changes"][0]
    assert latest["action"] == "update"
    assert latest["actor"] == "daemon:auto-transition"
    assert latest["after"]["status"] == "approved"


def test_process_stale_transitions_leaves_fresh_record_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "0")
    data_dir = tmp_path / "data"
    write_tasks_schema(data_dir)
    object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Fresh task", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    # No backdating: the record just entered waiting_on_client.

    result = object_daemon.process_stale_transitions(base_dir=data_dir)

    assert result is not None
    assert result["moved"] == 0
    current = object_records.get_collection_record("tasks", "t1", base_dir=data_dir)
    assert current["status"] == "waiting_on_client"


def test_process_stale_transitions_disabled_by_empty_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_RULES", "[]")
    data_dir = tmp_path / "data"
    write_tasks_schema(data_dir)
    object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Stale task", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    _backdate_change_log(data_dir, "tasks", "t1", hours_ago=200)

    result = object_daemon.process_stale_transitions(base_dir=data_dir)

    assert result == {"checked": 0, "moved": 0, "rules": []}
    current = object_records.get_collection_record("tasks", "t1", base_dir=data_dir)
    assert current["status"] == "waiting_on_client"  # untouched


def test_process_stale_transitions_skips_unknown_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv(
        "DBBASIC_AUTO_TRANSITION_RULES",
        json.dumps([{
            "collection": "not_installed",
            "field": "status",
            "from": "waiting_on_client",
            "to": "approved",
            "after_hours": 48,
        }]),
    )
    data_dir = tmp_path / "data"  # no schema, no records -- package "not installed"

    result = object_daemon.process_stale_transitions(base_dir=data_dir)

    assert result == {
        "checked": 1,
        "moved": 0,
        "rules": [{
            "collection": "not_installed",
            "field": "status",
            "from": "waiting_on_client",
            "to": "approved",
            "moved": 0,
        }],
    }


def test_process_stale_transitions_skips_record_that_moved_on(tmp_path, monkeypatch):
    """A record staged as a stale candidate by the pass's own listing, but
    whose live value has already moved off `from` by the time the pass
    gets to writing it (a race between the listing and the per-record
    write), must be left alone -- not force-moved back to `to`."""
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "0")
    data_dir = tmp_path / "data"
    write_tasks_schema(data_dir)
    object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Racing task", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    _backdate_change_log(data_dir, "tasks", "t1", hours_ago=50)

    stale_snapshot = object_records.read_collection_records("tasks", base_dir=data_dir)

    # The record moves on for real before the pass's write reaches it.
    object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[], actor="tester",
    )

    monkeypatch.setattr(
        object_records,
        "read_collection_records",
        lambda collection, **kwargs: stale_snapshot if collection == "tasks" else [],
    )

    result = object_daemon.process_stale_transitions(base_dir=data_dir)

    assert result["moved"] == 0
    current = object_records.get_collection_record("tasks", "t1", base_dir=data_dir)
    assert current["status"] == "assigned"  # not force-moved to "approved"


def test_process_stale_transitions_honors_interval_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_AUTO_TRANSITION_INTERVAL_SECONDS", "3600")
    data_dir = tmp_path / "data"
    write_tasks_schema(data_dir)
    object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Stale task", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    _backdate_change_log(data_dir, "tasks", "t1", hours_ago=50)

    first = object_daemon.process_stale_transitions(base_dir=data_dir)
    assert first is not None
    assert first["moved"] == 1

    # Put another stale record in place directly (bypassing the pass) and
    # call again immediately: the marker was just written, so the interval
    # hasn't elapsed and this call must be a pure no-op.
    object_records.create_collection_record(
        "tasks",
        {"id": "t2", "title": "Also stale", "status": "waiting_on_client"},
        base_dir=data_dir,
        roots=[],
        actor="tester",
    )
    _backdate_change_log(data_dir, "tasks", "t2", hours_ago=50)

    second = object_daemon.process_stale_transitions(base_dir=data_dir)
    assert second is None
    current = object_records.get_collection_record("tasks", "t2", base_dir=data_dir)
    assert current["status"] == "waiting_on_client"  # untouched


def write_orders_schema(data_dir: Path) -> Path:
    return write_schema(data_dir, "orders", [
        {"name": "id"},
        {"name": "channel", "type": "text"},
        {"name": "status", "type": "text"},
        {"name": "total_cents", "type": "integer"},
        {"name": "created_at", "type": "datetime"},
    ])


def write_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": collection, "fields": fields}))
    return path


def _rollup_definition_row(**overrides) -> dict:
    row = {
        "id": "rollup_orders_by_day",
        "name": "Orders per day",
        "source_collection": "orders",
        "filter": json.dumps({"status": "paid"}),
        "group_by": json.dumps(["channel"]),
        "time_bucket": json.dumps({"field": "created_at", "granularity": "day"}),
        "metrics": json.dumps([{"op": "count", "as": "order_count"}]),
        "target_collection": "rollup_orders_by_day",
        "min_group_size": "",
        "refresh_mode": "scheduled",
        "refresh_interval_seconds": "3600",
        "last_computed_at": "",
        "enabled": "true",
    }
    row.update(overrides)
    return row


def _install_rollup_definitions(data_dir: Path, rows: list[dict]) -> None:
    write_schema(data_dir, "rollup_definitions", [
        {"name": "id"},
        {"name": "name", "type": "text"},
        {"name": "source_collection", "type": "text"},
        {"name": "filter", "type": "textarea"},
        {"name": "group_by", "type": "textarea"},
        {"name": "time_bucket", "type": "textarea"},
        {"name": "metrics", "type": "textarea"},
        {"name": "target_collection", "type": "text"},
        {"name": "min_group_size", "type": "integer"},
        {"name": "refresh_mode", "type": "text"},
        {"name": "refresh_interval_seconds", "type": "integer"},
        {"name": "last_computed_at", "type": "datetime", "read_only": True},
        {"name": "enabled", "type": "boolean"},
    ])
    for row in rows:
        # preserve_read_only: seeding a fixture row directly, same posture
        # as object_import.py replaying another system's history -- a real
        # admin-authored create would omit last_computed_at entirely (the
        # generated form never renders a read_only field), never submit it.
        object_records.create_collection_record(
            "rollup_definitions", row, base_dir=data_dir, roots=[], actor="tester",
            preserve_read_only=True,
        )


def test_process_rollups_computes_a_due_definition_and_stamps_last_computed_at(tmp_path):
    data_dir = tmp_path / "data"
    write_orders_schema(data_dir)
    object_records.create_collection_record(
        "orders", {"id": "o1", "channel": "web", "status": "paid", "total_cents": "1000",
                   "created_at": "2026-07-01T00:00:00Z"},
        base_dir=data_dir, roots=[],
    )
    _install_rollup_definitions(data_dir, [_rollup_definition_row()])

    result = object_daemon.process_rollups(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 1
    assert [c["definition_id"] for c in result["computed"]] == ["rollup_orders_by_day"]

    target_rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert len(target_rows) == 1
    assert target_rows[0]["order_count"] == "1"

    definition = object_records.get_collection_record("rollup_definitions", "rollup_orders_by_day", base_dir=data_dir)
    assert definition["last_computed_at"]  # stamped, non-blank


def test_process_rollups_skips_a_definition_not_yet_due(tmp_path):
    data_dir = tmp_path / "data"
    write_orders_schema(data_dir)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _install_rollup_definitions(data_dir, [
        _rollup_definition_row(last_computed_at=now, refresh_interval_seconds="3600"),
    ])

    result = object_daemon.process_rollups(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 1
    assert result["computed"] == []


def test_process_rollups_skips_a_disabled_definition(tmp_path):
    data_dir = tmp_path / "data"
    write_orders_schema(data_dir)
    _install_rollup_definitions(data_dir, [_rollup_definition_row(enabled="false")])

    result = object_daemon.process_rollups(base_dir=data_dir)

    assert result is not None
    assert result["computed"] == []


def test_process_rollups_one_bad_definition_does_not_stop_others(tmp_path):
    data_dir = tmp_path / "data"
    write_orders_schema(data_dir)
    object_records.create_collection_record(
        "orders", {"id": "o1", "channel": "web", "status": "paid", "total_cents": "1000",
                   "created_at": "2026-07-01T00:00:00Z"},
        base_dir=data_dir, roots=[],
    )
    _install_rollup_definitions(data_dir, [
        _rollup_definition_row(id="bad", target_collection="rollup_bad", source_collection="no_such_collection"),
        _rollup_definition_row(id="good", target_collection="rollup_good"),
    ])

    result = object_daemon.process_rollups(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 2
    assert [c["definition_id"] for c in result["computed"]] == ["good"]
    good_rows = object_records.read_collection_records("rollup_good", base_dir=data_dir, roots=[])
    assert len(good_rows) == 1
    # The bad definition's own last_computed_at is never stamped -- it
    # never successfully computed.
    bad_definition = object_records.get_collection_record("rollup_definitions", "bad", base_dir=data_dir)
    assert bad_definition["last_computed_at"] == ""


def test_process_rollups_returns_none_when_flag_off(tmp_path):
    data_dir = tmp_path / "data"
    write_orders_schema(data_dir)
    write_schema(data_dir, "feature_flags", [
        {"name": "id"}, {"name": "flag", "type": "text"}, {"name": "value", "type": "text"},
    ])
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "rollup_enabled", "value": "off"}, base_dir=data_dir, roots=[],
    )
    _install_rollup_definitions(data_dir, [_rollup_definition_row()])

    result = object_daemon.process_rollups(base_dir=data_dir)

    assert result is None
    # Untouched: no target collection was ever created.
    assert not (data_dir / "collections" / "rollup_orders_by_day").exists()


def test_process_rollups_returns_none_without_rollup_definitions_collection(tmp_path):
    data_dir = tmp_path / "data"
    result = object_daemon.process_rollups(base_dir=data_dir)
    assert result is None


# --- Materialize (plan/vocabulary/61-materialize-spec.md) ------------------

def _materialize_definition_row(**overrides) -> dict:
    row = {
        "id": "matgen_fin_recurring",
        "name": "Recurring journal generation",
        "source_collection": "fin_recurring",
        "source_filter": json.dumps({"is_active": "true"}),
        "trigger": json.dumps({
            "mode": "scheduled", "interval_seconds": 3600,
            "period_field": "frequency", "anchor_field": "next_run",
        }),
        "output_collection": "fin_journals",
        "child_collection": "fin_journal_lines",
        "child_source_field": "template_lines",
        "child_link_field": "journal_id",
        "idempotency_key": "matgen_{definition_id}_{source_id}_{period_start}",
        "mapping": json.dumps({
            "date": {"from_period": "period_start"},
            "description": {"literal": "Recurring"},
            "status": {"literal": "draft"},
            "currency": {"literal": "USD"},
        }),
        "balance_check": json.dumps({"debit_field": "debit_cents", "credit_field": "credit_cents"}),
        "last_run_at": "",
        "actor": "daemon:materialize",
        "enabled": "true",
        "block": "false",
    }
    row.update(overrides)
    return row


def _install_materialize_fixtures(data_dir: Path, definitions: list[dict], recurring_rows: list[dict]) -> None:
    write_schema(data_dir, "materialize_definitions", [
        {"name": "id"},
        {"name": "name", "type": "text"},
        {"name": "source_collection", "type": "text"},
        {"name": "source_filter", "type": "textarea"},
        {"name": "trigger", "type": "textarea"},
        {"name": "output_collection", "type": "text"},
        {"name": "child_collection", "type": "text"},
        {"name": "child_source_field", "type": "text"},
        {"name": "child_link_field", "type": "text"},
        {"name": "idempotency_key", "type": "text"},
        {"name": "mapping", "type": "textarea"},
        {"name": "balance_check", "type": "textarea"},
        {"name": "debit_account_id", "type": "text"},
        {"name": "credit_account_id", "type": "text"},
        {"name": "actor", "type": "text"},
        {"name": "last_run_at", "type": "datetime", "read_only": True},
        {"name": "enabled", "type": "boolean"},
        {"name": "block", "type": "boolean"},
    ])
    write_schema(data_dir, "fin_recurring", [
        {"name": "id"},
        {"name": "name", "type": "text"},
        {"name": "template_lines", "type": "textarea"},
        {"name": "frequency", "type": "text"},
        {"name": "next_run", "type": "date"},
        {"name": "auto_post", "type": "boolean"},
        {"name": "is_active", "type": "boolean"},
    ])
    write_schema(data_dir, "fin_journals", [
        {"name": "id"}, {"name": "date", "type": "date"}, {"name": "description", "type": "text"},
        {"name": "status", "type": "text"}, {"name": "currency", "type": "text"},
    ])
    write_append_schema(data_dir, "fin_journal_lines", [
        {"name": "id"}, {"name": "journal_id", "type": "text"}, {"name": "account_id", "type": "text"},
        {"name": "debit_cents", "type": "integer"}, {"name": "credit_cents", "type": "integer"},
    ])

    for row in recurring_rows:
        object_records.create_collection_record("fin_recurring", row, base_dir=data_dir, roots=[], actor="tester")
    for row in definitions:
        object_records.create_collection_record(
            "materialize_definitions", row, base_dir=data_dir, roots=[], actor="tester",
            preserve_read_only=True,
        )


def _balanced_recurring_row(**overrides) -> dict:
    row = {
        "id": "rec1", "name": "Rent", "frequency": "monthly", "next_run": "2020-01-01",
        "auto_post": "false", "is_active": "true",
        "template_lines": json.dumps([
            {"account_id": "acct_cash", "debit_cents": 100, "credit_cents": 0},
            {"account_id": "acct_rev", "debit_cents": 0, "credit_cents": 100},
        ]),
    }
    row.update(overrides)
    return row


def test_process_materializations_generates_a_due_definition_and_stamps_last_run_at(tmp_path):
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(data_dir, [_materialize_definition_row()], [_balanced_recurring_row()])

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result is not None
    assert result["checked"] == 1
    assert result["generated"] >= 1

    journals = object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])
    assert len(journals) >= 1

    definition = object_records.get_collection_record(
        "materialize_definitions", "matgen_fin_recurring", base_dir=data_dir,
    )
    assert definition["last_run_at"]


def test_process_materializations_skips_a_disabled_definition(tmp_path):
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(
        data_dir, [_materialize_definition_row(enabled="false")], [_balanced_recurring_row()],
    )

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result["generated"] == 0
    assert object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]) == []


def test_process_materializations_refuses_a_blocked_definition(tmp_path):
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(
        data_dir, [_materialize_definition_row(block="true")], [_balanced_recurring_row()],
    )

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result["generated"] == 0
    assert object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]) == []


def test_process_materializations_skips_event_mode_definitions(tmp_path):
    """The scheduled daemon pass never drives an event-mode definition --
    that's materialize_seed's dispatch / materialize_run's job."""
    data_dir = tmp_path / "data"
    event_definition = _materialize_definition_row(
        id="matgen_event", source_collection="fin_recurring", output_collection="fin_recurring",
        trigger=json.dumps({"mode": "event", "on": "record.created"}),
        child_collection="", child_source_field="", child_link_field="",
        idempotency_key="{definition_id}_{source_id}", mapping=json.dumps({}), balance_check="",
    )
    _install_materialize_fixtures(data_dir, [event_definition], [_balanced_recurring_row()])

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result["checked"] == 1
    assert result["generated"] == 0


def test_process_materializations_one_bad_definition_does_not_stop_others(tmp_path):
    data_dir = tmp_path / "data"
    bad = _materialize_definition_row(id="bad", source_collection="no_such_collection")
    good = _materialize_definition_row(id="good")
    _install_materialize_fixtures(data_dir, [bad, good], [_balanced_recurring_row()])

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result["checked"] == 2
    assert result["generated"] >= 1
    good_definition = object_records.get_collection_record("materialize_definitions", "good", base_dir=data_dir)
    assert good_definition["last_run_at"]
    bad_definition = object_records.get_collection_record("materialize_definitions", "bad", base_dir=data_dir)
    assert not bad_definition["last_run_at"]  # never reached the stamp step


def test_process_materializations_returns_none_when_flag_off(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "feature_flags", [
        {"name": "id"}, {"name": "flag", "type": "text"}, {"name": "value", "type": "text"},
    ])
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "materialize_enabled", "value": "off"}, base_dir=data_dir, roots=[],
    )
    _install_materialize_fixtures(data_dir, [_materialize_definition_row()], [_balanced_recurring_row()])

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result is None
    assert object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]) == []


def test_process_materializations_returns_none_without_materialize_definitions_collection(tmp_path):
    data_dir = tmp_path / "data"
    result = object_daemon.process_materializations(base_dir=data_dir)
    assert result is None


def test_process_materializations_honors_interval_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_MATERIALIZE_INTERVAL_SECONDS", "3600")
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(data_dir, [_materialize_definition_row()], [_balanced_recurring_row()])

    first = object_daemon.process_materializations(base_dir=data_dir)
    assert first is not None
    assert first["generated"] >= 1
    journal_count = len(object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[]))

    # Add another due row and call again immediately: the marker was just
    # written, so the interval hasn't elapsed and this call must be a
    # pure no-op (nothing new generated).
    object_records.create_collection_record(
        "fin_recurring", _balanced_recurring_row(id="rec2", name="Rent 2"), base_dir=data_dir, roots=[],
    )
    second = object_daemon.process_materializations(base_dir=data_dir)
    assert second is None
    assert len(object_records.read_collection_records("fin_journals", base_dir=data_dir, roots=[])) == journal_count


def test_process_materializations_no_op_run_still_returns_zeroed_summary(tmp_path):
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(data_dir, [], [])

    result = object_daemon.process_materializations(base_dir=data_dir)

    assert result == {"checked": 0, "generated": 0, "skipped_already_generated": 0, "results": []}


def test_process_materializations_second_call_skips_already_generated(tmp_path, monkeypatch):
    # DBBASIC_MATERIALIZE_INTERVAL_SECONDS gates the PASS's own marker file;
    # is_definition_due (the per-definition gate) reads back last_run_at vs.
    # the definition's own trigger.interval_seconds (3600 in this fixture),
    # so after the first call stamps last_run_at to "now" the definition
    # would look "not due yet" on an immediate second call -- backdate it
    # to isolate what this test actually checks: a due-again definition
    # whose periods were already generated reports skips, not new rows.
    monkeypatch.setenv("DBBASIC_MATERIALIZE_INTERVAL_SECONDS", "0")
    data_dir = tmp_path / "data"
    _install_materialize_fixtures(data_dir, [_materialize_definition_row()], [_balanced_recurring_row()])

    first = object_daemon.process_materializations(base_dir=data_dir)
    assert first["generated"] >= 1

    object_records.update_collection_record(
        "materialize_definitions", "matgen_fin_recurring",
        {"last_run_at": "2000-01-01T00:00:00Z"}, base_dir=data_dir, actor="tester", preserve_read_only=True,
    )

    second = object_daemon.process_materializations(base_dir=data_dir)
    assert second["generated"] == 0
    assert second["skipped_already_generated"] >= 1


def test_daemon_entrypoint_is_runnable_without_optional_runtime():
    """The daemon's main() must start on a bare install: no
    dbbasic_object_core, no croniter -- the storage passes (compaction,
    auto-transitions, cleanups) are stdlib and must not be held hostage by
    an optional runtime import. --help exercises the full import path and
    argparse wiring without entering the loop. (This exact failure shipped:
    main() hard-imported the optional runtime and had never been run.)"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "object_daemon.py", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    assert "Object Primitive Daemon" in result.stdout
