"""Tests for plan/vocabulary/60-list-modes-spec.md: board/tree/calendar
list-modes on the shared list generator (window.dbbasicList, list.py).

Mirrors tests/test_detail_related_spec.py's testing style for the same
generator family: source-assertion + structural-install checks for the
JS-side pieces, plus real HTTP/library-level tests for the parts that
actually execute in Python (the permission-gated fetch every mode reuses
from 58, and the transition-guarded write board's drag reuses from the
platform's existing `_validate_field_transitions`). Where node is on PATH,
the PURE bucketing/nesting helpers (defaultGroupField, groupByColumn,
buildTree, bucketByDate, ...) are also run behaviorally, the same pattern
tests/test_app_talk_object.py uses for its own pure JS helpers -- these
functions take plain data in and return plain data out, no DOM, so they
run standalone under node with no shim beyond the extracted source.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_object_server import auth_headers, enable_admin_token, request, write_records

import object_records
import object_schemas
import object_server

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_THEME_DIR = PACKAGES_ROOT / "app-theme"
LIST_PATH = APP_THEME_DIR / "objects" / "site" / "list.py"
STYLE_PATH = APP_THEME_DIR / "objects" / "site" / "style.py"


def _list_source():
    return LIST_PATH.read_text()


def _style_source():
    return STYLE_PATH.read_text()


def _list_js():
    """The `_JS` string literal body only -- source assertions below run
    against the whole file (docstring included), but the node probes need
    just the script so a shim doesn't have to fake out Python syntax."""
    text = _list_source()
    before, _, rest = text.partition('_JS = r"""\n')
    assert rest, "_JS block not found in list.py"
    js, _, _after = rest.partition('\n"""\n')
    return js


def _pure_helpers_js():
    """The self-contained block of 60's pure functions (whereQueryString
    through bucketByDate) -- no closure over `mount`/`cfg`/`document`, so
    this excerpt runs standalone under node with zero shimming, the same
    extract-and-probe approach test_app_talk_object.py uses."""
    js = _list_js()
    start = js.index("  function whereQueryString")
    end = js.index("  // ---- feature flag + schema-driven mode resolution")
    return js[start:end]


def _run_node_probe(js_prelude, expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    probe = js_prelude + "\nconsole.log(JSON.stringify(" + expression + "));\n"
    result = subprocess.run([node, "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, f"node probe failed:\n{result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------
# Structural: list.py is still the one public static script (unchanged
# object identity/permissions -- 60 extends the existing generator, it
# does not add a new object, route, or execute rule).
# ---------------------------------------------------------------------


def test_site_list_is_still_the_one_public_static_script():
    manifest = json.loads((APP_THEME_DIR / "dbbasic-package.json").read_text())
    object_ids = {obj["id"] for obj in manifest["objects"]}
    assert "site_list" in object_ids

    rules = json.loads((APP_THEME_DIR / "permissions" / "rules.json").read_text())["rules"]
    list_rules = [r for r in rules if r.get("object_id") == "site_list"]
    assert len(list_rules) == 1
    assert list_rules[0]["effect"] == "allow"
    assert list_rules[0]["principal"] == "public"
    assert list_rules[0]["actions"] == ["execute"]


def test_list_generator_still_serves_window_dbbasicList():
    import object_execution
    import python_object_runtime

    runtime = python_object_runtime.PythonObjectRuntime()
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("site_list", payload={}),
        roots=[APP_THEME_DIR / "objects"],
    )
    assert result.ok is True
    assert result.result["content_type"] == "application/javascript; charset=utf-8"
    assert "window.dbbasicList = function" in result.result["body"]


def test_list_js_is_syntactically_valid_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    js = _list_js()
    result = subprocess.run([node, "--check", "-"], input=js, capture_output=True, text=True)
    assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


# ---------------------------------------------------------------------
# 58 dependency: all three modes fetch through the SAME query builder and
# endpoint shape the plain row list already uses -- no bespoke fetch, no
# new operator beyond 58's own eq/gte/lte.
# ---------------------------------------------------------------------


def test_all_three_modes_reuse_the_58_where_query_builder_and_endpoint():
    source = _list_source()
    assert 'function whereQueryString(where, extra)' in source
    assert 'encodeURIComponent(k) + "=" + encodeURIComponent(v)' in source
    # Every fetch (row list, board, tree, calendar) hits the identical
    # endpoint shape -- four call sites, one query-string builder.
    assert source.count('"/collections/" + collection + "/records?limit=500"') == 4
    # Calendar layers 58's own gte/lte range operators on top -- no new
    # operator invented for this spec.
    assert '".gte"' in source and '".lte"' in source


def test_board_group_field_derivation_chain_documented_and_present():
    source = _list_source()
    assert "function defaultGroupField(schema)" in source
    assert "schema.flow && schema.flow.field" in source
    assert "f.transitions && Object.keys(f.transitions).length > 0" in source


# ---------------------------------------------------------------------
# 10-flow dependency: board's drag issues the ordinary PUT that already
# runs _validate_field_transitions -- documented AND proven live below.
# ---------------------------------------------------------------------


def test_board_drag_issues_the_ordinary_put_no_new_write_path():
    source = _list_source()
    assert "_validate_field_transitions" in source
    assert 'method: "PUT"' in source
    assert '"/collections/" + collection + "/records/" + encodeURIComponent(id)' in source
    # Nothing applied client-side before the response -- a rejected move
    # reverts by re-drawing from the unmodified `all`, not an explicit undo.
    assert "draw();" in source


def test_board_is_one_mode_for_kanban_and_pipeline_not_two():
    source = _list_source()
    assert "ONE mode, not two" in source
    assert "contacts.lead_status" in source
    # The honest gap this spec is explicit about NOT closing here.
    assert "Stage-6" in source


def _write_transition_schema(data_dir, collection="board_items"):
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fields": [
            {"name": "id"},
            {"name": "title", "type": "text"},
            {
                "name": "status",
                "type": "enum",
                "enum": ["open", "assigned", "done", "cancelled"],
                "transitions": {
                    "open": ["assigned", "cancelled"],
                    "assigned": ["done", {"to": "open", "when": {"owner_id": "$user_id"}}],
                },
            },
        ],
    }))
    return collection


