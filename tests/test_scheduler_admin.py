"""The scheduler observability slice (Dan: "celery flower would do better"):
runs recorded as records by the daemon, and the /scheduler admin page --
task board + run history + run_now/pause/resume controls."""

import json
import pathlib
import time

import object_daemon
import object_execution
import object_records
import object_state
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
DASH_OBJECTS = PACKAGES / "system-dashboard" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

ADMIN = {"user_id": "dan", "roles": ["admin"]}
MEMBER = {"user_id": "pat", "roles": []}


def _header_from_schema(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch, *, with_runs_collection=True):
    objects_dir = tmp_path / "objects"
    (objects_dir / "triggers").mkdir(parents=True)
    (objects_dir / "triggers" / "scheduler.py").write_text(
        "def POST(request):\n    return {'ok': True}\n")
    (objects_dir / "action").mkdir()
    (objects_dir / "action" / "probe.py").write_text(
        "def POST(request):\n    return {'flipped': 3}\n")
    (objects_dir / "action" / "broken.py").write_text(
        "def POST(request):\n    raise RuntimeError('boom')\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    if with_runs_collection:
        schema_dir = data_dir / "schemas"
        schema_dir.mkdir()
        (schema_dir / "scheduler_runs.json").write_text(
            (PACKAGES / "system-dashboard" / "schemas" / "scheduler_runs.json").read_text())
        coll = data_dir / "collections" / "scheduler_runs"
        coll.mkdir(parents=True)
        (coll / "records.tsv").write_text(
            _header_from_schema("system-dashboard", "scheduler_runs"))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return objects_dir, data_dir


def register(data_dir, key, task):
    object_state.ObjectStateManager("scheduler", base_dir=data_dir).set(
        key, json.dumps(task))


def due_task(object_id, task_id="t1"):
    return {"id": task_id, "object_id": object_id, "method": "POST",
            "payload": {}, "schedule": "2020-01-01T00:00:00+00:00",
            "type": "onetime", "status": "active"}


def runs(data_dir):
    return object_records.read_collection_records("scheduler_runs", base_dir=data_dir)


def page(method, payload):
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_scheduler", method=method, payload=payload),
        roots=[DASH_OBJECTS])
    return result.result


# --- daemon records runs ------------------------------------------------------

def test_successful_run_is_recorded_with_result(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    register(data_dir, "task_ok", due_task("action_probe"))
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    rows = runs(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["ok"] == "true"
    assert row["task_id"] == "t1"
    assert row["object_id"] == "action_probe"
    assert json.loads(row["result"]) == {"flipped": 3}
    assert row["error"] == ""
    assert int(row["duration_ms"]) >= 0


def test_failed_run_is_recorded_with_error(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    register(data_dir, "task_bad", due_task("action_broken", task_id="t2"))
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    row = runs(data_dir)[0]
    assert row["ok"] == "false"
    assert row["result"] == ""
    assert "boom" in row["error"]
    assert row["error_type"]


def test_missing_runs_collection_never_breaks_the_pass(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch, with_runs_collection=False)
    register(data_dir, "task_ok", due_task("action_probe"))
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    task = json.loads(
        object_state.ObjectStateManager("scheduler", base_dir=data_dir).get("task_ok"))
    assert task["run_count"] == 1  # executed fine, just unrecorded


# --- the /scheduler page ------------------------------------------------------

def test_page_gates_on_admin_identity(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    anon = page("GET", {})
    assert "Sign in" in anon["body"]
    member = page("GET", {"_identity": MEMBER})
    assert "admin role" in member["body"]
    for body in (anon["body"], member["body"]):
        assert "Recent Runs" not in body  # no data leaks below the gate


def test_admin_page_shows_tasks_and_run_history(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    register(data_dir, "task_ok", due_task("action_probe"))
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    body = page("GET", {"_identity": ADMIN})["body"]
    assert "action_probe" in body
    assert "flipped" in body            # result JSON surfaced
    assert "Run now" in body
    assert "Runs (24h)" in body


def test_run_now_marks_task_due_and_next_pass_executes(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    task = due_task("action_probe")
    task["schedule"] = "2099-01-01T00:00:00+00:00"  # far future
    register(data_dir, "task_ok", task)
    denied = page("POST", {"_identity": MEMBER, "action": "run_now", "key": "task_ok"})
    assert denied["status"] == 403
    result = page("POST", {"_identity": ADMIN, "action": "run_now", "key": "task_ok"})
    assert result["status"] == 200
    assert result["next_run"] <= int(time.time())
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    assert len(runs(data_dir)) == 1  # ran despite the far-future schedule


def test_pause_and_resume(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    register(data_dir, "task_ok", due_task("action_probe"))
    paused = page("POST", {"_identity": ADMIN, "action": "pause", "key": "task_ok"})
    assert paused["task_status"] == "paused"
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    assert runs(data_dir) == []  # paused task never runs
    resumed = page("POST", {"_identity": ADMIN, "action": "resume", "key": "task_ok"})
    assert resumed["task_status"] == "active"
    assert resumed["next_run"] is None  # daemon recomputes from the schedule
    object_daemon.process_scheduler(python_object_runtime.PythonObjectRuntime())
    assert len(runs(data_dir)) == 1  # onetime schedule in the past -> due again


def test_post_validates_action_and_key(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert page("POST", {"_identity": ADMIN, "action": "explode", "key": "task_x"})["status"] == 400
    assert page("POST", {"_identity": ADMIN, "action": "run_now", "key": "nope"})["status"] == 400
    assert page("POST", {"_identity": ADMIN, "action": "run_now", "key": "task_missing"})["status"] == 404
