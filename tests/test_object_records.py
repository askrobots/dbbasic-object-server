import csv
import json
import random
from pathlib import Path
from uuid import UUID

import pytest

import object_collections
import object_records
import object_schemas


def write_source(path: Path, content: str = "def GET(request):\n    return {}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_records(data_dir: Path, collection: str, content: str) -> Path:
    path = data_dir / "collections" / collection / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_schema(data_dir: Path, collection: str, fields: list[dict] | None = None) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields or [{"name": "id"}]}))
    return path


def test_list_collection_records_reads_tsv_with_pagination(tmp_path):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "contacts",
        "id\tfirst_name\tlast_name\n"
        "c1\tAda\tLovelace\n"
        "c2\tGrace\tHopper\n"
        "c3\tKatherine\tJohnson\n",
    )

    result = object_records.list_collection_records(
        "contacts",
        base_dir=data_dir,
        roots=[],
        limit=2,
        offset=1,
    )

    assert result == {
        "collection": "contacts",
        "records": [
            {"id": "c2", "first_name": "Grace", "last_name": "Hopper"},
            {"id": "c3", "first_name": "Katherine", "last_name": "Johnson"},
        ],
        "count": 2,
        "total": 3,
        "limit": 2,
        "offset": 1,
        "has_more": False,
    }


def test_get_collection_record_returns_record_by_id(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")

    record = object_records.get_collection_record("contacts", "c2", base_dir=data_dir, roots=[])

    assert record == {"id": "c2", "name": "Grace"}


def test_create_collection_record_appends_row_and_new_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    record = object_records.create_collection_record(
        "contacts",
        {"id": "c2", "name": "Grace", "email": "grace@example.com"},
        base_dir=data_dir,
        roots=[],
    )

    assert record == {"id": "c2", "name": "Grace", "email": "grace@example.com"}
    assert object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"] == [
        {"id": "c1", "name": "Ada", "email": ""},
        {"id": "c2", "name": "Grace", "email": "grace@example.com"},
    ]


def test_create_collection_record_generates_uuid_id_when_missing(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    record = object_records.create_collection_record(
        "contacts",
        {"name": "Grace"},
        base_dir=data_dir,
        roots=[],
    )

    parsed = UUID(record["id"])
    assert parsed.version == 4
    assert record["name"] == "Grace"
    assert object_records.get_collection_record(
        "contacts",
        record["id"],
        base_dir=data_dir,
        roots=[],
    ) == record


def test_create_collection_record_can_start_schema_backed_collection(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices")

    record = object_records.create_collection_record(
        "invoices",
        {"id": "i1", "total": 120},
        base_dir=data_dir,
        roots=[],
    )

    assert record == {"id": "i1", "total": "120"}
    assert (data_dir / "collections" / "invoices" / "records.tsv").read_text() == (
        "id\ttotal\n"
        "i1\t120\n"
    )


def test_create_collection_record_applies_schema_defaults_and_allows_extra_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "invoices",
        [
            {"name": "id"},
            {"name": "invoice_date", "type": "date", "required": True},
            {"name": "status", "type": "enum", "enum": ["draft", "sent"], "default": "draft"},
            {"name": "quantity", "type": "integer", "validation": {"min": 1, "max": 20}},
            {"name": "notes", "validation": {"min_length": 3, "max_length": 20}},
        ],
    )

    record = object_records.create_collection_record(
        "invoices",
        {
            "id": "i1",
            "invoice_date": "2026-04-08",
            "quantity": 3,
            "notes": "Thanks",
            "extra_field": "still allowed",
        },
        base_dir=data_dir,
        roots=[],
    )

    assert record == {
        "id": "i1",
        "invoice_date": "2026-04-08",
        "quantity": "3",
        "notes": "Thanks",
        "extra_field": "still allowed",
        "status": "draft",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"id": "i1", "status": "draft"}, "invoice_date' is required"),
        ({"id": "i1", "invoice_date": "bad-date", "status": "draft"}, "invoice_date' must be a date"),
        ({"id": "i1", "invoice_date": "2026-04-08", "status": "paid"}, "status' must be one of"),
        ({"id": "i1", "invoice_date": "2026-04-08", "quantity": "0"}, "quantity' is below min"),
        ({"id": "i1", "invoice_date": "2026-04-08", "quantity": "3.5"}, "quantity' must be an integer"),
        ({"id": "i1", "invoice_date": "2026-04-08", "total": "100"}, "total' is computed"),
    ],
)
def test_create_collection_record_validates_schema_fields(tmp_path, payload, message):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "invoices",
        [
            {"name": "id"},
            {"name": "invoice_date", "type": "date", "required": True},
            {"name": "status", "type": "enum", "enum": ["draft", "sent"]},
            {"name": "quantity", "type": "integer", "validation": {"min": 1, "max": 20}},
            {"name": "total", "type": "computed", "computed": "sum(line_items)"},
        ],
    )

    with pytest.raises(object_records.InvalidRecordPayloadError, match=message):
        object_records.create_collection_record(
            "invoices",
            payload,
            base_dir=data_dir,
            roots=[],
        )


def test_create_collection_record_rejects_duplicate_id(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    with pytest.raises(object_records.DuplicateRecordIdError):
        object_records.create_collection_record(
            "contacts",
            {"id": "c1", "name": "Duplicate"},
            base_dir=data_dir,
            roots=[],
        )


def test_update_collection_record_merges_changes_and_preserves_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\temail\nc1\tAda\tada@example.com\n")

    record = object_records.update_collection_record(
        "contacts",
        "c1",
        {"name": "Ada Lovelace", "phone": "555-0100"},
        base_dir=data_dir,
        roots=[],
    )

    assert record == {
        "id": "c1",
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "555-0100",
    }
    assert object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[]) == record


def test_boolean_fields_are_stored_canonically(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        [
            {"name": "id"},
            {"name": "content"},
            {"name": "is_public", "type": "boolean", "default": "false"},
        ],
    )
    write_records(data_dir, "notes", "id\tcontent\tis_public\n")

    for record_id, submitted in (("n1", "True"), ("n2", "YES"), ("n3", "1")):
        record = object_records.create_collection_record(
            "notes",
            {"id": record_id, "content": "x", "is_public": submitted},
            base_dir=data_dir,
            roots=[],
        )
        assert record["is_public"] == "true"

    defaulted = object_records.create_collection_record(
        "notes", {"id": "n4", "content": "x"}, base_dir=data_dir, roots=[]
    )
    assert defaulted["is_public"] == "false"

    updated = object_records.update_collection_record(
        "notes", "n1", {"is_public": "OFF"}, base_dir=data_dir, roots=[]
    )
    assert updated["is_public"] == "false"


def test_enum_transitions_are_enforced_on_update(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "tasks",
        [
            {"name": "id"},
            {"name": "title"},
            {
                "name": "status",
                "type": "enum",
                "default": "open",
                "enum": ["open", "assigned", "done", "cancelled"],
                "transitions": {
                    "open": ["assigned", "cancelled"],
                    "assigned": ["done", "open", "cancelled"],
                },
            },
        ],
    )
    write_records(data_dir, "tasks", "id\ttitle\tstatus\nt1\tShip it\topen\n")

    with pytest.raises(
        object_records.InvalidRecordPayloadError,
        match="cannot move from 'open' to 'done'",
    ):
        object_records.update_collection_record(
            "tasks", "t1", {"status": "done"}, base_dir=data_dir, roots=[]
        )

    record = object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[]
    )
    assert record["status"] == "assigned"

    record = object_records.update_collection_record(
        "tasks", "t1", {"status": "done"}, base_dir=data_dir, roots=[]
    )
    assert record["status"] == "done"

    # done is not in the transitions map, so it is terminal.
    with pytest.raises(object_records.InvalidRecordPayloadError, match="allowed: none"):
        object_records.update_collection_record(
            "tasks", "t1", {"status": "open"}, base_dir=data_dir, roots=[]
        )

    # Unrelated updates to a record sitting in a terminal state stay fine.
    record = object_records.update_collection_record(
        "tasks", "t1", {"title": "Shipped"}, base_dir=data_dir, roots=[]
    )
    assert record["title"] == "Shipped"


