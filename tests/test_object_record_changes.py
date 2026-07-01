import json
from uuid import UUID

import pytest

import object_collections
import object_record_changes
import object_records


def test_append_record_change_writes_jsonl_and_lists_newest_first(tmp_path):
    data_dir = tmp_path / "data"

    first = object_record_changes.append_record_change(
        collection="contacts",
        record_id="c1",
        action="create",
        before=None,
        after={"id": "c1", "name": "Ada"},
        actor="admin",
        message="created from test",
        base_dir=data_dir,
    )
    second = object_record_changes.append_record_change(
        collection="contacts",
        record_id="c1",
        action="update",
        before={"id": "c1", "name": "Ada"},
        after={"id": "c1", "name": "Ada Lovelace"},
        actor="admin",
        base_dir=data_dir,
    )

    path = data_dir / "record_changes" / "contacts" / "changes.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]

    assert rows == [first, second]
    assert UUID(first["change_id"]).version == 4
    assert UUID(second["change_id"]).version == 4
    assert first["changed_fields"] == ["id", "name"]
    assert second["changed_fields"] == ["name"]
    assert second["message"] == "Updated record"

    payload = object_record_changes.list_record_changes("contacts", base_dir=data_dir)

    assert payload["collection"] == "contacts"
    assert payload["count"] == 2
    assert payload["total"] == 2
    assert [change["action"] for change in payload["changes"]] == ["update", "create"]


def test_list_record_changes_filters_record_and_paginates(tmp_path):
    data_dir = tmp_path / "data"
    for record_id in ("c1", "c2", "c1"):
        object_record_changes.append_record_change(
            collection="contacts",
            record_id=record_id,
            action="update",
            before={"id": record_id, "name": "old"},
            after={"id": record_id, "name": "new"},
            base_dir=data_dir,
        )

    payload = object_record_changes.list_record_changes(
        "contacts",
        record_id="c1",
        base_dir=data_dir,
        limit=1,
        offset=1,
    )

    assert payload["record_id"] == "c1"
    assert payload["count"] == 1
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert payload["changes"][0]["record_id"] == "c1"


def test_list_record_changes_returns_empty_for_valid_collection_without_log(tmp_path):
    payload = object_record_changes.list_record_changes("contacts", base_dir=tmp_path / "data")

    assert payload["changes"] == []
    assert payload["count"] == 0
    assert payload["total"] == 0


def test_record_changes_reject_invalid_names_and_actions(tmp_path):
    with pytest.raises(object_collections.InvalidCollectionNameError):
        object_record_changes.list_record_changes("bad.name", base_dir=tmp_path / "data")

    with pytest.raises(object_records.InvalidRecordIdError):
        object_record_changes.append_record_change(
            collection="contacts",
            record_id="bad.name",
            action="create",
            before=None,
            after={"id": "bad.name"},
            base_dir=tmp_path / "data",
        )

    with pytest.raises(object_record_changes.InvalidRecordChangeError):
        object_record_changes.append_record_change(
            collection="contacts",
            record_id="c1",
            action="publish",
            before=None,
            after={"id": "c1"},
            base_dir=tmp_path / "data",
        )


def test_record_changes_ignore_corrupt_lines(tmp_path):
    data_dir = tmp_path / "data"
    path = data_dir / "record_changes" / "contacts" / "changes.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not-json\n"
        '{"action":"create","collection":"contacts","record_id":"c1"}\n'
        "[]\n"
    )

    payload = object_record_changes.list_record_changes("contacts", base_dir=data_dir)

    assert payload["changes"] == [
        {"action": "create", "collection": "contacts", "record_id": "c1"}
    ]
