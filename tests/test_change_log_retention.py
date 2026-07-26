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

import gzip
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
    policy = object_record_changes.retention_policy({})
    assert policy["days"] == 0 and policy["max_entries"] == 0
    assert object_record_changes.retention_policy(
        {"DBBASIC_CHANGE_LOG_RETENTION_DAYS": "nonsense"})["days"] == 0


def test_archiving_is_the_default_and_keeping_archives_forever_is_too():
    """Somebody asking for a smaller hot file has not asked to lose the
    history, and the default for evidence is never 'throw it away'."""
    policy = object_record_changes.retention_policy(
        {"DBBASIC_CHANGE_LOG_RETENTION_DAYS": "90"})
    assert policy["archive"] is True
    assert policy["keep_archives"] == 0        # 0 = keep every segment
    assert object_record_changes.retention_policy(
        {"DBBASIC_CHANGE_LOG_ARCHIVE": "off"})["archive"] is False


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


# --- rotation, archival and removal are three different questions ----------------

def test_rotated_entries_are_archived_not_destroyed(tmp_path):
    """Rotating without archiving is deletion wearing a gentler word."""
    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(30)])
    result = object_record_changes.prune_record_changes(
        "notes", keep_last=5, base_dir=tmp_path)

    assert result["removed"] == 25 and result["archived"]
    assert len(entries(tmp_path, "notes")) == 5

    segments = object_record_changes.list_archives("notes", base_dir=tmp_path)
    assert len(segments) == 1
    with gzip.open(segments[0], "rt", encoding="utf-8") as handle:
        archived = [json.loads(line) for line in handle if line.strip()]
    assert [e["record_id"] for e in archived] == [f"r{i}" for i in range(25)]


def test_the_archive_and_the_hot_file_together_lose_nothing(tmp_path):
    stamps = [f"2026-07-26T00:00:{i:02d}Z" for i in range(40)]
    write_entries(tmp_path, "notes", stamps)
    object_record_changes.prune_record_changes(
        "notes", keep_last=8, base_dir=tmp_path)

    kept = [e["record_id"] for e in entries(tmp_path, "notes")]
    archived = []
    for segment in object_record_changes.list_archives("notes", base_dir=tmp_path):
        with gzip.open(segment, "rt", encoding="utf-8") as handle:
            archived += [json.loads(line)["record_id"]
                         for line in handle if line.strip()]
    assert sorted(archived + kept, key=lambda r: int(r[1:])) == [
        f"r{i}" for i in range(40)]


def test_duplicate_entries_are_counted_not_deduplicated(tmp_path):
    """Two identical entries are two facts. Differencing by set membership
    would archive one and lose the other."""
    path = log_path(tmp_path, "notes")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"change_id": "c", "timestamp": "2026-07-26T00:00:00Z",
                      "collection": "notes", "record_id": "r", "action": "update",
                      "actor": "dana"})
    path.write_text("\n".join([row] * 6) + "\n", encoding="utf-8")

    object_record_changes.prune_record_changes("notes", keep_last=2, base_dir=tmp_path)
    with gzip.open(object_record_changes.list_archives("notes", base_dir=tmp_path)[0],
                   "rt", encoding="utf-8") as handle:
        archived = [line for line in handle if line.strip()]
    assert len(archived) == 4 and len(entries(tmp_path, "notes")) == 2


def test_archiving_can_be_turned_off_for_a_genuinely_disposable_log(tmp_path):
    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(20)])
    result = object_record_changes.prune_record_changes(
        "notes", keep_last=5, archive=False, base_dir=tmp_path)
    assert result["archived"] is None
    assert object_record_changes.list_archives("notes", base_dir=tmp_path) == []


def test_old_archives_are_removed_only_when_asked(tmp_path):
    """An archive nobody ever deletes is the original problem moved one
    directory down -- but the default keeps everything."""
    for _ in range(4):
        write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(20)])
        object_record_changes.prune_record_changes("notes", keep_last=1, base_dir=tmp_path)
    assert len(object_record_changes.list_archives("notes", base_dir=tmp_path)) == 4

    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i:02d}Z" for i in range(20)])
    result = object_record_changes.prune_record_changes(
        "notes", keep_last=1, keep_archives=2, base_dir=tmp_path)
    assert len(result["archives_removed"]) == 3
    assert len(object_record_changes.list_archives("notes", base_dir=tmp_path)) == 2


def test_compression_is_worth_doing(tmp_path):
    """JSONL compresses roughly ten to twenty times; that ratio is the
    whole argument for archiving instead of deleting."""
    write_entries(tmp_path, "notes", [f"2026-07-26T00:00:{i % 60:02d}Z" for i in range(4000)])
    plain = log_path(tmp_path, "notes").stat().st_size
    object_record_changes.prune_record_changes("notes", keep_last=1, base_dir=tmp_path)
    segment = object_record_changes.list_archives("notes", base_dir=tmp_path)[0]
    assert segment.stat().st_size * 5 < plain
