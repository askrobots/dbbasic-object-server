"""Self-service signup, email verification and password reset (object_signup + the browser routes)."""

import os
import re
import stat
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import object_credentials
import object_identity
import object_packages
import object_permission_store
import object_permissions
import object_records
import object_server
import object_signup

from test_object_server import raw_request

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery"


# ---- tokens ---------------------------------------------------------------


def test_token_is_single_use_and_stored_only_as_a_hash(tmp_path):
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, "u1", "a@example.org", base_dir=tmp_path, now=NOW)

    stored = object_signup.tokens_path(tmp_path).read_text()
    assert token not in stored
    assert object_identity.hash_token(token) in stored
    assert stat.S_IMODE(os.stat(object_signup.tokens_path(tmp_path)).st_mode) == 0o600

    assert object_signup.peek_token(token, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW) is not None
    row = object_signup.consume_token(token, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW)
    assert row["user_id"] == "u1"
    assert object_signup.consume_token(token, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW) is None
    # the used row is gone, not kept around
    assert object_identity.hash_token(token) not in object_signup.tokens_path(tmp_path).read_text()


def test_token_expires_and_only_works_for_its_purpose(tmp_path):
    token = object_signup.issue_token(object_signup.PASSWORD_RESET, "u1", "a@example.org", base_dir=tmp_path, now=NOW)

    assert object_signup.consume_token(token, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW) is None
    later = NOW + object_signup.TOKEN_TTL[object_signup.PASSWORD_RESET] + timedelta(seconds=1)
    assert object_signup.consume_token(token, object_signup.PASSWORD_RESET, base_dir=tmp_path, now=later) is None
    assert object_signup.consume_token("", object_signup.PASSWORD_RESET, base_dir=tmp_path, now=NOW) is None
    assert object_signup.consume_token("nope", object_signup.PASSWORD_RESET, base_dir=tmp_path, now=NOW) is None


def test_a_new_link_retires_the_previous_one_and_the_file_stays_small(tmp_path):
    first = object_signup.issue_token(object_signup.VERIFY_EMAIL, "u1", "a@example.org", base_dir=tmp_path, now=NOW)
    second = object_signup.issue_token(object_signup.VERIFY_EMAIL, "u1", "a@example.org", base_dir=tmp_path, now=NOW)
    object_signup.issue_token(object_signup.PASSWORD_RESET, "u1", "a@example.org", base_dir=tmp_path, now=NOW)

    assert object_signup.peek_token(first, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW) is None
    assert object_signup.peek_token(second, object_signup.VERIFY_EMAIL, base_dir=tmp_path, now=NOW) is not None

    # a write long after everything expired keeps only the new row
    much_later = NOW + timedelta(days=30)
    object_signup.issue_token(object_signup.VERIFY_EMAIL, "u2", "b@example.org", base_dir=tmp_path, now=much_later)
    rows = object_signup._read_tokens(object_signup.tokens_path(tmp_path))
    assert [r["user_id"] for r in rows] == ["u2"]


# ---- accounts ---------------------------------------------------------------


def test_register_creates_a_pending_user_in_its_own_account(tmp_path):
    result = object_signup.register("Ann@Example.org", base_dir=tmp_path, now=NOW)

    assert result["outcome"] == "created"
    user = result["user"]
    assert user["status"] == object_identity.PENDING_STATUS
    assert user["roles"] == []
    assert user["display_name"] == ""
    assert object_identity.get_account(user["account_id"], base_dir=tmp_path)["status"] == "active"
    # no password exists until the person who holds the mailbox chooses one
    assert not object_credentials.has_password(user["user_id"], base_dir=tmp_path)
    # pending means no session, through the same gate every login uses
    with pytest.raises(ValueError):
        object_identity.create_session({"user_id": user["user_id"]}, base_dir=tmp_path, require_known_user=True)


