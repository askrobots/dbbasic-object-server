"""Deployment checks for the conservative single-VM layout."""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

DEFAULT_CODE_DIR = Path("/opt/dbbasic-object-server")
DEFAULT_OBJECTS_DIR = Path("/var/lib/dbbasic-object-server/objects")
DEFAULT_DATA_DIR = Path("/var/lib/dbbasic-object-server/data")
DEFAULT_ENV_FILE = Path("/etc/dbbasic-object-server.env")
DEFAULT_SERVICE_FILE = Path("/etc/systemd/system/dbbasic-object-server.service")
DEFAULT_SERVICE_USER = "dbbasic"
DEFAULT_SERVICE_GROUP = "dbbasic"

Status = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    path: str
    status: Status
    message: str


def check_single_vm_layout(
    *,
    code_dir: Path | str = DEFAULT_CODE_DIR,
    objects_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    env_file: Path | str = DEFAULT_ENV_FILE,
    service_file: Path | str = DEFAULT_SERVICE_FILE,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
    env_owner: str = "root",
    system_owner: str = "root",
    system_group: str = "root",
) -> list[CheckResult]:
    """Return filesystem placement and permission checks for one VM."""
    objects_path = Path(
        objects_dir
        if objects_dir is not None
        else os.environ.get("DBBASIC_OBJECTS_DIR", DEFAULT_OBJECTS_DIR)
    )
    data_path = Path(
        data_dir if data_dir is not None else os.environ.get("DBBASIC_DATA_DIR", DEFAULT_DATA_DIR)
    )

    results: list[CheckResult] = []
    results.extend(
        _check_service_directory(
            "code directory",
            Path(code_dir),
            expected_user=service_user,
            expected_group=service_group,
            private_runtime=False,
        )
    )
    results.extend(
        _check_service_directory(
            "object source directory",
            objects_path,
            expected_user=service_user,
            expected_group=service_group,
            private_runtime=True,
        )
    )
    results.extend(
        _check_service_directory(
            "data directory",
            data_path,
            expected_user=service_user,
            expected_group=service_group,
            private_runtime=True,
        )
    )
    results.extend(_check_env_file(Path(env_file), expected_owner=env_owner, expected_group=service_group))
    results.extend(
        _check_service_file(
            Path(service_file),
            expected_owner=system_owner,
            expected_group=system_group,
        )
    )
    return results


def has_errors(results: list[CheckResult]) -> bool:
    return any(result.status == "error" for result in results)


def _check_service_directory(
    name: str,
    path: Path,
    *,
    expected_user: str,
    expected_group: str,
    private_runtime: bool,
) -> list[CheckResult]:
    if not path.exists():
        return [_result(name, path, "error", "missing")]
    if not path.is_dir():
        return [_result(name, path, "error", "not a directory")]

    results = _check_owner(path, name, expected_user, expected_group)
    mode = stat.S_IMODE(path.stat().st_mode)

    if not mode & stat.S_IRUSR or not mode & stat.S_IWUSR or not mode & stat.S_IXUSR:
        results.append(_result(name, path, "error", f"owner needs rwx permissions, mode is {_mode(mode)}"))

    if mode & stat.S_IWGRP or mode & stat.S_IWOTH:
        results.append(_result(name, path, "error", f"group/world writable, mode is {_mode(mode)}"))

    if private_runtime and mode & (stat.S_IROTH | stat.S_IXOTH):
        results.append(
            _result(
                name,
                path,
                "warning",
                f"runtime path is visible to other local users, mode is {_mode(mode)}",
            )
        )

    if not results:
        results.append(_result(name, path, "ok", f"directory mode {_mode(mode)}"))
    return results


def _check_env_file(path: Path, *, expected_owner: str, expected_group: str) -> list[CheckResult]:
    name = "environment file"
    if not path.exists():
        return [_result(name, path, "error", "missing")]
    if not path.is_file():
        return [_result(name, path, "error", "not a file")]

    results: list[CheckResult] = []
    owner, group = _owner_group(path)
    mode = stat.S_IMODE(path.stat().st_mode)

    if owner != expected_owner:
        results.append(_result(name, path, "error", f"expected owner {expected_owner}, found {owner}"))
    if group not in {"root", expected_group}:
        results.append(
            _result(name, path, "warning", f"expected group root or {expected_group}, found {group}")
        )
    if mode & stat.S_IWGRP or mode & stat.S_IRWXO:
        results.append(
            _result(name, path, "error", f"too permissive for deployment secrets, mode is {_mode(mode)}")
        )
    if mode & (stat.S_IXUSR | stat.S_IXGRP):
        results.append(_result(name, path, "warning", f"environment file is executable, mode is {_mode(mode)}"))

    if not results:
        results.append(_result(name, path, "ok", f"file mode {_mode(mode)}"))
    return results


def _check_service_file(path: Path, *, expected_owner: str, expected_group: str) -> list[CheckResult]:
    name = "systemd service file"
    if not path.exists():
        return [_result(name, path, "error", "missing")]
    if not path.is_file():
        return [_result(name, path, "error", "not a file")]

    results: list[CheckResult] = []
    owner, group = _owner_group(path)
    mode = stat.S_IMODE(path.stat().st_mode)

    if owner != expected_owner:
        results.append(_result(name, path, "error", f"expected owner {expected_owner}, found {owner}"))
    if group != expected_group:
        results.append(_result(name, path, "warning", f"expected group {expected_group}, found {group}"))
    if mode & stat.S_IWGRP or mode & stat.S_IWOTH:
        results.append(_result(name, path, "error", f"group/world writable, mode is {_mode(mode)}"))

    if not results:
        results.append(_result(name, path, "ok", f"file mode {_mode(mode)}"))
    return results


def _check_owner(path: Path, name: str, expected_user: str, expected_group: str) -> list[CheckResult]:
    owner, group = _owner_group(path)
    results: list[CheckResult] = []
    if owner != expected_user:
        results.append(_result(name, path, "error", f"expected owner {expected_user}, found {owner}"))
    if group != expected_group:
        results.append(_result(name, path, "error", f"expected group {expected_group}, found {group}"))
    return results


def _owner_group(path: Path) -> tuple[str, str]:
    info = path.stat()
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = str(info.st_uid)
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = str(info.st_gid)
    return owner, group


def _result(name: str, path: Path, status: Status, message: str) -> CheckResult:
    return CheckResult(name=name, path=str(path), status=status, message=message)


def _mode(mode: int) -> str:
    return format(mode, "04o")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check DBBASIC single-VM filesystem layout.")
    parser.add_argument("--code-dir", type=Path, default=DEFAULT_CODE_DIR)
    parser.add_argument("--objects-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--service-file", type=Path, default=DEFAULT_SERVICE_FILE)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    parser.add_argument("--env-owner", default="root")
    parser.add_argument("--system-owner", default="root")
    parser.add_argument("--system-group", default="root")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    results = check_single_vm_layout(
        code_dir=args.code_dir,
        objects_dir=args.objects_dir,
        data_dir=args.data_dir,
        env_file=args.env_file,
        service_file=args.service_file,
        service_user=args.service_user,
        service_group=args.service_group,
        env_owner=args.env_owner,
        system_owner=args.system_owner,
        system_group=args.system_group,
    )

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            print(f"{result.status.upper():7} {result.name}: {result.path} - {result.message}")

    return 1 if has_errors(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
