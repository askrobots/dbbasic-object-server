"""Tests for the backup inventory module and the admin backup endpoints."""

import pytest
from test_object_server import (
    auth_headers,
    enable_admin_token,
    raw_request,
    request,
)

import object_backup
import object_backup_index
import object_server


def _write_backup(directory, name, content=b"archive-bytes"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    return directory / name


# --- Module ------------------------------------------------------------------

def test_validate_backup_id_and_path_guard(tmp_path, monkeypatch):
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    assert object_backup_index.validate_backup_id("20260101T000000Z-manual.tar.gz")
    assert not object_backup_index.validate_backup_id("../etc/passwd")
    assert not object_backup_index.validate_backup_id("notes.txt")
    assert not object_backup_index.validate_backup_id("a/b.tar.gz")
    assert not object_backup_index.validate_backup_id("..%2f.tar.gz")

    with pytest.raises(ValueError):
        object_backup_index.backup_path("../secret.tar.gz", data_dir=tmp_path)


def test_list_backups_parses_kind_and_scope(tmp_path, monkeypatch):
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    bdir = tmp_path / "backups"
    _write_backup(bdir, "20260101T000000Z-manual.tar.gz", b"aa")
    _write_backup(bdir, "20260102T000000Z-package-app-notes.tar.gz", b"bbb")
    _write_backup(bdir, "not-a-backup.txt", b"ignore me")

    backups = object_backup_index.list_backups(data_dir=tmp_path)
    assert len(backups) == 2
    by_id = {b["id"]: b for b in backups}
    manual = by_id["20260101T000000Z-manual.tar.gz"]
    assert manual["kind"] == "manual" and manual["scope"] == "runtime" and manual["size"] == 2
    pkg = by_id["20260102T000000Z-package-app-notes.tar.gz"]
    assert pkg["kind"] == "package" and pkg["scope"] == "app-notes"
    # newest first
    assert backups[0]["created_at"] >= backups[1]["created_at"]


def test_create_backup_writes_an_archive(tmp_path, monkeypatch):
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    data_dir = tmp_path / "data"
    objects_dir = tmp_path / "objects"
    (data_dir / "collections" / "notes").mkdir(parents=True)
    (data_dir / "collections" / "notes" / "records.tsv").write_text("id\tcontent\nn1\thi\n")
    objects_dir.mkdir()
    monkeypatch.setenv(object_backup.OBJECTS_DIR_ENV, str(objects_dir))

    backup = object_backup_index.create_backup(data_dir=data_dir)
    assert backup["kind"] == "manual"
    path = object_backup_index.backup_path(backup["id"], data_dir=data_dir)
    assert path.is_file()
    verification = object_backup.verify_runtime_backup(path)
    assert verification.ok


# --- Endpoints ---------------------------------------------------------------

def test_admin_backups_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    status, _, payload = request("/admin/backups")  # no auth
    assert status == 401
    assert "error" in payload


def test_admin_backups_list_create_download(tmp_path, monkeypatch):
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    data_dir = tmp_path / "data"
    objects_dir = tmp_path / "objects"
    (data_dir / "collections" / "notes").mkdir(parents=True)
    (data_dir / "collections" / "notes" / "records.tsv").write_text("id\tcontent\nn1\thi\n")
    objects_dir.mkdir()
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_backup.OBJECTS_DIR_ENV, str(objects_dir))
    enable_admin_token(monkeypatch)

    # empty to start
    status, _, listed = request("/admin/backups", headers=auth_headers())
    assert status == 200 and listed["count"] == 0
    assert listed["schedule"]["scheduled"] is False

    # create one
    status, _, created = request("/admin/backups", method="POST", headers=auth_headers())
    assert status == 201, created
    backup_id = created["backup"]["id"]
    assert created["backup"]["kind"] == "manual"

    # it shows up in the inventory
    status, _, listed = request("/admin/backups", headers=auth_headers())
    assert status == 200 and listed["count"] == 1
    assert listed["backups"][0]["id"] == backup_id

    # download returns the archive bytes (gzip magic), admin-gated
    status, resp_headers, body = raw_request(
        f"/admin/backups/{backup_id}/download", headers=auth_headers()
    )
    assert status == 200
    assert resp_headers[b"content-type"] == b"application/gzip"
    assert body[:2] == b"\x1f\x8b"  # gzip magic
    assert object_backup.verify_runtime_backup(
        object_backup_index.backup_path(backup_id, data_dir=data_dir)
    ).ok

    # download is admin-gated and traversal-safe
    status, _, _ = raw_request(f"/admin/backups/{backup_id}/download")  # no auth
    assert status == 401
    status, _, payload = request("/admin/backups/..%2Fpasswd/download", headers=auth_headers())
    assert status in (400, 404)


