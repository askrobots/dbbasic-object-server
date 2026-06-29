from pathlib import Path

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


def write_schema(data_dir: Path, collection: str) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"fields": [{"name": "id"}]}')
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