def test_board_drag_shaped_put_is_rejected_for_an_illegal_column_move(tmp_path, monkeypatch):
    """Proves 60's headline board claim at the HTTP layer, not just in
    object_records.py's own unit tests: the exact PUT a board drag issues
    (`PUT /collections/{c}/records/{id}` with the group field's new value)
    is validated by the platform's existing, flow-independent
    `_validate_field_transitions` -- an illegal `to` (not in the source
    value's transitions list) comes back non-2xx, so the client-side
    revert-by-redraw in list.py has something real to react to."""
    data_dir = tmp_path / "data"
    collection = _write_transition_schema(data_dir)
    write_records(data_dir, collection, "id\ttitle\tstatus\nb1\tShip it\topen\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    # "open" has no legal move straight to "done" -- exactly the shape of
    # dragging a card two columns over that the source column's
    # transitions map does not allow.
    illegal_status, _, illegal_payload = request(
        f"/collections/{collection}/records/b1",
        method="PUT",
        body=json.dumps({"status": "done"}).encode(),
        headers=auth_headers(),
    )
    assert illegal_status in (400, 403)
    assert illegal_payload["status"] == "error"

    # The record was never mutated by the rejected drag.
    unchanged = object_records.get_collection_record(collection, "b1", base_dir=data_dir)
    assert unchanged["status"] == "open"


def test_board_drag_shaped_put_succeeds_for_a_legal_column_move(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    collection = _write_transition_schema(data_dir)
    write_records(data_dir, collection, "id\ttitle\tstatus\nb1\tShip it\topen\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    status, _, payload = request(
        f"/collections/{collection}/records/b1",
        method="PUT",
        body=json.dumps({"status": "assigned"}).encode(),
        headers=auth_headers(),
    )
    assert status == 200
    assert payload["record"]["status"] == "assigned"


def test_board_drag_out_of_a_guarded_column_honors_the_when_clause(tmp_path, monkeypatch):
    """The 'assigned' -> 'open' move is guarded (owner_id must match the
    caller) -- flow's own gates/flow_configs are Stage-6+ wiring this spec
    doesn't build, but the underlying guard clause on the field's own
    transitions map (10-flow's substrate, already shipped) is exactly what
    a board drag rides, with or without flow installed on top."""
    data_dir = tmp_path / "data"
    path = data_dir / "schemas" / "board_items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fields": [
            {"name": "id"}, {"name": "owner_id"},
            {
                "name": "status", "type": "enum", "enum": ["open", "assigned"],
                "transitions": {"assigned": [{"to": "open", "when": {"owner_id": "$user_id"}}]},
            },
        ],
    }))
    write_records(data_dir, "board_items", "id\towner_id\tstatus\nb1\towner-a\tassigned\n")
    import object_permission_store
    object_permission_store.replace_policy(
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "registered",
                    "actions": ["read", "update"],
                    "collection": "board_items",
                    "reason": "any signed-in user may drag any card -- the field's own "
                              "'when' guard is what's under test here, not row scoping",
                }
            ],
        },
        data_dir,
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)

    other_status, _, other_payload = request(
        "/collections/board_items/records/b1",
        method="PUT",
        body=json.dumps({"status": "open"}).encode(),
        headers=[("x-dbbasic-user-id", "owner-b"), ("x-dbbasic-roles", "registered")],
    )
    owner_status, _, owner_payload = request(
        "/collections/board_items/records/b1",
        method="PUT",
        body=json.dumps({"status": "open"}).encode(),
        headers=[("x-dbbasic-user-id", "owner-a"), ("x-dbbasic-roles", "registered")],
    )

    assert other_status == 403
    assert owner_status == 200
    assert owner_payload["record"]["status"] == "open"


