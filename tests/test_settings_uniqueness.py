"""A setting is configured once, or it is configured invisibly.

app_settings is read by forty-odd objects, each through its own
`_setting(base, key)` helper that scans the collection and returns the
FIRST row whose key matches. Nothing stopped a second row with the same
key from being written, and when one was, it did not conflict and did not
warn -- it simply lost. The operator saw a saved setting that did nothing.

This shipped a wrong journal on the live box, which is why the invariant
moved from "every reader honours it" to "the write path enforces it":
`inventory.journal.inventory_account` held a stale verification value, a
correct one was added beside it, and the very next sale credited the
stale account. The entry balanced. It was still wrong.

Same shape as the provenance rule in hook_wallet_entries -- an invariant
honoured by N callers is an invariant that holds until the N+1th.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETTINGS_OBJECTS = REPO_ROOT / "packages" / "app-settings" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-settings", "app_settings")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def hook(record, action="create"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_app_settings", method="BEFORE_WRITE",
            payload={"action": action, "collection": "app_settings",
                     "record": record}),
        roots=[SETTINGS_OBJECTS]).result


def existing(data_dir, record_id, key, value):
    return object_records.create_collection_record(
        "app_settings", {"id": record_id, "key": key, "value": value},
        base_dir=data_dir)


def test_a_first_setting_is_allowed(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert hook({"id": "s1", "key": "a.b", "value": "x"}) is None


def test_a_duplicate_key_is_refused_and_names_the_row_that_owns_it(
        tmp_path, monkeypatch):
    """THE bug, in the shape it actually took: a stale account setting and
    a correct one beside it. The refusal must name the existing record,
    because the operator's next question is always where it IS set."""
    data_dir = setup_env(tmp_path, monkeypatch)
    existing(data_dir, "old", "inventory.journal.inventory_account",
             "acct-stale")

    result = hook({"id": "new", "key": "inventory.journal.inventory_account",
                   "value": "acct-correct"})
    assert result["status"] == 409
    assert "old" in result["error"]
    assert "acct-stale" in result["error"]
    assert "silently ignored" in result["error"]


def test_editing_the_row_that_owns_the_key_is_allowed(tmp_path, monkeypatch):
    """Changing a setting's value is the normal case and must not be
    mistaken for a collision with itself."""
    data_dir = setup_env(tmp_path, monkeypatch)
    existing(data_dir, "s1", "a.b", "x")
    assert hook({"id": "s1", "key": "a.b", "value": "y"}, action="update") is None


def test_a_different_key_is_never_blocked(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    existing(data_dir, "s1", "a.b", "x")
    assert hook({"id": "s2", "key": "a.c", "value": "x"}) is None


def test_whitespace_does_not_smuggle_a_second_row_past_the_gate(
        tmp_path, monkeypatch):
    """The readers strip; so must the gate, or " a.b" configures a key
    that no reader will ever find while still shadowing nothing."""
    data_dir = setup_env(tmp_path, monkeypatch)
    existing(data_dir, "s1", "a.b", "x")
    assert hook({"id": "s2", "key": "  a.b  ", "value": "y"})["status"] == 409


def test_a_delete_is_not_this_hook_s_business(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    existing(data_dir, "s1", "a.b", "x")
    assert hook({"id": "s1", "key": "a.b", "value": "x"}, action="delete") is None


def test_the_schema_actually_declares_the_hook():
    """The gate ships wired, not merely written.

    Every other test here calls the hook directly, which proves its LOGIC
    and proves nothing about whether the server ever invokes it. Hooks are
    wired by a `hooks.before_write` declaration in the SCHEMA -- not by
    naming convention -- so an object called hook_app_settings that no
    schema points at is a silent no-op that passes its own unit tests.
    That is exactly how this one first shipped: green tests, and the live
    box happily accepted a duplicate key thirty seconds after deploy.
    """
    import json
    schema = json.loads(
        (REPO_ROOT / "packages" / "app-settings" / "schemas"
         / "app_settings.json").read_text())
    assert schema["hooks"]["before_write"] == "hook_app_settings"
