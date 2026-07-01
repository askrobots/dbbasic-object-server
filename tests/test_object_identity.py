from datetime import datetime, timedelta, timezone
from uuid import UUID

import object_identity


def test_create_account_stores_public_payload(tmp_path):
    account = object_identity.create_account(
        {
            "account_id": "acme",
            "name": "Acme Corp",
            "subscriptions": ["pro", "pro", "support"],
        },
        base_dir=tmp_path,
    )

    assert account["account_id"] == "acme"
    assert account["name"] == "Acme Corp"
    assert account["status"] == "active"
    assert account["subscriptions"] == ["pro", "support"]

    assert object_identity.get_account("acme", base_dir=tmp_path) == account
    assert object_identity.list_accounts(base_dir=tmp_path) == [account]


def test_create_account_generates_uuid_id_when_missing(tmp_path):
    account = object_identity.create_account({"name": "Acme Corp"}, base_dir=tmp_path)

    parsed = UUID(account["account_id"])
    assert parsed.version == 4
    assert object_identity.get_account(account["account_id"], base_dir=tmp_path) == account


def test_create_user_stores_roles_and_requires_known_account(tmp_path):
    object_identity.create_account({"account_id": "acme"}, base_dir=tmp_path)

    user = object_identity.create_user(
        {
            "user_id": "u_7",
            "account_id": "acme",
            "email": "alice@example.com",
            "display_name": "Alice",
            "roles": ["sales", "sales", "manager"],
            "subscriptions": ["team"],
        },
        base_dir=tmp_path,
    )

    assert user["user_id"] == "u_7"
    assert user["account_id"] == "acme"
    assert user["email"] == "alice@example.com"
    assert user["roles"] == ["sales", "manager"]

    assert object_identity.get_user("u_7", base_dir=tmp_path) == user
    assert object_identity.list_users(base_dir=tmp_path) == [user]
    assert object_identity.list_users(account_id="acme", base_dir=tmp_path) == [user]
    assert object_identity.list_users(account_id="other", base_dir=tmp_path) == []

    try:
        object_identity.create_user(
            {"user_id": "u_8", "account_id": "missing"},
            base_dir=tmp_path,
        )
    except object_identity.AccountNotFoundError as exc:
        assert str(exc) == "Account not found: missing"
    else:
        raise AssertionError("unknown account should fail")


def test_create_user_generates_uuid_id_when_missing(tmp_path):
    user = object_identity.create_user({"display_name": "Alice"}, base_dir=tmp_path)

    parsed = UUID(user["user_id"])
    assert parsed.version == 4
    assert object_identity.get_user(user["user_id"], base_dir=tmp_path) == user


def test_create_identity_records_reject_duplicates(tmp_path):
    object_identity.create_account({"account_id": "acme"}, base_dir=tmp_path)
    object_identity.create_user({"user_id": "u_7"}, base_dir=tmp_path)

    try:
        object_identity.create_account({"account_id": "acme"}, base_dir=tmp_path)
    except object_identity.InvalidIdentityPayloadError as exc:
        assert str(exc) == "Account already exists: acme"
    else:
        raise AssertionError("duplicate account should fail")

    try:
        object_identity.create_user({"user_id": "u_7"}, base_dir=tmp_path)
    except object_identity.InvalidIdentityPayloadError as exc:
        assert str(exc) == "User already exists: u_7"
    else:
        raise AssertionError("duplicate user should fail")


def test_registered_user_defaults_session_roles_account_and_subscriptions(tmp_path):
    object_identity.create_account(
        {"account_id": "acme", "subscriptions": ["pro"]},
        base_dir=tmp_path,
    )
    object_identity.create_user(
        {
            "user_id": "u_7",
            "account_id": "acme",
            "roles": ["sales"],
            "subscriptions": ["team"],
        },
        base_dir=tmp_path,
    )

    result = object_identity.create_session({"user_id": "u_7"}, base_dir=tmp_path)

    assert result["session"]["account_id"] == "acme"
    assert result["session"]["roles"] == ["sales"]
    assert result["session"]["subscriptions"] == ["team", "pro"]


def test_session_rejects_disabled_user_or_account(tmp_path):
    object_identity.create_account({"account_id": "acme"}, base_dir=tmp_path)
    object_identity.create_user(
        {"user_id": "u_7", "account_id": "acme", "status": "disabled"},
        base_dir=tmp_path,
    )

    try:
        object_identity.create_session({"user_id": "u_7"}, base_dir=tmp_path)
    except object_identity.InvalidSessionPayloadError as exc:
        assert str(exc) == "User is not active: u_7"
    else:
        raise AssertionError("disabled user should not receive a session")

    object_identity.create_account(
        {"account_id": "closed", "status": "disabled"},
        base_dir=tmp_path,
    )
    object_identity.create_user(
        {"user_id": "u_8", "account_id": "closed"},
        base_dir=tmp_path,
    )

    try:
        object_identity.create_session({"user_id": "u_8"}, base_dir=tmp_path)
    except object_identity.InvalidSessionPayloadError as exc:
        assert str(exc) == "Account is not active: closed"
    else:
        raise AssertionError("disabled account should not receive a session")


def test_session_can_require_registered_user(tmp_path):
    try:
        object_identity.create_session(
            {"user_id": "u_missing"},
            base_dir=tmp_path,
            require_known_user=True,
        )
    except object_identity.UserNotFoundError as exc:
        assert str(exc) == "User not found: u_missing"
    else:
        raise AssertionError("strict sessions should require a known user")


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
    parsed = UUID(session["session_id"])
    assert parsed.version == 4
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
