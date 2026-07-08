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
    assert caps["scheduled"] is True and caps["schedule"] == "daily"
