"""Structural + behavioral tests for packages/app-activity.

Mirrors the package/schema/permission testing conventions used for
packages/app-notes and packages/app-tasks (see tests/test_app_settings_package.py,
tests/test_app_invoices_package.py) and the direct-execution pattern
tests/test_app_views_package.py uses for a renderer-style site object.
Route-level /api/activity behavior is tested through the ASGI harness in
tests/test_object_server.py, the same way tests/test_object_server_prefs.py
and tests/test_object_tts.py test their own signed-in-only GET routes.
"""

import json
import re
from pathlib import Path

import object_execution
import object_packages
import object_permissions
import object_server
import python_object_runtime

from test_object_server import create_identity_session, enable_admin_token, request, write_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ACTIVITY_DIR = PACKAGES_ROOT / "app-activity"


def _app_activity_policy():
    payload = json.loads((APP_ACTIVITY_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_get_package_normalizes_app_activity_manifest():
    package = object_packages.get_package("app-activity", root=PACKAGES_ROOT)

    assert package["id"] == "app-activity"
    assert package["name"] == "Activity"
    assert package["objects"] == [{"id": "site_activity", "path": "objects/site/activity.py"}]
    assert package["schemas"] == []
    assert package["seed"] == []
    assert package["permissions"] == [{"path": "permissions/rules.json"}]


def test_dry_run_app_activity_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-activity",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_site_activity_execute_is_public():
    """Same split as site_notes/site_tasks: public EXECUTE on the *page*
    object (it shows a sign-in prompt to visitors), never public read on a
    collection -- app-activity owns no collection at all.
    """
    policy = _app_activity_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_activity"
    )

    assert decision.allowed is True


def test_activity_page_prompts_anonymous_visitors_to_sign_in(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-activity", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_activity", payload={"_identity": {}}),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert "Sign in" in body
    assert 'id="feed"' not in body


def test_activity_page_serves_feed_scaffolding_for_signed_in_users(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-activity", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_activity", payload={"_identity": {"user_id": "alice"}}
        ),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert 'id="feed"' in body
    assert "/api/activity" in body
    assert '<script src="/nav">' in body


def test_no_disallowed_org_names_leak_into_the_package():
    """Public repo hygiene: no internal org/codename references anywhere
    in this package's source.
    """
    # Built from fragments so this guard file itself stays clean of the very
    # internal names it forbids (otherwise the test would flag its own source).
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    for path in APP_ACTIVITY_DIR.rglob("*"):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"


# ---------------------------------------------------------------------------
# GET /api/activity route
# ---------------------------------------------------------------------------


def signed_in_bearer(user_id):
    token, _ = create_identity_session({"user_id": user_id})
    return [("authorization", f"Bearer {token}")]


def test_get_api_activity_requires_a_signed_in_session(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    write_records(data_dir, "notes", "id\ttitle\n")

    status, _, payload = request("/api/activity")

    assert status == 401
    assert payload["status"] == "error"


def test_get_api_activity_rejects_unsupported_methods(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    headers = signed_in_bearer("alice")

    status, _, _ = request("/api/activity", method="POST", headers=headers)

    assert status == 405


def test_get_api_activity_returns_only_the_signed_in_users_feed(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    write_records(data_dir, "notes", "id\ttitle\n")

    import object_records

    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "Alice's note"}, base_dir=data_dir, roots=[], actor="alice",
    )
    object_records.create_collection_record(
        "notes", {"id": "n2", "title": "Bob's note"}, base_dir=data_dir, roots=[], actor="bob",
    )

    status, _, payload = request("/api/activity", headers=signed_in_bearer("alice"))

    assert status == 200
    assert payload["status"] == "ok"
    assert [entry["record_id"] for entry in payload["activity"]] == ["n1"]
    assert payload["activity"][0]["title"] == "Alice's note"
    assert payload["activity"][0]["action"] == "create"
    assert "before" not in payload["activity"][0]


def test_get_api_activity_respects_limit_query_param(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    enable_admin_token(monkeypatch)
    write_records(data_dir, "notes", "id\ttitle\n")

    import object_records

    for i in range(3):
        object_records.create_collection_record(
            "notes", {"id": f"n{i}", "title": f"Note {i}"},
            base_dir=data_dir, roots=[], actor="alice",
        )

    status, _, payload = request(
        "/api/activity", query_string="limit=1", headers=signed_in_bearer("alice")
    )

    assert status == 200
    assert len(payload["activity"]) == 1
    assert payload["activity"][0]["record_id"] == "n2"