def test_admin_status_reports_backup_capability(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_server.BACKUP_SCHEDULE_ENV, "daily")
    enable_admin_token(monkeypatch)
    status, _, payload = request("/admin/status", headers=auth_headers())
    assert status == 200
    caps = payload["capabilities"]["backups"]
    assert caps["can_create"] and caps["can_download"] and caps["can_restore"] is False
    assert caps["can_preview"] is True
    assert caps["scheduled"] is True and caps["schedule"] == "daily"


# --- preview_collection / preview_record --------------------------------------

def _make_backup_with_collection(tmp_path, monkeypatch, rows_tsv):
    """Build a runtime backup whose only collection is "notes" with the given TSV body."""
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    data_dir = tmp_path / "data"
    objects_dir = tmp_path / "objects"
    (data_dir / "collections" / "notes").mkdir(parents=True)
    (data_dir / "collections" / "notes" / "records.tsv").write_text(rows_tsv)
    objects_dir.mkdir()
    monkeypatch.setenv(object_backup.OBJECTS_DIR_ENV, str(objects_dir))
    backup = object_backup_index.create_backup(data_dir=data_dir)
    return data_dir, backup["id"]


def test_preview_collection_reports_added_removed_changed_unchanged(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "id\tname\tcolor\n"
        "n1\tFirst\tred\n"
        "n2\tSecond\tblue\n"
        "n3\tThird\tgreen\n",
    )

    # mutate the live dir: n2 gets a field change, n3 is deleted, n4 is added.
    (data_dir / "collections" / "notes" / "records.tsv").write_text(
        "id\tname\tcolor\n"
        "n1\tFirst\tred\n"
        "n2\tSecond\tpurple\n"
        "n4\tFourth\tyellow\n"
    )

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)

    assert preview["target"] == {"kind": "collection", "name": "notes"}
    assert preview["backup_id"] == backup_id
    assert preview["present_in_backup"] is True
    assert preview["added"] == ["n3"]
    assert preview["removed"] == ["n4"]
    assert preview["changed"] == [{"id": "n2", "fields": ["color"]}]
    assert preview["unchanged"] == 1
    assert isinstance(preview["diff_hash"], str) and len(preview["diff_hash"]) == 64

    # same inputs -> same hash
    again = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert again["diff_hash"] == preview["diff_hash"]


