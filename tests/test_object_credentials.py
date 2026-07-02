"""Tests for file-backed password credentials."""

import os
import stat

import pytest

import object_credentials


def test_set_and_verify_password_round_trip(tmp_path):
    result = object_credentials.set_password("usr_1", "correct horse battery", base_dir=tmp_path)

    assert result["user_id"] == "usr_1"
    assert result["operation"] == "created"
    assert object_credentials.verify_password("usr_1", "correct horse battery", base_dir=tmp_path)
    assert not object_credentials.verify_password("usr_1", "wrong password!", base_dir=tmp_path)


def test_verify_password_unknown_user_returns_false(tmp_path):
    assert not object_credentials.verify_password("usr_missing", "whatever12", base_dir=tmp_path)


def test_set_password_replaces_existing_hash(tmp_path):
    object_credentials.set_password("usr_1", "first-password", base_dir=tmp_path)
    result = object_credentials.set_password("usr_1", "second-password", base_dir=tmp_path)

    assert result["operation"] == "replaced"
    assert not object_credentials.verify_password("usr_1", "first-password", base_dir=tmp_path)
    assert object_credentials.verify_password("usr_1", "second-password", base_dir=tmp_path)

    stored = object_credentials.credentials_path(tmp_path).read_text()
    assert stored.count("usr_1") == 1


def test_password_length_rules(tmp_path):
    with pytest.raises(object_credentials.InvalidPasswordError):
        object_credentials.set_password("usr_1", "short", base_dir=tmp_path)
    with pytest.raises(object_credentials.InvalidPasswordError):
        object_credentials.set_password("usr_1", "x" * 1025, base_dir=tmp_path)
    with pytest.raises(object_credentials.InvalidPasswordError):
        object_credentials.set_password("usr_1", 12345678, base_dir=tmp_path)  # type: ignore[arg-type]


def test_user_id_required(tmp_path):
    with pytest.raises(object_credentials.InvalidPasswordError):
        object_credentials.set_password("  ", "long enough password", base_dir=tmp_path)


def test_stored_hash_never_contains_password(tmp_path):
    object_credentials.set_password("usr_1", "super secret password", base_dir=tmp_path)

    stored = object_credentials.credentials_path(tmp_path).read_text()
    assert "super secret password" not in stored
    assert stored.splitlines()[1].split("\t")[1].startswith("scrypt:")


def test_credentials_file_is_owner_only(tmp_path):
    object_credentials.set_password("usr_1", "long enough password", base_dir=tmp_path)

    mode = stat.S_IMODE(os.stat(object_credentials.credentials_path(tmp_path)).st_mode)
    assert mode == 0o600


def test_remove_and_has_password(tmp_path):
    assert not object_credentials.has_password("usr_1", base_dir=tmp_path)
    object_credentials.set_password("usr_1", "long enough password", base_dir=tmp_path)
    assert object_credentials.has_password("usr_1", base_dir=tmp_path)

    assert object_credentials.remove_password("usr_1", base_dir=tmp_path)
    assert not object_credentials.remove_password("usr_1", base_dir=tmp_path)
    assert not object_credentials.has_password("usr_1", base_dir=tmp_path)
    assert not object_credentials.verify_password("usr_1", "long enough password", base_dir=tmp_path)


def test_tampered_hash_fails_verification(tmp_path):
    object_credentials.set_password("usr_1", "long enough password", base_dir=tmp_path)
    path = object_credentials.credentials_path(tmp_path)
    content = path.read_text().replace("scrypt:", "bcrypt:")
    path.write_text(content)

    assert not object_credentials.verify_password("usr_1", "long enough password", base_dir=tmp_path)
