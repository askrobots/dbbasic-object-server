import json

import pytest

import object_handlers
import object_server
import object_state


@pytest.fixture(autouse=True)
def _reset_handler_index():
    object_handlers.invalidate()
    yield
    object_handlers.invalidate()


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# --- extract_handles -------------------------------------------------------


def test_extract_handles_reads_list_literal():
    code = 'HANDLES = ["notes.record.created", "notes.record.updated"]\n'
    assert object_handlers.extract_handles(code) == [
        "notes.record.created",
        "notes.record.updated",
    ]


def test_extract_handles_reads_tuple_literal():
    code = 'HANDLES = ("notes.record.created",)\n'
    assert object_handlers.extract_handles(code) == ["notes.record.created"]


def test_extract_handles_ignores_non_string_entries():
    code = 'HANDLES = ["notes.record.created", 1, None, "notes.record.deleted"]\n'
    assert object_handlers.extract_handles(code) == [
        "notes.record.created",
        "notes.record.deleted",
    ]


def test_extract_handles_absent_returns_empty():
    code = "def GET(request):\n    return {}\n"
    assert object_handlers.extract_handles(code) == []


def test_extract_handles_syntax_error_returns_empty():
    code = "def GET(request:\n    return {}\n"
    assert object_handlers.extract_handles(code) == []


def test_extract_handles_non_literal_returns_empty():
    code = "HANDLES = some_function_call()\n"
    assert object_handlers.extract_handles(code) == []


def test_extract_handles_non_list_value_returns_empty():
    code = 'HANDLES = "notes.record.created"\n'
    assert object_handlers.extract_handles(code) == []


# --- event_name --------------------------------------------------------


def test_event_name_maps_actions_to_present_tense_events():
    assert object_handlers.event_name("notes", "create") == "notes.record.created"
    assert object_handlers.event_name("notes", "update") == "notes.record.updated"
    assert object_handlers.event_name("notes", "delete") == "notes.record.deleted"


def test_event_name_returns_none_for_unknown_action():
    assert object_handlers.event_name("notes", "archive") is None


def test_event_name_returns_none_for_empty_collection():
    assert object_handlers.event_name("", "create") is None


# --- build_index / get_handlers / invalidate --------------------------


def test_build_index_indexes_system_objects_by_declared_event(tmp_path):
    root = tmp_path / "objects"
    write_source(
        root / "notes" / "handler.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )

    index = object_handlers.build_index([root])

    assert index == {"notes.record.created": ["notes_handler"]}


def test_build_index_skips_user_objects(tmp_path):
    root = tmp_path / "objects"
    write_source(
        root / "users" / "42" / "handler.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )

    index = object_handlers.build_index([root])

    assert index == {}


def test_build_index_multiple_handlers_sorted_for_same_event(tmp_path):
    root = tmp_path / "objects"
    write_source(
        root / "notes" / "a_handler.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )
    write_source(
        root / "notes" / "b_handler.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )

    index = object_handlers.build_index([root])

    assert index["notes.record.created"] == ["notes_a_handler", "notes_b_handler"]


def test_get_handlers_builds_and_caches_index(tmp_path):
    root = tmp_path / "objects"
    write_source(
        root / "notes" / "handler.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )

    assert object_handlers.get_handlers("notes.record.created", [root]) == ["notes_handler"]
    assert object_handlers.get_handlers("notes.record.updated", [root]) == []

    # A second handler added after the first get_handlers() call is not
    # picked up until invalidate() forces a rebuild.
    write_source(
        root / "notes" / "second.py",
        'HANDLES = ["notes.record.created"]\n\n\ndef EVENT(request):\n    return {}\n',
    )
    assert object_handlers.get_handlers("notes.record.created", [root]) == ["notes_handler"]

    object_handlers.invalidate()
    assert object_handlers.get_handlers("notes.record.created", [root]) == [
        "notes_handler",
        "notes_second",
    ]


# --- reentry guard ------------------------------------------------------


def test_dispatch_guard_tracks_depth():
    assert object_handlers.current_depth() == 0
    with object_handlers.dispatch_guard():
        assert object_handlers.current_depth() == 1
        with object_handlers.dispatch_guard():
            assert object_handlers.current_depth() == 2
        assert object_handlers.current_depth() == 1
    assert object_handlers.current_depth() == 0


def test_dispatch_guard_can_reach_max_depth():
    depths = []
    guards = []
    try:
        for _ in range(object_handlers.MAX_DISPATCH_DEPTH):
            guard = object_handlers.dispatch_guard()
            guard.__enter__()
            guards.append(guard)
            depths.append(object_handlers.current_depth())
    finally:
        for guard in reversed(guards):
            guard.__exit__(None, None, None)

    assert depths == list(range(1, object_handlers.MAX_DISPATCH_DEPTH + 1))
    assert object_handlers.current_depth() == 0


# --- dispatch integration (direct call) --------------------------------


HANDLER_SOURCE = (
    'HANDLES = ["notes.record.created"]\n\n'
    "import json\n\n\n"
    "def EVENT(request):\n"
    "    _state_manager.set('last_event', json.dumps(request))\n"
    "    return {'status': 'ok'}\n"
)

RAISING_HANDLER_SOURCE = (
    'HANDLES = ["notes.record.created"]\n\n\n'
    "def EVENT(request):\n"
    "    raise RuntimeError('boom')\n"
)


def test_dispatch_event_handlers_fires_matching_handler(tmp_path, monkeypatch):
    objects_root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(objects_root / "notes" / "handler.py", HANDLER_SOURCE)

    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_handlers.HANDLERS_ENABLED_ENV, "true")

    object_server._dispatch_event_handlers("notes", "n1", "create", {"id": "n1"})

    state = object_state.ObjectStateManager("notes_handler", base_dir=str(data_dir))
    assert json.loads(state.get("last_event")) == {
        "event": "notes.record.created",
        "collection": "notes",
        "record_id": "n1",
        "action": "create",
    }


def test_dispatch_event_handlers_is_noop_when_disabled(tmp_path, monkeypatch):
    objects_root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(objects_root / "notes" / "handler.py", HANDLER_SOURCE)

    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.delenv(object_handlers.HANDLERS_ENABLED_ENV, raising=False)

    object_server._dispatch_event_handlers("notes", "n1", "create", {"id": "n1"})

    state = object_state.ObjectStateManager("notes_handler", base_dir=str(data_dir))
    assert state.get("last_event") is None


def test_dispatch_event_handlers_swallows_handler_exceptions(tmp_path, monkeypatch):
    objects_root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(objects_root / "notes" / "handler.py", RAISING_HANDLER_SOURCE)

    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_handlers.HANDLERS_ENABLED_ENV, "true")

    # Must not raise, even though the only handler for this event blows up.
    object_server._dispatch_event_handlers("notes", "n1", "create", {"id": "n1"})


def test_dispatch_event_handlers_respects_max_depth(tmp_path, monkeypatch):
    objects_root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(objects_root / "notes" / "handler.py", HANDLER_SOURCE)

    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_handlers.HANDLERS_ENABLED_ENV, "true")

    guards = []
    try:
        for _ in range(object_handlers.MAX_DISPATCH_DEPTH):
            guard = object_handlers.dispatch_guard()
            guard.__enter__()
            guards.append(guard)

        # At max depth, dispatch must skip rather than recurse further.
        object_server._dispatch_event_handlers("notes", "n1", "create", {"id": "n1"})
    finally:
        for guard in reversed(guards):
            guard.__exit__(None, None, None)

    state = object_state.ObjectStateManager("notes_handler", base_dir=str(data_dir))
    assert state.get("last_event") is None