def test_register_again_does_not_create_a_second_user(tmp_path):
    first = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)
    again = object_signup.register("ANN@example.org", base_dir=tmp_path, now=NOW)
    assert again["outcome"] == "pending"
    assert again["user"]["user_id"] == first["user"]["user_id"]

    object_identity.set_user_status(first["user"]["user_id"], "active", base_dir=tmp_path)
    exists = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)
    assert exists["outcome"] == "exists"
    assert len(object_identity.list_users(base_dir=tmp_path)) == 1
    assert not object_credentials.has_password(first["user"]["user_id"], base_dir=tmp_path)


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@example.org"])
def test_register_refuses_bad_input_before_creating_anything(tmp_path, email):
    with pytest.raises(object_signup.SignupError):
        object_signup.register(email, base_dir=tmp_path)
    assert object_identity.list_users(base_dir=tmp_path) == []
    assert object_identity.list_accounts(base_dir=tmp_path) == []


def test_confirming_sets_the_chosen_password_and_activates_once(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, user["user_id"], user["email"], base_dir=tmp_path)

    with pytest.raises(object_signup.SignupError):
        object_signup.confirm_signup(token, "short", base_dir=tmp_path)
    # a password that won't do leaves the link usable, and the user pending with no password
    assert object_signup.peek_token(token, object_signup.VERIFY_EMAIL, base_dir=tmp_path) is not None
    assert not object_credentials.has_password(user["user_id"], base_dir=tmp_path)

    confirmed = object_signup.confirm_signup(token, PASSWORD, base_dir=tmp_path)
    assert confirmed["status"] == "active"
    assert object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    assert object_signup.confirm_signup(token, "a second password", base_dir=tmp_path) is None
    assert object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)


def test_a_stranger_who_signs_up_with_my_address_gets_no_way_in(tmp_path):
    """The pre-hijack: signing up first with someone's address must not choose their password."""
    stranger = object_signup.register("victim@example.org", base_dir=tmp_path, now=NOW)["user"]
    # nothing the stranger typed can be used to sign in as that address
    assert not object_credentials.has_password(stranger["user_id"], base_dir=tmp_path)
    with pytest.raises(ValueError):
        object_identity.create_session({"user_id": stranger["user_id"]}, base_dir=tmp_path, require_known_user=True)

    # the real owner signs up too, gets the link, and chooses the password
    owner = object_signup.register("Victim@Example.org", base_dir=tmp_path, now=NOW)
    assert owner["outcome"] == "pending" and owner["user"]["user_id"] == stranger["user_id"]
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, stranger["user_id"], "victim@example.org", base_dir=tmp_path)
    confirmed = object_signup.confirm_signup(token, "the owner's own password", "  Victim Owner ", base_dir=tmp_path)
    assert object_credentials.verify_password(stranger["user_id"], "the owner's own password", base_dir=tmp_path)
    # the name is the mailbox holder's too, not whatever the first signup typed
    assert confirmed["display_name"] == "Victim Owner"


def test_a_pending_user_made_before_the_fix_keeps_no_stranger_password(tmp_path):
    """Old data: a pending user whose password a stranger chose at signup."""
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    object_credentials.set_password(user["user_id"], "the stranger's password", base_dir=tmp_path)
    # pending users cannot sign in, password or no password
    with pytest.raises(ValueError):
        object_identity.create_session({"user_id": user["user_id"]}, base_dir=tmp_path, require_known_user=True)

    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, user["user_id"], user["email"], base_dir=tmp_path)
    object_signup.confirm_signup(token, PASSWORD, base_dir=tmp_path)
    assert object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    assert not object_credentials.verify_password(user["user_id"], "the stranger's password", base_dir=tmp_path)


def test_a_confirm_link_never_changes_an_account_already_in_use(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, user["user_id"], user["email"], base_dir=tmp_path)
    object_credentials.set_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    object_identity.set_user_status(user["user_id"], "active", base_dir=tmp_path)

    assert object_signup.confirm_signup(token, "an attacker's password", base_dir=tmp_path) is None
    assert object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)


def test_verify_never_reactivates_a_disabled_user(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, user["user_id"], user["email"], base_dir=tmp_path)
    object_identity.set_user_status(user["user_id"], "disabled", base_dir=tmp_path)

    assert object_signup.confirm_signup(token, PASSWORD, base_dir=tmp_path) is None
    assert object_identity.get_user(user["user_id"], base_dir=tmp_path)["status"] == "disabled"