def test_update_collection_record_validates_final_schema_record(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "invoices",
        [
            {"name": "id"},
            {"name": "customer_id", "type": "relation", "required": True},
            {"name": "paid", "type": "boolean"},
            {"name": "total", "type": "computed", "computed": "sum(line_items)"},
        ],
    )
    write_records(data_dir, "invoices", "id\tcustomer_id\tpaid\n"
                                        "i1\tc1\tfalse\n")

    record = object_records.update_collection_record(
        "invoices",
        "i1",
        {"paid": "true"},
        base_dir=data_dir,
        roots=[],
    )

    assert record == {"id": "i1", "customer_id": "c1", "paid": "true"}
    with pytest.raises(object_records.InvalidRecordPayloadError, match="customer_id' is required"):
        object_records.update_collection_record(
            "invoices",
            "i1",
            {"customer_id": ""},
            base_dir=data_dir,
            roots=[],
        )
    with pytest.raises(object_records.InvalidRecordPayloadError, match="total' is computed"):
        object_records.update_collection_record(
            "invoices",
            "i1",
            {"total": "100"},
            base_dir=data_dir,
            roots=[],
        )


def test_update_collection_record_rejects_id_change(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    with pytest.raises(object_records.InvalidRecordPayloadError, match="id cannot be changed"):
        object_records.update_collection_record(
            "contacts",
            "c1",
            {"id": "c2", "name": "Grace"},
            base_dir=data_dir,
            roots=[],
        )


def test_delete_collection_record_removes_row_and_returns_deleted_record(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")

    record = object_records.delete_collection_record("contacts", "c1", base_dir=data_dir, roots=[])

    assert record == {"id": "c1", "name": "Ada"}
    assert object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"] == [
        {"id": "c2", "name": "Grace"},
    ]


def test_record_payload_rejects_unsafe_fields_and_non_scalar_values():
    with pytest.raises(object_records.InvalidRecordPayloadError, match="field name"):
        object_records.normalize_record_payload({"bad\tfield": "value"})

    with pytest.raises(object_records.InvalidRecordPayloadError, match="scalar"):
        object_records.normalize_record_payload({"id": "c1", "tags": ["vip"]})


def test_schema_backed_collection_without_records_returns_empty_list(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "invoices")

    result = object_records.list_collection_records("invoices", base_dir=data_dir, roots=[])

    assert result == {
        "collection": "invoices",
        "records": [],
        "count": 0,
        "total": 0,
        "limit": 100,
        "offset": 0,
        "has_more": False,
    }


def test_object_backed_collection_without_records_returns_empty_list(tmp_path):
    data_dir = tmp_path / "data"
    root = tmp_path / "objects"
    write_source(root / "contacts" / "directory.py")

    result = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[root])

    assert result["records"] == []
    assert result["total"] == 0


def test_get_collection_record_rejects_missing_record(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    with pytest.raises(object_records.RecordNotFoundError):
        object_records.get_collection_record("contacts", "missing", base_dir=data_dir, roots=[])


def test_list_collection_records_rejects_missing_collection(tmp_path):
    with pytest.raises(object_collections.CollectionNotFoundError):
        object_records.list_collection_records("missing", base_dir=tmp_path / "data", roots=[])


def test_collection_records_reject_unsafe_names(tmp_path):
    with pytest.raises(object_collections.InvalidCollectionNameError):
        object_records.list_collection_records("../bad", base_dir=tmp_path / "data", roots=[])

    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    with pytest.raises(object_records.InvalidRecordIdError):
        object_records.get_collection_record("contacts", "../bad", base_dir=data_dir, roots=[])


def test_records_file_requires_id_column(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "name\nAda\n")

    with pytest.raises(ValueError, match="id column"):
        object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])


def test_records_file_rejects_duplicate_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\tname\nc1\tAda\tLovelace\n")

    with pytest.raises(ValueError, match="duplicate"):
        object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])


def test_records_file_rejects_extra_row_fields(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\textra\n")

    with pytest.raises(ValueError, match="extra fields"):
        object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])


def test_iter_record_collections_lists_safe_record_directories(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")
    write_records(data_dir, "bad-name", "id\tname\nbad\tBad\n")

    assert object_records.iter_record_collections(data_dir) == ["contacts"]


def test_relation_fields_require_existing_target_records(tmp_path):
    write_records(tmp_path, "projects", "id\tname\np1\tWebsite\n")
    schema_file = tmp_path / "schemas" / "tasks.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "id"},
                    {"name": "title", "required": True},
                    {"name": "project_id", "relation": {"collection": "projects", "display_field": "name"}},
                ]
            }
        )
    )

    created = object_records.create_collection_record(
        "tasks",
        {"id": "t1", "title": "Launch", "project_id": "p1"},
        base_dir=tmp_path,
    )
    assert created["project_id"] == "p1"

    with pytest.raises(object_records.InvalidRecordPayloadError) as missing:
        object_records.create_collection_record(
            "tasks",
            {"id": "t2", "title": "Broken", "project_id": "p999"},
            base_dir=tmp_path,
        )
    assert "missing record: projects/p999" in str(missing.value)

    # Empty relation values stay allowed unless the field is required.
    optional = object_records.create_collection_record(
        "tasks",
        {"id": "t3", "title": "No project"},
        base_dir=tmp_path,
    )
    assert optional["project_id"] == ""


def test_relation_accepts_string_shorthand_and_rejects_bad_shapes(tmp_path):
    write_records(tmp_path, "projects", "id\tname\np1\tWebsite\n")
    schema_file = tmp_path / "schemas" / "tasks.json"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        json.dumps({"fields": [{"name": "id"}, {"name": "project_id", "relation": "projects"}]})
    )

    created = object_records.create_collection_record(
        "tasks", {"id": "t1", "project_id": "p1"}, base_dir=tmp_path
    )
    assert created["project_id"] == "p1"

    schema_file.write_text(
        json.dumps({"fields": [{"name": "id"}, {"name": "project_id", "relation": 42}]})
    )
    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.create_collection_record(
            "tasks", {"id": "t2", "project_id": "p1"}, base_dir=tmp_path
        )


def test_created_at_is_server_set_on_create(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "links",
        [
            {"name": "id"},
            {"name": "title", "required": True},
            {"name": "created_at", "type": "datetime", "read_only": True},
        ],
    )
    write_records(data_dir, "links", "id\ttitle\tcreated_at\n")

    # client omits created_at -> server fills it, read_only is not violated
    rec = object_records.create_collection_record(
        "links", {"id": "l1", "title": "x"}, base_dir=data_dir, roots=[]
    )
    assert rec["created_at"].endswith("Z") and "T" in rec["created_at"]

    # client cannot spoof a read_only created_at
    import pytest
    with pytest.raises(object_records.InvalidRecordPayloadError, match="read-only"):
        object_records.create_collection_record(
            "links", {"id": "l2", "title": "y", "created_at": "2000-01-01T00:00:00Z"},
            base_dir=data_dir, roots=[],
        )


# --- Phase 4b: the `extra` extension field (docs/upgrade-and-customization.md Rule 3) ---


def test_backward_compat_no_store_extra_fields_means_no_extra_column(tmp_path):
    """A collection with no store:extra fields and no submitted `extra` key
    must behave byte-identically to before the `extra` feature existed: no
    `extra` column ever appears on disk."""
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "contacts",
        [{"name": "id"}, {"name": "name", "required": True}, {"name": "email"}],
    )

    created = object_records.create_collection_record(
        "contacts",
        {"id": "c1", "name": "Ada", "email": "ada@example.com"},
        base_dir=data_dir,
        roots=[],
    )
    assert created == {"id": "c1", "name": "Ada", "email": "ada@example.com"}

    updated = object_records.update_collection_record(
        "contacts", "c1", {"email": "ada@example.org"}, base_dir=data_dir, roots=[]
    )
    assert updated == {"id": "c1", "name": "Ada", "email": "ada@example.org"}

    fetched = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert fetched == updated

    listed = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"]
    assert listed == [updated]

    tsv_text = (data_dir / "collections" / "contacts" / "records.tsv").read_text()
    assert tsv_text == "id\tname\temail\nc1\tAda\tada@example.org\n"
    assert "extra" not in tsv_text.splitlines()[0].split("\t")


