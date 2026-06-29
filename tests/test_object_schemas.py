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
