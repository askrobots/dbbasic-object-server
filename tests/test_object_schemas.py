import json
from pathlib import Path

import pytest

import object_schemas


def write_source(path: Path, content: str = "def GET(request):\n    return {}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_schema(data_dir: Path, name: str, payload: dict) -> Path:
    path = data_dir / "schemas" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def test_get_schema_returns_manual_schema_with_normalized_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "invoices",
        {
            "title": "Invoices",
            "version": 2,
            "fields": [
                {
                    "name": "customer_id",
                    "type": "relation",
                    "required": True,
                    "relation": {"collection": "contacts"},
                    "validation": {"not_null": True},
                },
                {
                    "name": "total",
                    "type": "computed",
                    "computed": "sum(line_items)",
                },
            ],
        },
    )

    schema = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])

    assert schema == {
        "name": "invoices",
        "title": "Invoices",
        "source": "manual",
        "version": 2,
        "fields": [
            {
                "name": "customer_id",
                "type": "relation",
                "required": True,
                "relation": {"collection": "contacts"},
                "validation": {"not_null": True},
            },
            {
                "name": "total",
                "type": "computed",
                "required": False,
                "computed": "sum(line_items)",
            },
        ],
        "field_count": 2,
    }


def test_get_schema_derives_empty_schema_for_existing_collection(tmp_path):
    root = tmp_path / "objects"
    write_source(root / "contacts" / "directory.py")

    schema = object_schemas.get_schema("contacts", base_dir=tmp_path / "data", roots=[root])

    assert schema == {
        "name": "contacts",
        "title": "Contacts",
        "source": "derived",
        "version": 1,
        "fields": [],
        "field_count": 0,
    }


def test_list_schemas_includes_manual_and_derived_collection_schemas(tmp_path):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "contacts" / "directory.py")
    write_schema(
        data_dir,
        "invoices",
        {
            "title": "Invoices",
            "fields": [{"name": "invoice_date", "type": "date", "required": True}],
        },
    )

    schemas = object_schemas.list_schemas(base_dir=data_dir, roots=[root])

    assert schemas == [
        {
            "name": "contacts",
            "title": "Contacts",
            "source": "derived",
            "version": 1,
            "field_count": 0,
        },
        {
            "name": "invoices",
            "title": "Invoices",
            "source": "manual",
            "version": 1,
            "field_count": 1,
        },
    ]


def test_get_schema_preserves_scroll_field_metadata(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "invoices",
        {
            "fields": [
                {
                    "name": "cost_price",
                    "type": "currency",
                    "label": "Cost Price",
                    "read_only": True,
                    "ui": {"widget": "money", "section": "totals"},
                    "layout": {"column": 2, "order": 5},
                    "permissions": {
                        "admin": "edit",
                        "sales": "hidden",
                        "viewer": "hidden",
                    },
                    "placeholder": "0.00",
                    "help": "Internal margin input",
                }
            ]
        },
    )

    schema = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])

    assert schema["fields"] == [
        {
            "name": "cost_price",
            "type": "currency",
            "required": False,
            "label": "Cost Price",
            "read_only": True,
            "ui": {"widget": "money", "section": "totals"},
            "layout": {"column": 2, "order": 5},
            "permissions": {
                "admin": "edit",
                "sales": "hidden",
                "viewer": "hidden",
            },
            "placeholder": "0.00",
            "help": "Internal margin input",
        }
    ]


def test_get_schema_preserves_store_extra_field_attribute(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        {
            "fields": [
                {"name": "title", "type": "text"},
                {"name": "x_mood", "type": "text", "store": "extra"},
            ]
        },
    )

    schema = object_schemas.get_schema("notes", base_dir=data_dir, roots=[])

    assert schema["fields"] == [
        {"name": "title", "type": "text", "required": False},
        {"name": "x_mood", "type": "text", "required": False, "store": "extra"},
    ]

    # normalize_schema round-trips the same attribute directly (no file I/O).
    normalized = object_schemas.normalize_schema(
        "notes",
        {
            "fields": [
                {"name": "x_mood", "type": "text", "store": "extra"},
            ]
        },
    )
    assert normalized["fields"] == [
        {"name": "x_mood", "type": "text", "required": False, "store": "extra"},
    ]


def test_replace_schema_writes_normalized_schema_atomically(tmp_path):
    data_dir = tmp_path / "data"

    schema = object_schemas.replace_schema(
        "invoices",
        {
            "title": "Invoices",
            "version": 2,
            "ui": {"default_view": "form"},
            "views": [{"name": "admin_invoice_form", "type": "form"}],
            "fields": [
                {
                    "name": "customer_id",
                    "type": "relation",
                    "required": True,
                    "relation": {"collection": "contacts"},
                    "permissions": {"admin": "edit", "sales": "read"},
                }
            ],
        },
        base_dir=data_dir,
    )

    assert schema == {
        "name": "invoices",
        "title": "Invoices",
        "source": "manual",
        "version": 2,
        "fields": [
            {
                "name": "customer_id",
                "type": "relation",
                "required": True,
                "relation": {"collection": "contacts"},
                "permissions": {"admin": "edit", "sales": "read"},
            }
        ],
        "field_count": 1,
        "ui": {"default_view": "form"},
        "views": [{"name": "admin_invoice_form", "type": "form"}],
    }
    assert object_schemas.get_schema("invoices", base_dir=data_dir, roots=[]) == schema


