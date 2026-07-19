"""Tests for object_import.py (plan/vocabulary/13-import-export-spec.md, CLI slice).

Fixtures use synthetic collection/schema names ("legacy_accounts",
"legacy_widgets") standing in for an arbitrary spreadsheet import -- never
real data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import object_collections
import object_import
import object_record_changes
import object_records


# --------------------------------------------------------------------------
# Fixture helpers
# --------------------------------------------------------------------------

def write_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": collection, "fields": fields}))
    return path


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


ACCOUNTS_FIELDS = [
    {"name": "id"},
    {
        "name": "name",
        "type": "text",
        "required": True,
        "validation": {"min_length": 1, "max_length": 120},
    },
    {"name": "tier", "type": "enum", "enum": ["bronze", "silver", "gold"], "default": "bronze"},
    {"name": "created_at", "type": "datetime", "read_only": True},
]

WIDGETS_FIELDS = [
    {"name": "id"},
    {"name": "title", "type": "text", "required": True},
    {"name": "account_id", "relation": {"collection": "legacy_accounts", "display_field": "name"}},
    {"name": "priority", "type": "enum", "enum": ["low", "normal", "high"], "default": "normal"},
    {"name": "source_system", "type": "text"},
    {"name": "created_at", "type": "datetime", "read_only": True},
]


def setup_schemas(data_dir: Path) -> None:
    write_schema(data_dir, "legacy_accounts", ACCOUNTS_FIELDS)
    write_schema(data_dir, "legacy_widgets", WIDGETS_FIELDS)


def base_widgets_config(input_name: str = "widgets.json") -> dict:
    return {
        "input": input_name,
        "mapping": {
            "WidgetId": "id",
            "Title": "title",
            "AccountId": "account_id",
            "Priority": "priority",
            "InternalCode": "extra",
        },
        "extra_fields": ["LegacyNote"],
        "value_maps": [{"field": "priority", "map": {"med": "normal"}}],
        "constants": {"source_system": "legacy_crm"},
    }


def base_accounts_config(input_name: str = "accounts.json") -> dict:
    return {
        "input": input_name,
        "mapping": {"AccountId": "id", "AccountName": "name", "Tier": "tier"},
    }


def write_plan(tmp_path: Path, *, accounts_cfg: dict, widgets_cfg: dict | None = None) -> Path:
    collections = {"legacy_accounts": accounts_cfg}
    order = ["legacy_accounts"]
    if widgets_cfg is not None:
        collections["legacy_widgets"] = widgets_cfg
        order.append("legacy_widgets")
    return write_json(tmp_path / "plan.json", {"order": order, "collections": collections})


# --------------------------------------------------------------------------
# map_row
# --------------------------------------------------------------------------

def test_map_row_applies_rename_value_map_extra_and_constants():
    cfg = object_import._normalize_collection_config(
        "legacy_widgets", base_widgets_config(), base=Path(".")
    )
    source_row = {
        "WidgetId": "w_1",
        "Title": "Gadget",
        "AccountId": "acc_1",
        "Priority": "med",
        "InternalCode": "X1",
        "LegacyNote": "first",
    }

    dest, errors = object_import.map_row(source_row, cfg)

    assert errors == []
    assert dest["id"] == "w_1"
    assert dest["title"] == "Gadget"
    assert dest["account_id"] == "acc_1"
    assert dest["priority"] == "normal"  # value_map: med -> normal
    assert dest["source_system"] == "legacy_crm"  # constant stamp
    assert dest["extra"] == {"InternalCode": "X1", "LegacyNote": "first"}


def test_map_row_reports_unmapped_column_as_error():
    cfg = object_import._normalize_collection_config(
        "legacy_widgets",
        {"input": "widgets.json", "mapping": {"WidgetId": "id"}},
        base=Path("."),
    )

    dest, errors = object_import.map_row({"WidgetId": "w_1", "Mystery": "?"}, cfg)

    assert any("Mystery" in err for err in errors)


def test_map_row_reports_missing_id():
    cfg = object_import._normalize_collection_config(
        "legacy_widgets", {"input": "widgets.json", "mapping": {"Title": "title"}}, base=Path(".")
    )

    dest, errors = object_import.map_row({"Title": "Gadget"}, cfg)

    assert any("id" in err for err in errors)


# --------------------------------------------------------------------------
# load_plan
# --------------------------------------------------------------------------

def test_load_plan_resolves_relative_input_paths_and_preserves_order(tmp_path):
    plan_path = write_plan(
        tmp_path, accounts_cfg=base_accounts_config(), widgets_cfg=base_widgets_config()
    )

    plan = object_import.load_plan(plan_path)

    assert plan["order"] == ["legacy_accounts", "legacy_widgets"]
    assert plan["collections"]["legacy_accounts"]["input"] == tmp_path / "accounts.json"
    assert plan["collections"]["legacy_widgets"]["input"] == tmp_path / "widgets.json"


def test_load_plan_rejects_order_naming_unconfigured_collection(tmp_path):
    write_json(
        tmp_path / "plan.json",
        {"order": ["legacy_accounts", "ghost"], "collections": {"legacy_accounts": base_accounts_config()}},
    )

    with pytest.raises(object_import.ImportConfigError):
        object_import.load_plan(tmp_path / "plan.json")


# --------------------------------------------------------------------------
# Dry run: report correctness, dependency order, relation-within-run
# --------------------------------------------------------------------------

def test_dry_run_reports_ok_enum_violation_and_missing_relation(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)

    write_json(
        tmp_path / "accounts.json",
        [
            {"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold"},
            {"AccountId": "acc_2", "AccountName": "Globex", "Tier": "silver"},
        ],
    )
    write_json(
        tmp_path / "widgets.json",
        [
            {"WidgetId": "w_1", "Title": "Gadget One", "AccountId": "acc_1",
             "Priority": "med", "InternalCode": "X1", "LegacyNote": "n1"},
            {"WidgetId": "w_2", "Title": "Gadget Two", "AccountId": "acc_2",
             "Priority": "high", "InternalCode": "X2", "LegacyNote": "n2"},
            {"WidgetId": "w_bad_enum", "Title": "Bad Priority", "AccountId": "acc_1",
             "Priority": "urgent", "InternalCode": "X3", "LegacyNote": "n3"},
            {"WidgetId": "w_missing_rel", "Title": "Orphan", "AccountId": "acc_missing",
             "Priority": "low", "InternalCode": "X4", "LegacyNote": "n4"},
        ],
    )
    plan_path = write_plan(
        tmp_path, accounts_cfg=base_accounts_config(), widgets_cfg=base_widgets_config()
    )
    plan = object_import.load_plan(plan_path)

    reports = object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=True)

    accounts_report, widgets_report = reports
    assert accounts_report.ok == 2
    assert accounts_report.errors == 0

    assert widgets_report.ok == 2
    assert widgets_report.errors == 2
    error_ids = {row.id for row in widgets_report.rows if row.status == "error"}
    assert error_ids == {"w_bad_enum", "w_missing_rel"}
    bad_enum_row = next(row for row in widgets_report.rows if row.id == "w_bad_enum")
    assert any("priority" in reason for reason in bad_enum_row.reasons)
    missing_rel_row = next(row for row in widgets_report.rows if row.id == "w_missing_rel")
    assert any("missing record" in reason for reason in missing_rel_row.reasons)

    # Dry run writes nothing.
    assert object_records.read_collection_records("legacy_accounts", base_dir=data_dir, roots=[]) == []


def test_dry_run_detects_duplicate_id_within_file(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [
            {"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold"},
            {"AccountId": "acc_1", "AccountName": "Duplicate", "Tier": "silver"},
        ],
    )
    plan = object_import.load_plan(write_plan(tmp_path, accounts_cfg=base_accounts_config()))

    reports = object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=True)

    (report,) = reports
    assert report.ok == 1
    assert report.errors == 1
    dup_row = report.rows[1]
    assert dup_row.status == "error"
    assert any("duplicate id" in reason for reason in dup_row.reasons)


# --------------------------------------------------------------------------
# Real run: id preservation, created_at preservation, actor attribution
# --------------------------------------------------------------------------

def test_real_run_creates_records_preserving_ids_and_attributing_actor(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold"}],
    )
    write_json(
        tmp_path / "widgets.json",
        [{"WidgetId": "w_1", "Title": "Gadget One", "AccountId": "acc_1",
          "Priority": "high", "InternalCode": "X1", "LegacyNote": "n1"}],
    )
    plan = object_import.load_plan(
        write_plan(tmp_path, accounts_cfg=base_accounts_config(), widgets_cfg=base_widgets_config())
    )

    reports = object_import.run_import(
        plan, base_dir=data_dir, roots=[], dry_run=False, actor="import-cli-test"
    )

    accounts_report, widgets_report = reports
    assert accounts_report.created == 1
    assert widgets_report.created == 1

    account = object_records.get_collection_record("legacy_accounts", "acc_1", base_dir=data_dir, roots=[])
    assert account["id"] == "acc_1"  # source id preserved exactly
    assert account["name"] == "Acme Corp"

    widget = object_records.get_collection_record("legacy_widgets", "w_1", base_dir=data_dir, roots=[])
    assert widget["account_id"] == "acc_1"
    assert widget["priority"] == "high"
    assert widget["source_system"] == "legacy_crm"
    extra = json.loads(widget["extra"])
    assert extra == {"InternalCode": "X1", "LegacyNote": "n1"}

    account_changes = object_record_changes.list_record_changes(
        "legacy_accounts", record_id="acc_1", base_dir=data_dir
    )["changes"]
    assert account_changes[0]["actor"] == "import-cli-test"
    assert account_changes[0]["action"] == "create"

    widget_changes = object_record_changes.list_record_changes(
        "legacy_widgets", record_id="w_1", base_dir=data_dir
    )["changes"]
    assert widget_changes[0]["actor"] == "import-cli-test"


def test_real_run_requires_actor():
    plan = {"order": ["legacy_accounts"], "collections": {"legacy_accounts": {}}}
    with pytest.raises(object_import.ImportRunError):
        object_import.run_import(plan, base_dir=Path("data"), dry_run=False, actor=None)


def test_real_run_preserves_source_created_at(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold",
          "CreatedAt": "2020-01-01T00:00:00Z"}],
    )
    cfg = base_accounts_config()
    cfg["mapping"]["CreatedAt"] = "created_at"
    plan = object_import.load_plan(write_plan(tmp_path, accounts_cfg=cfg))

    object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=False, actor="import-cli-test")

    account = object_records.get_collection_record("legacy_accounts", "acc_1", base_dir=data_dir, roots=[])
    assert account["created_at"] == "2020-01-01T00:00:00Z"


def test_create_collection_record_still_rejects_read_only_by_default(tmp_path):
    """preserve_read_only defaults False: an ordinary write path is unaffected."""
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)

    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "legacy_accounts",
            {"id": "acc_1", "name": "Acme", "created_at": "2020-01-01T00:00:00Z"},
            base_dir=data_dir,
            roots=[],
        )


# --------------------------------------------------------------------------
# Idempotency: exists-skip and --update-existing
# --------------------------------------------------------------------------

def test_second_run_skips_existing_ids_by_default(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold"}],
    )
    plan = object_import.load_plan(write_plan(tmp_path, accounts_cfg=base_accounts_config()))

    object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=False, actor="first-run")
    reports = object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=False, actor="second-run")

    (report,) = reports
    assert report.exists == 1
    assert report.created == 0
    assert report.skipped == 1

    records = object_records.read_collection_records("legacy_accounts", base_dir=data_dir, roots=[])
    assert len(records) == 1  # not duplicated

    changes = object_record_changes.list_record_changes(
        "legacy_accounts", record_id="acc_1", base_dir=data_dir
    )["changes"]
    assert len(changes) == 1  # the skip never touched the record
    assert changes[0]["actor"] == "first-run"


def test_update_existing_upserts_and_never_moves_created_at(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold",
          "CreatedAt": "2020-01-01T00:00:00Z"}],
    )
    cfg = base_accounts_config()
    cfg["mapping"]["CreatedAt"] = "created_at"
    plan = object_import.load_plan(write_plan(tmp_path, accounts_cfg=cfg))
    object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=False, actor="first-run")

    # A repeat import with a renamed org and a (deliberately ignored on update) new created_at.
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corporation", "Tier": "gold",
          "CreatedAt": "2099-01-01T00:00:00Z"}],
    )
    reports = object_import.run_import(
        plan, base_dir=data_dir, roots=[], dry_run=False, actor="second-run", update_existing=True
    )

    (report,) = reports
    assert report.updated == 1
    account = object_records.get_collection_record("legacy_accounts", "acc_1", base_dir=data_dir, roots=[])
    assert account["name"] == "Acme Corporation"
    assert account["created_at"] == "2020-01-01T00:00:00Z"  # untouched by update


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def test_export_collection_returns_records_as_the_read_api_does(tmp_path):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(
        tmp_path / "accounts.json",
        [{"AccountId": "acc_1", "AccountName": "Acme Corp", "Tier": "gold"}],
    )
    plan = object_import.load_plan(write_plan(tmp_path, accounts_cfg=base_accounts_config()))
    object_import.run_import(plan, base_dir=data_dir, roots=[], dry_run=False, actor="seed")

    exported = object_import.export_collection("legacy_accounts", base_dir=data_dir, roots=[])
    api_read = object_records.read_collection_records("legacy_accounts", base_dir=data_dir, roots=[])

    assert exported == api_read
    assert exported[0]["id"] == "acc_1"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_dry_run_reports_errors_and_exits_nonzero(tmp_path, capsys):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(tmp_path / "accounts.json", [{"AccountId": "acc_1", "AccountName": "Acme", "Tier": "platinum"}])
    plan_path = write_plan(tmp_path, accounts_cfg=base_accounts_config())

    exit_code = object_import.main(
        ["--data-dir", str(data_dir), "run", str(plan_path), "--dry-run", "--json"]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["errors"] == 1
    assert object_records.read_collection_records("legacy_accounts", base_dir=data_dir, roots=[]) == []


def test_cli_real_run_requires_actor(tmp_path, capsys):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(tmp_path / "accounts.json", [{"AccountId": "acc_1", "AccountName": "Acme", "Tier": "gold"}])
    plan_path = write_plan(tmp_path, accounts_cfg=base_accounts_config())

    exit_code = object_import.main(["--data-dir", str(data_dir), "run", str(plan_path)])

    assert exit_code == 1
    assert "actor" in capsys.readouterr().err
    assert object_records.read_collection_records("legacy_accounts", base_dir=data_dir, roots=[]) == []


def test_cli_run_and_export_round_trip(tmp_path, capsys):
    data_dir = tmp_path / "data"
    setup_schemas(data_dir)
    write_json(tmp_path / "accounts.json", [{"AccountId": "acc_1", "AccountName": "Acme", "Tier": "gold"}])
    plan_path = write_plan(tmp_path, accounts_cfg=base_accounts_config())

    exit_code = object_import.main(
        ["--data-dir", str(data_dir), "run", str(plan_path), "--actor", "cli-actor"]
    )
    assert exit_code == 0
    capsys.readouterr()

    output_path = tmp_path / "export.json"
    exit_code = object_import.main(
        ["--data-dir", str(data_dir), "export", "legacy_accounts", "--output", str(output_path)]
    )
    assert exit_code == 0
    exported = json.loads(output_path.read_text())
    assert exported[0]["id"] == "acc_1"
    assert exported[0]["name"] == "Acme"


# --------------------------------------------------------------------------
# Schema delta (part B) and comments package (part C) sanity checks
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_tasks_schema_gains_draft_status():
    schema = json.loads((REPO_ROOT / "packages/app-tasks/schemas/tasks.json").read_text())
    status_field = next(f for f in schema["fields"] if f["name"] == "status")
    assert "draft" in status_field["enum"]
    assert status_field["transitions"]["draft"] == ["open", "assigned", "cancelled"]


def test_tasks_schema_draft_transition_enforced(tmp_path):
    data_dir = tmp_path / "data"
    schema_path = data_dir / "schemas" / "tasks.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text((REPO_ROOT / "packages/app-tasks/schemas/tasks.json").read_text())

    task = object_records.create_collection_record(
        "tasks", {"id": "t1", "title": "Draft task", "status": "draft"}, base_dir=data_dir, roots=[]
    )
    assert task["status"] == "draft"

    # draft -> assigned is allowed
    updated = object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[]
    )
    assert updated["status"] == "assigned"

    # assigned -> draft is not in the transitions map
    task2 = object_records.create_collection_record(
        "tasks", {"id": "t2", "title": "Another", "status": "assigned"}, base_dir=data_dir, roots=[]
    )
    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.update_collection_record(
            "tasks", "t2", {"status": "draft"}, base_dir=data_dir, roots=[]
        )


def test_comments_schema_and_permissions_shape():
    """The early-landed comments package must match the full thread block's
    schema exactly (collection thread_comments, parent_* pointer fields,
    owner_id author, moderation status with transitions) so adopting the
    block later never requires a data migration."""
    schema = json.loads(
        (REPO_ROOT / "packages/app-thread/schemas/thread_comments.json").read_text()
    )
    assert schema["name"] == "thread_comments"
    assert schema["storage"] == "append"
    names = {f["name"] for f in schema["fields"]}
    assert {
        "id", "parent_collection", "parent_id", "reply_to_id", "body",
        "owner_id", "author_name", "status", "created_at", "edited_at",
    } <= names
    status = next(f for f in schema["fields"] if f["name"] == "status")
    assert status["default"] == "published"
    assert status["transitions"]["removed"] == []

    manifest = json.loads((REPO_ROOT / "packages/app-thread/dbbasic-package.json").read_text())
    assert manifest["id"] == "app-thread"
    assert manifest["schemas"] == [
        {"collection": "thread_comments", "path": "schemas/thread_comments.json"}
    ]

    rules = json.loads((REPO_ROOT / "packages/app-thread/permissions/rules.json").read_text())
    owner_rule = rules["rules"][0]
    assert owner_rule["collection"] == "thread_comments"
    assert owner_rule["row_filter"] == {"owner_id": "$user_id"}
