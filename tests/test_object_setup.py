"""First-owner setup of a one-click box (object_setup + /setup and the waiting gate in object_server).

What these protect: a box on the public internet with no owner yet must not answer anything but its
health and its setup page; only the holder of the installer's code may become its admin; and that can
happen once, even when two people try at the same moment.
"""

import json
import threading
import urllib.parse

import pytest

import object_credentials
import object_identity
import object_server
import object_setup

from test_object_server import raw_request

PASSWORD = "correct horse battery"
ADMIN_TOKEN = "setup-test-admin-token"


def _place_code(data_dir, code):
    path = object_setup.code_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(object_setup.code_hash(code) + "\n", encoding="ascii")
    return path


# ---- the module ---------------------------------------------------------------


def test_the_box_keeps_only_a_hash_of_the_code(tmp_path):
    code = object_setup.new_code()
    path = _place_code(tmp_path, code)

    stored = path.read_text()
    assert code not in stored
    assert stored.startswith("sha256:") and len(stored.strip()) == len("sha256:") + 64


def test_typing_the_code_is_forgiving_but_a_wrong_length_is_nothing():
    code = "7K3MQ9ZT1W4XB8RN2PVE"
    assert object_setup.normalize_code(object_setup.display(code).lower()) == code
    assert object_setup.normalize_code(" 7k3mq 9zt1w-4xb8r n2pve ") == code
    # base32 leaves out I, L and O; someone reading them off a screen means 1, 1 and 0
    assert object_setup.normalize_code("7K3MQ9ZTIW4XB8RN2PVE") == code
    assert object_setup.normalize_code(code[:-1]) == ""
    assert object_setup.normalize_code(code + "A") == ""
    assert object_setup.normalize_code("U" * 20) == ""        # U is not in the alphabet either


def test_the_right_code_makes_one_admin_with_the_chosen_password_and_closes_setup(tmp_path):
    code = object_setup.new_code()
    _place_code(tmp_path, code)
    assert object_setup.waiting(tmp_path)

    user = object_setup.complete(object_setup.display(code), "Owner@Example.org", PASSWORD, "Owner", base_dir=tmp_path)

    assert user["roles"] == ["admin"] and user["email"] == "Owner@Example.org"
    assert object_credentials.verify_password(user["user_id"], PASSWORD, base_dir=tmp_path)
    assert not object_setup.code_path(tmp_path).exists()
    assert list(object_setup.code_path(tmp_path).parent.glob("setup_code*")) == []
    assert not object_setup.waiting(tmp_path)
    # the code can't be used again, even by the same person
    assert object_setup.complete(code, "other@example.org", PASSWORD, base_dir=tmp_path) is None
    assert len(object_identity.list_users(base_dir=tmp_path)) == 1


def test_a_wrong_code_makes_nothing_and_the_right_one_still_works(tmp_path):
    code = object_setup.new_code()
    _place_code(tmp_path, code)

    assert object_setup.complete(object_setup.new_code(), "a@example.org", PASSWORD, base_dir=tmp_path) is None
    assert object_setup.complete("", "a@example.org", PASSWORD, base_dir=tmp_path) is None
    assert object_identity.list_users(base_dir=tmp_path) == []
    assert object_setup.waiting(tmp_path)
    assert object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path) is not None


def test_a_bad_password_or_email_is_refused_before_the_code_is_used(tmp_path):
    """The person fixes the form and tries again: a short password must not burn their only code."""
    code = object_setup.new_code()
    _place_code(tmp_path, code)

    with pytest.raises(object_setup.SetupError):
        object_setup.complete(code, "a@example.org", "short", base_dir=tmp_path)
    with pytest.raises(object_setup.SetupError):
        object_setup.complete(code, "not an email", PASSWORD, base_dir=tmp_path)
    assert object_setup.waiting(tmp_path)
    assert object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path) is not None


def test_a_failure_while_making_the_admin_gives_the_code_back(tmp_path, monkeypatch):
    code = object_setup.new_code()
    _place_code(tmp_path, code)

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(object_identity, "create_user", broken)
    with pytest.raises(OSError):
        object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path)
    monkeypatch.undo()

    assert object_setup.waiting(tmp_path)
    assert object_credentials._read_credentials(object_credentials.credentials_path(tmp_path)) == []
    assert object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path) is not None


