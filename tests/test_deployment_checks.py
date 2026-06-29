import json
import stat
from pathlib import Path

import deployment_checks


def chmod(path: Path, mode: int):
    path.chmod(mode)
    return path


def current_owner_group(path: Path) -> tuple[str, str]:
    owner, group = deployment_checks._owner_group(path)
    return owner, group


def make_layout(tmp_path):
    code_dir = tmp_path / "opt" / "dbbasic-object-server"
    objects_dir = tmp_path / "var" / "lib" / "dbbasic-object-server" / "objects"
    data_dir = tmp_path / "var" / "lib" / "dbbasic-object-server" / "data"
    env_file = tmp_path / "etc" / "dbbasic-object-server.env"
    service_file = tmp_path / "etc" / "systemd" / "system" / "dbbasic-object-server.service"
    journald_dropin = tmp_path / "etc" / "systemd" / "journald.conf.d" / "99-dbbasic.conf"

    for directory in [code_dir, objects_dir, data_dir, env_file.parent, service_file.parent, journald_dropin.parent]:
        directory.mkdir(parents=True, exist_ok=True)
    env_file.write_text("DBBASIC_ENABLE_SOURCE_WRITES=false\n")
    service_file.write_text(
        "[Service]\n"
        "User=dbbasic\n"
        "ExecStart=/opt/dbbasic-object-server/.venv/bin/uvicorn object_server:app "
        "--host 127.0.0.1 --port 8001 --no-access-log\n"
    )
    journald_dropin.write_text("[Journal]\nSystemMaxUse=128M\nMaxRetentionSec=7day\n")
    chmod(code_dir, 0o755)
    chmod(objects_dir, 0o750)
    chmod(data_dir, 0o750)
    chmod(env_file, 0o640)
    chmod(service_file, 0o644)
    chmod(journald_dropin, 0o644)

    return code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin


def check_layout(tmp_path, **overrides):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    owner, group = current_owner_group(code_dir)
    defaults = {
        "code_dir": code_dir,
        "objects_dir": objects_dir,
        "data_dir": data_dir,
        "env_file": env_file,
        "service_file": service_file,
        "journald_dropin": journald_dropin,
        "service_user": owner,
        "service_group": group,
        "env_owner": owner,
        "system_owner": owner,
        "system_group": group,
    }
    defaults.update(overrides)
    return deployment_checks.check_single_vm_layout(**defaults)


def test_single_vm_layout_passes_for_expected_paths(tmp_path):
    results = check_layout(tmp_path)

    assert [result.status for result in results] == ["ok", "ok", "ok", "ok", "ok", "ok"]
    assert not deployment_checks.has_errors(results)


def test_runtime_directories_warn_when_visible_to_other_users(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    objects_dir.chmod(0o755)
    data_dir.chmod(0o755)
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    warnings = [result for result in results if result.status == "warning"]
    assert [warning.name for warning in warnings] == [
        "object source directory",
        "data directory",
    ]
    assert not deployment_checks.has_errors(results)


def test_group_writable_runtime_directory_is_an_error(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    data_dir.chmod(0o770)
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(result.status == "error" and result.name == "data directory" for result in results)
    assert deployment_checks.has_errors(results)


def test_environment_file_rejects_world_readable_secrets(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    env_file.chmod(0o644)
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(result.status == "error" and result.name == "environment file" for result in results)
    assert deployment_checks.has_errors(results)


def test_missing_service_file_is_an_error(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    service_file.unlink()
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(result.status == "error" and result.name == "systemd service file" for result in results)
    assert deployment_checks.has_errors(results)


def test_service_file_warns_when_uvicorn_access_logs_are_enabled(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    service_file.write_text(
        "[Service]\n"
        "ExecStart=/opt/dbbasic-object-server/.venv/bin/uvicorn "
        "object_server:app --host 127.0.0.1 --port 8001\n"
    )
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(
        result.status == "warning"
        and result.name == "systemd service file"
        and "access logs are enabled" in result.message
        for result in results
    )
    assert not deployment_checks.has_errors(results)


def test_missing_journald_dropin_warns_without_failing(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    journald_dropin.unlink()
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(
        result.status == "warning"
        and result.name == "journald retention drop-in"
        and "missing" in result.message
        for result in results
    )
    assert not deployment_checks.has_errors(results)


def test_journald_dropin_warns_when_missing_size_cap(tmp_path):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    journald_dropin.write_text("[Journal]\nMaxRetentionSec=7day\n")
    owner, group = current_owner_group(code_dir)

    results = deployment_checks.check_single_vm_layout(
        code_dir=code_dir,
        objects_dir=objects_dir,
        data_dir=data_dir,
        env_file=env_file,
        service_file=service_file,
        journald_dropin=journald_dropin,
        service_user=owner,
        service_group=group,
        env_owner=owner,
        system_owner=owner,
        system_group=group,
    )

    assert any(
        result.status == "warning"
        and result.name == "journald retention drop-in"
        and "missing journal size cap" in result.message
        for result in results
    )
    assert not deployment_checks.has_errors(results)


def test_cli_returns_json_and_success_for_warnings(tmp_path, capsys):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    objects_dir.chmod(0o755)
    owner, group = current_owner_group(code_dir)

    exit_code = deployment_checks.main(
        [
            "--code-dir",
            str(code_dir),
            "--objects-dir",
            str(objects_dir),
            "--data-dir",
            str(data_dir),
            "--env-file",
            str(env_file),
            "--service-file",
            str(service_file),
            "--journald-dropin",
            str(journald_dropin),
            "--service-user",
            owner,
            "--service-group",
            group,
            "--env-owner",
            owner,
            "--system-owner",
            owner,
            "--system-group",
            group,
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert any(result["status"] == "warning" for result in payload)


def test_cli_uses_environment_paths(tmp_path, monkeypatch):
    code_dir, objects_dir, data_dir, env_file, service_file, journald_dropin = make_layout(tmp_path)
    owner, group = current_owner_group(code_dir)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    exit_code = deployment_checks.main(
        [
            "--code-dir",
            str(code_dir),
            "--env-file",
            str(env_file),
            "--service-file",
            str(service_file),
            "--journald-dropin",
            str(journald_dropin),
            "--service-user",
            owner,
            "--service-group",
            group,
            "--env-owner",
            owner,
            "--system-owner",
            owner,
            "--system-group",
            group,
        ]
    )

    assert exit_code == 0


def test_mode_format_includes_leading_zero():
    assert deployment_checks._mode(stat.S_IMODE(0o750)) == "0750"
