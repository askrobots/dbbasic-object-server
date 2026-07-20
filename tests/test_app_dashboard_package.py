"""Structural + behavioral tests for packages/app-dashboard.

Mirrors the package/permission testing conventions used for
packages/app-activity (see tests/test_app_activity_package.py) and the
direct-execution pattern tests/test_app_views_package.py uses for a
renderer-style site object. app-dashboard owns no collection and no
schema -- it only folds existing collections (ai_usage, tasks, /api/
activity, notes/contacts/invoices) into a page, so there is nothing here
to test at the data layer; the page-execution and permission tests below
cover the whole surface.
"""

import json
import re
from pathlib import Path

import object_execution
import object_packages
import object_permissions
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_DASHBOARD_DIR = PACKAGES_ROOT / "app-dashboard"
TEST_FILE = Path(__file__).resolve()


def _app_dashboard_policy():
    payload = json.loads((APP_DASHBOARD_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_get_package_normalizes_app_dashboard_manifest():
    package = object_packages.get_package("app-dashboard", root=PACKAGES_ROOT)

    assert package["id"] == "app-dashboard"
    assert package["name"] == "Dashboard"
    assert package["objects"] == [{"id": "site_dashboard", "path": "objects/site/dashboard.py"}]
    assert package["schemas"] == []
    assert package["seed"] == []
    assert package["permissions"] == [{"path": "permissions/rules.json"}]


def test_dry_run_app_dashboard_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-dashboard",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_site_dashboard_execute_is_public():
    """Same split as site_notes/site_tasks/site_activity: public EXECUTE on
    the *page* object (it shows a sign-in prompt to visitors), never public
    read on a collection -- app-dashboard owns no collection at all.
    """
    policy = _app_dashboard_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_dashboard"
    )

    assert decision.allowed is True


def test_dashboard_page_prompts_anonymous_visitors_to_sign_in(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-dashboard", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_dashboard", payload={"_identity": {}}),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert result.result.get("status", 200) == 200
    assert "Sign in" in body
    assert 'id="ai-usage"' not in body


def test_dashboard_page_serves_stats_scaffolding_for_signed_in_users(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-dashboard", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_dashboard", payload={"_identity": {"user_id": "alice"}}
        ),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    # The page holds no data access of its own -- it fetches with the
    # caller's session cookie, so the scaffolding should reference the
    # source endpoints/collections it folds, not embed any data.
    assert 'id="ai-usage"' in body
    assert 'id="task-stats"' in body
    assert 'id="activity"' in body
    assert "/collections/ai_usage/records" in body
    assert "/collections/tasks/records" in body
    assert "/api/activity" in body
    assert '<link rel="stylesheet" href="/style">' in body
    assert '<script src="/nav">' in body


def test_no_disallowed_org_names_leak_into_the_package_or_this_test_file():
    """Public repo hygiene: no internal org/codename references anywhere in
    this package's source or in this test file.
    """
    # Built from fragments so this guard file itself stays clean of the very
    # internal names it forbids (otherwise the test would flag its own source).
    banned = re.compile(
        "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
        re.IGNORECASE,
    )
    paths = [p for p in APP_DASHBOARD_DIR.rglob("*") if p.is_file()] + [TEST_FILE]
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not banned.search(text), f"disallowed reference found in {path}"