def test_two_setups_at_once_make_exactly_one_admin(tmp_path):
    """The claim is the rename of the code file. Without it both requests pass the hash check and both
    become admin of someone's server."""
    code = object_setup.new_code()
    _place_code(tmp_path, code)
    start = threading.Barrier(8)
    results = []

    def attempt(n):
        start.wait()
        results.append(object_setup.complete(code, f"p{n}@example.org", PASSWORD, base_dir=tmp_path))

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r is not None]) == 1
    assert len(object_identity.list_users(base_dir=tmp_path)) == 1


def test_an_admin_made_from_the_shell_ends_waiting_whatever_file_is_left(tmp_path):
    """The way out when the code is lost: SSH in and run create-superuser."""
    code = object_setup.new_code()
    _place_code(tmp_path, code)
    object_identity.create_user({"user_id": "operator", "roles": ["admin"]}, base_dir=tmp_path)

    assert not object_setup.waiting(tmp_path)
    assert object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path) is None
    assert [u["user_id"] for u in object_identity.list_users(base_dir=tmp_path)] == ["operator"]


def test_an_identity_store_that_cant_be_read_is_not_handed_out(tmp_path, monkeypatch):
    code = object_setup.new_code()
    _place_code(tmp_path, code)

    def unreadable(**kwargs):
        raise ValueError("bad row")
    monkeypatch.setattr(object_identity, "list_users", unreadable)

    assert not object_setup.waiting(tmp_path)
    assert object_setup.complete(code, "a@example.org", PASSWORD, base_dir=tmp_path) is None


def test_a_server_without_a_code_file_is_never_waiting(tmp_path):
    assert not object_setup.waiting(tmp_path)


# ---- the server -----------------------------------------------------------------