def test_a_link_sent_to_an_old_address_proves_nothing(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    token = object_signup.issue_token(object_signup.VERIFY_EMAIL, user["user_id"], "someone-else@example.org", base_dir=tmp_path)

    assert object_signup.confirm_signup(token, PASSWORD, base_dir=tmp_path) is None
    assert object_identity.get_user(user["user_id"], base_dir=tmp_path)["status"] == "pending"
    assert not object_credentials.has_password(user["user_id"], base_dir=tmp_path)


def test_reset_sets_the_password_and_signs_out_everywhere(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    object_credentials.set_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    object_identity.set_user_status(user["user_id"], "active", base_dir=tmp_path)
    session = object_identity.create_session({"user_id": user["user_id"]}, base_dir=tmp_path, require_known_user=True)
    token = object_signup.issue_token(object_signup.PASSWORD_RESET, user["user_id"], user["email"], base_dir=tmp_path)

    with pytest.raises(object_signup.SignupError):
        object_signup.reset_password(token, "short", base_dir=tmp_path)
    # a password that won't do leaves the link usable
    assert object_signup.peek_token(token, object_signup.PASSWORD_RESET, base_dir=tmp_path) is not None

    assert object_signup.reset_password(token, "a brand new password", base_dir=tmp_path)["user_id"] == user["user_id"]
    assert object_credentials.verify_password(user["user_id"], "a brand new password", base_dir=tmp_path)
    assert not object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    assert object_identity.resolve_session_token(session["token"], base_dir=tmp_path) is None
    assert object_signup.reset_password(token, "yet another password", base_dir=tmp_path) is None


def test_reset_confirms_a_pending_address(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    token = object_signup.issue_token(object_signup.PASSWORD_RESET, user["user_id"], user["email"], base_dir=tmp_path)

    assert object_signup.reset_password(token, "a brand new password", base_dir=tmp_path)["status"] == "active"


def test_disabled_users_get_no_reset_link(tmp_path):
    user = object_signup.register("ann@example.org", base_dir=tmp_path, now=NOW)["user"]
    assert object_signup.reset_candidate("ANN@example.org", base_dir=tmp_path)["user_id"] == user["user_id"]
    object_identity.set_user_status(user["user_id"], "disabled", base_dir=tmp_path)
    assert object_signup.reset_candidate("ann@example.org", base_dir=tmp_path) is None
    assert object_signup.reset_candidate("nobody@example.org", base_dir=tmp_path) is None


# ---- whether it may run ---------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("https://example.org", "https://example.org"),
    ("https://example.org/", "https://example.org"),
    ("https://example.org/app", "https://example.org/app"),
    ("http://localhost:8001", "http://localhost:8001"),
    ("http://example.org", None),
    ("example.org", None),
    ("https://example.org/?next=x", None),
    ("", None),
])
def test_public_url_must_be_https_except_on_this_machine(value, expected):
    assert object_signup.public_url({object_signup.PUBLIC_URL_ENV: value}) == expected


def test_the_starter_policy_blocks_open_signup():
    starter = object_permissions.policy_from_dict(object_permission_store.starter_policy_payload())
    assert object_signup.unscoped_registered_rules(starter)

    scoped = object_permissions.PermissionPolicy(rules=(
        object_permissions.PermissionRule.allow("registered", ["read", "create"], collection="notes", row_filter={"owner_id": "$user_id"}),
        object_permissions.PermissionRule.allow("public", ["read"]),
    ))
    assert object_signup.unscoped_registered_rules(scoped) == []


def test_blockers_name_everything_that_is_missing(tmp_path):
    blockers = object_signup.signup_blockers(
        policy=object_permissions.PermissionPolicy(),
        enforcement_enabled=False,
        password_login_enabled=False,
        base_dir=tmp_path,
        env={},
    )
    text = "\n".join(blockers)
    for needed in ("DBBASIC_ENABLE_SIGNUP", "DBBASIC_ENABLE_PASSWORD_LOGIN", "DBBASIC_ENABLE_PERMISSION_ENFORCEMENT",
                   "DBBASIC_SMTP_MODE", "app-email", "DBBASIC_PUBLIC_URL"):
        assert needed in text

    reset = "\n".join(object_signup.reset_blockers(password_login_enabled=True, base_dir=tmp_path, env={}))
    assert "DBBASIC_ENABLE_PASSWORD_RESET" in reset and "DBBASIC_ENABLE_PERMISSION_ENFORCEMENT" not in reset


# ---- the browser flow -------------------------------------------------------


@pytest.fixture
def open_signup(tmp_path, monkeypatch):
    """A server where signup and reset may run: email in log mode, app-email installed."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package("app-email", root=PACKAGES_ROOT, base_dir=data_dir,
                                    object_roots=[object_root], allow_replace=True)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.PASSWORD_LOGIN_ENV, "true")
    monkeypatch.setenv(object_signup.SIGNUP_ENV, "true")
    monkeypatch.setenv(object_signup.PASSWORD_RESET_ENV, "true")
    monkeypatch.setenv(object_signup.PUBLIC_URL_ENV, "https://desk.example.org")
    monkeypatch.setenv("DBBASIC_SMTP_MODE", "log")
    monkeypatch.setenv(object_server.COOKIE_SECURE_ENV, "false")
    monkeypatch.setattr(object_server, "_permission_enforcement_enabled", lambda: True)
    monkeypatch.setattr(object_server, "PASSWORD_LOGIN_FAILURE_DELAY_SECONDS", 0)
    assert object_server._signup_blockers() == []
    return data_dir


def _post(path, form, headers=()):
    return raw_request(
        path,
        method="POST",
        body=urllib.parse.urlencode(form).encode(),
        headers=[("content-type", "application/x-www-form-urlencoded"), *headers],
    )


def _mail(data_dir):
    return object_records.read_collection_records("email_outbox", base_dir=data_dir)


def _token_from(mail_row, path):
    match = re.search(r"https://desk\.example\.org" + re.escape(path) + r"\?token=([A-Za-z0-9_%-]+)", mail_row["text_body"])
    assert match, mail_row["text_body"]
    return urllib.parse.unquote(match.group(1))


def _cookie(headers):
    value = headers.get(b"set-cookie", b"").decode()
    return value.split(";", 1)[0]


def test_signup_is_closed_until_everything_is_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(object_signup.SIGNUP_ENV, "true")

    status, _, body = raw_request("/signup")
    assert status == 403
    assert b"can't be created" in body
    status, _, _ = _post("/signup", {"email": "ann@example.org"})
    assert status == 403
    assert object_identity.list_users(base_dir=tmp_path) == []

    monkeypatch.setenv(object_server.PASSWORD_LOGIN_ENV, "true")
    _, _, login = raw_request("/login")
    assert b"Create an account" not in login and b"Forgot your password" not in login


def test_signup_verify_and_sign_in(open_signup, monkeypatch):
    data_dir = open_signup
    _, _, login = raw_request("/login")
    assert b'href="/signup"' in login and b'href="/reset"' in login
    assert b"choose your password there" in login

    # the form asks for no password
    _, _, form = raw_request("/signup")
    assert b'name="password"' not in form

    # a password sent anyway (an old form, a script) is ignored
    status, headers, _ = _post("/signup", {"email": "ann@example.org", "display_name": "Not Ann", "password": "sent by a stranger"})
    assert status == 303 and headers[b"location"] == b"/signup?sent=1"
    (pending,) = object_identity.find_users_by_email("ann@example.org", base_dir=data_dir)
    assert not object_credentials.has_password(pending["user_id"], base_dir=data_dir)
    assert pending["display_name"] == ""
    status, _, body = raw_request("/signup", query_string="sent=1")
    assert b"Check your email" in body

    (mail,) = _mail(data_dir)
    assert mail["to"] == "ann@example.org"
    assert "choose your password" in mail["text_body"]
    token = _token_from(mail, "/verify")
    (user,) = object_identity.find_users_by_email("ann@example.org", base_dir=data_dir)
    assert user["status"] == "pending"

    # a password typed into the signup form, or at sign-in, gets a stranger nothing
    for guess in (PASSWORD, "sent by a stranger"):
        status, headers, _ = _post("/login", {"email": "ann@example.org", "password": guess})
        assert headers[b"location"] == b"/login?error=1"
    assert b"set-cookie" not in headers
    assert len(_mail(data_dir)) == 1

    # opening the link only shows the form, and does not use the link up
    status, _, body = raw_request("/verify", query_string=f"token={token}")
    assert status == 200 and b"Confirm my email" in body and b'name="password"' in body
    assert object_signup.peek_token(token, object_signup.VERIFY_EMAIL, base_dir=data_dir) is not None

    # a password that won't do keeps the link alive
    status, _, body = _post("/verify", {"token": token, "password": "short", "display_name": "Ann <b>"})
    assert status == 400 and b"at least 8 characters" in body and b"Ann &lt;b&gt;" in body
    assert object_signup.peek_token(token, object_signup.VERIFY_EMAIL, base_dir=data_dir) is not None

    monkeypatch.setenv(object_server.LANDING_PATH_ENV, "/sell/?confirmed=1")
    status, headers, _ = _post("/verify", {"token": token, "password": PASSWORD, "display_name": "Ann"})
    assert status == 303 and headers[b"location"] == b"/sell/?confirmed=1"
    assert _cookie(headers).startswith("dbbasic_session=") and _cookie(headers) != "dbbasic_session="
    (user,) = object_identity.find_users_by_email("ann@example.org", base_dir=data_dir)
    assert user["status"] == "active" and user["display_name"] == "Ann"

    status, _, body = raw_request("/verify", query_string=f"token={token}")
    assert status == 400 and b"expired or was already used" in body and b'href="/signup"' in body
    status, _, body = _post("/verify", {"token": token, "password": "another password"})
    assert status == 400 and b"expired or was already used" in body

    status, headers, _ = _post("/login", {"email": "ann@example.org", "password": PASSWORD})
    assert headers[b"location"] == b"/sell/?confirmed=1" and _cookie(headers).startswith("dbbasic_session=")


def test_a_landing_path_that_is_not_local_is_reported_not_silently_ignored(open_signup, monkeypatch):
    assert object_server._signup_blockers() == []
    monkeypatch.setenv(object_server.LANDING_PATH_ENV, "https://evil.example/")
    (blocker,) = object_server._signup_blockers()
    assert "DBBASIC_AUTH_LANDING_PATH" in blocker
    monkeypatch.setenv(object_server.LANDING_PATH_ENV, "/sell/?confirmed=1")
    assert object_server._signup_blockers() == []


def test_the_landing_path_is_local_only(monkeypatch):
    monkeypatch.delenv(object_server.LANDING_PATH_ENV, raising=False)
    assert object_server._landing_path() == "/"
    for bad in ("https://evil.example/", "//evil.example", "evil", "/a\\b"):
        monkeypatch.setenv(object_server.LANDING_PATH_ENV, bad)
        assert object_server._landing_path() == "/"
    monkeypatch.setenv(object_server.LANDING_PATH_ENV, "/sell/")
    assert object_server._landing_path() == "/sell/"


def test_auth_pages_send_an_origin_with_their_forms(open_signup):
    """no-referrer made browsers send "Origin: null" with a form post, which the same-origin check refuses."""
    for path in ("/login", "/signup", "/reset"):
        _, _, body = raw_request(path)
        assert b'<meta name="referrer" content="strict-origin">' in body
        assert b"no-referrer" not in body


def test_signup_for_a_taken_address_looks_the_same_but_tells_the_mailbox(open_signup):
    data_dir = open_signup
    user = object_signup.register("ann@example.org", base_dir=data_dir)["user"]
    object_credentials.set_password(user["user_id"], PASSWORD, base_dir=data_dir)
    object_identity.set_user_status(user["user_id"], "active", base_dir=data_dir)

    status, headers, _ = _post("/signup", {"email": "Ann@Example.org"})
    assert status == 303 and headers[b"location"] == b"/signup?sent=1"
    (mail,) = _mail(data_dir)
    assert mail["subject"] == "You already have an account"
    assert "https://desk.example.org/reset" in mail["text_body"]
    assert len(object_identity.list_users(base_dir=data_dir)) == 1


def test_signup_form_errors_keep_what_was_typed(open_signup):
    status, _, body = _post("/signup", {"email": "not an email"})
    assert status == 400
    assert b"valid email" in body
    assert b'value="not an email"' in body


def test_signup_is_rate_limited_per_address_and_per_ip(open_signup, monkeypatch):
    monkeypatch.setattr(object_server, "SIGNUP_ATTEMPTS_PER_HOUR_PER_IP", 2)
    _post("/signup", {"email": "a1@example.org"})
    _post("/signup", {"email": "a2@example.org"})
    status, _, body = _post("/signup", {"email": "a3@example.org"})
    assert status == 429 and b"Too many" in body
    assert len(object_identity.list_users(base_dir=open_signup)) == 2


def test_a_cross_site_signup_form_is_refused(open_signup):
    status, _, _ = _post("/signup", {"email": "ann@example.org"},
                         headers=[("host", "desk.example.org"), ("origin", "https://evil.example")])
    assert status == 400
    assert object_identity.list_users(base_dir=open_signup) == []


def test_password_reset_end_to_end(open_signup):
    data_dir = open_signup
    user = object_signup.register("ann@example.org", base_dir=data_dir)["user"]
    object_credentials.set_password(user["user_id"], PASSWORD, base_dir=data_dir)
    object_identity.set_user_status(user["user_id"], "active", base_dir=data_dir)
    session = object_identity.create_session({"user_id": user["user_id"]}, base_dir=data_dir, require_known_user=True)

    # an unknown address gets the same answer and no email
    status, headers, _ = _post("/reset", {"email": "nobody@example.org"})
    assert headers[b"location"] == b"/reset?sent=1"
    assert _mail(data_dir) == []

    status, headers, _ = _post("/reset", {"email": "ANN@example.org"})
    assert headers[b"location"] == b"/reset?sent=1"
    (mail,) = _mail(data_dir)
    token = _token_from(mail, "/reset/confirm")

    status, _, body = raw_request("/reset/confirm", query_string=f"token={token}")
    assert status == 200 and b"Choose a new password" in body

    status, _, body = _post("/reset/confirm", {"token": token, "password": "short"})
    assert status == 400 and b"at least 8 characters" in body

    status, headers, _ = _post("/reset/confirm", {"token": token, "password": "a brand new password"})
    assert status == 303 and headers[b"location"] == b"/login?reset=1"
    assert object_identity.resolve_session_token(session["token"], base_dir=data_dir) is None

    _, _, body = raw_request("/login", query_string="reset=1")
    assert b"Your password was changed" in body
    status, headers, _ = _post("/login", {"email": "ann@example.org", "password": "a brand new password"})
    assert headers[b"location"] == b"/"

    status, _, body = _post("/reset/confirm", {"token": token, "password": "another new password"})
    assert status == 400 and b"expired or was already used" in body


def test_a_reset_clears_the_login_lockout(open_signup, monkeypatch):
    data_dir = open_signup
    monkeypatch.setenv(object_server.LOGIN_LOCKOUT_ATTEMPTS_ENV, "2")
    user = object_signup.register("ann@example.org", base_dir=data_dir)["user"]
    object_credentials.set_password(user["user_id"], PASSWORD, base_dir=data_dir)
    object_identity.set_user_status(user["user_id"], "active", base_dir=data_dir)
    for _ in range(2):
        _post("/login", {"email": "ann@example.org", "password": "wrong password"})
    _, headers, _ = _post("/login", {"email": "ann@example.org", "password": PASSWORD})
    assert headers[b"location"] == b"/login?error=locked"

    token = object_signup.issue_token(object_signup.PASSWORD_RESET, user["user_id"], user["email"], base_dir=data_dir)
    _post("/reset/confirm", {"token": token, "password": "a brand new password"})
    _, headers, _ = _post("/login", {"email": "ann@example.org", "password": "a brand new password"})
    assert headers[b"location"] == b"/"


def test_admin_capabilities_show_why_signup_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    identity = object_server._admin_capabilities_payload()["identity"]
    assert identity["signup_enabled"] is False
    assert any("DBBASIC_ENABLE_SIGNUP" in b for b in identity["signup_blockers"])
    assert identity["public_url_env"] == "DBBASIC_PUBLIC_URL"