# ---------------------------------------------------------------------
# Board pure helpers, run behaviorally under node.
# ---------------------------------------------------------------------


def test_default_group_field_chain_behaves_under_node():
    helpers = _pure_helpers_js()
    probe = helpers + "\nconsole.log(JSON.stringify({" + """
      flowWins: defaultGroupField({
        flow: {field: "stage"},
        fields: [{name: "stage", type: "enum", enum: ["a", "b"]},
                 {name: "other", type: "enum", enum: ["x"], transitions: {x: ["y"]}}],
      }),
      guardedEnumWins: defaultGroupField({
        fields: [{name: "kind", type: "enum", enum: ["a"]},
                 {name: "status", type: "enum", enum: ["open", "closed"], transitions: {open: ["closed"]}}],
      }),
      bareEnumFallback: defaultGroupField({
        fields: [{name: "lead_status", type: "enum", enum: ["new", "hot"]}],
      }),
      none: defaultGroupField({fields: [{name: "title", type: "text"}]}),
    }));
    """
    result = subprocess.run(
        [shutil.which("node") or "node", "-e", probe], capture_output=True, text=True
    )
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    assert result.returncode == 0, f"node probe failed:\n{result.stderr}"
    out = json.loads(result.stdout)
    assert out["flowWins"] == "stage"
    assert out["guardedEnumWins"] == "status"
    assert out["bareEnumFallback"] == "lead_status"
    assert out["none"] is None


def test_board_columns_and_grouping_behave_under_node():
    helpers = _pure_helpers_js()
    expr = """
    (function () {
      const field = {enum: ["draft", "open", "assigned", "cancelled"]};
      const columns = boardColumns(field);
      const records = [
        {id: "r1", status: "open"},
        {id: "r2", status: "assigned"},
        {id: "r3"},
        {id: "r4", status: "not_a_real_value"},
        {id: "r5", status: "open"},
      ];
      const buckets = groupByColumn(records, "status", columns);
      return {
        columns: columns,
        unsetIds: buckets[""].map((r) => r.id).sort(),
        openIds: buckets["open"].map((r) => r.id).sort(),
        assignedIds: buckets["assigned"].map((r) => r.id).sort(),
        totalBucketed: Object.values(buckets).reduce((n, b) => n + b.length, 0),
      };
    })()
    """
    out = _run_node_probe(helpers, expr)
    assert out["columns"] == ["", "draft", "open", "assigned", "cancelled"]
    assert out["columns"][0] == ""  # (unset) column is first
    # r3 (no value) and r4 (stray/unknown value) both fold into unset --
    # never dropped.
    assert out["unsetIds"] == ["r3", "r4"]
    assert out["openIds"] == ["r1", "r5"]
    assert out["assignedIds"] == ["r2"]
    assert out["totalBucketed"] == 5  # every record accounted for


def test_resolve_board_config_error_when_no_enum_field_exists():
    helpers = _pure_helpers_js()
    expr = 'resolveBoardConfig({name: "things", fields: [{name: "title", type: "text"}]})'
    out = _run_node_probe(helpers, expr)
    assert "error" in out
    assert "enum" in out["error"]


# ---------------------------------------------------------------------
# Tree pure helpers: nesting order, cycle guard, depth cap.
# ---------------------------------------------------------------------


