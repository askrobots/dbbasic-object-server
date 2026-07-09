"""Tests for the backup inventory module and the admin backup endpoints."""

import json

import pytest

import object_backup
import object_backup_index
import object_server

from test_object_server import (
    auth_headers,
    enable_admin_token,
    raw_request,
    request,
)


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