def test_preview_collection_absent_from_backup(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(tmp_path, monkeypatch, "id\tname\nn1\tFirst\n")

    preview = object_backup_index.preview_collection(backup_id, "other", data_dir=data_dir)
    assert preview["present_in_backup"] is False
    assert preview["added"] == []
    assert preview["removed"] == []
    assert preview["changed"] == []
    assert preview["unchanged"] == 0


def test_preview_collection_rejects_invalid_collection_name(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(tmp_path, monkeypatch, "id\tname\nn1\tFirst\n")
    with pytest.raises(ValueError):
        object_backup_index.preview_collection(backup_id, "../etc", data_dir=data_dir)


def test_preview_record_present_in_both_backup_only_and_absent(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "id\tname\nn1\tFirst\nn2\tSecond\n",
    )
    (data_dir / "collections" / "notes" / "records.tsv").write_text("id\tname\nn1\tFirst\n")

    both = object_backup_index.preview_record(backup_id, "notes", "n1", data_dir=data_dir)
    assert both["present_in_backup"] is True and both["present_in_live"] is True
    assert both["record"] == {"id": "n1", "name": "First"}

    backup_only = object_backup_index.preview_record(backup_id, "notes", "n2", data_dir=data_dir)
    assert backup_only["present_in_backup"] is True and backup_only["present_in_live"] is False
    assert backup_only["record"] == {"id": "n2", "name": "Second"}

    absent = object_backup_index.preview_record(backup_id, "notes", "nope", data_dir=data_dir)
    assert absent["present_in_backup"] is False and absent["present_in_live"] is False
    assert absent["record"] is None


# --- preview_collection / preview_record: append-mode ("_op") folding ---------
#
# docs/storage-modes.md "Current limits" used to note that preview/diff did
# not interpret `_op`. These build a records.tsv payload by hand (the
# physical on-disk format append mode writes -- see object_records.py's
# OP_FIELD/OP_UPSERT/OP_DELETE and _fold_append_rows) rather than going
# through the record engine, since object_backup_index's fold is meant to
# be a small, independent replica of that format, not a round-trip through
# object_records itself.

def _live_path(data_dir):
    return data_dir / "collections" / "notes" / "records.tsv"


def test_preview_collection_append_backup_vs_classic_live(tmp_path, monkeypatch):
    # backup: append-mode log. n2 is superseded once (final color: purple);
    # n3 is created then tombstoned (must not appear as live in the backup).
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\tcolor\n"
        "\tn1\tFirst\tred\n"
        "\tn2\tSecond\tblue\n"
        "\tn2\tSecond\tpurple\n"
        "\tn3\tThird\tgreen\n"
        "del\tn3\t\t\n",
    )

    # live: classic, mutated after the backup.
    _live_path(data_dir).write_text(
        "id\tname\tcolor\n"
        "n1\tFirst\tred\n"
        "n2\tSecond\tblue\n"
        "n4\tFourth\tyellow\n"
    )

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == []  # n3 is tombstoned in the backup -> not live there
    assert preview["removed"] == ["n4"]  # restoring would drop n4 from live
    assert preview["changed"] == [{"id": "n2", "fields": ["color"]}]  # folds to purple
    assert preview["unchanged"] == 1  # n1


def test_preview_collection_classic_backup_vs_append_live(tmp_path, monkeypatch):
    # backup: classic.
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "id\tname\tcolor\n"
        "n1\tFirst\tred\n"
        "n2\tSecond\tpurple\n",
    )

    # live: append-mode log, mutated after the backup.
    _live_path(data_dir).write_text(
        "_op\tid\tname\tcolor\n"
        "\tn1\tFirst\tred\n"
        "\tn2\tSecond\tblue\n"
        "\tn4\tFourth\tyellow\n"
    )

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == []
    assert preview["removed"] == ["n4"]
    assert preview["changed"] == [{"id": "n2", "fields": ["color"]}]
    assert preview["unchanged"] == 1


def test_preview_collection_superseded_rows_fold_to_final_values_only(tmp_path, monkeypatch):
    # id updated 3x in the log -> counts once, and the diff only ever sees
    # the final value (not any intermediate one).
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\n"
        "\tn1\tv1\n"
        "\tn1\tv2\n"
        "\tn1\tv3\n",
    )
    _live_path(data_dir).write_text("id\tname\nn1\tv3\n")

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == []
    assert preview["removed"] == []
    assert preview["changed"] == []
    assert preview["unchanged"] == 1


def test_preview_collection_tombstone_in_backup_reports_removed(tmp_path, monkeypatch):
    # Restore semantics are "live becomes the backup": a tombstoned id in
    # the backup, with the id still live now, would be dropped by a
    # restore -- so it belongs in "removed", not "added".
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "del\tn1\t\n",
    )
    _live_path(data_dir).write_text("id\tname\nn1\tFirst\n")

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == []
    assert preview["removed"] == ["n1"]
    assert preview["changed"] == []


