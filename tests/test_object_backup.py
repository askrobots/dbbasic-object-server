import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

import object_backup
import object_logs
import object_state


def write_file(path: Path, content: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def make_runtime_tree(tmp_path):
    objects_dir = tmp_path / "objects"
    data_dir = tmp_path / "data"

    write_file(
        objects_dir / "site" / "home.py",
        "def GET(request):\n    return {'status': 'ok'}\n",
    )
    write_file(objects_dir / "__pycache__" / "cached.pyc", "cache")
    write_file(objects_dir / ".secret.py", "secret")

    write_file(data_dir / "state" / "site_home" / "state.tsv", "count\t3\t1.0\n")
    write_file(data_dir / "state" / "site_home" / ".state.tsv.tmp", "partial")

    write_file(
        data_dir / "logs" / "site_home" / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "cur\t2026-01-01T00:00:01Z\tINFO\tcurrent served\n",
    )
    with gzip.open(data_dir / "logs" / "site_home" / "log-20260101-000000.tsv.gz", "wt") as f:
        f.write(
            "entry_id\ttimestamp\tlevel\tmessage\n"
            "old\t2026-01-01T00:00:00Z\tINFO\trotated served\n"
        )
    write_file(data_dir / "logs" / "site_home" / ".log.tsv.lock", "")

    write_file(
        data_dir / "versions" / "site_home" / "metadata.tsv",
        "version_id\ttimestamp\tauthor\tmessage\thash\n"
        "1\t2026-01-01T00:00:00Z\ttest\tinitial\tabc\n",
    )
    write_file(data_dir / "versions" / "site_home" / "v1.txt", "def GET(request):\n    return {}\n")
    write_file(
        data_dir / "source_changes" / "site_home" / "changes.jsonl",
        '{"action":"source_update","object_id":"site_home","version_id":1}\n',
    )
    write_file(
        data_dir / "schema_versions" / "contacts" / "metadata.tsv",
        "version_id\ttimestamp\tauthor\tmessage\thash\n"
        "1\t2026-01-01T00:00:00Z\ttest\tinitial\tabc\n",
    )
    write_file(data_dir / "schema_versions" / "contacts" / "v1.json", '{"fields": []}\n')
    write_file(
        data_dir / "record_changes" / "contacts" / "changes.jsonl",
        '{"action":"create","collection":"contacts","record_id":"c1"}\n',
    )
    write_file(
        data_dir / "package_changes" / "hello-world" / "changes.jsonl",
        '{"action":"dry_run","package_id":"hello-world"}\n',
    )
    write_file(data_dir / "files" / "site_home" / "upload.txt", "file payload\n")
    write_file(data_dir / "schemas" / "contacts.json", '{"fields": [{"name": "id"}]}\n')
    write_file(data_dir / "collections" / "contacts" / "records.tsv", "id\tname\nc1\tAda\n")
    write_file(data_dir / "ratelimit" / "site_home.txt", "123\n")

    return objects_dir, data_dir


def archive_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:*") as archive:
        return archive.getnames()


def read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:*") as archive:
        manifest = archive.extractfile(object_backup.MANIFEST_NAME)
        assert manifest is not None
        return json.loads(manifest.read().decode("utf-8"))


def test_create_runtime_backup_includes_runtime_files_and_manifest(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "backup" / "runtime.tar.gz"

    summary = object_backup.create_runtime_backup(
        backup,
        objects_dir=objects_dir,
        data_dir=data_dir,
        created_at="2026-01-01T00:00:00Z",
    )

    names = archive_names(backup)
    assert object_backup.MANIFEST_NAME in names
    assert "objects/site/home.py" in names
    assert "data/state/site_home/state.tsv" in names
    assert "data/logs/site_home/log.tsv" in names
    assert "data/logs/site_home/log-20260101-000000.tsv.gz" in names
    assert "data/versions/site_home/metadata.tsv" in names
    assert "data/versions/site_home/v1.txt" in names
    assert "data/source_changes/site_home/changes.jsonl" in names
    assert "data/schema_versions/contacts/metadata.tsv" in names
    assert "data/schema_versions/contacts/v1.json" in names
    assert "data/record_changes/contacts/changes.jsonl" in names
    assert "data/package_changes/hello-world/changes.jsonl" in names
    assert "data/files/site_home/upload.txt" in names
    assert "data/schemas/contacts.json" in names
    assert "data/collections/contacts/records.tsv" in names

    assert "objects/.secret.py" not in names
    assert "objects/__pycache__/cached.pyc" not in names
    assert "data/state/site_home/.state.tsv.tmp" not in names
    assert "data/logs/site_home/.log.tsv.lock" not in names
    assert "data/ratelimit/site_home.txt" not in names

    manifest = read_manifest(backup)
    assert manifest["format_version"] == object_backup.BACKUP_FORMAT_VERSION
    assert manifest["created_at"] == "2026-01-01T00:00:00Z"
    assert manifest["files"] == summary.files == 14
    assert "deployment secrets are not included" in manifest["notes"]

    verification = object_backup.verify_runtime_backup(backup)
    assert verification.ok
    assert verification.files == 14
    assert verification.entries == [
        "data/collections",
        "data/files",
        "data/logs",
        "data/package_changes",
        "data/record_changes",
        "data/schema_versions",
        "data/schemas",
        "data/source_changes",
        "data/state",
        "data/versions",
        "objects",
    ]


def test_create_runtime_restore_point_uses_data_backups_and_excludes_existing_backups(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    write_file(data_dir / "backups" / "old-runtime.tar.gz", "old backup")

    summary = object_backup.create_runtime_restore_point(
        "package-hello-world",
        objects_dir=objects_dir,
        data_dir=data_dir,
        created_at="2026-01-01T00:00:00Z",
    )

    backup = Path(summary.path)
    assert backup.parent == data_dir / "backups"
    assert backup.name == "20260101T000000Z-package-hello-world.tar.gz"
    names = archive_names(backup)
    assert "objects/site/home.py" in names
    assert "data/backups/old-runtime.tar.gz" not in names

    manifest = read_manifest(backup)
    assert manifest["created_at"] == "2026-01-01T00:00:00Z"


def test_create_runtime_restore_point_rejects_unsafe_labels(tmp_path):
    with pytest.raises(object_backup.BackupRestoreError, match="invalid restore point label"):
        object_backup.create_runtime_restore_point(
            "../bad",
            objects_dir=tmp_path / "objects",
            data_dir=tmp_path / "data",
        )


def test_restore_runtime_backup_restores_objects_state_logs_versions_and_files(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "runtime.tar.gz"
    object_backup.create_runtime_backup(backup, objects_dir=objects_dir, data_dir=data_dir)

    restored_objects = tmp_path / "restored" / "objects"
    restored_data = tmp_path / "restored" / "data"
    summary = object_backup.restore_runtime_backup(
        backup,
        objects_dir=restored_objects,
        data_dir=restored_data,
    )

    assert summary.files == 14
    assert (restored_objects / "site" / "home.py").read_text().startswith("def GET")
    assert object_state.get_object_state("site_home", base_dir=restored_data) == {"count": 3}
    assert (restored_data / "schema_versions" / "contacts" / "v1.json").exists()

    logs = object_logs.get_object_logs("site_home", base_dir=restored_data)
    assert [entry["message"] for entry in logs] == ["current served", "rotated served"]

    assert (restored_data / "versions" / "site_home" / "metadata.tsv").exists()
    assert (restored_data / "source_changes" / "site_home" / "changes.jsonl").exists()
    assert (restored_data / "record_changes" / "contacts" / "changes.jsonl").exists()
    assert (restored_data / "package_changes" / "hello-world" / "changes.jsonl").exists()
    assert (restored_data / "files" / "site_home" / "upload.txt").read_text() == "file payload\n"
    assert (restored_data / "schemas" / "contacts.json").exists()
    assert (restored_data / "collections" / "contacts" / "records.tsv").read_text() == "id\tname\nc1\tAda\n"


def test_restore_refuses_to_overwrite_existing_runtime_files(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "runtime.tar.gz"
    object_backup.create_runtime_backup(backup, objects_dir=objects_dir, data_dir=data_dir)

    restored_objects = tmp_path / "restored" / "objects"
    restored_data = tmp_path / "restored" / "data"
    object_backup.restore_runtime_backup(backup, objects_dir=restored_objects, data_dir=restored_data)

    with pytest.raises(object_backup.BackupRestoreError, match="already exists"):
        object_backup.restore_runtime_backup(backup, objects_dir=restored_objects, data_dir=restored_data)

    result = object_backup.restore_runtime_backup(
        backup,
        objects_dir=restored_objects,
        data_dir=restored_data,
        overwrite=True,
    )
    assert result.overwritten is True


def test_restore_runtime_backup_can_prune_extra_runtime_files(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "runtime.tar.gz"
    object_backup.create_runtime_backup(backup, objects_dir=objects_dir, data_dir=data_dir)

    restored_objects = tmp_path / "restored" / "objects"
    restored_data = tmp_path / "restored" / "data"
    object_backup.restore_runtime_backup(backup, objects_dir=restored_objects, data_dir=restored_data)

    write_file(restored_objects / "new" / "package_object.py", "def GET(request): return {}\n")
    write_file(restored_data / "collections" / "new" / "records.tsv", "id\tname\nn1\tnew\n")
    write_file(restored_data / "backups" / "keep-this.tar.gz", "backup payload\n")

    result = object_backup.restore_runtime_backup(
        backup,
        objects_dir=restored_objects,
        data_dir=restored_data,
        overwrite=True,
        prune_extra=True,
    )

    assert result.pruned_files == 2
    assert result.pruned_dirs >= 2
    assert not (restored_objects / "new" / "package_object.py").exists()
    assert not (restored_data / "collections" / "new" / "records.tsv").exists()
    assert (restored_data / "backups" / "keep-this.tar.gz").exists()


def test_restore_runtime_backup_prune_requires_overwrite(tmp_path):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "runtime.tar.gz"
    object_backup.create_runtime_backup(backup, objects_dir=objects_dir, data_dir=data_dir)

    with pytest.raises(object_backup.BackupRestoreError, match="prune_extra requires overwrite"):
        object_backup.restore_runtime_backup(
            backup,
            objects_dir=tmp_path / "restored" / "objects",
            data_dir=tmp_path / "restored" / "data",
            prune_extra=True,
        )


def test_verify_and_restore_reject_path_traversal_member(tmp_path):
    backup = tmp_path / "bad.tar.gz"
    with tarfile.open(backup, "w:gz") as archive:
        _add_manifest(archive, files=1, bytes_count=4)
        info = tarfile.TarInfo("../outside.py")
        payload = b"evil"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    verification = object_backup.verify_runtime_backup(backup)
    assert not verification.ok
    assert any("unsafe archive member path" in error for error in verification.errors)

    with pytest.raises(object_backup.BackupRestoreError, match="unsafe archive member path"):
        object_backup.restore_runtime_backup(
            backup,
            objects_dir=tmp_path / "objects",
            data_dir=tmp_path / "data",
        )


def test_verify_and_restore_reject_link_members(tmp_path):
    backup = tmp_path / "link.tar.gz"
    with tarfile.open(backup, "w:gz") as archive:
        _add_manifest(archive, files=0, bytes_count=0)
        info = tarfile.TarInfo("objects/link.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)

    verification = object_backup.verify_runtime_backup(backup)
    assert not verification.ok
    assert any("uses links" in error for error in verification.errors)

    with pytest.raises(object_backup.BackupRestoreError, match="uses links"):
        object_backup.restore_runtime_backup(
            backup,
            objects_dir=tmp_path / "objects",
            data_dir=tmp_path / "data",
        )


def test_cli_create_verify_and_restore_json(tmp_path, capsys):
    objects_dir, data_dir = make_runtime_tree(tmp_path)
    backup = tmp_path / "runtime.tar.gz"

    create_exit = object_backup.main(
        [
            "create",
            str(backup),
            "--objects-dir",
            str(objects_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ]
    )
    create_payload = json.loads(capsys.readouterr().out)
    assert create_exit == 0
    assert create_payload["files"] == 14

    verify_exit = object_backup.main(["verify", str(backup), "--json"])
    verify_payload = json.loads(capsys.readouterr().out)
    assert verify_exit == 0
    assert verify_payload["ok"] is True

    restore_exit = object_backup.main(
        [
            "restore",
            str(backup),
            "--objects-dir",
            str(tmp_path / "restored" / "objects"),
            "--data-dir",
            str(tmp_path / "restored" / "data"),
            "--json",
        ]
    )
    restore_payload = json.loads(capsys.readouterr().out)
    assert restore_exit == 0
    assert restore_payload["files"] == 14


def _add_manifest(archive: tarfile.TarFile, *, files: int, bytes_count: int):
    manifest = {
        "format_version": object_backup.BACKUP_FORMAT_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "type": "dbbasic-runtime",
        "entries": [],
        "files": files,
        "bytes": bytes_count,
        "warnings": [],
    }
    payload = json.dumps(manifest).encode("utf-8")
    info = tarfile.TarInfo(object_backup.MANIFEST_NAME)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))
