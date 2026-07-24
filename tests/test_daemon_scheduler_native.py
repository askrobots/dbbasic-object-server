"""The daemon's scheduler pass on the NATIVE runtime.

Found live 2026-07-24: the droplet daemon printed "Object runtime: NOT
installed (scheduler/queue/events passes disabled)" -- process_scheduler
only knew the legacy dbbasic_object_core runtime, so scheduled tasks
(invoice aging, recurring journals) had NO working schedule path in
production, the exact q9 failure mode (auto-approve coded, never
scheduled). The daemon now falls back to python_object_runtime; these
tests pin that the whole pass -- trigger state read, due-task execution,
bookkeeping -- works on it.
"""

import json
import time

import pytest

import object_daemon
from python_object_runtime import PythonObjectRuntime


def setup_env(tmp_path, monkeypatch):
    objects_dir = tmp_path / "objects"
    (objects_dir / "triggers").mkdir(parents=True)
    (objects_dir / "triggers" / "scheduler.py").write_text(
        "def POST(request):\n    return {'ok': True}\n")
    (objects_dir / "action").mkdir()
    (objects_dir / "action" / "probe.py").write_text(
        "import os\n"
        "def POST(request):\n"
        "    with open(os.environ['DBBASIC_DATA_DIR'] + '/probe.txt', 'w') as fh:\n"
        "        fh.write(str(request.get('mark')))\n"
        "    return {'ok': True}\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return objects_dir, data_dir


def register(objects_dir, key, task):
    runtime = PythonObjectRuntime()
    obj = runtime.load_object(objects_dir / "triggers" / "scheduler.py")
    obj.state_manager.set(key, json.dumps(task))


def read_task(objects_dir, key):
    runtime = PythonObjectRuntime()
    obj = runtime.load_object(objects_dir / "triggers" / "scheduler.py")
    obj.state_manager.reload()
    return json.loads(obj.state_manager.get(key))


def test_due_onetime_task_executes_target_and_completes(tmp_path, monkeypatch):
    objects_dir, data_dir = setup_env(tmp_path, monkeypatch)
    register(objects_dir, "task_probe", {
        "id": "probe-once", "object_id": "action_probe", "method": "POST",
        "payload": {"mark": "ran-by-scheduler"},
        "schedule": "2020-01-01T00:00:00+00:00", "type": "onetime",
        "status": "active",
    })
    object_daemon.process_scheduler(PythonObjectRuntime())
    assert (data_dir / "probe.txt").read_text() == "ran-by-scheduler"
    task = read_task(objects_dir, "task_probe")
    assert task["status"] == "completed"
    assert task["run_count"] == 1
    assert task["next_run"] is None


def test_future_task_is_left_alone(tmp_path, monkeypatch):
    objects_dir, data_dir = setup_env(tmp_path, monkeypatch)
    register(objects_dir, "task_future", {
        "id": "probe-later", "object_id": "action_probe", "method": "POST",
        "payload": {}, "schedule": "2099-01-01T00:00:00+00:00",
        "type": "onetime", "status": "active",
    })
    object_daemon.process_scheduler(PythonObjectRuntime())
    assert not (data_dir / "probe.txt").exists()
    task = read_task(objects_dir, "task_future")
    assert task.get("run_count", 0) == 0
    assert task["status"] == "active"


def test_cron_task_runs_and_reschedules(tmp_path, monkeypatch):
    pytest.importorskip("croniter")
    objects_dir, data_dir = setup_env(tmp_path, monkeypatch)
    register(objects_dir, "task_cron", {
        "id": "probe-cron", "object_id": "action_probe", "method": "POST",
        "payload": {"mark": "cron"}, "schedule": "* * * * *", "type": "cron",
        "status": "active",
        # Pre-set next_run in the past so the pass fires immediately (the
        # daemon normally computes it on first sight, which lands in the
        # future for a fresh cron task).
        "next_run": int(time.time()) - 60,
    })
    object_daemon.process_scheduler(PythonObjectRuntime())
    assert (data_dir / "probe.txt").read_text() == "cron"
    task = read_task(objects_dir, "task_cron")
    assert task["status"] == "active"          # cron tasks stay active
    assert task["run_count"] == 1
    assert task["next_run"] > int(time.time()) - 5   # rescheduled forward


def test_inactive_and_malformed_entries_are_skipped(tmp_path, monkeypatch):
    objects_dir, data_dir = setup_env(tmp_path, monkeypatch)
    register(objects_dir, "task_paused", {
        "id": "paused", "object_id": "action_probe", "method": "POST",
        "payload": {}, "schedule": "2020-01-01T00:00:00+00:00",
        "type": "onetime", "status": "paused",
    })
    runtime = PythonObjectRuntime()
    obj = runtime.load_object(objects_dir / "triggers" / "scheduler.py")
    obj.state_manager.set("task_garbage", "not json")
    object_daemon.process_scheduler(PythonObjectRuntime())
    assert not (data_dir / "probe.txt").exists()
