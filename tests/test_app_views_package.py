"""Structural tests for packages/app-views (dynamic UI: views as records).

Mirrors the package/schema/permission/renderer testing conventions used for
packages/app-notes and packages/app-settings in tests/test_object_packages.py
and tests/test_app_settings_package.py.
"""

import json
import re
from pathlib import Path

import object_execution
import object_packages
import object_permissions
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_VIEWS_DIR = PACKAGES_ROOT / "app-views"

CLOSED_VOCABULARY = {"list", "form", "detail", "count", "markdown", "reader"}


def test_get_package_normalizes_app_views_manifest():
    package = object_packages.get_package("app-views", root=PACKAGES_ROOT)

    assert package["id"] == "app-views"
    assert package["name"] == "Views"
    assert package["objects"] == [
        {"id": "site_view_render", "path": "objects/site/view_render.py"}
    ]
    assert package["schemas"] == [{"collection": "views", "path": "schemas/views.json"}]
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    assert package["seed"] == []


def test_dry_run_app_views_package_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()

    plan = object_packages.dry_run_package(
        "app-views",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert {schema["collection"] for schema in plan["schemas"]} == {"views"}


def test_views_schema_matches_spec_fields():
    payload = json.loads((APP_VIEWS_DIR / "schemas" / "views.json").read_text())

    assert payload["name"] == "views"
    field_names = [field["name"] for field in payload["fields"]]
    assert field_names == [
        "id", "title", "route", "layout", "blocks",
        "owner_id", "is_public", "pinned", "created_at",
    ]

    by_name = {field["name"]: field for field in payload["fields"]}
    assert by_name["title"]["required"] is True
    assert by_name["route"]["type"] == "text"
    assert by_name["layout"]["type"] == "enum"
    assert by_name["layout"]["enum"] == ["single", "two_column", "grid"]
    assert by_name["layout"]["default"] == "single"
    assert by_name["blocks"]["type"] == "textarea"
    assert by_name["owner_id"]["type"] == "text"
    assert by_name["is_public"]["type"] == "boolean"
    assert by_name["is_public"]["default"] == "false"
    assert by_name["pinned"]["type"] == "boolean"
    assert by_name["pinned"]["default"] == "false"
    assert by_name["created_at"]["type"] == "datetime"
    assert by_name["created_at"]["read_only"] is True

    # forms/views/search keys are present and sensible for a records-list UI.
    assert "blocks" in payload["forms"]["default"]["fields"]
    assert "title" in payload["forms"]["default"]["fields"]
    assert "id" not in payload["forms"]["default"]["fields"]
    assert "created_at" not in payload["forms"]["default"]["fields"]
    assert payload["views"]["list_mode"] in {"table", "cards"}
    assert "title" in payload["views"]["list_fields"]
    assert "title" in payload["search"]["fields"]


def test_blocks_vocabulary_documented_in_schema_help():
    payload = json.loads((APP_VIEWS_DIR / "schemas" / "views.json").read_text())
    by_name = {field["name"]: field for field in payload["fields"]}
    help_text = by_name["blocks"].get("help", "")

    for kind in CLOSED_VOCABULARY:
        assert kind in help_text, f"blocks help text must document the {kind!r} kind"


def test_install_app_views_package_loads_schema(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-views",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    import object_schemas

    schema = object_schemas.get_schema("views", base_dir=data_dir)
    assert schema["name"] == "views"
    assert (object_root / "site" / "view_render.py").is_file()


def _app_views_policy():
    payload = json.loads((APP_VIEWS_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_owner_can_crud_own_views():
    policy = _app_views_policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "title": "My Dashboard", "is_public": "false"}

    for action in (
        object_permissions.CREATE,
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="views", record=record
        )
        assert decision.allowed is True


def test_others_cannot_touch_a_private_view():
    policy = _app_views_policy()
    subject = object_permissions.PermissionSubject(user_id="8")
    record = {"owner_id": "7", "title": "My Dashboard", "is_public": "false"}

    for action in (
        object_permissions.READ,
        object_permissions.UPDATE,
        object_permissions.DELETE,
    ):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="views", record=record
        )
        assert decision.allowed is False


def test_public_view_is_readable_by_anonymous_visitors():
    policy = _app_views_policy()
    record = {"owner_id": "7", "title": "Public Board", "is_public": "true"}

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="views", record=record
    )
    assert decision.allowed is True


def test_private_view_is_not_readable_by_anonymous_visitors():
    policy = _app_views_policy()
    record = {"owner_id": "7", "title": "My Dashboard", "is_public": "false"}

    decision = object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="views", record=record
    )
    assert decision.allowed is False


