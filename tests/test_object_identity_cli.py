"""Tests for the identity management CLI."""

import json

import pytest

import object_credentials
import object_identity
import object_identity_cli


def run_cli(args, tmp_path):
    return object_identity_cli.main(["--data-dir", str(tmp_path), *args])


def test_create_account_and_user(tmp_path, capsys):
    assert run_cli(["create-account", "--account-id", "acme", "--name", "Acme"], tmp_path) == 0
    assert (
        run_cli(
            [
                "create-user",
                "--user-id",
                "dan",
                "--email",
                "dan@example.com",
                "--account-id",
                "acme",
                "--roles",
                "admin,ops",
            ],
            tmp_path,
        )
        == 0
    )

    users = object_identity.list_users(base_dir=tmp_path)
    assert users[0]["user_id"] == "dan"
    assert users[0]["roles"] == ["admin", "ops"]
    output = capsys.readouterr().out
    assert "Created account acme" in output
    assert "Created user dan" in output


def test_create_superuser_sets_admin_role_and_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("long enough password\n"))

    exit_code = run_cli(
        ["create-superuser", "--user-id", "dan", "--email", "dan@example.com", "--password-stdin"],
        tmp_path,
    )

    assert exit_code == 0
    user = object_identity.get_user("dan", base_dir=tmp_path)
    assert user["roles"] == ["admin"]
    assert object_credentials.verify_password("dan", "long enough password", base_dir=tmp_path)
    output = capsys.readouterr().out
    assert "long enough password" not in output


def test_set_password_prompts_and_rejects_mismatch(tmp_path, monkeypatch, capsys):
    run_cli(["create-user", "--user-id", "dan"], tmp_path)

    prompts = iter(["first password!", "different password"])
    monkeypatch.setattr(object_identity_cli.getpass, "getpass", lambda _: next(prompts))

    assert run_cli(["set-password", "--user-id", "dan"], tmp_path) == 1
    assert "passwords do not match" in capsys.readouterr().err
    assert not object_credentials.has_password("dan", base_dir=tmp_path)


def test_set_password_unknown_user_fails(tmp_path, capsys):
    assert run_cli(["set-password", "--user-id", "ghost"], tmp_path) == 1
    assert "User not found" in capsys.readouterr().err


def test_remove_password(tmp_path, monkeypatch, capsys):
    run_cli(["create-user", "--user-id", "dan"], tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("long enough password\n"))
    run_cli(["set-password", "--user-id", "dan", "--password-stdin"], tmp_path)

    assert run_cli(["remove-password", "--user-id", "dan"], tmp_path) == 0
    assert not object_credentials.has_password("dan", base_dir=tmp_path)
    assert "Password removed" in capsys.readouterr().out


def test_list_users_reports_password_state(tmp_path, monkeypatch, capsys):
    run_cli(["create-user", "--user-id", "dan", "--email", "dan@example.com"], tmp_path)
    run_cli(["create-user", "--user-id", "sam"], tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("long enough password\n"))
    run_cli(["set-password", "--user-id", "dan", "--password-stdin"], tmp_path)
    capsys.readouterr()

    assert run_cli(["list-users"], tmp_path) == 0
    output = capsys.readouterr().out

    assert "dan" in output and "password" in output
    assert "sam" in output and "no-password" in output
    assert "2 user(s)" in output
    assert "scrypt" not in output


def test_list_users_json(tmp_path, capsys):
    run_cli(["create-user", "--user-id", "dan"], tmp_path)
    capsys.readouterr()

    assert run_cli(["list-users", "--json"], tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["user_id"] == "dan"
    assert "password_hash" not in payload[0]


def test_duplicate_user_fails_cleanly(tmp_path, capsys):
    run_cli(["create-user", "--user-id", "dan"], tmp_path)

    assert run_cli(["create-user", "--user-id", "dan"], tmp_path) == 1
    assert "already exists" in capsys.readouterr().err
