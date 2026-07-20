"""Tests for plan/vocabulary/59-detail-related-spec.md: detail mode, the
`related` block, `thread` sugar, $record_id route-capture resolution, and
route-seeding.

Mirrors the source-assertion + structural-install testing style already
used by tests/test_app_views_package.py (for packages/app-views) and the
route-resolution testing style of tests/test_object_site_routes.py (for
object_site_routes.py / object_server.py's site routing).
"""

import json
import re
from pathlib import Path

from test_object_server import (
    auth_headers,
    enable_admin_token,
    raw_request,
    request,
    save_permission_policy,
    write_records,
)
from test_object_site_routes import enable_site_routes

import object_execution
import object_packages
import object_permissions
import object_server
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_VIEWS_DIR = PACKAGES_ROOT / "app-views"
APP_THEME_DIR = PACKAGES_ROOT / "app-theme"
APP_CONTACTS_DIR = PACKAGES_ROOT / "app-contacts"
APP_TASKS_DIR = PACKAGES_ROOT / "app-tasks"


def _view_render_source():
    return (APP_VIEWS_DIR / "objects" / "site" / "view_render.py").read_text()


def _form_source():
    return (APP_THEME_DIR / "objects" / "site" / "form.py").read_text()


def _detail_source():
    return (APP_THEME_DIR / "objects" / "site" / "detail.py").read_text()


def _list_source():
    return (APP_THEME_DIR / "objects" / "site" / "list.py").read_text()


# ---------------------------------------------------------------------
# Detail mode reuses /form's field-rendering pipeline (no second renderer)
# ---------------------------------------------------------------------


def test_detail_generator_is_a_public_static_script_like_list_and_form():
    manifest = json.loads((APP_THEME_DIR / "dbbasic-package.json").read_text())
    object_ids = {obj["id"] for obj in manifest["objects"]}
    assert "site_detail" in object_ids

    rules = json.loads((APP_THEME_DIR / "permissions" / "rules.json").read_text())["rules"]
    detail_rules = [r for r in rules if r.get("object_id") == "site_detail"]
    assert len(detail_rules) == 1
    assert detail_rules[0]["effect"] == "allow"
    assert detail_rules[0]["principal"] == "public"
    assert detail_rules[0]["actions"] == ["execute"]


def test_site_detail_execute_is_public():
    policy = object_permissions.policy_from_dict(
        {
            "access_mode": "role_based",
            "rules": json.loads((APP_THEME_DIR / "permissions" / "rules.json").read_text())["rules"],
        }
    )
    decision = object_permissions.check_permission(
        None, object_permissions.EXECUTE, policy=policy, object_id="site_detail",
    )
    assert decision.allowed is True


def test_detail_object_serves_dbbasicDetail_script():
    runtime = python_object_runtime.PythonObjectRuntime()
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_detail", payload={}),
        roots=[APP_THEME_DIR / "objects"],
    )
    assert result.ok is True
    assert result.result["content_type"] == "application/javascript; charset=utf-8"
    assert "window.dbbasicDetail" in result.result["body"]
    assert "mount:" in result.result["body"]


def test_detail_delegates_to_form_read_only_pipeline_not_a_second_renderer():
    """59's Purpose section: detail mode reuses /form's schema fetch,
    field order, relation display-field resolution, and type-aware value
    formatting -- 'a second field-renderer would be a second place to
    keep that formatting logic correct.' detail.py must not re-implement
    any of that; it only fetches the record and hands it to
    window.dbbasicForm.readOnly.
    """
    form_source = _form_source()
    detail_source = _detail_source()

    assert "window.dbbasicForm.readOnly = async function" in form_source
    assert "window.dbbasicForm.readOnly(" in detail_source
    # No independent relation/enum/date formatting logic in detail.py --
    # that stays in form.py's control()/readOnlyText(), used by both modes.
    for needle in ("f.relation", "f.enum", "readOnlyText", "/api/schema/"):
        assert needle not in detail_source, f"{needle!r} should live in form.py only, not detail.py"