def test_is_self_relation_and_resolve_tree_config_behave_under_node():
    helpers = _pure_helpers_js()
    expr = """
    (function () {
      const selfSchema = {
        name: "fin_accounts",
        fields: [{name: "parent_id", type: "relation", relation: {collection: "fin_accounts"}}],
      };
      const crossSchema = {
        name: "contacts",
        fields: [{name: "organization_id", type: "relation", relation: {collection: "organizations"}}],
      };
      return {
        selfOk: resolveTreeConfig(selfSchema),
        crossError: resolveTreeConfig({
          name: "contacts",
          fields: [{name: "parent_id", type: "relation", relation: {collection: "organizations"}}],
        }),
        noFieldError: resolveTreeConfig({name: "x", fields: []}),
      };
    })()
    """
    out = _run_node_probe(helpers, expr)
    assert out["selfOk"]["config"] == {"parentField": "parent_id", "maxDepth": 10}
    assert "error" in out["crossError"]
    assert "error" in out["noFieldError"]


def test_build_tree_nests_in_parent_order_under_node():
    helpers = _pure_helpers_js()
    expr = """
    buildTree([
      {id: "root1", parent_id: ""},
      {id: "child1", parent_id: "root1"},
      {id: "child2", parent_id: "root1"},
      {id: "grandchild1", parent_id: "child1"},
    ], {parentField: "parent_id", maxDepth: 10})
    """
    out = _run_node_probe(helpers, expr)
    assert out["cycleDetected"] is False
    assert len(out["nodes"]) == 1
    root = out["nodes"][0]
    assert root["record"]["id"] == "root1"
    assert root["depth"] == 0
    child_ids = sorted(n["record"]["id"] for n in root["children"])
    assert child_ids == ["child1", "child2"]
    child1 = next(n for n in root["children"] if n["record"]["id"] == "child1")
    assert child1["depth"] == 1
    assert [n["record"]["id"] for n in child1["children"]] == ["grandchild1"]


def test_build_tree_cycle_guard_renders_once_and_never_hangs_under_node():
    """A malformed parent_id chain: 'root' -> 'mid' -> a SECOND row that
    reuses id 'root' again as mid's child (duplicate id, the kind of
    data-quality bug neither fin_accounts nor locations' own schema
    enforces against). Without the visited-id guard this would recurse
    forever; with it, the second 'root' row is rendered once at first
    encounter and never re-descended -- proven here by the probe actually
    returning (a real infinite loop would hang/crash the node process)."""
    helpers = _pure_helpers_js()
    expr = """
    buildTree([
      {id: "root", parent_id: ""},
      {id: "mid", parent_id: "root"},
      {id: "root", parent_id: "mid"},
    ], {parentField: "parent_id", maxDepth: 10})
    """
    out = _run_node_probe(helpers, expr)
    assert out["cycleDetected"] is True
    assert len(out["nodes"]) == 1
    mid = out["nodes"][0]["children"][0]
    assert mid["record"]["id"] == "mid"
    # The re-encountered "root" is skipped, not appended as a child of mid.
    assert mid["children"] == []


def test_build_tree_depth_cap_stops_descent_and_flags_truncation_under_node():
    # A straight-line chain 12 deep, capped at max_depth=3.
    ids = [f"n{i}" for i in range(12)]
    chain = [{"id": ids[0], "parent_id": ""}]
    for i in range(1, len(ids)):
        chain.append({"id": ids[i], "parent_id": ids[i - 1]})
    helpers = _pure_helpers_js()
    expr = "buildTree(" + json.dumps(chain) + ", {parentField: 'parent_id', maxDepth: 3})"
    out = _run_node_probe(helpers, expr)
    assert out["cycleDetected"] is False

    depths = []
    node = out["nodes"][0]
    while True:
        depths.append(node["depth"])
        if not node["children"]:
            assert node["truncated"] is True
            break
        node = node["children"][0]
    assert depths == [0, 1, 2]  # exactly max_depth levels rendered, no more


# ---------------------------------------------------------------------
# Calendar pure helpers: month bucketing + undated overflow.
# ---------------------------------------------------------------------


def test_default_date_field_skips_read_only_under_node():
    helpers = _pure_helpers_js()
    expr = """
    defaultDateField({
      fields: [
        {name: "created_at", type: "datetime", read_only: true},
        {name: "due_date", type: "date"},
      ],
    })
    """
    out = _run_node_probe(helpers, expr)
    assert out == "due_date"


