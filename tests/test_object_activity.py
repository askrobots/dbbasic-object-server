"""Tests for object_activity: the activity feed as a fold over the existing
record-change ledger written by object_records.py (see object_activity.py's
module docstring). No new storage: these tests write through
object_records.create/update/delete_collection_record with different
actors and assert recent_activity() reads that same ledger back out.
"""

from pathlib import Path

import object_activity
import object_records


def write_records(data_dir: Path, collection: str, content: str) -> Path:
    path = data_dir / "collections" / collection / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_recent_activity_scopes_to_one_actor_newest_first_with_titles(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")
    write_records(data_dir, "tasks", "id\ttitle\n")

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "Alice's first note"},
        base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "tasks", {"id": "t1", "title": "Bob's task"},
        base_dir=data_dir, roots=[], actor="bob",
    )
    object_records.update_collection_record(
        "notes", "n1", {"title": "Alice's edited note"},
        base_dir=data_dir, roots=[], actor="alice",
    )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")

    assert [entry["action"] for entry in feed] == ["update", "create"]
    assert {entry["actor"] for entry in feed} == {"alice"}
    assert feed[0]["title"] == "Alice's edited note"
    assert feed[0]["collection"] == "notes"
    assert feed[0]["record_id"] == "n1"
    assert feed[1]["title"] == "Alice's first note"
    # A feed is a signal, not a data dump: no full snapshot leaks through.
    assert "before" not in feed[0] and "after" not in feed[0]


def test_recent_activity_merges_across_collections_newest_first(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")
    write_records(data_dir, "tasks", "id\ttitle\n")

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "First"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "tasks", {"id": "t1", "title": "Second"}, base_dir=data_dir, roots=[], actor="alice",
    )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")

    assert [entry["collection"] for entry in feed] == ["tasks", "notes"]


def test_recent_activity_title_falls_back_through_field_order(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\n")
    write_records(data_dir, "orders", "id\tnumber\n")
    write_records(data_dir, "widgets", "id\n")

    object_records.create_collection_record(
        "contacts", {"id": "c1", "name": "Ada"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "orders", {"id": "o1", "number": "PO-1"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "widgets", {"id": "w1"}, base_dir=data_dir, roots=[], actor="alice",
    )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")
    titles_by_collection = {entry["collection"]: entry["title"] for entry in feed}

    assert titles_by_collection["contacts"] == "Ada"
    assert titles_by_collection["orders"] == "PO-1"
    # No title/name/number/subject field on the snapshot -> falls back to id.
    assert titles_by_collection["widgets"] == "w1"


def test_recent_activity_title_falls_back_to_before_snapshot_on_delete(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\nn1\tDoomed note\n")

    object_records.delete_collection_record(
        "notes", "n1", base_dir=data_dir, roots=[], actor="alice",
    )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")

    assert feed[0]["action"] == "delete"
    assert feed[0]["title"] == "Doomed note"


def test_recent_activity_excludes_shell_commands_and_ai_usage(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")
    write_records(data_dir, "shell_commands", "id\tcommand\n")
    write_records(data_dir, "ai_usage", "id\ttokens\n")

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "A note"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "shell_commands", {"id": "s1", "command": "ls"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "ai_usage", {"id": "u1", "tokens": "100"}, base_dir=data_dir, roots=[], actor="alice",
    )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")

    assert [entry["collection"] for entry in feed] == ["notes"]


def test_recent_activity_only_returns_the_given_actors_entries(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "Alice's note"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "notes", {"id": "n2", "title": "Bob's note"}, base_dir=data_dir, roots=[], actor="bob",
    )

    alice_feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")
    bob_feed = object_activity.recent_activity(base_dir=data_dir, actor="bob")

    assert [entry["record_id"] for entry in alice_feed] == ["n1"]
    assert [entry["record_id"] for entry in bob_feed] == ["n2"]


def test_recent_activity_respects_limit(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")
    for i in range(5):
        object_records.create_collection_record(
            "notes", {"id": f"n{i}", "title": f"Note {i}"},
            base_dir=data_dir, roots=[], actor="alice",
        )

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice", limit=2)

    assert len(feed) == 2
    assert [entry["record_id"] for entry in feed] == ["n4", "n3"]


def test_recent_activity_returns_empty_list_when_no_changes_exist(tmp_path):
    data_dir = tmp_path / "data"

    feed = object_activity.recent_activity(base_dir=data_dir, actor="alice")

    assert feed == []


def test_recent_activity_without_actor_returns_every_actors_entries(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\ttitle\n")

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "Alice's note"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "notes", {"id": "n2", "title": "Bob's note"}, base_dir=data_dir, roots=[], actor="bob",
    )

    feed = object_activity.recent_activity(base_dir=data_dir)

    assert {entry["actor"] for entry in feed} == {"alice", "bob"}


def test_recent_activity_rejects_limit_below_one(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        object_activity.recent_activity(base_dir=tmp_path / "data", limit=0)
