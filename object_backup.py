"""Runtime backup and restore helpers for DBBASIC Object Server.

The runtime backup format is intentionally narrow. It captures live object
source and runtime-owned data, but it does not include deployment secrets,
systemd units, provider metadata, virtualenvs, caches, or git history.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from object_namespace import DEFAULT_OBJECTS_DIR, OBJECTS_DIR_ENV
from object_versions import DEFAULT_DATA_DIR

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
BACKUPS_DIR_ENV = "DBBASIC_BACKUPS_DIR"
BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "dbbasic-backup-manifest.json"
BACKUPS_DIR = "backups"
RUNTIME_DATA_DIRS = (
    "state",
    "logs",
    "versions",
    "schema_versions",
    "record_changes",
    "package_changes",
    "files",
    "schemas",
    "collections",
)
SKIP_PARTS = {"__pycache__", ".git", ".venv", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".lock")
RESTORE_POINT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class BackupError(Exception):
    """Base exception for backup operations."""


class BackupRestoreError(BackupError):
    """Raised when a backup cannot be safely restored."""


@dataclass(frozen=True)
class BackupSummary:
    path: str
    format_version: int
    created_at: str
    files: int
    bytes: int
    entries: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackupVerification:
    path: str
    ok: bool
    format_version: int | None
    files: int
    bytes: int
    entries: list[str]
    errors: list[str]
    warnings: list[str]
    manifest: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestoreSummary:
    backup_path: str
    objects_dir: str
    data_dir: str
    files: int
    bytes: int
    overwritten: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_runtime_backup(
    output_path: Path | str,
    *,
    objects_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    created_at: str | None = None,
) -> BackupSummary:
    """Create a tar/gzip archive containing object source and runtime data."""
    output = Path(output_path)
    objects_path = _objects_dir(objects_dir)
    data_path = _data_dir(data_dir)
    timestamp = created_at or _utc_timestamp()

    members: list[tuple[Path, PurePosixPath]] = []
    warnings: list[str] = []
    entries: list[str] = []
    file_count = 0
    byte_count = 0

    for source_path, archive_root in _runtime_sources(objects_path, data_path):
        if not source_path.exists():
            continue
        if not source_path.is_dir():
            warnings.append(f"skipped non-directory source: {archive_root.as_posix()}")
            continue

        entries.append(archive_root.as_posix())
        for path, archive_name in _iter_backup_members(source_path, archive_root):
            members.append((path, archive_name))
            if path.is_file():
                file_count += 1
                byte_count += path.stat().st_size

    if file_count == 0:
        warnings.append("backup contains no runtime files")

    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": timestamp,
        "type": "dbbasic-runtime",
        "entries": entries,
        "files": file_count,
        "bytes": byte_count,
        "notes": [
            "deployment secrets are not included",
            "systemd service files are not included",
            "git history and virtualenvs are not included",
        ],
        "warnings": warnings,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        _add_manifest(archive, manifest)
        for path, archive_name in members:
            _add_path(archive, path, archive_name)

    return BackupSummary(
        path=str(output),
        format_version=BACKUP_FORMAT_VERSION,
        created_at=timestamp,
        files=file_count,
        bytes=byte_count,
        entries=entries,
        warnings=warnings,
    )


def create_runtime_restore_point(
    label: str,
    *,
    objects_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    backups_dir: Path | str | None = None,
    created_at: str | None = None,
) -> BackupSummary:
    """Create a named runtime backup under the configured restore-point directory."""
    clean_label = _restore_point_label(label)
    timestamp = created_at or _utc_timestamp()
    output_dir = _backups_dir(backups_dir, data_dir=data_dir)
    output_path = output_dir / f"{_backup_filename_timestamp(timestamp)}-{clean_label}.tar.gz"
    return create_runtime_backup(
        output_path,
        objects_dir=objects_dir,
        data_dir=data_dir,
        created_at=timestamp,
    )


def verify_runtime_backup(backup_path: Path | str) -> BackupVerification:
    """Inspect a runtime backup archive without extracting it."""
    path = Path(backup_path)
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, Any] | None = None
    entries: set[str] = set()
    file_count = 0
    byte_count = 0

    try:
        with tarfile.open(path, "r:*") as archive:
            manifest = _read_manifest(archive, errors)
            for member in archive.getmembers():
                if member.name == MANIFEST_NAME:
                    continue
                errors.extend(_validate_member(member))
                root = _member_entry(member.name)
                if root is not None:
                    entries.add(root)
                if member.isfile():
                    file_count += 1
                    byte_count += member.size
    except (tarfile.TarError, OSError) as exc:
        errors.append(f"cannot read backup archive: {exc}")

    format_version = None
    if manifest is not None:
        format_version = _manifest_int(manifest.get("format_version"))
        if format_version != BACKUP_FORMAT_VERSION:
            errors.append(f"unsupported backup format version: {format_version}")
        if _manifest_int(manifest.get("files")) != file_count:
            errors.append("manifest file count does not match archive")
        if _manifest_int(manifest.get("bytes")) != byte_count:
            errors.append("manifest byte count does not match archive")
        warnings.extend(str(warning) for warning in manifest.get("warnings", []))

    return BackupVerification(
        path=str(path),
        ok=not errors,
        format_version=format_version,
        files=file_count,
        bytes=byte_count,
        entries=sorted(entries),
        errors=errors,
        warnings=warnings,
        manifest=manifest,
    )


def restore_runtime_backup(
    backup_path: Path | str,
    *,
    objects_dir: Path | str,
    data_dir: Path | str,
    overwrite: bool = False,
) -> RestoreSummary:
    """Restore a runtime backup into explicit object and data directories."""
    backup = Path(backup_path)
    objects_path = Path(objects_dir)
    data_path = Path(data_dir)
    verification = verify_runtime_backup(backup)
    if not verification.ok:
        raise BackupRestoreError("; ".join(verification.errors))

    planned: list[tuple[tarfile.TarInfo, Path]] = []
    with tarfile.open(backup, "r:*") as archive:
        for member in archive.getmembers():
            if member.name == MANIFEST_NAME:
                continue
            destination = _member_destination(member.name, objects_path, data_path)
            planned.append((member, destination))
            if member.isdir():
                if destination.exists() and not destination.is_dir():
                    raise BackupRestoreError(f"restore target is not a directory: {destination}")
                continue
            if destination.exists() and not overwrite:
                raise BackupRestoreError(f"restore target already exists: {destination}")

        restored_files = 0
        restored_bytes = 0
        for member, destination in planned:
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                _chmod_from_member(destination, member)
                continue

            extracted = archive.extractfile(member)
            if extracted is None:
                raise BackupRestoreError(f"cannot read archive member: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_restored_file(destination, extracted.read(), member)
            restored_files += 1
            restored_bytes += member.size

    return RestoreSummary(
        backup_path=str(backup),
        objects_dir=str(objects_path),
        data_dir=str(data_path),
        files=restored_files,
        bytes=restored_bytes,
        overwritten=overwrite,
    )


def _objects_dir(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value)
    return Path(os.environ.get(OBJECTS_DIR_ENV, DEFAULT_OBJECTS_DIR))


def _data_dir(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value)
    return Path(os.environ.get(DATA_DIR_ENV, DEFAULT_DATA_DIR))


def _backups_dir(value: Path | str | None, *, data_dir: Path | str | None) -> Path:
    if value is not None:
        return Path(value)
    env_value = os.environ.get(BACKUPS_DIR_ENV)
    if env_value:
        return Path(env_value)
    return _data_dir(data_dir) / BACKUPS_DIR


def _restore_point_label(value: str) -> str:
    label = str(value).strip().lower()
    if not RESTORE_POINT_LABEL_RE.fullmatch(label):
        raise BackupRestoreError(f"invalid restore point label: {value!r}")
    return label


def _backup_filename_timestamp(timestamp: str) -> str:
    compact = timestamp.replace("+00:00", "Z")
    compact = compact.replace("-", "").replace(":", "").replace(".", "")
    compact = compact.replace("+", "")
    if not compact.endswith("Z"):
        compact = f"{compact}Z"
    return compact


def _runtime_sources(objects_dir: Path, data_dir: Path) -> list[tuple[Path, PurePosixPath]]:
    sources = [(objects_dir, PurePosixPath("objects"))]
    for name in RUNTIME_DATA_DIRS:
        sources.append((data_dir / name, PurePosixPath("data") / name))
    return sources


def _iter_backup_members(
    source_root: Path,
    archive_root: PurePosixPath,
) -> list[tuple[Path, PurePosixPath]]:
    members: list[tuple[Path, PurePosixPath]] = [(source_root, archive_root)]
    children = sorted(source_root.rglob("*"), key=lambda path: path.relative_to(source_root).as_posix())
    for path in children:
        if _should_skip(path, source_root):
            continue
        relative = path.relative_to(source_root).as_posix()
        members.append((path, archive_root / relative))
    return members


def _should_skip(path: Path, source_root: Path) -> bool:
    relative = path.relative_to(source_root)
    parts = relative.parts
    if any(part in SKIP_PARTS for part in parts):
        return True
    if any(part.startswith(".") for part in parts):
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    return not (path.is_dir() or path.is_file())


def _add_manifest(archive: tarfile.TarFile, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    info = tarfile.TarInfo(MANIFEST_NAME)
    info.size = len(payload)
    info.mode = 0o644
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    _normalize_tarinfo(info)
    archive.addfile(info, io.BytesIO(payload))


def _add_path(archive: tarfile.TarFile, path: Path, archive_name: PurePosixPath) -> None:
    info = archive.gettarinfo(str(path), arcname=archive_name.as_posix())
    _normalize_tarinfo(info)
    if path.is_dir():
        archive.addfile(info)
        return
    with path.open("rb") as file_obj:
        archive.addfile(info, file_obj)


def _normalize_tarinfo(info: tarfile.TarInfo) -> None:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}


def _read_manifest(archive: tarfile.TarFile, errors: list[str]) -> dict[str, Any] | None:
    try:
        member = archive.getmember(MANIFEST_NAME)
    except KeyError:
        errors.append("backup manifest is missing")
        return None

    manifest_file = archive.extractfile(member)
    if manifest_file is None:
        errors.append("backup manifest cannot be read")
        return None

    try:
        payload = json.loads(manifest_file.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"backup manifest is invalid JSON: {exc}")
        return None

    if not isinstance(payload, dict):
        errors.append("backup manifest must be a JSON object")
        return None
    return payload


def _validate_member(member: tarfile.TarInfo) -> list[str]:
    errors: list[str] = []
    try:
        path = _safe_archive_path(member.name)
    except BackupRestoreError as exc:
        return [str(exc)]

    if member.issym() or member.islnk():
        errors.append(f"archive member uses links: {member.name}")
    elif not (member.isfile() or member.isdir()):
        errors.append(f"archive member has unsupported type: {member.name}")

    parts = path.parts
    if not parts:
        errors.append("archive member has empty name")
    elif parts[0] == "objects":
        pass
    elif parts[0] == "data":
        if len(parts) == 1:
            pass
        elif parts[1] not in RUNTIME_DATA_DIRS:
            errors.append(f"archive member uses unsupported data directory: {member.name}")
    else:
        errors.append(f"archive member is outside runtime backup roots: {member.name}")

    return errors


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute():
        raise BackupRestoreError(f"unsafe archive member path: {name!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise BackupRestoreError(f"unsafe archive member path: {name!r}")
    return path


def _member_entry(name: str) -> str | None:
    try:
        path = _safe_archive_path(name)
    except BackupRestoreError:
        return None
    if path.parts[0] == "objects":
        return "objects"
    if path.parts[0] == "data" and len(path.parts) > 1:
        return f"data/{path.parts[1]}"
    return None


def _member_destination(name: str, objects_dir: Path, data_dir: Path) -> Path:
    path = _safe_archive_path(name)
    parts = path.parts
    if parts[0] == "objects":
        destination = objects_dir.joinpath(*parts[1:])
        _ensure_under(destination, objects_dir)
        return destination

    if parts[0] == "data":
        if len(parts) == 1:
            destination = data_dir
        elif parts[1] in RUNTIME_DATA_DIRS:
            destination = data_dir.joinpath(*parts[1:])
        else:
            raise BackupRestoreError(f"unsupported data directory in backup: {name}")
        _ensure_under(destination, data_dir)
        return destination

    raise BackupRestoreError(f"unsupported backup root: {name}")


def _ensure_under(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise BackupRestoreError(f"restore target escapes runtime root: {path}") from exc


def _write_restored_file(destination: Path, payload: bytes, member: tarfile.TarInfo) -> None:
    temp_path = destination.with_name(f".{destination.name}.restore.tmp")
    with temp_path.open("wb") as file_obj:
        file_obj.write(payload)
    temp_path.replace(destination)
    _chmod_from_member(destination, member)


def _chmod_from_member(path: Path, member: tarfile.TarInfo) -> None:
    mode = member.mode & 0o777
    if mode:
        path.chmod(mode)


def _manifest_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, verify, and restore DBBASIC runtime backups.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create", help="create a runtime backup")
    create.add_argument("output", type=Path)
    create.add_argument("--objects-dir", type=Path)
    create.add_argument("--data-dir", type=Path)
    create.add_argument("--json", action="store_true")

    verify = subcommands.add_parser("verify", help="verify a runtime backup")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--json", action="store_true")

    restore = subcommands.add_parser("restore", help="restore a runtime backup")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--objects-dir", type=Path, required=True)
    restore.add_argument("--data-dir", type=Path, required=True)
    restore.add_argument("--overwrite", action="store_true")
    restore.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            result = create_runtime_backup(
                args.output,
                objects_dir=args.objects_dir,
                data_dir=args.data_dir,
            )
            _print_result(result.to_dict(), json_output=args.json)
            return 0
        if args.command == "verify":
            result = verify_runtime_backup(args.backup)
            _print_result(result.to_dict(), json_output=args.json)
            return 0 if result.ok else 1
        if args.command == "restore":
            result = restore_runtime_backup(
                args.backup,
                objects_dir=args.objects_dir,
                data_dir=args.data_dir,
                overwrite=args.overwrite,
            )
            _print_result(result.to_dict(), json_output=args.json)
            return 0
    except BackupError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}")
        return 1

    return 1


def _print_result(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, indent=2))
        return

    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
