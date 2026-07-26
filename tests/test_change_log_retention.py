"""Bounding an audit log without quietly losing accountability.

The third unbounded log this server grew, after page_views and the package
restore points. What makes this one different is that a change log IS the
audit trail, so the rules are deliberately more conservative than the
other two: nothing is pruned unless an operator asked for it.

The churn that motivated it is fixed at the source instead -- a rollup
rewriting derived rows no longer writes change entries at all -- and these
tests pin that too, because an entry never written costs nothing to store,
nothing to read past, and nothing to decide about later.
"""

import json

import object_ids
import object_record_changes
import object_records


def log_path(tmp_path, collection):
    return object_record_changes.record_changes_file(collection, base_dir=tmp_path)


def write_entries(tmp_path, collection, stamps):
    path = log_path(tmp_path, collection)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for i, stamp in enumerate(stamps):
            handle.write(json.dumps({
                "change_id": f"c{i}", "timestamp": stamp,
                "collection": collection, "record_id": f"r{i}",
                "action": "update", "actor": "dana",
            }))
            handle.write("\n")
    return path


def entries(tmp_path, collection):
    path = log_path(tmp_path, collection)
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- the policy is opt-in ------------------------------------------------------

def test_no_policy_means_no_bound_at_all():
    """An operator who has never thought about audit retention has not
    thereby consented to losing audit history."""
    assert object_record_changes.retention_policy({}) == {"days": 0, "max_entries": 0}
    assert object_record_changes.retention_policy(
        {"DBBASIC_CHANGE_LOG_RETENTION_DAYS": "nonsense"})["days"] == 0


def test_an_unconfigured_sweep_is_an_honest_no_op(tmp_path):
    write_entries(tmp_path, "notes", ["2020-01-01T00:00:00Z"] * 5)
    result = object_record_changes.prune_all_record_changes(base_dir=tmp_path)
    assert result["removed"] == 0
    assert result["skipped"] == "no policy set"
    assert len(entries(tmp_path, "notes")) == 5


# --- both bounds ----------------------------------------------------------------

def test_age_prunes_what_is_older(tmp_path):
    write_entries(tmp_path, "notes", [
        "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z", "2026-07-20T00:00:00Z"])
    result = object_record_changes.prune_record_changes(
        "notes", keep_newer_than="2026-01-01T00:00:00Z", base_dir=tmp_path)
    assert result["removed"] == 2 and result["entries_after"] == 1
    assert entries(tmp_path, "notes")[0]["record_id"] == "r2"


def test_a_count_bound_keeps_the_newest(tmp_path):
    """Age alone bounds nothing: a window says how far back to keep and
    nothing about how much can arrive inside it."""
    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(50)])
    result = object_record_changes.prune_record_changes(
        "notes", keep_last=10, base_dir=tmp_path)
    assert result["entries_after"] == 10
    kept = entries(tmp_path, "notes")
    assert [e["record_id"] for e in kept] == [f"r{i}" for i in range(40, 50)]


def test_both_bounds_compose(tmp_path):
    write_entries(tmp_path, "notes",
                  ["2020-01-01T00:00:00Z"] * 5
                  + [f"2026-07-26T00:00:{i:02d}Z" for i in range(20)])
    object_record_changes.prune_record_changes(
        "notes", keep_newer_than="2026-01-01T00:00:00Z", keep_last=5,
        base_dir=tmp_path)
    kept = entries(tmp_path, "notes")
    assert len(kept) == 5
    assert all(e["timestamp"].startswith("2026") for e in kept)


def test_an_undatable_entry_is_never_deleted(tmp_path):
    """Never delete evidence we cannot date."""
    write_entries(tmp_path, "notes", ["", "2020-01-01T00:00:00Z"])
    object_record_changes.prune_record_changes(
        "notes", keep_newer_than="2026-01-01T00:00:00Z", base_dir=tmp_path)
    kept = entries(tmp_path, "notes")
    assert [e["record_id"] for e in kept] == ["r0"]


def test_a_missing_log_is_not_an_error(tmp_path):
    result = object_record_changes.prune_record_changes(
        "never_written", keep_last=10, base_dir=tmp_path)
    assert result["pruned"] is False


def test_the_sweep_covers_every_collection(tmp_path):
    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(30)])
    write_entries(tmp_path, "invoices", [f"2026-07-26T00:00:{i:02d}Z" for i in range(30)])
    result = object_record_changes.prune_all_record_changes(
        base_dir=tmp_path, max_entries=5)
    assert result["collections"] == 2 and result["removed"] == 50
    assert len(entries(tmp_path, "notes")) == 5


# --- the better fix: never written at all ----------------------------------------

def test_a_derived_write_leaves_no_audit_entry(tmp_path):
    """A rollup rewriting its own output describes nothing a person did,
    and the value is recomputable from the source and the definition --
    both still on disk. Left on, this produced a 944MB log for a
    collection of 1818 rows."""
    from conftest import stage_collection

    stage_collection(tmp_path, "app-notes", "notes")
    object_records.create_collection_record(
        "notes", {"id": object_ids.new_uuid4(), "title": "Derived", "content": "x"},
        base_dir=tmp_path, actor="system:rollup", record_changes=False)

    assert not log_path(tmp_path, "notes").exists()


def test_an_ordinary_write_is_still_traceable(tmp_path):
    """The flag is for recomputes only. A write nobody can trace is the
    thing the log exists to prevent."""
    from conftest import stage_collection

    stage_collection(tmp_path, "app-notes", "notes")
    object_records.create_collection_record(
        "notes", {"id": object_ids.new_uuid4(), "title": "By hand", "content": "x"},
        base_dir=tmp_path, actor="dana")

    logged = entries(tmp_path, "notes")
    assert len(logged) == 1 and logged[0]["actor"] == "dana"