def test_replace_schema_keeps_search_metadata(tmp_path):
    data_dir = tmp_path / "data"

    schema = object_schemas.replace_schema(
        "notes",
        {
            "fields": [{"name": "id"}, {"name": "content"}],
            "search": {"fields": ["content"], "result_fields": ["id", "content"]},
        },
        base_dir=data_dir,
    )

    assert schema["search"] == {"fields": ["content"], "result_fields": ["id", "content"]}
    assert object_schemas.get_schema("notes", base_dir=data_dir, roots=[])["search"] == schema["search"]


def test_replace_schema_rejects_mismatched_name(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        object_schemas.replace_schema(
            "invoices",
            {"name": "contacts", "fields": []},
            base_dir=tmp_path / "data",
        )


def test_get_schema_rejects_unsafe_names(tmp_path):
    with pytest.raises(object_schemas.InvalidSchemaNameError):
        object_schemas.get_schema("../bad", base_dir=tmp_path / "data", roots=[])


def test_get_schema_rejects_missing_schemas(tmp_path):
    with pytest.raises(object_schemas.SchemaNotFoundError):
        object_schemas.get_schema("missing", base_dir=tmp_path / "data", roots=[])


def test_manual_schema_rejects_mismatched_collection_name(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices", {"name": "contacts", "fields": []})

    with pytest.raises(ValueError, match="does not match"):
        object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])


def test_manual_schema_rejects_invalid_field_name(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices", {"fields": [{"name": "bad.name"}]})

    with pytest.raises(ValueError, match="invalid name"):
        object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])


def test_get_schema_accepts_guarded_transition_entries(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "tasks",
        {
            "fields": [
                {"name": "id"},
                {
                    "name": "status",
                    "type": "enum",
                    "enum": ["open", "closed"],
                    "transitions": {
                        "open": [{"to": "closed", "when": {"owner_id": "$user_id"}}],
                        "closed": ["open"],
                    },
                },
            ]
        },
    )

    schema = object_schemas.get_schema("tasks", base_dir=data_dir, roots=[])

    status_field = next(f for f in schema["fields"] if f["name"] == "status")
    assert status_field["transitions"] == {
        "open": [{"to": "closed", "when": {"owner_id": "$user_id"}}],
        "closed": ["open"],
    }


def test_manual_schema_rejects_transition_entry_missing_to(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "tasks",
        {
            "fields": [
                {"name": "id"},
                {
                    "name": "status",
                    "type": "enum",
                    "transitions": {"open": [{"when": {"owner_id": "$user_id"}}]},
                },
            ]
        },
    )

    with pytest.raises(ValueError, match="'to'"):
        object_schemas.get_schema("tasks", base_dir=data_dir, roots=[])


def test_manual_schema_rejects_non_string_when_values(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "tasks",
        {
            "fields": [
                {"name": "id"},
                {
                    "name": "status",
                    "type": "enum",
                    "transitions": {
                        "open": [{"to": "closed", "when": {"owner_id": 42}}]
                    },
                },
            ]
        },
    )

    with pytest.raises(ValueError, match="'when' values"):
        object_schemas.get_schema("tasks", base_dir=data_dir, roots=[])


# --- Perf pass: schema cache ---


def test_get_schema_cache_reflects_external_file_change(tmp_path):
    """A schema file rewritten directly on disk (bypassing replace_schema)
    must be visible on the next get_schema call -- the stat-signature check
    the schema cache relies on."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices", {"fields": [{"name": "id"}, {"name": "total"}]})

    first = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])
    assert [f["name"] for f in first["fields"]] == ["id", "total"]

    write_schema(
        data_dir,
        "invoices",
        {"fields": [{"name": "id"}, {"name": "total"}, {"name": "paid"}]},
    )

    second = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])
    assert [f["name"] for f in second["fields"]] == ["id", "total", "paid"]


def test_replace_schema_invalidates_cache_immediately(tmp_path):
    data_dir = tmp_path / "data"
    object_schemas.replace_schema("invoices", {"fields": [{"name": "id"}]}, base_dir=data_dir)
    warm = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])
    assert [f["name"] for f in warm["fields"]] == ["id"]

    object_schemas.replace_schema(
        "invoices", {"fields": [{"name": "id"}, {"name": "total"}]}, base_dir=data_dir
    )
    updated = object_schemas.get_schema("invoices", base_dir=data_dir, roots=[])
    assert [f["name"] for f in updated["fields"]] == ["id", "total"]


def test_public_schema_meta_endpoint_returns_structure(tmp_path, monkeypatch):
    import object_server
    from test_object_server import request, write_records
    data_dir = tmp_path / "data"
    write_records(data_dir, "links", "id\ttitle\turl\n")
    schema_file = data_dir / "schemas" / "links.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json.dumps({
        "fields": [{"name": "id"}, {"name": "title", "required": True}, {"name": "url"}],
        "forms": {"default": {"fields": ["title", "url"]}},
        "views": {"list_mode": "cards"},
        "search": {"fields": ["title"]},
        "capabilities": {"comments": True},
    }))
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    # public — no auth needed (structure, not data)
    status, _, payload = request("/api/schema/links")
    assert status == 200
    schema = payload["schema"]
    assert [f["name"] for f in schema["fields"]] == ["id", "title", "url"]
    assert schema["forms"]["default"]["fields"] == ["title", "url"]
    assert schema["views"]["list_mode"] == "cards"
    # capabilities surface in the public meta so a UI can wire generic
    # behaviors (e.g. the comment thread) from the schema alone.
    assert schema["capabilities"] == {"comments": True}
    # unknown collection -> 404-ish (derived empty schema still returns; bad name -> 400)
    status, _, _ = request("/api/schema/bad!name")
    assert status == 400