def test_preview_collection_tombstone_in_live_reports_added(tmp_path, monkeypatch):
    # Mirror image: a tombstoned id in LIVE, with the id present in the
    # backup, would reappear on restore -- so it belongs in "added".
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "id\tname\nn1\tFirst\n",
    )
    _live_path(data_dir).write_text(
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "del\tn1\t\n"
    )

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == ["n1"]
    assert preview["removed"] == []
    assert preview["changed"] == []


def test_preview_collection_resurrection_uses_final_values(tmp_path, monkeypatch):
    # del then re-create in the log -> treated as live, holding only the
    # values from after the re-create.
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "del\tn1\t\n"
        "\tn1\tResurrected\n",
    )
    _live_path(data_dir).write_text("id\tname\nn1\tFirst\n")

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    assert preview["added"] == []
    assert preview["removed"] == []
    assert preview["changed"] == [{"id": "n1", "fields": ["name"]}]
    assert preview["unchanged"] == 0

    record = object_backup_index.preview_record(backup_id, "notes", "n1", data_dir=data_dir)
    assert record["present_in_backup"] is True
    assert record["record"] == {"id": "n1", "name": "Resurrected"}


def test_preview_op_field_never_leaks_into_changed_fields_or_record(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\tcolor\n"
        "\tn1\tFirst\tred\n"
        "\tn1\tFirst\tblue\n",
    )
    _live_path(data_dir).write_text(
        "_op\tid\tname\tcolor\n"
        "\tn1\tFirst\tgreen\n"
    )

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    for entry in preview["changed"]:
        assert "_op" not in entry["fields"]

    record = object_backup_index.preview_record(backup_id, "notes", "n1", data_dir=data_dir)
    assert "_op" not in record["record"]


def test_preview_torn_final_line_ignored_on_backup_and_live(tmp_path, monkeypatch):
    # A torn (unterminated) final physical line represents an in-flight
    # write and must be dropped, on either side.
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "\tn2\tSec",  # torn: no trailing newline
    )
    live_text = (
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "\tn1\tUpdated"  # torn: no trailing newline -- must not apply
    )
    _live_path(data_dir).write_text(live_text)
    assert not live_text.endswith("\n")

    preview = object_backup_index.preview_collection(backup_id, "notes", data_dir=data_dir)
    # backup: n2's torn row is dropped, so only n1 is live in the backup.
    # live: n1's torn update is dropped, so live n1 keeps its prior value.
    assert preview["added"] == []
    assert preview["removed"] == []
    assert preview["changed"] == []
    assert preview["unchanged"] == 1


def test_preview_diff_hash_stable_across_physical_layout(tmp_path, monkeypatch):
    # Same logical content via two different physical log layouts (one has
    # an extra superseded row) must hash identically, since diff_hash is
    # computed over the folded diff, not the raw rows.
    data_dir_a, backup_id_a = _make_backup_with_collection(
        tmp_path / "a",
        monkeypatch,
        "_op\tid\tname\n\tn1\tFirst\n",
    )
    data_dir_b, backup_id_b = _make_backup_with_collection(
        tmp_path / "b",
        monkeypatch,
        "_op\tid\tname\n\tn1\tOld\n\tn1\tFirst\n",
    )
    _live_path(data_dir_a).write_text("id\tname\nn1\tFirst\n")
    _live_path(data_dir_b).write_text("id\tname\nn1\tFirst\n")

    preview_a = object_backup_index.preview_collection(backup_id_a, "notes", data_dir=data_dir_a)
    preview_b = object_backup_index.preview_collection(backup_id_b, "notes", data_dir=data_dir_b)
    assert preview_a["diff_hash"] == preview_b["diff_hash"]


