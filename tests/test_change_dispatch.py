"""object_change_dispatch: the record-change-log dispatch pass that closes
docs/logic-decisions.md #9 -- a storage-level write (a runner, an action)
bypasses object_server's synchronous HTTP dispatch entirely, so its HANDLES
reactions only ever fire once this pass polls the change log and finds the
resulting entry.

Fixture style follows tests/test_scheduler_admin.py and
tests/test_books_spine.py: a tmp objects dir (DBBASIC_OBJECTS_DIR) holding
handler sources, a tmp data dir (DBBASIC_DATA_DIR) holding a pre-created
collection, writes made directly through object_records (the storage-level
path this whole module exists to cover -- never through object_server).
"""

import json

import object_change_dispatch
import object_records

HANDLER_SOURCE = '''
import json
import os

HANDLES = ["widgets.record.created", "widgets.record.updated"]


def EVENT(request):
    log_path = os.environ["WIDGET_HANDLER_LOG"]
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(request) + "\\n")
    return {"ok": True}
'''

FAILING_HANDLER_SOURCE = '''
HANDLES = ["widgets.record.created"]


def EVENT(request):
    raise RuntimeError("handler blew up")
'''

ZERO_RESULT = {"processed": 0, "dispatched": 0, "skipped": 0, "errors": 0, "results": []}


def setup_env(tmp_path, monkeypatch, *, with_handler=True, with_failing_handler=False, enabled=True):
    objects_dir = tmp_path / "objects"
    (objects_dir / "system").mkdir(parents=True)
    if with_handler:
        (objects_dir / "system" / "widget_handler.py").write_text(HANDLER_SOURCE)
    if with_failing_handler:
        (objects_dir / "system" / "widget_failer.py").write_text(FAILING_HANDLER_SOURCE)

    data_dir = tmp_path / "data"
    coll_dir = data_dir / "collections" / "widgets"
    coll_dir.mkdir(parents=True)
    (coll_dir / "records.tsv").write_text("id\tname\n")

    log_path = tmp_path / "handler_log.jsonl"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WIDGET_HANDLER_LOG", str(log_path))
    if enabled:
        monkeypatch.setenv(object_change_dispatch.ENABLE_ENV, "1")
    else:
        monkeypatch.delenv(object_change_dispatch.ENABLE_ENV, raising=False)
    return objects_dir, data_dir, log_path


def create_widget(data_dir, record_id="w1", name="Widget One", actor="test_runner"):
    return object_records.create_collection_record(
        "widgets", {"id": record_id, "name": name}, base_dir=data_dir, actor=actor
    )


# --- core behavior: a storage-level write reaches its handler -----------------

def test_storage_write_reaches_handler_on_next_pass(tmp_path, monkeypatch):
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch)

    # First pass just stamps the cursor -- nothing to dispatch yet.
    first = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert first == ZERO_RESULT

    # A storage-level write -- object_records directly, never object_server --
    # is exactly the write this module exists to cover.
    create_widget(data_dir)

    second = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert second["processed"] == 1
    assert second["dispatched"] == 1
    assert second["errors"] == 0
    assert second["skipped"] == 0
    assert len(second["results"]) == 1

    entry = second["results"][0]
    assert entry["ok"] is True
    assert entry["handler_id"] == "system_widget_handler"
    assert entry["collection"] == "widgets"
    assert entry["record_id"] == "w1"

    assert log_path.exists()
    logged_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(logged_lines) == 1


def test_raw_verb_payload_shape_matches_http_dispatcher(tmp_path, monkeypatch):
    """The dispatcher's payload carries the RAW verb ("create") in `action`
    while `event` uses the past-participle ("...record.created") -- the
    exact mismatch a handler once got wrong and silently skipped every
    entry in production (see packages/app-payments/objects/system/books.py
    EVENT()'s comment). This pass must build the identical shape."""
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch)
    object_change_dispatch.dispatch_pending(base_dir=data_dir)
    create_widget(data_dir)
    result = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert result["dispatched"] == 1

    payload = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload == {
        "event": "widgets.record.created",
        "collection": "widgets",
        "record_id": "w1",
        "action": "create",
    }


# --- cursor / idempotency -------------------------------------------------

def test_cursor_advances_so_second_pass_does_not_redispatch(tmp_path, monkeypatch):
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch)
    object_change_dispatch.dispatch_pending(base_dir=data_dir)  # stamp cursor
    create_widget(data_dir)

    first = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert first["dispatched"] == 1

    second = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert second == ZERO_RESULT

    # The handler log should still show exactly one invocation.
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_first_run_does_not_replay_history(tmp_path, monkeypatch):
    """A write that happened before this pass ever ran (or before the flag
    was ever turned on) must not flood every handler the first time the
    daemon polls -- same posture as process_notifications' cursor."""
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch)
    create_widget(data_dir)  # pre-existing history, cursor does not exist yet

    first = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert first == ZERO_RESULT
    assert not log_path.exists()

    second = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert second == ZERO_RESULT


# --- isolation --------------------------------------------------------------

def test_one_raising_handler_does_not_stop_the_others(tmp_path, monkeypatch):
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch, with_failing_handler=True)
    object_change_dispatch.dispatch_pending(base_dir=data_dir)
    create_widget(data_dir)

    result = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert result["processed"] == 1
    assert result["dispatched"] == 2  # both handlers attempted
    assert result["errors"] == 1

    by_handler = {r["handler_id"]: r for r in result["results"]}
    assert by_handler["system_widget_failer"]["ok"] is False
    assert "handler blew up" in (by_handler["system_widget_failer"]["error"] or "")
    assert by_handler["system_widget_handler"]["ok"] is True

    # The good handler still ran despite its sibling raising.
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1


# --- graceful skips ----------------------------------------------------------

def test_no_handler_objects_is_a_graceful_skip(tmp_path, monkeypatch):
    _, data_dir, _log_path = setup_env(tmp_path, monkeypatch, with_handler=False)
    create_widget(data_dir)
    result = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert result == ZERO_RESULT


def test_no_change_log_is_a_graceful_skip(tmp_path, monkeypatch):
    # A handler is installed (so "widgets" is a watched collection) but no
    # write has ever happened -- record_changes/widgets/changes.jsonl does
    # not exist on disk at all.
    _, data_dir, _log_path = setup_env(tmp_path, monkeypatch)
    object_change_dispatch.dispatch_pending(base_dir=data_dir)  # stamp cursor
    result = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert result == ZERO_RESULT


def test_disabled_by_default_unless_flag_set(tmp_path, monkeypatch):
    _, data_dir, log_path = setup_env(tmp_path, monkeypatch, enabled=False)
    create_widget(data_dir)
    result = object_change_dispatch.dispatch_pending(base_dir=data_dir)
    assert result == ZERO_RESULT
    assert not log_path.exists()
    # Disabled means inert -- it must not even stamp a cursor.
    assert not (data_dir / object_change_dispatch.CURSOR_FILE_NAME).exists()


def test_change_dispatch_enabled_reads_the_env_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(object_change_dispatch.ENABLE_ENV, raising=False)
    assert object_change_dispatch.change_dispatch_enabled(base_dir=tmp_path) is False
    monkeypatch.setenv(object_change_dispatch.ENABLE_ENV, "1")
    assert object_change_dispatch.change_dispatch_enabled(base_dir=tmp_path) is True
    monkeypatch.setenv(object_change_dispatch.ENABLE_ENV, "off")
    assert object_change_dispatch.change_dispatch_enabled(base_dir=tmp_path) is False
