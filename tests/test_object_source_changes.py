from __future__ import annotations

import json

import pytest

import object_source_changes
from object_versions import InvalidObjectIdError


def test_append_and_list_source_changes_newest_first(tmp_path):
    first = object_source_changes.append_source_change(
        object_id="basics_counter",
        action="source_update",
        version_id=1,
        actor="tester",
        message="first edit",
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
        base_dir=tmp_path,
    )
    second = object_source_changes.append_source_change(
        object_id="basics_counter",
        action="source_rollback",
        version_id=2,
        from_version_id=1,
        actor="tester",
        message="rollback",
        details={"reason": "bad output", "nested": {"ok": True}},
        base_dir=tmp_path,
    )

    payload = object_source_changes.list_source_changes(
        "basics_counter",
        base_dir=tmp_path,
        limit=10,
    )

    assert payload["object_id"] == "basics_counter"
    assert payload["count"] == 2
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert [entry["change_id"] for entry in payload["changes"]] == [
        second["change_id"],
        first["change_id"],
    ]
    assert payload["changes"][0]["action"] == "source_rollback"
    assert payload["changes"][0]["from_version_id"] == 1
    assert payload["changes"][0]["details"] == {"reason": "bad output", "nested": {"ok": True}}
    assert payload["changes"][1]["correlation_id"] == "123e4567-e89b-42d3-a456-426614174000"

    changes_path = tmp_path / "source_changes" / "basics_counter" / "changes.jsonl"
    lines = changes_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "source_update"


def test_source_changes_support_limit_and_offset(tmp_path):
    for version_id in range(1, 5):
        object_source_changes.append_source_change(
            object_id="basics_counter",
            action="source_update",
            version_id=version_id,
            message=f"edit {version_id}",
            base_dir=tmp_path,
        )

    payload = object_source_changes.list_source_changes(
        "basics_counter",
        base_dir=tmp_path,
        limit=2,
        offset=1,
    )

    assert payload["count"] == 2
    assert payload["total"] == 4
    assert payload["has_more"] is True
    assert [entry["version_id"] for entry in payload["changes"]] == [3, 2]


def test_source_changes_accept_source_create_action(tmp_path):
    entry = object_source_changes.append_source_change(
        object_id="site_home",
        action="source_create",
        version_id=1,
        actor="alice",
        message="Create home",
        details={"description": "Home page"},
        base_dir=tmp_path,
    )

    payload = object_source_changes.list_source_changes("site_home", base_dir=tmp_path)

    assert entry["action"] == "source_create"
    assert payload["changes"][0]["action"] == "source_create"
    assert payload["changes"][0]["message"] == "Create home"
    assert payload["changes"][0]["details"] == {"description": "Home page"}


def test_source_changes_reject_invalid_inputs(tmp_path):
    with pytest.raises(InvalidObjectIdError):
        object_source_changes.append_source_change(
            object_id="../bad",
            action="source_update",
            version_id=1,
            base_dir=tmp_path,
        )

    with pytest.raises(object_source_changes.InvalidSourceChangeError):
        object_source_changes.append_source_change(
            object_id="basics_counter",
            action="delete_everything",
            version_id=1,
            base_dir=tmp_path,
        )

    with pytest.raises(object_source_changes.InvalidSourceChangeError):
        object_source_changes.append_source_change(
            object_id="basics_counter",
            action="source_update",
            version_id=0,
            base_dir=tmp_path,
        )


def test_list_source_changes_ignores_corrupt_lines(tmp_path):
    changes_path = tmp_path / "source_changes" / "basics_counter" / "changes.jsonl"
    changes_path.parent.mkdir(parents=True)
    changes_path.write_text(
        '{"action":"source_update","version_id":1}\nnot-json\n[]\n',
        encoding="utf-8",
    )

    payload = object_source_changes.list_source_changes("basics_counter", base_dir=tmp_path)

    assert payload["count"] == 1
    assert payload["changes"][0]["version_id"] == 1
