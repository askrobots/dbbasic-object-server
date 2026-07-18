import csv
import json
from pathlib import Path
from uuid import UUID

import pytest

import object_collections
import object_records


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