@pytest.fixture
def waiting_box(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(object_server.PASSWORD_LOGIN_ENV, "true")
    monkeypatch.setenv(object_server.COOKIE_SECURE_ENV, "false")
    monkeypatch.setenv(object_server.ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.setattr(object_server, "PASSWORD_LOGIN_FAILURE_DELAY_SECONDS", 0)
    code = object_setup.new_code()
    _place_code(data_dir, code)
    return data_dir, code


def _post(path, form, headers=()):
    return raw_request(path, method="POST", body=urllib.parse.urlencode(form).encode(),
                       headers=[("content-type", "application/x-www-form-urlencoded"), *headers])


def test_a_waiting_box_answers_only_health_and_setup(waiting_box):
    for path in ("/objects", "/identity/users", "/login", "/signup", "/", "/dav/files/"):
        status, _headers, body = raw_request(path)
        assert status == 503, path
        assert json.loads(body)["setup"] == "/setup"

    status, headers, _ = raw_request("/notes", headers=[("accept", "text/html")])
    assert status == 303 and headers[b"location"] == b"/setup"

    status, _, body = raw_request("/health")
    assert status == 200 and json.loads(body)["setup"] == "waiting"
    status, _, body = raw_request("/setup", headers=[("accept", "text/html")])
    assert status == 200 and b'name="code"' in body


def test_the_admin_token_still_reaches_a_waiting_box(waiting_box):
    """Whoever holds it has root on the box already; locking them out would only stop repairs."""
    status, _, _ = raw_request("/identity/users", headers=[("authorization", f"Token {ADMIN_TOKEN}")])
    assert status == 200


def test_the_setup_page_reads_the_code_from_the_part_of_the_address_never_sent(waiting_box):
    _, _, body = raw_request("/setup", headers=[("accept", "text/html")])
    page = body.decode()
    assert "location.hash" in page and "history.replaceState" in page
    assert 'name="referrer" content="strict-origin"' in page


def test_setup_through_the_page_signs_the_owner_in_and_opens_the_box(waiting_box):
    data_dir, code = waiting_box
    status, headers, _ = _post("/setup", {"code": code, "email": "owner@example.org", "password": PASSWORD})

    assert status == 303
    assert headers[b"set-cookie"].startswith(b"dbbasic_session=")
    assert not object_setup.waiting(data_dir)
    status, _, body = raw_request("/health")
    assert "setup" not in json.loads(body)
    status, _, _ = raw_request("/setup", headers=[("accept", "text/html")])
    assert status == 404
    # and the password they chose is the one that signs them in
    status, headers, _ = _post("/login", {"email": "owner@example.org", "password": PASSWORD})
    assert status == 303 and headers[b"set-cookie"].startswith(b"dbbasic_session=")


def test_a_wrong_code_on_the_page_says_so_and_makes_nothing(waiting_box):
    data_dir, _code = waiting_box
    status, headers, body = _post("/setup", {"code": object_setup.new_code(), "email": "x@example.org", "password": PASSWORD})

    assert status == 400 and b"isn&#x27;t right" in body
    assert b"set-cookie" not in headers
    assert object_identity.list_users(base_dir=data_dir) == []


def test_setup_from_another_site_is_refused(waiting_box):
    data_dir, code = waiting_box
    status, _, _ = _post("/setup", {"code": code, "email": "x@example.org", "password": PASSWORD},
                         headers=[("origin", "https://evil.example"), ("host", "box.example")])
    assert status == 400
    assert object_setup.waiting(data_dir)


def test_guessing_codes_is_limited(waiting_box):
    data_dir, code = waiting_box
    for _ in range(object_server.SETUP_ATTEMPTS_PER_HOUR_PER_IP):
        _post("/setup", {"code": object_setup.new_code(), "email": "x@example.org", "password": PASSWORD})
    status, _, _ = _post("/setup", {"code": code, "email": "x@example.org", "password": PASSWORD})
    assert status == 429
    assert object_setup.waiting(data_dir)


def test_setup_refuses_when_password_login_is_off(waiting_box, monkeypatch):
    """It would make an admin who could never sign in again."""
    data_dir, code = waiting_box
    monkeypatch.setenv(object_server.PASSWORD_LOGIN_ENV, "false")
    status, _, body = _post("/setup", {"code": code, "email": "x@example.org", "password": PASSWORD})
    assert status == 409 and object_server.PASSWORD_LOGIN_ENV.encode() in body
    assert object_setup.waiting(data_dir)


def test_health_names_the_installed_release_and_commit(tmp_path, monkeypatch):
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "installed.json").write_text(json.dumps(
        {"release": "2026.11v1", "commit": "a" * 40, "extra": "<script>"}))
    _, _, body = raw_request("/health")
    assert json.loads(body) == {"status": "ok", "release": "2026.11v1", "commit": "a" * 40}

    (tmp_path / "installed.json").write_text(json.dumps({"release": "bad value!", "commit": "nope"}))
    _, _, body = raw_request("/health")
    assert json.loads(body) == {"status": "ok"}


def test_the_owner_gets_a_readable_id_from_their_email_unless_it_is_taken(tmp_path):
    """Pages say "signed in as <id>"; a random id there was the first thing the first test box showed."""
    code = object_setup.new_code()
    _place_code(tmp_path, code)
    object_identity.create_user({"user_id": "owner", "roles": []}, base_dir=tmp_path)

    user = object_setup.complete(code, "Owner@example.org", PASSWORD, base_dir=tmp_path)
    assert user["user_id"] != "owner"            # taken: a random id, never someone else's

    other = tmp_path / "other"
    _place_code(other, code)
    assert object_setup.complete(code, "Ann.Lee+x@example.org", PASSWORD, base_dir=other)["user_id"] == "ann.leex"


def test_the_setup_page_also_reads_a_code_link_opened_on_the_same_page(waiting_box):
    """Changing only the part after # loads nothing new, so the page must listen for it (seen on a test box)."""
    _, _, body = raw_request("/setup", headers=[("accept", "text/html")])
    assert b'addEventListener("hashchange"' in body


def test_the_one_click_starting_apps_come_after_what_they_depend_on():
    """object_packages.install_package doesn't install dependencies, and 2026.11v1 installed notes without
    app-views: its notes page said "Object not found: site_view_render"."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "one-click-install.sh").read_text()
    block = re.search(r'for app in \(([^)]*)\):', script).group(1)
    apps = re.findall(r'"(app-[a-z-]+)"', block)
    assert "app-notes" in apps and "app-views" in apps
    for position, app in enumerate(apps):
        manifest = json.loads((root / "packages" / app / "dbbasic-package.json").read_text())
        for needed in manifest.get("dependencies") or []:
            assert needed in apps[:position], f"{app} needs {needed} installed before it"


def test_the_cloud_init_installs_one_release_and_clones_at_it():
    """A release whose cloud-init still names the one before installs the one before. And a clone of main runs
    the script from files install.sh then checks out from under it."""
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "cloud-init.yaml").read_text()
    clone = re.search(r"git clone --quiet --branch (\S+) https://\S+ /opt/dbbasic-object-server", text)
    run = re.search(r"one-click-install\.sh (\S+)\s*$", text, re.M)
    assert clone and run and clone.group(1) == run.group(1)
    assert re.fullmatch(r"20\d{2}\.(0[1-9]|1[0-2])v[1-9]\d*", run.group(1))