def test_store_extra_view_field_is_stored_in_extra_column_not_own_column(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        [
            {"name": "id"},
            {"name": "title", "required": True},
            {"name": "x_mood", "type": "text", "store": "extra"},
        ],
    )

    record = object_records.create_collection_record(
        "notes",
        {"id": "n1", "title": "Hi", "x_mood": "curious"},
        base_dir=data_dir,
        roots=[],
    )

    assert record["title"] == "Hi"
    assert record["x_mood"] == "curious"

    path = data_dir / "collections" / "notes" / "records.tsv"
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    header = rows[0]
    assert "extra" in header
    assert "x_mood" not in header

    extra_cell = rows[1][header.index("extra")]
    assert json.loads(extra_cell) == {"x_mood": "curious"}

    fetched = object_records.get_collection_record("notes", "n1", base_dir=data_dir, roots=[])
    assert fetched["x_mood"] == "curious"
    assert json.loads(fetched["extra"]) == {"x_mood": "curious"}

    listed = object_records.list_collection_records("notes", base_dir=data_dir, roots=[])["records"]
    assert listed[0]["x_mood"] == "curious"


def test_store_extra_view_field_still_validates(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        [
            {"name": "id"},
            {"name": "title", "required": True},
            {
                "name": "x_mood",
                "type": "enum",
                "enum": ["happy", "curious", "grumpy"],
                "required": True,
                "store": "extra",
            },
        ],
    )

    with pytest.raises(object_records.InvalidRecordPayloadError, match="x_mood' is required"):
        object_records.create_collection_record(
            "notes", {"id": "n1", "title": "Hi"}, base_dir=data_dir, roots=[]
        )

    with pytest.raises(object_records.InvalidRecordPayloadError, match="x_mood' must be one of"):
        object_records.create_collection_record(
            "notes",
            {"id": "n1", "title": "Hi", "x_mood": "furious"},
            base_dir=data_dir,
            roots=[],
        )

    record = object_records.create_collection_record(
        "notes",
        {"id": "n1", "title": "Hi", "x_mood": "curious"},
        base_dir=data_dir,
        roots=[],
    )
    assert record["x_mood"] == "curious"


def test_partial_update_preserves_untouched_extra_keys(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        [
            {"name": "id"},
            {"name": "title"},
            {"name": "x_mood", "type": "text", "store": "extra"},
        ],
    )

    created = object_records.create_collection_record(
        "notes",
        {"id": "n1", "title": "Hi", "x_mood": "curious", "extra": {"note": "hi"}},
        base_dir=data_dir,
        roots=[],
    )
    assert created["x_mood"] == "curious"
    assert json.loads(created["extra"]) == {"x_mood": "curious", "note": "hi"}

    # Updating only `title` must not wipe x_mood or the undeclared `note` key.
    after_title = object_records.update_collection_record(
        "notes", "n1", {"title": "Hello"}, base_dir=data_dir, roots=[]
    )
    assert after_title["title"] == "Hello"
    assert after_title["x_mood"] == "curious"
    assert json.loads(after_title["extra"]) == {"x_mood": "curious", "note": "hi"}

    # Updating only x_mood must not wipe `note`, and must not touch title.
    after_mood = object_records.update_collection_record(
        "notes", "n1", {"x_mood": "grumpy"}, base_dir=data_dir, roots=[]
    )
    assert after_mood["title"] == "Hello"
    assert after_mood["x_mood"] == "grumpy"
    assert json.loads(after_mood["extra"]) == {"x_mood": "grumpy", "note": "hi"}

    fetched = object_records.get_collection_record("notes", "n1", base_dir=data_dir, roots=[])
    assert fetched["x_mood"] == "grumpy"
    assert json.loads(fetched["extra"]) == {"x_mood": "grumpy", "note": "hi"}


def test_raw_extra_passthrough_accepts_dict_and_json_string(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "widgets", "id\tname\n")

    from_dict = object_records.create_collection_record(
        "widgets",
        {"id": "w1", "name": "Gadget", "extra": {"a": "1"}},
        base_dir=data_dir,
        roots=[],
    )
    assert json.loads(from_dict["extra"]) == {"a": "1"}

    from_string = object_records.create_collection_record(
        "widgets",
        {"id": "w2", "name": "Gizmo", "extra": json.dumps({"a": "1"})},
        base_dir=data_dir,
        roots=[],
    )
    assert json.loads(from_string["extra"]) == {"a": "1"}

    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert json.loads(fetched["extra"]) == {"a": "1"}


@pytest.mark.parametrize("bad_extra", [["a", "1"], "[1, 2]", "not json", 5, True])
def test_invalid_extra_payload_is_rejected(tmp_path, bad_extra):
    data_dir = tmp_path / "data"
    write_records(data_dir, "widgets", "id\tname\n")

    with pytest.raises(object_records.InvalidRecordPayloadError, match="extra"):
        object_records.create_collection_record(
            "widgets",
            {"id": "w1", "name": "Gadget", "extra": bad_extra},
            base_dir=data_dir,
            roots=[],
        )


# --- Perf pass: records cache, id index, csv.reader-based parsing ---
#
# These tests describe behavior that must hold both BEFORE and AFTER the
# caching/parsing change (there is no cache pre-change, so "a mutation
# never leaks" and "external changes are always seen" hold trivially; the
# point is that they must ALSO hold once a cache is introduced).


def test_short_row_missing_fields_become_empty_string(tmp_path):
    """A data row with fewer tab-separated values than the header (e.g. a
    hand-edited or partially-written TSV) must fill the missing trailing
    fields with "" -- this mirrors csv.DictReader's restval=None behavior,
    which the surrounding code already converts to "" (see the
    `value if value is not None else ""` normalization)."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\temail\tphone\nc1\tAda\n")

    result = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])
    assert result["records"] == [{"id": "c1", "name": "Ada", "email": "", "phone": ""}]

    record = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert record == {"id": "c1", "name": "Ada", "email": "", "phone": ""}


def test_long_row_extra_fields_raise_matching_original_error(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\tLovelace\textra\n")

    with pytest.raises(ValueError, match="extra fields on row 2"):
        object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])


def test_quoted_value_containing_tab_round_trips(tmp_path):
    """A field value containing a literal tab (and newline) is legitimate --
    only field NAMES/ids are restricted, not values (see
    _normalize_record_payload). csv.DictWriter's default QUOTE_MINIMAL
    quotes such a value on write; the read path must undo that quoting
    exactly, never naively split on tab."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\tbody\n")

    tricky_value = "line one\tline two\nline three"
    created = object_records.create_collection_record(
        "notes",
        {"id": "n1", "body": tricky_value},
        base_dir=data_dir,
        roots=[],
    )
    assert created["body"] == tricky_value

    fetched = object_records.get_collection_record("notes", "n1", base_dir=data_dir, roots=[])
    assert fetched["body"] == tricky_value

    listed = object_records.list_collection_records("notes", base_dir=data_dir, roots=[])["records"]
    assert listed[0]["body"] == tricky_value


def test_get_collection_record_with_duplicate_ids_returns_first_match(tmp_path):
    """Malformed/hand-edited data can contain a duplicate id. Any id index
    used for O(1) lookup must replicate the original linear scan's
    first-match-wins semantics exactly."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc1\tGrace\n")

    record = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert record == {"id": "c1", "name": "Ada"}


def test_mutating_returned_record_does_not_corrupt_next_read(tmp_path):
    """Every record dict/list handed back across the public API must be a
    fresh copy: a caller mutating what it got back must never affect a
    later, independent call. This is the aliasing guard for the records
    cache -- cached objects must never be returned by reference."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\nc2\tGrace\n")

    first = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    first["name"] = "CORRUPTED"
    first["new_field"] = "also corrupted"

    second = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert second == {"id": "c1", "name": "Ada"}

    records = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"]
    records[0]["name"] = "ALSO CORRUPTED"

    still_clean = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert still_clean["name"] == "Ada"
    still_listed = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"]
    assert still_listed[0]["name"] == "Ada"


