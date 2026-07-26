"""Structural + behavioral tests for packages/app-nav.

Follows the package/schema/permission conventions of
tests/test_app_activity_package.py and the direct-execution pattern it
uses for a site object. The platform half of the nav registry (manifest
validation, install idempotency, the operator_hidden rule, the
shipped-package sweep) lives in tests/test_package_nav.py; this file is
about what the two surfaces actually render.
"""

import json
from pathlib import Path

import pytest

import object_execution
import object_packages
import object_permissions
import object_records
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_NAV_DIR = PACKAGES_ROOT / "app-nav"


def _app_nav_policy():
    payload = json.loads((APP_NAV_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]})


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """app-nav installed into empty roots, with the environment pointed at
    them: site_home calls action_nav_entries in-process, which resolves
    through the same roots the server would use."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))

    object_packages.install_package(
        "app-nav", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root])
    return {"data": data_dir, "objects": object_root}


def add_entry(installed, nav_id, **fields):
    row = {"id": nav_id, "package": "app-test", "label": nav_id.title(),
           "path": f"/{nav_id}", "blurb": "", "group": "Work",
           "surface": "member", "order": "10", "operator_hidden": "false"}
    row.update(fields)
    object_records.create_collection_record(
        "nav_entries", row, base_dir=installed["data"], actor="test")


def run(installed, object_id, identity, method="POST"):
    runtime = python_object_runtime.PythonObjectRuntime(base_dir=installed["data"])
    return object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload={"_identity": identity}),
        roots=[installed["objects"]])


def page(installed, identity):
    """site_home is a GET page; the action it folds over answers either."""
    result = run(installed, "site_home", identity, method="GET")
    assert result.ok, result.error
    return result.result["body"]


def labels(result):
    return [entry["label"] for entry in result.result["entries"]]


# --- the package ------------------------------------------------------------

def test_get_package_normalizes_the_app_nav_manifest():
    package = object_packages.get_package("app-nav", root=PACKAGES_ROOT)

    assert package["id"] == "app-nav"
    assert [entry["id"] for entry in package["objects"]] == [
        "action_nav_entries", "site_home"]
    assert package["schemas"] == [
        {"collection": "nav_entries", "path": "schemas/nav_entries.json"}]
    assert package["nav"][0]["path"] == "/"


def test_dry_run_app_nav_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-nav", root=PACKAGES_ROOT, base_dir=tmp_path / "data",
        object_roots=[object_root])

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_the_seed_is_header_only():
    """The registry's rows come from other packages' manifests, not from
    a seed file that would be a fourth hand-maintained list."""
    lines = (APP_NAV_DIR / "seed" / "nav_entries.tsv").read_text().splitlines()
    assert len(lines) == 1
    schema = json.loads((APP_NAV_DIR / "schemas" / "nav_entries.json").read_text())
    assert lines[0].split("\t") == [field["name"] for field in schema["fields"]]


def test_reading_the_menu_is_public_and_so_is_the_front_door():
    policy = _app_nav_policy()
    for object_id in ("action_nav_entries", "site_home"):
        decision = object_permissions.check_permission(
            None, object_permissions.EXECUTE, policy=policy, object_id=object_id)
        assert decision.allowed is True, object_id


def test_the_registry_itself_is_not_public_and_only_a_manager_may_edit_it():
    """A visitor reads the menu through the object that filters it, never
    the raw collection -- otherwise `surface` would leak every operator
    door to anyone who asked for the records."""
    policy = _app_nav_policy()

    anonymous = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="nav_entries")
    assert anonymous.allowed is False

    member = object_permissions.PermissionSubject(user_id="alice", roles=("user",))
    assert object_permissions.check_permission(
        member, object_permissions.READ, policy=policy, collection="nav_entries").allowed is True
    assert object_permissions.check_permission(
        member, object_permissions.UPDATE, policy=policy, collection="nav_entries").allowed is False

    manager = object_permissions.PermissionSubject(user_id="mo", roles=("manager",))
    assert object_permissions.check_permission(
        manager, object_permissions.UPDATE, policy=policy, collection="nav_entries").allowed is True


# --- action_nav_entries: the visibility tiers ---------------------------------

def test_a_visitor_sees_only_the_public_tier(installed):
    add_entry(installed, "shop", surface="public")
    add_entry(installed, "notes", surface="member")
    add_entry(installed, "scheduler", surface="operator")

    assert labels(run(installed, "action_nav_entries", {})) == ["Home", "Shop"]


def test_a_signed_in_user_also_sees_the_member_tier(installed):
    add_entry(installed, "shop", surface="public")
    add_entry(installed, "notes", surface="member")
    add_entry(installed, "scheduler", surface="operator")

    result = run(installed, "action_nav_entries", {"user_id": "alice"})
    assert labels(result) == ["Home", "Notes", "Shop"]


def test_only_an_admin_sees_the_operator_tier(installed):
    """The same admin test system_scheduler makes before it will draw the
    task board -- one answer on this server to 'is this an operator?'."""
    add_entry(installed, "scheduler", surface="operator")

    assert "Scheduler" not in labels(run(installed, "action_nav_entries",
                                         {"user_id": "alice", "roles": ["user"]}))
    assert "Scheduler" in labels(run(installed, "action_nav_entries",
                                     {"user_id": "root", "roles": ["admin"]}))


def test_hidden_surfaces_and_operator_hidden_rows_are_never_returned(installed):
    """Two columns because they answer two different questions: the
    package registered a door it does not want rendered, and the operator
    took a door off this deployment's menu."""
    add_entry(installed, "internal", surface="hidden")
    add_entry(installed, "removed", surface="member", operator_hidden="true")
    add_entry(installed, "removed_public", surface="public", operator_hidden="true")

    for identity in ({}, {"user_id": "alice"}, {"user_id": "root", "roles": ["admin"]}):
        shown = labels(run(installed, "action_nav_entries", identity))
        assert "Internal" not in shown
        assert "Removed" not in shown
        assert "Removed_Public" not in shown