def test_renderer_execute_is_public():
    policy = _app_views_policy()

    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_view_render"
    )
    assert decision.allowed is True


def test_renderer_object_serves_scaffolding_for_a_view_id(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-views",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    view_id = "3fbb7e9e-2222-4d3d-8b8a-9d6b7f000001"
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_view_render", payload={"view_id": view_id, "_identity": {}}
        ),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert view_id in body
    assert 'id="blocks"' in body
    assert 'id="viewtitle"' in body
    assert '<script src="/list">' in body
    assert '<script src="/form">' in body
    assert '<script src="/nav">' in body


def test_renderer_returns_404_shape_for_a_missing_view_id(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    object_packages.install_package(
        "app-views",
        root=PACKAGES_ROOT,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_view_render", payload={"_identity": {}}),
        roots=[object_root],
    )

    assert result.ok is True
    assert result.result["status"] == 404


def test_renderer_source_covers_the_closed_block_vocabulary():
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()

    assert 'KNOWN_KINDS = ["list", "form", "detail", "count", "markdown", "reader"]' in source
    for kind in CLOSED_VOCABULARY:
        assert f'"{kind}": render' in source or f"render{kind.capitalize()}" in source
    assert "unsupported" in source.lower()
    assert "Invalid blocks JSON" in source


def test_renderer_markdown_block_escapes_before_formatting():
    """The markdown block must escape ALL html first, then apply
    bold/italic/links/line-breaks on the already-escaped text -- never
    innerHTML the raw block text. Assert the order directly on the JS
    source, matching how other site-object tests assert on generated
    script text.
    """
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()

    match = re.search(
        r"function renderMarkdown\(block, mount\) \{(.*?)\n\}", source, re.S
    )
    assert match, "renderMarkdown function not found in view_render.py"
    body = match.group(1)

    esc_pos = body.index("esc(block.text)")
    bold_pos = body.index("<strong>")
    italic_pos = body.index("<em>")
    link_pos = body.index('target="_blank"')
    br_pos = body.index("<br>")

    # Escaping happens first; every formatting transform runs on the
    # already-escaped string, never on raw block.text.
    assert esc_pos < bold_pos < italic_pos < link_pos < br_pos
    assert "innerHTML = block.text" not in source
    assert "innerHTML = mount.textContent" not in source


def test_renderer_reader_block_fetches_api_read_and_escapes_output():
    """The reader block is client-side like every other block here: it
    POSTs to /api/read and renders whatever comes back through esc() --
    title, paragraph text, and link labels must never be innerHTML'd raw,
    same discipline as renderMarkdown."""
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()

    match = re.search(r"function renderReader\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert match, "renderReader function not found in view_render.py"
    body = match.group(1)

    assert '"/api/read"' in body
    assert "method: \"POST\"" in body
    assert "esc(data.title" in body
    assert "esc(p)" in body
    assert "esc(l.href)" in body
    assert "esc(l.label)" in body
    assert "unsupportedCard(data.error" in body


def test_nav_lists_pinned_views_and_fails_silently():
    source = (PACKAGES_ROOT / "app-theme" / "objects" / "site" / "nav.py").read_text()

    assert "loadPinnedViews" in source
    assert "/collections/views/records" in source
    assert "v.pinned" in source
    # The fetch is wrapped so a missing app-views package cannot break the bar.
    assert "catch (e)" in source


def test_app_theme_manifest_still_normalizes_after_nav_change():
    package = object_packages.get_package("app-theme", root=PACKAGES_ROOT)
    assert package["id"] == "app-theme"
    assert {obj["id"] for obj in package["objects"]} >= {"site_nav"}