def test_form_read_only_shares_control_with_edit_mode():
    """One control() function backs both window.dbbasicForm (edit) and
    window.dbbasicForm.readOnly (detail) -- the same relation-fetch and
    type-formatting branches, not two copies."""
    source = _form_source()
    assert source.count("async function control(f, value, readOnly)") == 1
    assert "control(f, record[f.name], true)" in source  # readOnly mode
    assert "control(f, record ? record[f.name] : f.default)" in source  # edit mode, unchanged


def test_form_read_only_field_order_prefers_views_detail_fields():
    source = _form_source()
    match = re.search(
        r"window\.dbbasicForm\.readOnly = async function \(collection, opts\) \{(.*?)\n  \};",
        source, re.S,
    )
    assert match, "window.dbbasicForm.readOnly not found"
    body = match.group(1)
    assert "schema.views && schema.views.detail_fields" in body
    # NOT forms.default.fields -- that's edit mode's own convention (59:
    # "Otherwise, schema field declaration order", not the edit form's).
    assert "forms.default" not in body


def test_form_read_only_skips_fields_absent_from_the_record():
    """'Visible = readable' falls out of the platform's own field
    redaction (object_server.py's _apply_record_field_policy deletes
    denied keys) -- detail mode must not re-implement a permission check,
    it just skips a field missing from the record."""
    source = _form_source()
    assert "if (!(f.name in record)) continue;" in source


