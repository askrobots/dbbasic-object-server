import importlib.util
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("object_daemon", ROOT / "object_daemon.py")
object_daemon = importlib.util.module_from_spec(spec)
sys.modules["object_daemon"] = object_daemon
assert spec.loader is not None
spec.loader.exec_module(object_daemon)


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
