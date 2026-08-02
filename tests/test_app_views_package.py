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

# The `object` kind is the escape hatch that keeps a generated page from
# being all-or-nothing: one block may be a hand-written object, so a page
# can stay generated everywhere except the part that genuinely needs code.
CLOSED_VOCABULARY = {"list", "form", "detail", "related", "thread", "count",
                     "aggregate", "markdown", "reader", "object"}


def test_get_package_normalizes_app_views_manifest():
    package = object_packages.get_package("app-views", root=PACKAGES_ROOT)

    assert package["id"] == "app-views"
    assert package["name"] == "Views"
    assert package["objects"] == [
        {"id": "site_view_render", "path": "objects/site/view_render.py"},
        # 0.2.0: /flow -- the workflow viewable, compiled live by
        # object_governance from the declarations the server enforces.
        {"id": "site_flow", "path": "objects/site/flow.py"},
        # 0.3.0: /urls -- the site map, compiled (convention + routes +
        # views + core constants), with shadowing called out.
        {"id": "site_urls", "path": "objects/site/urls.py"},
    ]
    assert package["schemas"] == [{"collection": "views", "path": "schemas/views.json"}]
    assert package["permissions"] == [{"path": "permissions/rules.json"}]
    # 0.2.0 seeds the /flow routes.
    assert package["seed"] == [
        {"collection": "site_routes", "path": "seed/site_routes.tsv"}
    ]


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

    assert ('KNOWN_KINDS = ["list", "form", "detail", "related", "thread", '
            '"count", "aggregate", "markdown", "reader", "object"]') in source
    for kind in CLOSED_VOCABULARY:
        assert f'"{kind}": render' in source or f"render{kind.capitalize()}" in source
    assert "unsupported" in source.lower()
    assert "Invalid blocks JSON" in source


def test_renderer_markdown_block_delegates_to_shared_renderer_and_never_raw_innerhtmls():
    """The markdown block delegates to the ONE shared renderer at
    /markdown (window.dbbasicMarkdown, in markdown.py -- escapes ALL html
    first, then applies markdown on the already-escaped text) rather than
    formatting block.text itself. Assert the delegation directly on the
    JS source, and that block.text is never innerHTML'd raw regardless of
    whether window.dbbasicMarkdown happens to be loaded.
    """
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()

    match = re.search(
        r"function renderMarkdown\(block, mount\) \{(.*?)\n\}", source, re.S
    )
    assert match, "renderMarkdown function not found in view_render.py"
    body = match.group(1)

    assert "window.dbbasicMarkdown" in body
    assert "window.dbbasicMarkdown(block.text)" in body
    # The no-/markdown-loaded fallback still escapes -- never a raw pass-through.
    assert "esc(block.text)" in body
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


def test_view_render_resolves_capture_less_list_view_by_path(tmp_path):
    """view_render can route a capture-LESS index/list view (e.g. /entities,
    whose views.route is a plain literal with no {param}) by matching the raw
    request path (request["_path"], set by object_server for every routed
    object) against the view's `route` -- not just detail views with a route
    capture. The views record stays the single source of truth; no second
    route table."""
    import types

    src = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()
    mod = types.ModuleType("view_render_under_test")
    exec(compile(src, "view_render.py", "exec"), mod.__dict__)

    data_dir = tmp_path / "data"
    (data_dir / "collections" / "views").mkdir(parents=True)
    (data_dir / "collections" / "views" / "records.tsv").write_text(
        "id\troute\n"
        "view_entities_list\t/entities\n"
        "view_entities_detail\t/entities/{entity_id}\n"
    )

    uuid = "00000000-0000-4000-8000-000000000000"
    # capture-less list route resolves by _path, with no record id
    assert mod._resolve_view_and_record({"_path": "/entities"}, data_dir) == (
        "view_entities_list", "",
    )
    # detail route still resolves by its single capture (record id present)
    assert mod._resolve_view_and_record(
        {"entity_id": uuid, "_path": f"/entities/{uuid}"}, data_dir
    ) == ("view_entities_detail", uuid)
    # an unknown path resolves to nothing (404 upstream), never a wrong view
    assert mod._resolve_view_and_record({"_path": "/nope"}, data_dir) == ("", "")
    # _path is reserved -- never mistaken for a route capture value
    assert "_path" in mod._RESERVED_REQUEST_KEYS


def test_view_render_has_an_aggregate_block_for_document_totals():
    """The `aggregate` block sums numeric fields across a related/filtered set
    (a journal's debit/credit balance, an invoice's line subtotal) -- the
    document-totals shape `related` (lists) and `count` (counts) can't express.
    Money `_cents` sums render in whole units; `balance: [a,b]` adds a
    Balanced/Not badge. The one new block the Stage-6 retrofit surfaced a real
    need for (the journal's client-computed balance summary)."""
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()
    assert '"aggregate"' in source  # registered in KNOWN_KINDS
    assert "aggregate: renderAggregate" in source  # wired into RENDERERS
    assert "function renderAggregate(block, mount)" in source
    # sums numeric fields, filtered to the parent like `related`
    assert "block.sums" in source and "block.fk_field" in source or "const fk = block.fk_field" in source
    assert "reduce((a, r) => a + (Number(r[f]) || 0), 0)" in source
    # money-aware (cents -> whole units) and the balance badge
    assert '/_cents$/.test(String(f)) ? (Number(n) / 100).toFixed(2)' in source
    assert "block.balance" in source and "Balanced" in source and "Not balanced" in source


def test_a_list_block_can_carry_a_standing_where_instead_of_a_client_side_filter():
    """0.4.0: `where` on a `list` block compiles to the same server-side
    narrowing `related` has always used (window.dbbasicList's `where`, one
    field=value param applied after the permission row filter), so an index
    over ONE SLICE of a shared collection keeps the real generator.

    The distinction matters because the alternative is not merely worse, it
    is unusable: `filters` routes the block through renderFilteredList,
    which titles every row `title || name || id`, so a collection with
    neither a title nor a name (shipments) renders as a column of raw ids
    with no table, no filter bar, no search and no row cap. app-returns'
    inbound bench is the case that surfaced it.
    """
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()

    assert "cfg.where = block.where" in source
    # It must NOT divert into the client-side fallback: that branch keys off
    # `filters`/`limit` only, and `where` has to reach window.dbbasicList.
    assert "const hasFilters = block.filters && Object.keys(block.filters).length;" in source
    assert "block.where" not in source.split("const hasFilters")[1].split("const cfg =")[0]
    # Literals only -- a parent-scoped child list is what `related` is for.
    assert "resolveRecordId(block.where" not in source


def test_the_where_key_is_documented_where_a_view_author_would_look():
    """A block option nobody can discover is a private API. The module
    docstring is the vocabulary reference for this renderer, and it has to
    say why `where` exists next to `filters` rather than just that it
    does."""
    source = (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()
    docstring = source.split('"""')[1]

    assert "`where`" in docstring
    assert "filters" in docstring
    manifest = json.loads((APP_VIEWS_DIR / "dbbasic-package.json").read_text())
    assert manifest["version"] == "0.4.0"
    assert "`where`" in manifest["description"]