def test_bucket_by_date_keys_by_day_and_never_drops_undated_under_node():
    helpers = _pure_helpers_js()
    expr = """
    (function () {
      const records = [
        {id: "e1", due_date: "2026-07-04"},
        {id: "e2", due_date: "2026-07-04T09:00:00"},
        {id: "e3", due_date: "2026-07-05"},
        {id: "e4", due_date: ""},
        {id: "e5"},
      ];
      const bucketed = bucketByDate(records, "due_date");
      return {
        july4Ids: bucketed.byDay["2026-07-04"].map((r) => r.id).sort(),
        july5Ids: bucketed.byDay["2026-07-05"].map((r) => r.id).sort(),
        undatedIds: bucketed.undated.map((r) => r.id).sort(),
        totalDated: Object.values(bucketed.byDay).reduce((n, b) => n + b.length, 0),
      };
    })()
    """
    out = _run_node_probe(helpers, expr)
    assert out["july4Ids"] == ["e1", "e2"]
    assert out["july5Ids"] == ["e3"]
    assert out["undatedIds"] == ["e4", "e5"]  # empty string AND missing key both count as undated
    assert out["totalDated"] == 3


def test_month_grid_has_42_cells_with_correct_in_month_flags_under_node():
    helpers = _pure_helpers_js()
    # July 2026: 31 days, starts on a Wednesday.
    out = _run_node_probe(helpers, "monthGridDays(2026, 6)")
    assert len(out) == 42
    in_month_dates = [c["date"] for c in out if c["inMonth"]]
    assert in_month_dates[0] == "2026-07-01"
    assert in_month_dates[-1] == "2026-07-31"
    assert len(in_month_dates) == 31
    assert any(not c["inMonth"] for c in out)  # leading/trailing filler present


def test_resolve_calendar_config_error_when_no_date_field_exists():
    helpers = _pure_helpers_js()
    expr = 'resolveCalendarConfig({fields: [{name: "title", type: "text"}]})'
    out = _run_node_probe(helpers, expr)
    assert "error" in out


# ---------------------------------------------------------------------
# 58 permissions posture inherited verbatim: the calendar range fetch
# (58's gte/lte, newly exercised in this spec's own context) still
# applies the permission row filter FIRST -- proven live, not just
# asserted from the JS source, mirroring 59's own
# test_related_query_shape_cannot_leak_a_row_outside_the_row_filter.
# ---------------------------------------------------------------------


def test_calendar_shaped_range_fetch_cannot_leak_a_row_outside_the_row_filter(tmp_path, monkeypatch):
    import object_permission_store

    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "bookings",
        "id\tstart_date\towner_id\n"
        "bk1\t2026-07-10\towner-a\n"
        "bk2\t2026-07-12\towner-b\n",
    )
    object_permission_store.replace_policy(
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "registered",
                    "actions": ["create", "read", "update", "delete"],
                    "collection": "bookings",
                    "row_filter": {"owner_id": "$user_id"},
                }
            ],
        },
        data_dir,
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)

    # The exact query shape renderCalendar's load() compiles to for a
    # visible July 2026 -- 58's gte/lte range operators on the date field.
    owner_status, _, owner_payload = request(
        "/collections/bookings/records",
        query_string="start_date.gte=2026-07-01&start_date.lte=2026-07-31",
        headers=[("x-dbbasic-user-id", "owner-a"), ("x-dbbasic-roles", "registered")],
    )
    other_status, _, other_payload = request(
        "/collections/bookings/records",
        query_string="start_date.gte=2026-07-01&start_date.lte=2026-07-31",
        headers=[("x-dbbasic-user-id", "owner-c"), ("x-dbbasic-roles", "registered")],
    )
    admin_status, _, admin_payload = request(
        "/collections/bookings/records",
        query_string="start_date.gte=2026-07-01&start_date.lte=2026-07-31",
        headers=auth_headers(),
    )

    assert owner_status == 200
    assert [r["id"] for r in owner_payload["records"]] == ["bk1"]

    assert other_status == 200
    assert other_payload["records"] == []
    assert "bk1" not in json.dumps(other_payload)
    assert "bk2" not in json.dumps(other_payload)

    assert admin_status == 200
    assert {r["id"] for r in admin_payload["records"]} == {"bk1", "bk2"}


# ---------------------------------------------------------------------
# Feature flag: list_modes_enabled defaults ON, off falls back to table.
# ---------------------------------------------------------------------


def test_list_modes_enabled_flag_defaults_on_and_gates_the_three_modes():
    source = _list_source()
    assert "async function listModesEnabled()" in source
    assert 'v !== "off" && v !== "false"' in source
    assert "dbbasicFlags" in source