def test_cache_reflects_external_file_change(tmp_path):
    """Another process (or the same process writing directly, bypassing the
    API) can rewrite records.tsv on disk. The next API read must observe
    the new content -- the stat-signature check the records cache relies
    on for cross-process consistency."""
    data_dir = tmp_path / "data"
    path = write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    warm = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert warm["name"] == "Ada"

    # Rewrite the file directly, outside the object_records API.
    path.write_text("id\tname\nc1\tAda Lovelace\nc2\tGrace Hopper\n")

    refreshed = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert refreshed["name"] == "Ada Lovelace"

    listed = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"]
    assert listed == [
        {"id": "c1", "name": "Ada Lovelace"},
        {"id": "c2", "name": "Grace Hopper"},
    ]


def test_update_after_cached_read_returns_fresh_data(tmp_path):
    """A get/list right after a create/update must never return a value
    that predates the write, whether or not an earlier read happened to
    warm the cache first."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    warm = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert warm["name"] == "Ada"

    object_records.update_collection_record(
        "contacts", "c1", {"name": "Ada Lovelace"}, base_dir=data_dir, roots=[]
    )

    refreshed = object_records.get_collection_record("contacts", "c1", base_dir=data_dir, roots=[])
    assert refreshed["name"] == "Ada Lovelace"

    listed = object_records.list_collection_records("contacts", base_dir=data_dir, roots=[])["records"]
    assert listed == [{"id": "c1", "name": "Ada Lovelace"}]


def test_delete_collection_record_unaffected_by_extra(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(
        data_dir,
        "notes",
        [
            {"name": "id"},
            {"name": "title"},
            {"name": "x_mood", "type": "text", "store": "extra"},
        ],
    )
    object_records.create_collection_record(
        "notes",
        {"id": "n1", "title": "Hi", "x_mood": "curious"},
        base_dir=data_dir,
        roots=[],
    )

    removed = object_records.delete_collection_record("notes", "n1", base_dir=data_dir, roots=[])
    assert removed["id"] == "n1"

    assert object_records.list_collection_records("notes", base_dir=data_dir, roots=[])["records"] == []


def _cache_key(collection: str, data_dir: Path) -> str:
    path = object_records.collection_records_file(collection, base_dir=data_dir)
    return str(path.resolve(strict=False))


def test_records_cache_lru_evicts_least_recently_used(tmp_path, monkeypatch):
    """With capacity 2, reading a third collection must evict the least
    recently touched one (not necessarily the first-read one), and
    re-reading the evicted collection must still return correct data via
    the reparse path."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ENTRIES", "2")
    data_dir = tmp_path / "data"
    write_records(data_dir, "coll_a", "id\tname\na1\tAda\n")
    write_records(data_dir, "coll_b", "id\tname\nb1\tBob\n")
    write_records(data_dir, "coll_c", "id\tname\nc1\tCleo\n")

    object_records._RECORDS_CACHE.clear()
    object_records.read_collection_records("coll_a", base_dir=data_dir, roots=[])
    object_records.read_collection_records("coll_b", base_dir=data_dir, roots=[])
    # coll_a is now the least-recently-used of {coll_a, coll_b}; reading
    # coll_c should push the cache over capacity and evict coll_a.
    object_records.read_collection_records("coll_c", base_dir=data_dir, roots=[])

    keys = set(object_records._RECORDS_CACHE.keys())
    assert keys == {_cache_key("coll_b", data_dir), _cache_key("coll_c", data_dir)}
    assert _cache_key("coll_a", data_dir) not in object_records._RECORDS_CACHE

    # Re-reading the evicted collection must still work correctly.
    result = object_records.read_collection_records("coll_a", base_dir=data_dir, roots=[])
    assert result == [{"id": "a1", "name": "Ada"}]
    assert _cache_key("coll_a", data_dir) in object_records._RECORDS_CACHE


def test_records_cache_lru_hit_refreshes_recency(tmp_path, monkeypatch):
    """A cache HIT (not just a store) must count as use: touching coll_a
    again before reading coll_c should save it from eviction."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ENTRIES", "2")
    data_dir = tmp_path / "data"
    write_records(data_dir, "coll_a", "id\tname\na1\tAda\n")
    write_records(data_dir, "coll_b", "id\tname\nb1\tBob\n")
    write_records(data_dir, "coll_c", "id\tname\nc1\tCleo\n")

    object_records._RECORDS_CACHE.clear()
    object_records.read_collection_records("coll_a", base_dir=data_dir, roots=[])
    object_records.read_collection_records("coll_b", base_dir=data_dir, roots=[])
    # Touch coll_a again -- it is now the most-recently-used, so coll_b
    # (untouched since its initial read) should be evicted instead.
    object_records.read_collection_records("coll_a", base_dir=data_dir, roots=[])
    object_records.read_collection_records("coll_c", base_dir=data_dir, roots=[])

    keys = set(object_records._RECORDS_CACHE.keys())
    assert keys == {_cache_key("coll_a", data_dir), _cache_key("coll_c", data_dir)}


def test_records_cache_row_threshold_excludes_large_collections(tmp_path, monkeypatch):
    """A collection parsing to more rows than DBBASIC_RECORDS_CACHE_MAX_ROWS
    is returned correctly but never stored -- and must not evict a
    small, hot collection just because it was read once."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "10")
    data_dir = tmp_path / "data"
    small_rows = "".join(f"s{i}\tName{i}\n" for i in range(5))
    write_records(data_dir, "small", "id\tname\n" + small_rows)
    large_rows = "".join(f"l{i}\tName{i}\n" for i in range(20))
    write_records(data_dir, "large", "id\tname\n" + large_rows)

    object_records._RECORDS_CACHE.clear()

    small_result = object_records.read_collection_records("small", base_dir=data_dir, roots=[])
    assert len(small_result) == 5
    assert _cache_key("small", data_dir) in object_records._RECORDS_CACHE

    large_result = object_records.read_collection_records("large", base_dir=data_dir, roots=[])
    assert len(large_result) == 20
    assert large_result[0]["id"] == "l0"
    assert large_result[-1]["id"] == "l19"

    assert _cache_key("large", data_dir) not in object_records._RECORDS_CACHE
    # The small collection's entry must have survived reading the large one.
    assert _cache_key("small", data_dir) in object_records._RECORDS_CACHE