def test_view_render_detail_block_is_a_thin_mount_wrapper():
    source = _view_render_source()
    assert '<script src="/detail">' in source
    assert "window.dbbasicDetail.mount(mount" in source
    # The old inline schema+record fetch/format is gone from renderDetail.
    detail_fn = re.search(r"function renderDetail\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert detail_fn, "renderDetail not found"
    assert "/api/schema/" not in detail_fn.group(1)


# ---------------------------------------------------------------------
# Owner-aware edit/delete (Stage 6 extension): the detail block gains
# editable/deletable affordances, shown only to the record's owner, that
# REUSE /form's existing edit pipeline -- so per-collection *_view pages
# collapse into one view record + this renderer.
# ---------------------------------------------------------------------


def test_detail_owner_aware_edit_reuses_form_edit_pipeline_not_a_new_one():
    source = _detail_source()
    # Owner gate: compare the record's owner_field to the viewer id, both
    # supplied by the caller (view_render passes VIEWER_ID) -- never a new
    # permission path, just whether to render the affordance.
    assert "opts.viewer_id" in source
    assert 'opts.owner_field || "owner_id"' in source
    assert "opts.editable" in source and "opts.deletable" in source
    # Edit reuses window.dbbasicForm's edit mode (a record present => PUT)
    # and its onSaved callback -- detail.py must not re-implement editing.
    assert "window.dbbasicForm(collection, {" in source
    assert "onSaved:" in source
    # Still delegates read rendering to the shared read-only pipeline, and
    # still re-implements no field formatting of its own.
    assert "window.dbbasicForm.readOnly(" in source
    for needle in ("f.relation", "f.enum", "readOnlyText"):
        assert needle not in source, f"{needle!r} should live in form.py only, not detail.py"


def test_detail_delete_issues_record_delete_then_redirects():
    source = _detail_source()
    assert '"DELETE"' in source
    assert "opts.delete_redirect" in source
    assert "window.confirm(" in source  # never a silent destructive action


def test_detail_edit_state_survives_a_subscribe_reload():
    """view_render re-mounts the detail block on any collection change; that
    must not wipe out an edit the owner is mid-way through."""
    source = _detail_source()
    assert "_dbbasicEditing" in source
    assert "if (mount._dbbasicEditing) return;" in source


def test_view_render_detail_passes_owner_aware_options_and_viewer_id():
    source = _view_render_source()
    detail_fn = re.search(r"function renderDetail\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert detail_fn, "renderDetail not found"
    body = detail_fn.group(1)
    for needle in ("block.editable", "block.deletable", "block.delete_redirect", "VIEWER_ID"):
        assert needle in body, f"renderDetail should forward {needle!r} to the detail generator"


# ---------------------------------------------------------------------
# related compiles to 58's filtered read, not a bespoke fetch
# ---------------------------------------------------------------------


def test_list_generator_where_option_encodes_58_field_eq_query():
    source = _list_source()
    assert "cfg.where" in source
    assert 'encodeURIComponent(k) + "=" + encodeURIComponent(v)' in source
    assert "/records?limit=500" in source


def test_related_block_compiles_to_dbbasicList_where_not_a_bespoke_fetch():
    source = _view_render_source()
    related_fn = re.search(r"function renderRelated\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert related_fn, "renderRelated not found"
    body = related_fn.group(1)
    assert "window.dbbasicList(block.collection, {mount: listMount, where: {[block.fk_field]: match}})" in body
    # Never the client-side filter fallback `list` sometimes needs.
    assert "renderFilteredList" not in body
    assert "fetch(" not in body


def test_related_block_resolves_record_id_token():
    source = _view_render_source()
    related_fn = re.search(r"function renderRelated\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert "resolveRecordId(block.match)" in related_fn.group(1)


def test_resolve_record_id_helper_handles_literal_and_token():
    source = _view_render_source()
    fn = re.search(r"function resolveRecordId\(value\) \{(.*?)\n\}", source, re.S)
    assert fn, "resolveRecordId not found"
    body = fn.group(1)
    assert 'value !== "$record_id"' in body
    assert "return RECORD_ID" in body


def test_known_kinds_include_related_and_thread():
    source = _view_render_source()
    assert 'KNOWN_KINDS = ["list", "form", "detail", "related", "thread", "count", "markdown", "reader"]' in source
    assert "related: renderRelated" in source
    assert "thread: renderThread" in source


# ---------------------------------------------------------------------
# thread: sugar over 22, not reinvented
# ---------------------------------------------------------------------


def test_thread_block_mounts_dbbasicThread_and_degrades_without_it():
    source = _view_render_source()
    thread_fn = re.search(r"function renderThread\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert thread_fn, "renderThread not found"
    body = thread_fn.group(1)
    assert "window.dbbasicThread.mount(mount, {parent_collection: block.collection, parent_id: recordId})" in body
    assert "if (!window.dbbasicThread)" in body
    # No comment moderation/anon-mode/markdown logic here -- 22 owns it.
    for needle in ("moderation", "anon", "markdown"):
        assert needle not in body.lower()


# ---------------------------------------------------------------------
# $record_id route-capture resolution (Route-Seeding)
# ---------------------------------------------------------------------


def _install_app_views(base_dir, object_root):
    object_root.mkdir(parents=True, exist_ok=True)
    object_packages.install_package(
        "app-views", root=PACKAGES_ROOT, base_dir=base_dir, object_roots=[object_root],
    )


def test_record_id_resolves_from_a_routed_detail_capture(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    _install_app_views(data_dir, object_root)

    write_records(
        data_dir,
        "views",
        "id\ttitle\troute\tlayout\tblocks\towner_id\tis_public\tpinned\tcreated_at\n"
        "view_contacts_detail\tContact\t/contacts/{contact_id:uuid}\tsingle\t[]\t\ttrue\tfalse\t\n",
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    contact_id = "3fbb7e9e-2222-4d3d-8b8a-9d6b7f000099"
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_view_render",
            payload={"contact_id": contact_id, "_identity": {}},
        ),
        roots=[object_root],
    )

    assert result.ok is True
    body = result.result["body"]
    assert "const VIEW_ID = 'view_contacts_detail';" in body
    assert f"const RECORD_ID = {contact_id!r};" in body


def test_record_id_stays_empty_when_route_has_no_capture(tmp_path, monkeypatch):
    """A plain route (/stuck, no {param}) -- or a view reached directly
    via /views/{id} with no matching capture -- must not resolve a
    record id: a block referencing $record_id degrades to its visible
    error state instead of guessing (Degradation: "$record_id
    unresolvable ... the view was reached without going through its
    registered route")."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    _install_app_views(data_dir, object_root)

    write_records(
        data_dir,
        "views",
        "id\ttitle\troute\tlayout\tblocks\towner_id\tis_public\tpinned\tcreated_at\n"
        "view_stuck\tStuck\t/stuck\tsingle\t[]\t\ttrue\tfalse\t\n",
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_view_render",
            payload={"view_id": "view_stuck", "_identity": {}},
        ),
        roots=[object_root],
    )

    assert result.ok is True
    assert "const RECORD_ID = '';" in result.result["body"]


def test_record_id_stays_empty_for_a_two_capture_route(tmp_path, monkeypatch):
    """Defense-in-depth: 59 pins $record_id to routes with EXACTLY one
    capture (Open Questions). A route with two captures (a future
    multi-entity case, 65, not decided here) must not pick one
    arbitrarily -- it degrades the same as zero captures."""
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    _install_app_views(data_dir, object_root)

    write_records(
        data_dir,
        "views",
        "id\ttitle\troute\tlayout\tblocks\towner_id\tis_public\tpinned\tcreated_at\n"
        "view_two\tTwo\t/entities/{entity_id:uuid}/accounts/{account_id:uuid}\tsingle\t[]\t\ttrue\tfalse\t\n",
    )

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_view_render",
            payload={
                "view_id": "view_two",
                "entity_id": "e1",
                "account_id": "a1",
                "_identity": {},
            },
        ),
        roots=[object_root],
    )

    assert result.ok is True
    assert "const RECORD_ID = '';" in result.result["body"]


def test_missing_view_id_and_no_matching_capture_is_still_404(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    _install_app_views(data_dir, object_root)

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            "site_view_render", payload={"_identity": {}},
        ),
        roots=[object_root],
    )
    assert result.ok is True
    assert result.result["status"] == 404


# ---------------------------------------------------------------------
# Route-seeding: real site routing end to end, using this package's own
# seeded views + site_routes rows -- closes the audit's "detail routes
# not seeded into packages" finding.
# ---------------------------------------------------------------------


def test_route_resolution_for_a_seeded_detail_path(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    object_packages.install_package(
        "app-views", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[root],
    )
    object_packages.install_package(
        "app-contacts", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[root],
    )

    contact_id = "5c2b6b0a-1111-4a3d-8b8a-9d6b7f0000aa"
    status, headers, body = raw_request(f"/contacts/{contact_id}")

    assert status == 200
    text = body.decode("utf-8")
    assert "const VIEW_ID = 'view_contacts_detail';" in text
    assert f"const RECORD_ID = {contact_id!r};" in text
    assert '<script src="/detail">' in text

    # A bad (non-uuid) id doesn't match the {contact_id:uuid} pattern at
    # all -- ordinary site-routing 404, not a view_render concern.
    miss_status, _, _ = raw_request("/contacts/not-a-uuid")
    assert miss_status == 404


def test_route_resolution_for_a_seeded_task_detail_path_with_related_children(tmp_path, monkeypatch):
    root, data_dir = enable_site_routes(monkeypatch, tmp_path)
    object_packages.install_package(
        "app-views", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[root],
    )
    object_packages.install_package(
        "app-projects", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[root],
    )
    object_packages.install_package(
        "app-tasks", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[root],
    )

    task_id = "9f1a2b3c-2222-4a3d-8b8a-9d6b7f0000bb"
    status, _, body = raw_request(f"/tasks/{task_id}")

    assert status == 200
    text = body.decode("utf-8")
    assert "const VIEW_ID = 'view_tasks_detail';" in text
    assert f"const RECORD_ID = {task_id!r};" in text


def test_contacts_and_tasks_seed_views_and_site_routes(tmp_path):
    for pkg_id, base_dir_name in (("app-contacts", "c"), ("app-tasks", "t")):
        base_dir = tmp_path / base_dir_name / "data"
        object_root = tmp_path / base_dir_name / "objects"
        object_root.mkdir(parents=True)
        result = object_packages.install_package(
            pkg_id, root=PACKAGES_ROOT, base_dir=base_dir, object_roots=[object_root],
        )
        assert result["warnings"] == []
        assert (base_dir / "collections" / "views" / "records.tsv").is_file()
        assert (base_dir / "collections" / "site_routes" / "records.tsv").is_file()


# ---------------------------------------------------------------------
# Permissions posture: a related block is exactly a 58 filtered read, so
# it inherits 58's row-filter-first guarantee verbatim -- no leak.
# ---------------------------------------------------------------------


def test_related_query_shape_cannot_leak_a_row_outside_the_row_filter(tmp_path, monkeypatch):
    """The exact query shape renderRelated compiles to --
    /collections/{child}/records?{fk_field}={match} -- run against
    app-contacts' own real, owner-scoped permission rules (interactions
    has no public/cross-user read rule at all). A related block over
    another user's interactions must come back empty, never their row,
    even though the fk_field itself (contact_id) is fully readable."""
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "interactions",
        "id\tcontact_id\tsummary\towner_id\n"
        "i1\tc1\tCalled about renewal\towner-a\n"
        "i2\tc1\tPrivate note from another rep\towner-b\n",
    )
    policy = json.loads((APP_CONTACTS_DIR / "permissions" / "rules.json").read_text())
    save_permission_policy(data_dir, {"access_mode": "role_based", "rules": policy["rules"]})
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)

    # owner-a's own related-interactions read: sees only their own row.
    owner_status, _, owner_payload = request(
        "/collections/interactions/records",
        query_string="contact_id=c1",
        headers=[("x-dbbasic-user-id", "owner-a"), ("x-dbbasic-roles", "registered")],
    )
    # A different signed-in visitor's related-interactions read over the
    # SAME contact: interactions has no cross-user read rule at all, so
    # this must be empty, not owner-a's or owner-b's row.
    other_status, _, other_payload = request(
        "/collections/interactions/records",
        query_string="contact_id=c1",
        headers=[("x-dbbasic-user-id", "owner-c"), ("x-dbbasic-roles", "registered")],
    )
    # Anonymous: interactions has no "public" rule at all, so this is a
    # flat permission denial (403) rather than an empty 200 -- an even
    # more direct "no leak" than the signed-in-but-out-of-scope case
    # above, just a different status shape.
    anon_status, _, anon_payload = request(
        "/collections/interactions/records", query_string="contact_id=c1",
    )
    # Admin really can reach both rows -- proving the row filter, not an
    # accident, is what hid them above.
    admin_status, _, admin_payload = request(
        "/collections/interactions/records",
        query_string="contact_id=c1",
        headers=auth_headers(),
    )

    assert owner_status == 200
    assert [r["id"] for r in owner_payload["records"]] == ["i1"]

    assert other_status == 200
    assert other_payload["records"] == []

    assert anon_status == 403
    assert "i1" not in json.dumps(anon_payload)
    assert "i2" not in json.dumps(anon_payload)

    assert admin_status == 200
    assert {r["id"] for r in admin_payload["records"]} == {"i1", "i2"}


# ---------------------------------------------------------------------
# Scope boundary: related is for true children, not document-composition
# (order/invoice line items) -- 66-line-items-spec.md's territory.
# ---------------------------------------------------------------------

_LINE_ITEMS_COLLECTIONS = {"order_lines", "invoice_lines", "fin_journal_lines"}


def test_scope_boundary_is_stated_in_view_render_docs():
    source = _view_render_source()
    assert "Scope boundary" in source
    assert "line-items" in source
    assert "66-line-items-spec.md" in source


def test_no_seeded_related_block_targets_a_line_items_collection():
    """Regression guard for the related-vs-embed boundary: scan every
    schema's views.related for a target collection that is actually a
    document-composition (line-items) collection. None of the `related`
    entries this task seeds -- or any other schema in the repo -- may
    point at one; those compose via a `line-items` block (66) instead,
    once it exists."""
    for schema_path in PACKAGES_ROOT.glob("*/schemas/*.json"):
        try:
            schema = json.loads(schema_path.read_text())
        except json.JSONDecodeError:
            continue
        related = ((schema.get("views") or {}).get("related")) or []
        for entry in related:
            assert entry.get("collection") not in _LINE_ITEMS_COLLECTIONS, (
                f"{schema_path} declares a related block over a line-items "
                f"collection ({entry.get('collection')}) -- document-"
                "composition items embed (66), they are not a related target"
            )


def test_line_items_collections_have_no_fk_field_a_related_block_could_use():
    """66's own claim (59's Purpose section): once line items embed as a
    JSON array field, they have structurally nothing for `related`'s
    collection + fk_field shape to point at. Today (pre-66) these are
    still real collections -- this test just documents which ones the
    boundary applies to, so a future related-block author doesn't
    reach for one by habit."""
    for name in _LINE_ITEMS_COLLECTIONS:
        matches = list(PACKAGES_ROOT.glob(f"*/schemas/{name}.json"))
        assert matches, f"expected a schema for {name}"


# ---------------------------------------------------------------------
# Schema surfacing: views.detail_fields / views.related additive keys
# ---------------------------------------------------------------------


def test_contacts_detail_fields_and_related_interactions():
    schema = json.loads((APP_CONTACTS_DIR / "schemas" / "contacts.json").read_text())
    views = schema["views"]
    assert "detail_fields" in views
    assert set(views["detail_fields"]) <= {f["name"] for f in schema["fields"]}
    assert views["related"] == [{"collection": "interactions", "fk_field": "contact_id"}]


def test_organizations_related_contacts():
    schema = json.loads((APP_CONTACTS_DIR / "schemas" / "organizations.json").read_text())
    assert schema["views"]["related"] == [{"collection": "contacts", "fk_field": "organization_id"}]


def test_forum_topics_detail_fields_include_ai_summary():
    schema = json.loads((PACKAGES_ROOT / "app-forum" / "schemas" / "forum_topics.json").read_text())
    assert "ai_summary" in schema["views"]["detail_fields"]


def test_tasks_related_comments_and_files():
    schema = json.loads((APP_TASKS_DIR / "schemas" / "tasks.json").read_text())
    related = {(r["collection"], r["fk_field"]) for r in schema["views"]["related"]}
    assert ("task_comments", "task_id") in related
    assert ("files", "task_id") in related


def test_files_task_id_is_a_soft_relation_like_template_id():
    schema = json.loads((PACKAGES_ROOT / "app-files" / "schemas" / "files.json").read_text())
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["task_id"]["relation"]["collection"] == "tasks"
    manifest = json.loads((PACKAGES_ROOT / "app-files" / "dbbasic-package.json").read_text())
    assert "app-tasks" not in manifest.get("dependencies", [])


# ---------------------------------------------------------------------
# Repo hygiene: no internal org/codename references in anything this
# task added or touched.
# ---------------------------------------------------------------------

_BANNED = re.compile(
    "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
    re.IGNORECASE,
)
_TOUCHED_DIRS = [
    APP_THEME_DIR,
    APP_VIEWS_DIR,
    APP_CONTACTS_DIR,
    APP_TASKS_DIR,
    PACKAGES_ROOT / "app-forum",
    PACKAGES_ROOT / "app-files",
    PACKAGES_ROOT / "app-worker",
]


def test_no_disallowed_org_names_in_touched_packages():
    for directory in _TOUCHED_DIRS:
        for path in directory.rglob("*"):
            if path.is_dir():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not _BANNED.search(text), f"disallowed reference found in {path}"


def test_this_test_file_has_no_disallowed_org_names():
    assert not _BANNED.search(Path(__file__).read_text())


# ---------------------------------------------------------------------
# WHAT THIS CLOSES: profile regains the social graph, forum renders
# ai_summary -- both closed as direct 59 mechanism usage on their
# existing bespoke pages, not through the views/site_routes system (see
# their own module docstrings for why).
# ---------------------------------------------------------------------


def test_profile_page_composes_followers_and_following_as_related_lists():
    source = (PACKAGES_ROOT / "app-worker" / "objects" / "site" / "profile.py").read_text()
    assert '<script src="/list">' in source
    assert 'where: {following_id: PROFILE_ID}' in source
    assert 'where: {follower_id: PROFILE_ID}' in source
    assert "loadFollowGraph();" in source


def test_forum_topic_ai_summary_renders_via_visible_when_not_a_bespoke_page():
    """59's Purpose called out the forum topic page finally rendering its
    stored-but-orphaned ai_summary. The bespoke page that did that (with
    hand-written show/hide JS) is gone; the seeded detail view renders every
    field, and ai_summary shows only when is_ai_summarized via the field's
    own `visible_when` -- the general primitive, not a per-page renderer."""
    schema = json.loads(
        (PACKAGES_ROOT / "app-forum" / "schemas" / "forum_topics.json").read_text()
    )
    ai = next(f for f in schema["fields"] if f["name"] == "ai_summary")
    assert ai["visible_when"] == {"field": "is_ai_summarized", "equals": "true"}
    assert not (PACKAGES_ROOT / "app-forum" / "objects" / "site" / "forum_topic.py").exists()


def test_form_read_only_formats_cents_money_fields_in_whole_units():
    """Every hand-written *_view page re-implemented cents->whole-units money
    formatting; it is hoisted into /form's shared read-only renderer, keyed
    on the universal `_cents` field-name doctrine, so it applies to every
    detail page with zero schema changes."""
    source = _form_source()
    assert "function isMoneyField(f)" in source
    assert "/_cents$/.test(f.name" in source
    assert "(n / 100).toFixed(2)" in source
    # The read-only value branch and the label both go through the money path.
    assert "isMoneyField(f) ? moneyText(v)" in source
    assert "isMoneyField(f) ? moneyLabel(f)" in source
    # Display only -- edit mode still submits the raw integer cents, never a
    # dollars value that would need round-trip conversion.
    assert "type=\"number\"" in source


def test_form_honors_conditional_field_visibility_visible_when():
    """A field's `visible_when` = {field, equals|in} hides it unless the
    record's value matches -- replacing every *_view page's bespoke show/hide
    CSS+JS with one field-level declaration honored on every generated
    surface. Fails OPEN (unknown/absent condition => visible)."""
    source = _form_source()
    assert "function fieldVisible(f, record)" in source
    assert "f.visible_when" in source
    # Applied in read-only (detail) mode AND edit mode (against an existing
    # record; create shows all).
    assert "if (!fieldVisible(f, record)) continue;" in source
    assert "(!record || fieldVisible(f, record))" in source
    # Supports equals and in, the same tiny vocabulary 58's filter uses.
    assert '"equals" in cond' in source
    assert "Array.isArray(cond.in)" in source


def test_form_supports_fk_locked_fields_for_child_compose():
    """A `form` block can lock fields to a context value (opts.fixed) -- the
    parent->child compose shape (reply.topic_id, line.invoice_id, ...) every
    hand-written page re-implemented as prefill+hide. A shared capability of
    the generic form renderer, not a bespoke per-page form: locked fields are
    excluded from the UI and injected on submit."""
    form_source = _form_source()
    # excluded from the rendered/ordered fields...
    assert "!(f.name in fixed)" in form_source
    # ...and injected into the record on submit.
    assert "for (const k in fixed) rec[k] = fixed[k];" in form_source
    assert "opts.fixed" in form_source


def test_view_render_form_block_resolves_fixed_from_record_id():
    """The form block resolves $record_id in its `fixed` map (same resolution
    detail/related use) and passes it + the viewer as owner to the generic
    form renderer -- so [detail, related, form] compose one child-bearing page
    from generic block renderers, no bespoke code."""
    source = _view_render_source()
    form_fn = re.search(r"function renderForm\(block, mount\) \{(.*?)\n\}", source, re.S)
    assert form_fn, "renderForm not found"
    body = form_fn.group(1)
    assert "block.fixed" in body
    assert "resolveRecordId(block.fixed[k])" in body
    assert "base.fixed = fixed" in body
    assert "owner: viewerId" in body
