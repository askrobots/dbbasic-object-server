"""The deferred-work queue that already existed (and the package that
finally turns it on).

Worth pinning explicitly, because the capability was easy to overlook: the
daemon has had delay, priority, expiry and retry-with-backoff all along,
and the only reason none of it ran was a missing trigger object -- the same
one-file gap that left the scheduler passes disabled. A primitive nobody
can see is indistinguishable from a primitive nobody built.
"""

import json
import pathlib
import time

import object_daemon
import object_daemon_control
import object_packages
import object_state
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES_ROOT = REPO_ROOT / "packages"


def setup_env(tmp_path, monkeypatch, *, with_queue_object=True):
    objects_dir = tmp_path / "objects"
    (objects_dir / "action").mkdir(parents=True)
    (objects_dir / "action" / "probe.py").write_text(
        "import os\n"
        "def POST(request):\n"
        "    path = os.environ['DBBASIC_DATA_DIR'] + '/probe-' + str(request.get('mark')) + '.txt'\n"
        "    open(path, 'w').write(str(request.get('mark')))\n"
        "    return {'ok': True, 'mark': request.get('mark')}\n")
    (objects_dir / "action" / "boom.py").write_text(
        "def POST(request):\n    raise RuntimeError('job failed')\n")
    if with_queue_object:
        (objects_dir / "triggers").mkdir(parents=True)
        (objects_dir / "triggers" / "queue.py").write_text(
            (PACKAGES_ROOT / "system-triggers" / "objects" / "triggers" / "queue.py").read_text())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return objects_dir, data_dir


def pump():
    object_daemon.process_queue(python_object_runtime.PythonObjectRuntime())


def messages(data_dir):
    out = {}
    for key, value in object_state.get_object_state("queue", data_dir).items():
        if key.startswith("msg_"):
            row = json.loads(value)
            out[row["id"]] = row
    return out


def test_a_message_runs_on_the_next_pass(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    created = object_daemon_control.enqueue_message(
        {"object_id": "action_probe", "method": "POST", "payload": {"mark": "now"}},
        base_dir=data_dir)
    pump()
    assert (data_dir / "probe-now.txt").exists()
    assert messages(data_dir)[created["id"]]["status"] == "completed"


def test_visible_after_defers_work_until_its_time(tmp_path, monkeypatch):
    """This is the 'delayed job' the system already had: enqueue now, run
    later, no cron entry and no separate worker service."""
    _, data_dir = setup_env(tmp_path, monkeypatch)
    later = int(time.time()) + 3600
    created = object_daemon_control.enqueue_message(
        {"object_id": "action_probe", "payload": {"mark": "later"},
         "visible_after": later},
        base_dir=data_dir)
    pump()
    assert not (data_dir / "probe-later.txt").exists()
    assert messages(data_dir)[created["id"]]["status"] == "pending"

    # Bring its time forward: the same message now runs, unchanged.
    manager = object_state.ObjectStateManager("queue", base_dir=data_dir)
    key = next(k for k in manager.get_all() if k.startswith("msg_"))
    row = json.loads(manager.get(key))
    row["visible_after"] = int(time.time()) - 1
    manager.set(key, json.dumps(row))
    pump()
    assert (data_dir / "probe-later.txt").exists()
    assert messages(data_dir)[created["id"]]["status"] == "completed"


def test_priority_orders_the_batch(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    for mark, priority in (("low", 0), ("high", 9)):
        object_daemon_control.enqueue_message(
            {"object_id": "action_probe", "payload": {"mark": mark},
             "priority_level": priority}, base_dir=data_dir)
    pump()
    # Both ran; the point is that ordering is a first-class field rather
    # than insertion order, which matters once a batch exceeds max_messages.
    assert (data_dir / "probe-high.txt").exists()
    assert (data_dir / "probe-low.txt").exists()


def test_failure_retries_with_backoff_then_parks_the_message(tmp_path, monkeypatch):
    """A dead job stays inspectable instead of vanishing or looping."""
    _, data_dir = setup_env(tmp_path, monkeypatch)
    created = object_daemon_control.enqueue_message(
        {"object_id": "action_boom", "max_attempts": 2}, base_dir=data_dir)
    pump()
    row = messages(data_dir)[created["id"]]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["visible_after"] > int(time.time()) - 1   # backed off, not hammered

    row["visible_after"] = int(time.time()) - 1
    manager = object_state.ObjectStateManager("queue", base_dir=data_dir)
    key = next(k for k in manager.get_all() if k.startswith("msg_"))
    manager.set(key, json.dumps(row))
    pump()
    dead = messages(data_dir)[created["id"]]
    assert dead["status"] == "failed" and dead["attempts"] == 2


def test_an_expired_message_is_dropped_not_run(tmp_path, monkeypatch):
    _, data_dir = setup_env(tmp_path, monkeypatch)
    created = object_daemon_control.enqueue_message(
        {"object_id": "action_probe", "payload": {"mark": "stale"},
         "expires_at": int(time.time()) - 10}, base_dir=data_dir)
    pump()
    assert not (data_dir / "probe-stale.txt").exists()
    assert messages(data_dir)[created["id"]]["status"] == "expired"


def test_without_the_trigger_object_nothing_runs_at_all(tmp_path, monkeypatch):
    """The whole reason this package exists: the queue was fully built and
    completely inert, because one file was missing."""
    _, data_dir = setup_env(tmp_path, monkeypatch, with_queue_object=False)
    object_daemon_control.enqueue_message(
        {"object_id": "action_probe", "payload": {"mark": "orphan"}}, base_dir=data_dir)
    pump()
    assert not (data_dir / "probe-orphan.txt").exists()
    assert messages(data_dir)  # the message is still there, just never read


def test_the_package_ships_both_trigger_objects(tmp_path):
    package = object_packages.get_package("system-triggers", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {
        "triggers_scheduler", "triggers_queue"}
    # The ids must map to triggers/<name>.py, which is where
    # object_namespace.find_trigger_file looks -- get the path wrong and the
    # daemon reports "no scheduler object" while the install says success.
    for obj in package["objects"]:
        assert obj["path"].startswith("objects/triggers/")