def test_records_cache_row_threshold_reparses_correctly_every_time(tmp_path, monkeypatch):
    """A collection over the row threshold is never cached, so repeated
    reads (and a read after an update) must each reparse from disk and
    return correct, current data -- not a stale first result."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "10")
    data_dir = tmp_path / "data"
    large_rows = "".join(f"l{i}\tName{i}\n" for i in range(20))
    write_records(data_dir, "large", "id\tname\n" + large_rows)

    object_records._RECORDS_CACHE.clear()
    first = object_records.get_collection_record("large", "l5", base_dir=data_dir, roots=[])
    assert first["name"] == "Name5"
    assert _cache_key("large", data_dir) not in object_records._RECORDS_CACHE

    object_records.update_collection_record(
        "large", "l5", {"name": "Updated"}, base_dir=data_dir, roots=[]
    )
    second = object_records.get_collection_record("large", "l5", base_dir=data_dir, roots=[])
    assert second["name"] == "Updated"


def test_list_collection_records_window_copy_and_boundaries(tmp_path):
    """The pagination window copy path: correct slice, correct total/
    has_more at both interior and edge offsets, and mutating a returned
    record must not leak into the cache or a later independent call."""
    data_dir = tmp_path / "data"
    rows = "".join(f"r{i}\tName{i}\n" for i in range(10))
    write_records(data_dir, "items", "id\tname\n" + rows)

    # Warm the cache first.
    object_records.read_collection_records("items", base_dir=data_dir, roots=[])

    page = object_records.list_collection_records(
        "items", base_dir=data_dir, roots=[], limit=3, offset=2
    )
    assert page["records"] == [
        {"id": "r2", "name": "Name2"},
        {"id": "r3", "name": "Name3"},
        {"id": "r4", "name": "Name4"},
    ]
    assert page["total"] == 10
    assert page["count"] == 3
    assert page["has_more"] is True

    # Aliasing guard: mutating a returned record must not affect a later,
    # independent get/list call.
    page["records"][0]["name"] = "CORRUPTED"
    page["records"][0]["new_field"] = "also corrupted"
    again = object_records.get_collection_record("items", "r2", base_dir=data_dir, roots=[])
    assert again == {"id": "r2", "name": "Name2"}
    relisted = object_records.list_collection_records(
        "items", base_dir=data_dir, roots=[], limit=3, offset=2
    )
    assert relisted["records"][0] == {"id": "r2", "name": "Name2"}

    # offset beyond the end of the collection.
    empty_page = object_records.list_collection_records(
        "items", base_dir=data_dir, roots=[], limit=5, offset=100
    )
    assert empty_page == {
        "collection": "items",
        "records": [],
        "count": 0,
        "total": 10,
        "limit": 5,
        "offset": 100,
        "has_more": False,
    }

    # limit reaching past the end of the collection.
    tail_page = object_records.list_collection_records(
        "items", base_dir=data_dir, roots=[], limit=100, offset=8
    )
    assert [record["id"] for record in tail_page["records"]] == ["r8", "r9"]
    assert tail_page["count"] == 2
    assert tail_page["total"] == 10
    assert tail_page["has_more"] is False


def test_list_collection_records_warm_copies_bounded_by_window(tmp_path, monkeypatch):
    """Perf guard for the pagination fix: a warm list with a small limit on
    a large collection must copy only the window's records, not every row
    in the collection. Verified precisely (not by timing, which is
    unreliable on a network share) by shadowing the module's `dict` name
    and counting calls made while servicing the list request."""
    data_dir = tmp_path / "data"
    path = data_dir / "collections" / "big" / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        handle.write("id\tname\n")
        for i in range(50_000):
            handle.write(f"r{i}\tName{i}\n")

    # Warm the cache with an initial read, outside the counted region.
    warm = object_records.read_collection_records("big", base_dir=data_dir, roots=[])
    assert len(warm) == 50_000
    cache_records = object_records._cached_records_ref("big", base_dir=data_dir)
    assert len(cache_records) == 50_000

    copy_calls: list[None] = []
    real_dict = dict

    def counting_dict(*args, **kwargs):
        copy_calls.append(None)
        return real_dict(*args, **kwargs)

    monkeypatch.setattr(object_records, "dict", counting_dict, raising=False)

    result = object_records.list_collection_records(
        "big", base_dir=data_dir, roots=[], limit=10, offset=0
    )

    assert result["total"] == 50_000
    assert result["count"] == 10
    assert len(result["records"]) == 10
    for i, record in enumerate(result["records"]):
        assert record == {"id": f"r{i}", "name": f"Name{i}"}
        # Each returned record is its own copy, never the cache's dict.
        assert record is not cache_records[i]

    # Exactly one dict() copy per windowed record -- not one per row in
    # the 50k-row collection.
    assert len(copy_calls) == 10


# --- Append-only storage (docs/append-only-storage-design.md) ---


def write_append_schema(data_dir: Path, collection: str, fields: list[dict] | None = None) -> Path:
    """Like write_schema, but opts the collection into append storage."""
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fields": fields or [{"name": "id"}], "storage": "append"})
    )
    return path


def test_op_field_is_rejected_in_user_payloads(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "widgets", "id\tname\n")

    with pytest.raises(object_records.InvalidRecordPayloadError, match="reserved"):
        object_records.create_collection_record(
            "widgets", {"id": "w1", "name": "Gadget", "_op": "del"}, base_dir=data_dir, roots=[]
        )


def test_schema_field_named_op_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="reserved"):
        object_schemas.normalize_schema("widgets", {"fields": [{"name": "_op"}]})


def test_schema_storage_key_rejects_invalid_values(tmp_path):
    with pytest.raises(ValueError, match="storage"):
        object_schemas.normalize_schema("widgets", {"fields": [], "storage": "bogus"})


def test_schema_storage_key_survives_normalization(tmp_path):
    normalized = object_schemas.normalize_schema(
        "widgets", {"fields": [{"name": "id"}], "storage": "append"}
    )
    assert normalized["storage"] == "append"

    # Absent by default -- byte-identical to a schema that never mentions
    # storage (see the classic-mode-untouched test below).
    classic = object_schemas.normalize_schema("widgets", {"fields": [{"name": "id"}]})
    assert "storage" not in classic


def test_classic_mode_is_byte_identical_to_before_append_storage_existed(tmp_path):
    """A collection with no `storage` key must write records.tsv exactly
    as it always has -- no `_op` column, no folding, no torn-tail
    tolerance. This is the opt-in guarantee: default/absent means
    unchanged behavior."""
    data_dir = tmp_path / "data"
    write_schema(data_dir, "contacts", [{"name": "id"}, {"name": "name"}])

    object_records.create_collection_record(
        "contacts", {"id": "c1", "name": "Ada"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "contacts", {"id": "c2", "name": "Grace"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "contacts", "c1", {"name": "Ada Lovelace"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("contacts", "c2", base_dir=data_dir, roots=[])

    path = data_dir / "collections" / "contacts" / "records.tsv"
    assert path.read_text() == "id\tname\nc1\tAda Lovelace\n"
    assert "_op" not in path.read_text().splitlines()[0].split("\t")


def test_append_mode_create_update_delete_use_op_column(tmp_path):
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    assert rows[0] == ["_op", "id", "name"]
    assert rows[1] == ["", "w1", "Alpha"]
    assert rows[2] == ["", "w2", "Beta"]

    object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha2"}, base_dir=data_dir, roots=[]
    )
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    # update appends a superseding row rather than rewriting in place.
    assert len(rows) == 4
    assert rows[3] == ["", "w1", "Alpha2"]

    removed = object_records.delete_collection_record("widgets", "w2", base_dir=data_dir, roots=[])
    assert removed == {"id": "w2", "name": "Beta"}
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    # Tombstone visible in the raw file, pre-compaction: obvious in a cat.
    assert rows[4] == ["del", "w2", ""]

    with pytest.raises(object_records.RecordNotFoundError):
        object_records.delete_collection_record("widgets", "w2", base_dir=data_dir, roots=[])

    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": "w1", "name": "Alpha2"}]


def test_append_mode_resurrection_after_delete(tmp_path):
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("widgets", "w1", base_dir=data_dir, roots=[])

    with pytest.raises(object_records.RecordNotFoundError):
        object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])

    resurrected = object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "reborn"}, base_dir=data_dir, roots=[]
    )
    assert resurrected == {"id": "w1", "name": "reborn"}
    assert object_records.get_collection_record(
        "widgets", "w1", base_dir=data_dir, roots=[]
    ) == {"id": "w1", "name": "reborn"}


def test_append_mode_transition_on_next_write_adds_op_column(tmp_path):
    """Switching a classic collection to storage:append doesn't touch the
    file until the collection's next write, which performs one final
    classic-shaped rewrite that adds the `_op` header column (existing
    rows get "")."""
    data_dir = tmp_path / "data"
    schema_path = write_schema(data_dir, "notes", [{"name": "id"}, {"name": "title"}])
    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "notes", {"id": "n2", "title": "two"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "notes" / "records.tsv"
    assert path.read_text() == "id\ttitle\nn1\tone\nn2\ttwo\n"

    # Opt in -- the file on disk does not change yet, only the schema does.
    schema_path.write_text(
        json.dumps({"fields": [{"name": "id"}, {"name": "title"}], "storage": "append"})
    )
    assert path.read_text() == "id\ttitle\nn1\tone\nn2\ttwo\n"

    updated = object_records.update_collection_record(
        "notes", "n1", {"title": "one-updated"}, base_dir=data_dir, roots=[]
    )
    assert updated == {"id": "n1", "title": "one-updated"}

    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    assert rows[0] == ["_op", "id", "title"]
    assert rows[1] == ["", "n1", "one-updated"]
    assert rows[2] == ["", "n2", "two"]
    assert len(rows) == 3


def test_append_mode_switch_back_compacts_to_classic_form(tmp_path):
    """Removing storage:append causes the collection's next write to fold
    current content and drop the `_op` column -- byte-compatible with a
    classic file (docs/append-only-storage-design.md Migration and
    Compatibility: "A compacted append-only file is byte-compatible with
    a classic one")."""
    data_dir = tmp_path / "data"
    schema_path = write_append_schema(data_dir, "notes", [{"name": "id"}, {"name": "title"}])
    object_records.create_collection_record(
        "notes", {"id": "n1", "title": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "notes", "n1", {"title": "one-updated"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "notes", {"id": "n2", "title": "two"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "notes" / "records.tsv"
    with path.open(newline="") as handle:
        rows_before = list(csv.reader(handle, delimiter="\t"))
    assert rows_before[0][0] == "_op"
    assert len(rows_before) == 4  # header + 3 physical rows (1 superseded)

    # Opt back out.
    schema_path.write_text(
        json.dumps({"fields": [{"name": "id"}, {"name": "title"}]})
    )
    object_records.update_collection_record(
        "notes", "n2", {"title": "two-updated"}, base_dir=data_dir, roots=[]
    )

    assert path.read_text() == "id\ttitle\nn1\tone-updated\nn2\ttwo-updated\n"


def test_append_mode_new_field_falls_back_to_full_rewrite_and_extends_header(tmp_path):
    """Append cannot extend an existing physical header: a write that
    introduces a field the current header doesn't have must fall back to
    a full rewrite that folds current content and adds the column -- the
    new value must not be lost."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "widgets" / "records.tsv"
    with path.open(newline="") as handle:
        header_before = next(csv.reader(handle, delimiter="\t"))
    assert "color" not in header_before

    updated = object_records.update_collection_record(
        "widgets", "w1", {"color": "red"}, base_dir=data_dir, roots=[]
    )
    assert updated == {"id": "w1", "name": "Alpha", "color": "red"}

    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    assert rows[0] == ["_op", "id", "name", "color"]
    assert "color" in rows[0]

    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert fetched["color"] == "red"
    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])["records"]
    assert {r["id"]: r["color"] for r in listing} == {"w1": "red", "w2": ""}


def test_compact_collection_removes_superseded_and_tombstoned_rows(tmp_path):
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha2"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha3"}, base_dir=data_dir, roots=[]
    )
    object_records.delete_collection_record("widgets", "w2", base_dir=data_dir, roots=[])

    path = data_dir / "collections" / "widgets" / "records.tsv"
    raw_before = path.read_text()
    assert "del" in raw_before  # tombstone present pre-compaction

    result = object_records.compact_collection("widgets", base_dir=data_dir, roots=[])
    assert result["rows_before"] == 5
    assert result["rows_after"] == 1
    assert result["bytes_after"] < result["bytes_before"]

    raw_after = path.read_text()
    assert "del" not in raw_after
    assert raw_after == "_op\tid\tname\n\tw1\tAlpha3\n"

    # Data is unaffected by compaction.
    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": "w1", "name": "Alpha3"}]


def test_compact_collection_is_a_reported_no_op_for_classic_collections(tmp_path):
    data_dir = tmp_path / "data"
    write_records(data_dir, "contacts", "id\tname\nc1\tAda\n")

    result = object_records.compact_collection("contacts", base_dir=data_dir, roots=[])
    assert result == {"rows_before": 1, "rows_after": 1, "bytes_before": result["bytes_before"], "bytes_after": result["bytes_before"]}
    assert (data_dir / "collections" / "contacts" / "records.tsv").read_text() == "id\tname\nc1\tAda\n"


def test_append_mode_auto_compact_triggers_on_next_write_past_threshold(tmp_path, monkeypatch):
    """When a cold parse observes physical rows past the (monkeypatched
    small) threshold, with superseded+deleted rows outnumbering live
    rows, it flags the collection -- but performs no write itself (reads
    stay read-only). The collection's NEXT write then compacts instead of
    appending."""
    monkeypatch.setenv("DBBASIC_APPEND_COMPACT_MIN_ROWS", "5")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "churn", [{"name": "id"}, {"name": "value"}])

    object_records.create_collection_record(
        "churn", {"id": "c1", "value": "v0"}, base_dir=data_dir, roots=[]
    )
    for i in range(10):
        object_records.update_collection_record(
            "churn", "c1", {"value": f"v{i}"}, base_dir=data_dir, roots=[]
        )

    path = data_dir / "collections" / "churn" / "records.tsv"
    physical_rows_before = len(path.read_text().splitlines()) - 1
    assert physical_rows_before > 5

    cache_key = str(object_records.collection_records_file("churn", base_dir=data_dir).resolve())

    # Force a cold parse (simulating cache eviction / a fresh process) --
    # a pure read.
    object_records._RECORDS_CACHE.clear()
    before_stat = path.stat()
    object_records.get_collection_record("churn", "c1", base_dir=data_dir, roots=[])
    after_stat = path.stat()
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_size == after_stat.st_size
    assert cache_key in object_records._PENDING_COMPACTION

    object_records.update_collection_record(
        "churn", "c1", {"value": "final"}, base_dir=data_dir, roots=[]
    )
    physical_rows_after = len(path.read_text().splitlines()) - 1
    assert physical_rows_after == 1
    assert cache_key not in object_records._PENDING_COMPACTION

    listing = object_records.list_collection_records("churn", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": "c1", "value": "final"}]


def test_append_mode_torn_tail_is_ignored_and_self_heals(tmp_path):
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "logs", [{"name": "id"}, {"name": "value"}])
    object_records.create_collection_record(
        "logs", {"id": "l1", "value": "one"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "logs", {"id": "l2", "value": "two"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "logs" / "records.tsv"
    full_text = path.read_text()

    # Simulate a crash mid-write: chop off the last few bytes of the file,
    # tearing the final row (no trailing newline).
    path.write_text(full_text[:-4])
    assert not path.read_text().endswith("\n")

    object_records._RECORDS_CACHE.clear()
    before_stat = path.stat()
    listing = object_records.list_collection_records("logs", base_dir=data_dir, roots=[])["records"]
    after_stat = path.stat()

    # Read succeeds, ignoring the torn fragment -- and never writes.
    assert listing == [{"id": "l1", "value": "one"}]
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_size == after_stat.st_size

    # The next write self-heals: the fragment is truncated away (not
    # resurrected), and a fresh, well-formed row is appended after it.
    object_records.create_collection_record(
        "logs", {"id": "l3", "value": "three"}, base_dir=data_dir, roots=[]
    )
    assert path.read_text().endswith("\n")

    # Subsequent reads are clean -- the fragment never reappears.
    object_records._RECORDS_CACHE.clear()
    listing_after_heal = object_records.list_collection_records(
        "logs", base_dir=data_dir, roots=[]
    )["records"]
    assert listing_after_heal == [
        {"id": "l1", "value": "one"},
        {"id": "l3", "value": "three"},
    ]


def test_append_mode_cache_refreshes_on_same_inode_append(tmp_path):
    """Unlike a classic full rewrite (always a fresh inode via atomic
    replace), a fast append mutates the SAME inode in place. The cache
    must still detect and correctly refresh on this change (only size/
    mtime move) -- verified here by asserting the inode is unchanged
    across the write while a subsequent cache-hit read returns fresh
    data, not stale data from the pre-append signature."""
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "widgets" / "records.tsv"
    inode_before = path.stat().st_ino

    object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha2"}, base_dir=data_dir, roots=[]
    )
    inode_after = path.stat().st_ino
    assert inode_after == inode_before  # same inode: a fast append, not a rewrite

    # A cache HIT (no reparse) must still return the fresh value.
    cache_key = str(path.resolve())
    signature, _, cached_records, _ = object_records._RECORDS_CACHE[cache_key]
    assert signature == object_records._stat_signature(path)
    assert cached_records == [{"id": "w1", "name": "Alpha2"}]

    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert fetched == {"id": "w1", "name": "Alpha2"}


def test_append_mode_equivalent_to_classic_mode_across_random_operations(tmp_path):
    """THE EQUIVALENCE PROPERTY. An identical, seeded, randomized sequence
    of create/update/delete/get/list operations run against a classic
    collection and an append-mode collection (same schema otherwise) must
    produce API-visible-identical results after EVERY single operation:
    same returned records, same exception types, same list contents (see
    _fold_append_rows for why order matches classic exactly), same
    totals. Deterministic seed, no wall-clock dependence -- every value
    written is derived from the loop counter, never wall-clock time.
    """
    classic_dir = tmp_path / "classic"
    append_dir = tmp_path / "append"
    write_schema(classic_dir, "items", [{"name": "id"}, {"name": "value"}])
    write_append_schema(append_dir, "items", [{"name": "id"}, {"name": "value"}])

    def apply(data_dir, kind, record_id, payload):
        try:
            if kind == "create":
                return ("ok", object_records.create_collection_record(
                    "items", {"id": record_id, "value": payload}, base_dir=data_dir, roots=[]
                ))
            if kind == "update":
                return ("ok", object_records.update_collection_record(
                    "items", record_id, {"value": payload}, base_dir=data_dir, roots=[]
                ))
            if kind == "delete":
                return ("ok", object_records.delete_collection_record(
                    "items", record_id, base_dir=data_dir, roots=[]
                ))
            if kind == "get":
                return ("ok", object_records.get_collection_record(
                    "items", record_id, base_dir=data_dir, roots=[]
                ))
            return ("ok", object_records.list_collection_records(
                "items", base_dir=data_dir, roots=[]
            ))
        except Exception as exc:  # noqa: BLE001 -- comparing exception SHAPES across both runs
            return ("error", type(exc), str(exc))

    rng = random.Random(20260718)
    id_pool = [f"e{i}" for i in range(12)]

    for step in range(200):
        kind = rng.choice(["create", "update", "delete", "get", "list"])
        record_id = rng.choice(id_pool)
        payload = f"v{step}"

        classic_outcome = apply(classic_dir, kind, record_id, payload)
        append_outcome = apply(append_dir, kind, record_id, payload)

        assert classic_outcome == append_outcome, (
            f"step {step} op={kind!r} id={record_id!r}: "
            f"classic={classic_outcome!r} append={append_outcome!r}"
        )

    # Independent final-state cross-check, beyond the per-step comparisons.
    classic_listing = object_records.list_collection_records("items", base_dir=classic_dir, roots=[])
    append_listing = object_records.list_collection_records("items", base_dir=append_dir, roots=[])
    assert classic_listing == append_listing

    # And a full compaction on the append side must not change what it
    # reports afterward, either.
    object_records.compact_collection("items", base_dir=append_dir, roots=[])
    append_listing_after_compact = object_records.list_collection_records(
        "items", base_dir=append_dir, roots=[]
    )
    assert append_listing_after_compact == classic_listing


# ---------------------------------------------------------------------------
# id -> byte-offset sidecar (docs/append-only-storage-design.md item 4).
#
# Every test below forces DBBASIC_RECORDS_CACHE_MAX_ROWS to 0 so that
# _RECORDS_CACHE never stores an entry for these (tiny, real-world-normal-
# sized) test collections -- i.e. every point op is a cache MISS, which is
# exactly the condition _fast_record_lookup uses to try the sidecar. This
# is what lets a handful of records exercise the same code path a real
# over-threshold, million-row collection would use, without the tests
# needing to actually write a million rows.
# ---------------------------------------------------------------------------


def test_oidx_sidecar_builds_lazily_and_serves_point_ops(tmp_path, monkeypatch):
    """No sidecar file exists until the first point op against an already
    append-physical file with a cold cache; from then on, get/create's
    duplicate check/update/delete all resolve correctly through it."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    assert not oidx_path.exists()
    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    # This first write is a TRANSITION-IN full rewrite (the file only
    # just became append-physical) -- nothing has read through the
    # sidecar yet, so it still doesn't exist.
    assert not oidx_path.exists()

    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    # This create's duplicate check is the first point op against an
    # already append-physical file with a cold cache -- it must have
    # built the sidecar.
    assert oidx_path.exists()

    with pytest.raises(object_records.DuplicateRecordIdError):
        object_records.create_collection_record(
            "widgets", {"id": "w1", "name": "Gamma"}, base_dir=data_dir, roots=[]
        )

    assert object_records.get_collection_record(
        "widgets", "w2", base_dir=data_dir, roots=[]
    ) == {"id": "w2", "name": "Beta"}

    updated = object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha2"}, base_dir=data_dir, roots=[]
    )
    assert updated == {"id": "w1", "name": "Alpha2"}
    assert object_records.get_collection_record(
        "widgets", "w1", base_dir=data_dir, roots=[]
    ) == {"id": "w1", "name": "Alpha2"}

    removed = object_records.delete_collection_record(
        "widgets", "w2", base_dir=data_dir, roots=[]
    )
    assert removed == {"id": "w2", "name": "Beta"}
    with pytest.raises(object_records.RecordNotFoundError):
        object_records.get_collection_record("widgets", "w2", base_dir=data_dir, roots=[])

    listing = object_records.list_collection_records(
        "widgets", base_dir=data_dir, roots=[]
    )["records"]
    assert listing == [{"id": "w1", "name": "Alpha2"}]


def test_oidx_sidecar_catches_up_when_behind(tmp_path, monkeypatch):
    """A sidecar whose body is missing its last few lines (simulating a
    crash between a data append and its matching idx append, or another
    writer that didn't maintain the sidecar at all) is caught up by
    scanning only the uncovered tail of records.tsv, not rebuilt from
    scratch."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    for i in range(5):
        object_records.create_collection_record(
            "widgets", {"id": f"w{i}", "name": f"n{i}"}, base_dir=data_dir, roots=[]
        )
    object_records._OIDX_CACHE.clear()
    object_records.get_collection_record("widgets", "w0", base_dir=data_dir, roots=[])
    assert oidx_path.exists()

    lines = oidx_path.read_text().splitlines(keepends=True)
    assert len(lines) == 6  # header + 5 data lines
    oidx_path.write_text("".join(lines[:-2]))
    object_records._OIDX_CACHE.clear()

    fetched4 = object_records.get_collection_record("widgets", "w4", base_dir=data_dir, roots=[])
    assert fetched4 == {"id": "w4", "name": "n4"}
    fetched0 = object_records.get_collection_record("widgets", "w0", base_dir=data_dir, roots=[])
    assert fetched0 == {"id": "w0", "name": "n0"}

    id_offsets, coherent = object_records._load_oidx(path)
    assert coherent
    assert set(id_offsets) == {"w0", "w1", "w2", "w3", "w4"}
    assert len(oidx_path.read_text().splitlines()) == 6


def test_oidx_sidecar_rebuilds_on_stale_ino_header(tmp_path, monkeypatch):
    """A sidecar whose header ino doesn't match the data file's current
    inode (e.g. left over from before a compaction) must never be
    trusted -- _load_oidx rebuilds from scratch instead."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )

    # Hand-write a sidecar with a bogus header ino and a body entry for
    # an id that was never actually written -- if the mismatch weren't
    # detected, this would leak straight into the answer.
    oidx_path.write_text("oidx1\t999999999\n0\t20\t\tbogus\n")
    object_records._OIDX_CACHE.clear()

    id_offsets, coherent = object_records._load_oidx(path)
    assert coherent
    assert "bogus" not in id_offsets
    assert "w1" in id_offsets
    assert object_records.get_collection_record(
        "widgets", "w1", base_dir=data_dir, roots=[]
    ) == {"id": "w1", "name": "Alpha"}


def test_oidx_sidecar_rebuilds_on_corrupt_sidecar_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()

    oidx_path.write_text("not even close to a valid sidecar\n\x00garbage\tmore\tfields\there\n")
    object_records._OIDX_CACHE.clear()

    # Never raises -- self-heals via a rebuild, and the API call still
    # returns the correct record.
    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert fetched == {"id": "w1", "name": "Alpha"}
    id_offsets, coherent = object_records._load_oidx(path)
    assert coherent
    assert set(id_offsets) == {"w1"}


def test_oidx_sidecar_rebuilds_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()

    oidx_path.unlink()
    object_records._OIDX_CACHE.clear()

    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert fetched == {"id": "w1", "name": "Alpha"}
    assert oidx_path.exists()


def test_oidx_sidecar_rebuilds_after_compaction_changes_inode(tmp_path, monkeypatch):
    """Compaction rewrites records.tsv atomically (new inode) and
    explicitly discards the sidecar (docs/append-only-storage-design.md:
    "delete or rebuild sidecar after the rewrite") -- a subsequent point
    op rebuilds correctly against the compacted content."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.update_collection_record(
        "widgets", "w1", {"name": "Alpha2"}, base_dir=data_dir, roots=[]
    )
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()
    ino_before = path.stat().st_ino

    object_records.compact_collection("widgets", base_dir=data_dir, roots=[])
    assert not oidx_path.exists()
    assert path.stat().st_ino != ino_before

    fetched = object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert fetched == {"id": "w1", "name": "Alpha2"}
    assert oidx_path.exists()


def test_oidx_sidecar_torn_idx_tail_is_dropped(tmp_path, monkeypatch):
    """A torn sidecar line (no trailing newline on its own final physical
    line, from a crash mid idx-write) is dropped, not resurrected as a
    bogus entry -- the catch-up scan of records.tsv resupplies the
    correct data for whatever the torn line was trying to record."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()

    text = oidx_path.read_text()
    assert text.endswith("\n")
    oidx_path.write_text(text[:-4])
    assert not oidx_path.read_text().endswith("\n")

    object_records._OIDX_CACHE.clear()
    id_offsets, coherent = object_records._load_oidx(path)
    assert coherent
    assert set(id_offsets) == {"w1", "w2"}
    assert object_records.get_collection_record(
        "widgets", "w2", base_dir=data_dir, roots=[]
    ) == {"id": "w2", "name": "Beta"}


def test_oidx_sidecar_survives_torn_data_tail_repair(tmp_path, monkeypatch):
    """A crash mid-write can leave records.tsv itself with a torn final
    row. The next append self-heals it (_repair_torn_tail truncates the
    fragment away before appending), which can leave a previously-built
    sidecar's covered_bytes pointing past the (now shorter, then
    re-grown) file -- _load_oidx must detect and rebuild rather than
    trust stale offsets, at every step of that sequence."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)
    object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    assert oidx_path.exists()

    full_text = path.read_text()
    path.write_text(full_text[:-3])
    assert not path.read_text().endswith("\n")

    object_records._OIDX_CACHE.clear()
    listing = object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])["records"]
    assert listing == [{"id": "w1", "name": "Alpha"}]

    object_records._OIDX_CACHE.clear()
    created = object_records.create_collection_record(
        "widgets", {"id": "w3", "name": "Gamma"}, base_dir=data_dir, roots=[]
    )
    assert created == {"id": "w3", "name": "Gamma"}
    assert path.read_text().endswith("\n")

    object_records._OIDX_CACHE.clear()
    assert object_records.get_collection_record(
        "widgets", "w3", base_dir=data_dir, roots=[]
    ) == {"id": "w3", "name": "Gamma"}
    listing_after = object_records.list_collection_records(
        "widgets", base_dir=data_dir, roots=[]
    )["records"]
    assert listing_after == [{"id": "w1", "name": "Alpha"}, {"id": "w3", "name": "Gamma"}]


def test_oidx_sidecar_resurrection_after_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_append_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Alpha"}, base_dir=data_dir, roots=[]
    )
    object_records.create_collection_record(
        "widgets", {"id": "w2", "name": "Beta"}, base_dir=data_dir, roots=[]
    )
    assert oidx_path.exists()  # built by w2's duplicate check

    object_records.delete_collection_record("widgets", "w1", base_dir=data_dir, roots=[])
    with pytest.raises(object_records.RecordNotFoundError):
        object_records.get_collection_record("widgets", "w1", base_dir=data_dir, roots=[])

    # Resurrect: creating "w1" again must succeed (not raise Duplicate),
    # resolved via the sidecar's absence-is-authoritative answer.
    resurrected = object_records.create_collection_record(
        "widgets", {"id": "w1", "name": "Gamma"}, base_dir=data_dir, roots=[]
    )
    assert resurrected == {"id": "w1", "name": "Gamma"}
    assert object_records.get_collection_record(
        "widgets", "w1", base_dir=data_dir, roots=[]
    ) == {"id": "w1", "name": "Gamma"}


def test_classic_mode_never_creates_oidx_sidecar(tmp_path, monkeypatch):
    """A classic-mode collection's records.tsv is never `_op`-columned,
    so _fast_record_lookup's sidecar branch is never reached -- confirm
    no `.records.oidx` ever appears, across create/update/delete/get/list,
    even with the cache forced cold on every single op."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    write_schema(data_dir, "widgets", [{"name": "id"}, {"name": "name"}])
    path = data_dir / "collections" / "widgets" / "records.tsv"
    oidx_path = object_records._oidx_path(path)

    for i in range(5):
        object_records.create_collection_record(
            "widgets", {"id": f"w{i}", "name": f"n{i}"}, base_dir=data_dir, roots=[]
        )
    for i in range(5):
        object_records.update_collection_record(
            "widgets", f"w{i}", {"name": f"n{i}-updated"}, base_dir=data_dir, roots=[]
        )
    object_records.delete_collection_record("widgets", "w0", base_dir=data_dir, roots=[])
    for i in range(1, 5):
        object_records.get_collection_record("widgets", f"w{i}", base_dir=data_dir, roots=[])
    object_records.list_collection_records("widgets", base_dir=data_dir, roots=[])

    assert not oidx_path.exists()
    assert str(path.resolve()) not in object_records._OIDX_CACHE


def test_append_mode_equivalent_to_classic_mode_with_sidecar_forced(tmp_path, monkeypatch):
    """Same equivalence property as
    test_append_mode_equivalent_to_classic_mode_across_random_operations,
    but with DBBASIC_RECORDS_CACHE_MAX_ROWS forced to 0 so every single
    op on both sides is a _RECORDS_CACHE miss -- meaning every append-side
    create/update/delete/get resolves through the id->offset sidecar
    (build, catch-up, or its full-rewrite fallback) for the entire 200-op
    sequence, never the warm-cache fast path."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    classic_dir = tmp_path / "classic"
    append_dir = tmp_path / "append"
    write_schema(classic_dir, "items", [{"name": "id"}, {"name": "value"}])
    write_append_schema(append_dir, "items", [{"name": "id"}, {"name": "value"}])

    def apply(data_dir, kind, record_id, payload):
        try:
            if kind == "create":
                return ("ok", object_records.create_collection_record(
                    "items", {"id": record_id, "value": payload}, base_dir=data_dir, roots=[]
                ))
            if kind == "update":
                return ("ok", object_records.update_collection_record(
                    "items", record_id, {"value": payload}, base_dir=data_dir, roots=[]
                ))
            if kind == "delete":
                return ("ok", object_records.delete_collection_record(
                    "items", record_id, base_dir=data_dir, roots=[]
                ))
            if kind == "get":
                return ("ok", object_records.get_collection_record(
                    "items", record_id, base_dir=data_dir, roots=[]
                ))
            return ("ok", object_records.list_collection_records(
                "items", base_dir=data_dir, roots=[]
            ))
        except Exception as exc:  # noqa: BLE001 -- comparing exception SHAPES across both runs
            return ("error", type(exc), str(exc))

    rng = random.Random(20260718)
    id_pool = [f"e{i}" for i in range(12)]

    for step in range(200):
        kind = rng.choice(["create", "update", "delete", "get", "list"])
        record_id = rng.choice(id_pool)
        payload = f"v{step}"

        classic_outcome = apply(classic_dir, kind, record_id, payload)
        append_outcome = apply(append_dir, kind, record_id, payload)

        assert classic_outcome == append_outcome, (
            f"step {step} op={kind!r} id={record_id!r}: "
            f"classic={classic_outcome!r} append={append_outcome!r}"
        )

    classic_listing = object_records.list_collection_records("items", base_dir=classic_dir, roots=[])
    append_listing = object_records.list_collection_records("items", base_dir=append_dir, roots=[])
    assert classic_listing == append_listing
