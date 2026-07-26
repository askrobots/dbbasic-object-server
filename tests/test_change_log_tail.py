"""Reading the end of a log without reading all of it.

The activity page stopped finishing. Its own docstring had predicted why:
"reads each collection's whole change log... fine at current scale...
future work if/when logs grow large." Logs grew large -- 944MB for one
derived collection -- and the feed was parsing a gigabyte of JSON on a
single core to show fifty rows.
"""

import json

import object_activity
import object_record_changes


def write_log(tmp_path, collection, count, *, actor="dana"):
    path = object_record_changes.record_changes_file(collection, base_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(json.dumps({
                "change_id": f"c{i}", "collection": collection,
                "record_id": f"r{i}", "action": "update", "actor": actor,
                "timestamp": f"2026-07-{(i % 28) + 1:02d}T00:00:{i % 60:02d}Z",
                # Padding, so the file is comfortably larger than one read
                # block and the tail path is actually exercised.
                "snapshot": {"note": "x" * 400},
            }))
            handle.write("\n")
    return path


def test_the_tail_returns_the_newest_entries(tmp_path):
    write_log(tmp_path, "notes", 2000)
    payload = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=10, tail_only=True)

    assert [c["record_id"] for c in payload["changes"]] == [
        f"r{i}" for i in range(1999, 1989, -1)]


def test_the_tail_agrees_with_the_full_read(tmp_path):
    """The optimisation may be faster; it may not be different."""
    write_log(tmp_path, "notes", 2000)
    full = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=25)
    tail = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=25, tail_only=True)

    assert tail["changes"] == full["changes"]


def test_a_partial_first_line_is_never_parsed_as_a_record(tmp_path):
    """Reading backwards lands mid-line; that fragment must be dropped, not
    silently swallowed as a corrupt entry that shifts every row after it."""
    write_log(tmp_path, "notes", 3000)
    payload = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=100, tail_only=True)

    assert len(payload["changes"]) == 100
    assert all(c["record_id"].startswith("r") for c in payload["changes"])


def test_a_small_log_still_reads_whole(tmp_path):
    write_log(tmp_path, "notes", 3)
    payload = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=50, tail_only=True)
    assert payload["total"] == 3


def test_a_record_scoped_query_still_scans(tmp_path):
    """One record's entries can be anywhere in the file. Answering "show me
    this invoice's history" from the tail alone would lose the half that
    matters, so tail_only deliberately does not apply."""
    write_log(tmp_path, "notes", 2000)
    payload = object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, record_id="r3", limit=10, tail_only=True)
    assert [c["record_id"] for c in payload["changes"]] == ["r3"]


def test_the_default_is_still_a_full_read(tmp_path):
    """tail_only is opt-in because it makes `total` a count of what was
    READ. A feed does not care; an audit page showing "1 of 4,312" does."""
    write_log(tmp_path, "notes", 500)
    assert object_record_changes.list_record_changes(
        "notes", base_dir=tmp_path, limit=10)["total"] == 500


# --- what belongs in a feed at all ---------------------------------------------

def test_machine_churn_is_not_activity(tmp_path):
    """A rollup rewriting its own output is not something a person did, and
    a feed that is 99% machine churn is a feed nobody reads."""
    write_log(tmp_path, "notes", 5, actor="dana")
    write_log(tmp_path, "analytics_top_paths", 400, actor="system:rollup")

    feed = object_activity.recent_activity(base_dir=tmp_path, limit=50)
    assert {entry["collection"] for entry in feed} == {"notes"}


def test_a_persons_own_changes_still_arrive(tmp_path):
    write_log(tmp_path, "notes", 5, actor="dana")
    write_log(tmp_path, "invoices", 5, actor="sam")

    mine = object_activity.recent_activity(base_dir=tmp_path, actor="dana", limit=50)
    assert {entry["collection"] for entry in mine} == {"notes"}
    assert len(mine) == 5