def test_entries_are_grouped_and_a_group_sorts_where_its_first_entry_does(installed):
    """Group order falls out of the same `order` the entries carry, so
    there is no seventh list holding the reading order of the groups."""
    add_entry(installed, "dashboard", group="System", order="600")
    add_entry(installed, "invoices", group="Money", order="300")
    add_entry(installed, "notes", group="Work", order="30")

    result = run(installed, "action_nav_entries", {"user_id": "alice"})
    assert [group["group"] for group in result.result["groups"]] == [
        "Work", "Money", "System"]
    assert [entry["label"] for entry in result.result["groups"][0]["entries"]] == [
        "Home", "Notes"]


def test_a_hand_edited_order_that_will_not_parse_does_not_break_the_menu(installed):
    """The schema refuses a non-integer `order` on the write path, so this
    can only arrive by somebody editing the TSV -- which is exactly when a
    menu that raised instead of degrading would be least welcome."""
    records = installed["data"] / "collections" / "nav_entries" / "records.tsv"
    records.write_text(records.read_text()
                       + "typo\tapp-test\tTypo\t/typo\t\tWork\tmember\tsoon\tfalse\t\t\n")

    result = run(installed, "action_nav_entries", {"user_id": "alice"})
    assert result.ok is True
    assert "Typo" in labels(result)


def test_an_empty_registry_is_an_empty_menu_not_an_error(tmp_path, monkeypatch):
    """The nav app installed before anything else has registered: a real
    answer, which is what lets both surfaces fall back on their own."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    object_packages.install_package(
        "app-nav", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root])
    object_records.delete_collection_record(
        "nav_entries", "home", base_dir=data_dir, actor="test")

    result = run({"data": data_dir, "objects": object_root},
                 "action_nav_entries", {"user_id": "alice"})
    assert result.ok is True
    assert result.result == {"ok": True, "groups": [], "entries": [], "count": 0}


# --- site_home: the fold ------------------------------------------------------

def test_the_home_page_renders_the_registry_grouped_under_headings(installed):
    add_entry(installed, "notes", group="Work", order="30", blurb="Quick capture")
    add_entry(installed, "invoices", group="Money", order="300")

    body = page(installed, {"user_id": "alice"})

    assert "<h2>Work</h2>" in body
    assert "<h2>Money</h2>" in body
    assert body.index("<h2>Work</h2>") < body.index("<h2>Money</h2>")
    assert '<a class="tile" href="/notes">' in body
    assert "Quick capture" in body
    assert '<script src="/nav">' in body        # the shared design system
    assert "<style" not in body                 # no inline CSS: /style owns it


def test_the_home_page_shows_a_visitor_only_public_doors(installed):
    add_entry(installed, "notes", surface="member")

    body = page(installed, {})
    assert '<a href="/login">sign in</a>' in body
    assert "/notes" not in body
    assert '<a class="tile" href="/">' in body   # the public Home entry


def test_the_home_page_falls_back_rather_than_showing_a_blank_front_door(installed):
    """A fresh install renders this page before anything has registered,
    and a blank switchboard is a worse first impression than a stale one."""
    object_records.delete_collection_record(
        "nav_entries", "home", base_dir=installed["data"], actor="test")

    body = page(installed, {"user_id": "alice"})
    assert "<h2>Work</h2>" in body
    assert 'href="/shell"' in body
    assert 'href="/dashboard"' in body


def test_the_home_page_escapes_what_the_registry_hands_it(installed):
    add_entry(installed, "xss", label='<script>alert(1)</script>', blurb='" onmouseover=x')

    body = page(installed, {"user_id": "alice"})
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_the_nav_script_folds_the_switcher_over_the_registry():
    """site_nav's hardcoded APPS array is now a fallback for a failed
    fetch, not the list -- and HIT_URL is deliberately still hand-kept,
    because a search hit needs a permalink for a RECORD, which the
    registry does not model."""
    source = (PACKAGES_ROOT / "app-theme" / "objects" / "site" / "nav.py").read_text()

    assert "/objects/action_nav_entries" in source
    assert "const APPS" in source
    assert "Fallback only" in source
    assert "const HIT_URL" in source