def test_preview_record_tombstoned_in_backup_reports_absent(tmp_path, monkeypatch):
    data_dir, backup_id = _make_backup_with_collection(
        tmp_path,
        monkeypatch,
        "_op\tid\tname\n"
        "\tn1\tFirst\n"
        "del\tn1\t\n",
    )
    _live_path(data_dir).write_text("id\tname\nn1\tFirst\n")

    record = object_backup_index.preview_record(backup_id, "notes", "n1", data_dir=data_dir)
    assert record["record"] is None
    assert record["present_in_backup"] is False
    assert record["present_in_live"] is True


# --- preview endpoints ---------------------------------------------------------

def test_admin_backup_preview_and_record_require_admin(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    enable_admin_token(monkeypatch)
    status, _, _ = request(
        "/admin/backups/whatever.tar.gz/preview", query_string="kind=collection&name=notes"
    )
    assert status == 401
    status, _, _ = request(
        "/admin/backups/whatever.tar.gz/record", query_string="collection=notes&id=n1"
    )
    assert status == 401


def test_admin_backup_preview_and_record_endpoints(tmp_path, monkeypatch):
    monkeypatch.delenv(object_backup.BACKUPS_DIR_ENV, raising=False)
    data_dir = tmp_path / "data"
    objects_dir = tmp_path / "objects"
    (data_dir / "collections" / "notes").mkdir(parents=True)
    (data_dir / "collections" / "notes" / "records.tsv").write_text(
        "id\tname\nn1\tFirst\nn2\tSecond\n"
    )
    objects_dir.mkdir()
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_backup.OBJECTS_DIR_ENV, str(objects_dir))
    enable_admin_token(monkeypatch)

    status, _, created = request("/admin/backups", method="POST", headers=auth_headers())
    assert status == 201, created
    backup_id = created["backup"]["id"]

    # mutate live after the backup was taken
    (data_dir / "collections" / "notes" / "records.tsv").write_text("id\tname\nn1\tFirst\n")

    status, _, payload = request(
        f"/admin/backups/{backup_id}/preview",
        query_string="kind=collection&name=notes",
        headers=auth_headers(),
    )
    assert status == 200, payload
    assert payload["status"] == "ok"
    preview = payload["preview"]
    assert preview["target"] == {"kind": "collection", "name": "notes"}
    assert preview["added"] == ["n2"]
    assert preview["present_in_backup"] is True

    status, _, payload = request(
        f"/admin/backups/{backup_id}/record",
        query_string="collection=notes&id=n2",
        headers=auth_headers(),
    )
    assert status == 200, payload
    record = payload["record"]
    assert record["present_in_backup"] is True
    assert record["present_in_live"] is False
    assert record["record"] == {"id": "n2", "name": "Second"}

    # bad kind
    status, _, payload = request(
        f"/admin/backups/{backup_id}/preview",
        query_string="kind=record&name=notes",
        headers=auth_headers(),
    )
    assert status == 400

    # missing params
    status, _, payload = request(
        f"/admin/backups/{backup_id}/preview",
        query_string="kind=collection",
        headers=auth_headers(),
    )
    assert status == 400
    status, _, payload = request(
        f"/admin/backups/{backup_id}/record",
        query_string="collection=notes",
        headers=auth_headers(),
    )
    assert status == 400

    # unknown backup id -> 404; malformed id -> 400
    status, _, payload = request(
        "/admin/backups/does-not-exist.tar.gz/preview",
        query_string="kind=collection&name=notes",
        headers=auth_headers(),
    )
    assert status == 404
    status, _, payload = request(
        "/admin/backups/..%2Fpasswd/preview",
        query_string="kind=collection&name=notes",
        headers=auth_headers(),
    )
    assert status in (400, 404)

    # method not allowed
    status, _, payload = request(
        f"/admin/backups/{backup_id}/preview",
        query_string="kind=collection&name=notes",
        method="POST",
        headers=auth_headers(),
    )
    assert status == 405