def test_degradation_notices_are_visible_not_silent():
    source = _list_source()
    assert "board mode needs an enum field" in source
    assert "tree mode needs a self-relation field" in source
    assert "calendar mode needs a date field" in source
    # The row-list render() actually shows the notice -- not just a string
    # that's built and discarded.
    assert '<div class="state notice">' in source


def test_mode_resolution_falls_back_to_the_row_list_on_any_non_match():
    source = _list_source()
    assert "activeReload = startRowList(resolved.notice);" in source


# ---------------------------------------------------------------------
# Schema surfacing: `flow` now survives normalization (whitelisted,
# additive, board's default-group-field chain has something real to read
# the moment a schema declares it) and is exposed on the public schema
# endpoint list.py's resolveListMode() fetches.
# ---------------------------------------------------------------------


def test_flow_key_survives_schema_normalization(tmp_path):
    data_dir = tmp_path / "data"
    path = data_dir / "schemas" / "widgets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fields": [{"name": "id"}, {"name": "stage", "type": "enum", "enum": ["a", "b"]}],
        "flow": {"field": "stage"},
    }))
    schema = object_schemas.get_schema("widgets", base_dir=data_dir, roots=[])
    assert schema["flow"] == {"field": "stage"}


def test_public_schema_endpoint_exposes_flow(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "widgets", "id\tstage\nw1\ta\n")
    path = data_dir / "schemas" / "widgets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fields": [{"name": "id"}, {"name": "stage", "type": "enum", "enum": ["a", "b"]}],
        "flow": {"field": "stage"},
    }))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    status, _, payload = request("/api/schema/widgets")
    assert status == 200
    assert payload["schema"]["flow"] == {"field": "stage"}


# ---------------------------------------------------------------------
# CSS: board/tree/calendar chrome lives in /style, per convention (same
# global stylesheet every other generated surface's classes live in).
# ---------------------------------------------------------------------


def test_board_tree_calendar_css_classes_are_in_the_shared_stylesheet():
    style_source = _style_source()
    for cls in (".board", ".boardcol", ".boardcard", ".tree", ".treenode",
                ".calgrid", ".calcell", ".calevent"):
        assert cls in style_source


# ---------------------------------------------------------------------
# Out of scope, stated in-line (v1 tree/calendar are read-only; no
# persisted board rank) -- regression guard against silently growing scope.
# ---------------------------------------------------------------------


def test_tree_and_calendar_issue_no_writes_in_v1():
    js = _list_js()
    tree_fn = re.search(r"function renderTree\(collection, cfg, mount, treeCfg\) \{(.*?)\n  \}\n\n  function renderCalendar", js, re.S)
    assert tree_fn, "renderTree not found"
    assert "method:" not in tree_fn.group(1)

    cal_fn = re.search(r"function renderCalendar\(collection, cfg, mount, calCfg\) \{(.*?)\n  \}\n\n  // ---- the plain row list", js, re.S)
    assert cal_fn, "renderCalendar not found"
    assert "method:" not in cal_fn.group(1)


def test_no_persisted_board_rank_field_invented():
    source = _list_source()
    assert "board_rank" not in source
    assert "Persisted card order within a board column" not in source  # that's the spec's own text, not this file's


# ---------------------------------------------------------------------
# Scroll-parity open question carried forward verbatim, per the task's
# explicit instruction not to silently resolve it.
# ---------------------------------------------------------------------


def test_scroll_parity_open_question_is_carried_forward_in_the_docstring():
    source = _list_source()
    assert "Open question carried forward verbatim" in source
    assert "31-wizard-kanban-stub.md" in source
    assert "mid-scroll" in source


# ---------------------------------------------------------------------
# Repo hygiene: no internal org/codename references in anything touched.
# ---------------------------------------------------------------------

_BANNED = re.compile(
    "|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots", r"\b" + "wo" + "ld" + r"\b"]),
    re.IGNORECASE,
)
_TOUCHED_FILES = [
    LIST_PATH,
    STYLE_PATH,
    Path(__file__).resolve().parents[1] / "object_server.py",
    Path(__file__).resolve().parents[1] / "object_schemas.py",
]


def test_no_disallowed_org_names_in_touched_files():
    for path in _TOUCHED_FILES:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not _BANNED.search(text), f"disallowed reference found in {path}"


def test_this_test_file_has_no_disallowed_org_names():
    assert not _BANNED.search(Path(__file__).read_text())
