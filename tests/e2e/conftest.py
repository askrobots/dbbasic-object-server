"""Fixtures for the browser end-to-end lane: one real `uvicorn
object_server:app` subprocess, seeded through the same public package/
identity APIs every other test uses, driven by a real Chromium via
pytest-playwright.

Why this exists: 13 apps now share ONE generated-page pipeline (a schema,
a seeded `views` record, site_view_render, and the shared
window.dbbasicList/window.dbbasicForm JS). Unit/structural tests already
cover each package's manifest and permission rules in isolation, but
nothing exercises the pipeline the way a person actually does -- click
the Add button, fill in fields the schema produced, watch the row show up
without a reload. That gap is what tests/e2e/test_generated_ui.py closes;
this file only builds the fixture (server + two identities) it drives.

The server is booted ONCE per test session (module-costly: package
install + process boot), not once per test -- pytest-playwright's own
`page`/`context` fixtures are function-scoped and start each test with a
brand-new, cookie-free browser context, so tests still don't leak session
state into each other even though they share one server process and one
data directory.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import object_credentials  # noqa: E402
import object_identity  # noqa: E402
import object_packages  # noqa: E402

PACKAGES_ROOT = ROOT / "packages"

# app-views + app-theme give the generated pipeline its renderer and JS
# (site_view_render, /style, /list, /form, /nav); app-notes and app-tasks
# are the two collections proving one pipeline serves many apps with zero
# extra page code.
PACKAGES_TO_INSTALL = ["app-views", "app-theme", "app-notes", "app-tasks"]

# Long enough to clear object_credentials' MIN_PASSWORD_LENGTH by a wide
# margin; not a security-relevant value, just a fixed test credential.
PASSWORD = "correct horse battery staple"

_SERVER_BOOT_TIMEOUT_SECONDS = 30.0


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_healthy(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise RuntimeError(
                f"server process exited early (code {exit_code}) before answering /health:\n{log_text}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.1)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise RuntimeError(
        f"server at {base_url} never answered /health within "
        f"{_SERVER_BOOT_TIMEOUT_SECONDS}s (last error: {last_error}):\n{log_text}"
    )


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Boot one real object server for the whole e2e session.

    Packages are installed through object_packages.install_package (the
    same public write contract every package test uses) BEFORE the server
    starts, and the two test users are created directly through
    object_identity/object_credentials -- fixture setup, not something
    under test. Signing in through /login IS under test; see
    test_generated_ui.py.
    """
    data_dir = tmp_path_factory.mktemp("e2e-data")
    objects_dir = tmp_path_factory.mktemp("e2e-objects")

    for package_id in PACKAGES_TO_INSTALL:
        object_packages.install_package(
            package_id,
            root=PACKAGES_ROOT,
            base_dir=data_dir,
            object_roots=[objects_dir],
        )

    users = {}
    for label in ("alice", "bob"):
        user_id = f"e2e_{label}"
        email = f"{label}@e2e.test"
        object_identity.create_user(
            {"user_id": user_id, "email": email, "display_name": label.title()},
            base_dir=data_dir,
        )
        object_credentials.set_password(user_id, PASSWORD, base_dir=data_dir)
        users[label] = {"user_id": user_id, "email": email, "password": PASSWORD}

    port = _free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = data_dir / "server.log"

    env = dict(os.environ)
    env["DBBASIC_DATA_DIR"] = str(data_dir)
    env["DBBASIC_OBJECTS_DIR"] = str(objects_dir)
    # Password login is off by default (see object_server.PASSWORD_LOGIN_ENV);
    # the login test drives the real /login form, so it must be reachable.
    env["DBBASIC_ENABLE_PASSWORD_LOGIN"] = "true"
    # Clean public URLs (/notes, /tasks -> site_routes -> site_view_render)
    # are also feature-flagged off by default (object_server.SITE_ROUTES_ENV,
    # _handle_site_route) -- without this every generated page 404s before
    # site_routes is ever consulted.
    env["DBBASIC_ENABLE_SITE_ROUTES"] = "true"
    # Permission enforcement (row_filter, owner_scoped, etc.) is ALSO off
    # by default -- object_server.PERMISSION_ENFORCEMENT_ENV -- and without
    # it every /collections/... read/write instead falls back to the admin-
    # token gate, which 403s every generated block on a server with no
    # DBBASIC_ADMIN_TOKEN. Test 4 (owner_scoped notes) is meaningless
    # without this actually on.
    env["DBBASIC_ENABLE_PERMISSION_ENFORCEMENT"] = "true"
    # object_permission_status.readiness_status refuses to actually engage
    # enforcement (writes silently keep falling back to the admin-token
    # gate above) unless an admin recovery token is configured -- a
    # deliberate lockout guard, not a bug to route around. This is that
    # token; nothing here relies on it for anything but clearing the gate.
    env["DBBASIC_ADMIN_TOKEN"] = "e2e-test-admin-token"

    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "object_server:app",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_healthy(base_url, process, log_path)
            yield {"base_url": base_url, "users": users}
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture(scope="session")
def base_url(e2e_server: dict[str, Any]) -> str:
    """Override pytest-playwright/pytest-base-url's default (None) so every
    browser context this session creates resolves relative page.goto("/x")
    calls against our own server -- see browser_context_args in
    pytest_playwright, which reads this same fixture name."""
    return e2e_server["base_url"]
