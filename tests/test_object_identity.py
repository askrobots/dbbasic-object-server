from datetime import datetime, timedelta, timezone

import object_identity


def test_create_session_stores_hash_and_resolves_subject(tmp_path):
    now = datetime.now(timezone.utc)

    result = object_identity.create_session(
        {
            "user_id": "7",
            "account_id": "acme",
            "roles": ["sales", "sales", "manager"],
            "subscriptions": ["pro"],
            "label": "scroll",
            "ttl_seconds": 60,
        },
        base_dir=tmp_path,
        now=now,
    )

    token = result["token"]
    session = result["session"]
    assert token
    assert session["user_id"] == "7"
    assert session["roles"] == ["sales", "manager"]
    assert session["active"] is True

    session_file = object_identity.sessions_path(tmp_path)
    stored = session_file.read_text()
    assert token not in stored
    assert "sha256:" in stored

    resolved = object_identity.resolve_session_token(
        token,
        base_dir=tmp_path,
        now=now + timedelta(seconds=1),
    )

    assert resolved is not None
    assert resolved.subject().user_id == "7"
    assert resolved.subject().account_id == "acme"
    assert resolved.subject().roles == ("sales", "manager")


def test_expired_session_does_not_resolve(tmp_path):
    now = datetime.now(timezone.utc)
    result = object_identity.create_session(
        {"user_id": "7", "ttl_seconds": 1},
        base_dir=tmp_path,
        now=now,
    )

    resolved = object_identity.resolve_session_token(
        result["token"],
        base_dir=tmp_path,
        now=now + timedelta(seconds=2),
    )

    assert resolved is None


def test_revoke_session_prevents_resolution(tmp_path):
    now = datetime.now(timezone.utc)
    result = object_identity.create_session(
        {"user_id": "7", "ttl_seconds": 60},
        base_dir=tmp_path,
        now=now,
    )

    revoked = object_identity.revoke_session(
        result["session"]["session_id"],
        base_dir=tmp_path,
        now=now + timedelta(seconds=10),
    )

    assert revoked["active"] is False
    assert revoked["revoked_at"] is not None
    assert object_identity.resolve_session_token(result["token"], base_dir=tmp_path, now=now) is None


def test_list_sessions_omits_token_material(tmp_path):
    result = object_identity.create_session({"user_id": "7"}, base_dir=tmp_path)

    sessions = object_identity.list_sessions(base_dir=tmp_path)

    assert sessions == [result["session"]]
    assert "token" not in sessions[0]
    assert "token_hash" not in sessions[0]


def test_list_sessions_skips_corrupt_rows(tmp_path):
    result = object_identity.create_session({"user_id": "7"}, base_dir=tmp_path)
    session_file = object_identity.sessions_path(tmp_path)
    session_file.write_text(
        session_file.read_text()
        + "not-a-session\tbad-hash\t\t\t[]\t[]\t\tbad\tbad\t\n"
    )

    sessions = object_identity.list_sessions(base_dir=tmp_path)

    assert sessions == [result["session"]]


def test_list_sessions_skips_naive_timestamp_rows(tmp_path):
    result = object_identity.create_session({"user_id": "7"}, base_dir=tmp_path)
    session_file = object_identity.sessions_path(tmp_path)
    session_file.write_text(
        session_file.read_text()
        + "sess_badbadbadbad\tsha256:"
        + "0" * 64
        + "\t8\t\t[]\t[]\t\t2026-06-30T18:00:00\t2026-07-01T18:00:00\t\n"
    )

    sessions = object_identity.list_sessions(base_dir=tmp_path)

    assert sessions == [result["session"]]


def test_create_session_rejects_missing_user_id(tmp_path):
    try:
        object_identity.create_session({"roles": ["sales"]}, base_dir=tmp_path)
    except object_identity.InvalidSessionPayloadError as exc:
        assert str(exc) == "user_id is required"
    else:
        raise AssertionError("missing user_id should fail")
