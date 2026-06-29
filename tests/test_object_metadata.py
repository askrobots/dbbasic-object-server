import pytest

import object_metadata
import object_source
import object_versions


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_get_object_metadata_summarizes_existing_object(tmp_path):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    source = write_file(root / "basics" / "counter.py", "def GET(request):\n    return {}\n")
    write_file(
        data_dir / "state" / "basics_counter" / "state.tsv",
        "key\tvalue\ttimestamp\ncount\t3\t1710000000.1\nname\tcounter\t1710000000.2\n",
    )
    write_file(
        data_dir / "logs" / "basics_counter" / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "a1\t2026-01-01T00:00:00\tINFO\tstarted\n"
        "a2\t2026-01-01T00:00:01\tERROR\tboom\n",
    )
    manager = object_versions.VersionManager(data_dir)
    manager.save_version("basics_counter", "v1\n", author="test", message="first")
    manager.save_version("basics_counter", "v2\n", author="test", message="second")

    metadata = object_metadata.get_object_metadata(
        "basics_counter",
        base_dir=data_dir,
        roots=[root],
    )

    assert metadata == {
        "object_id": "basics_counter",
        "source_path": "basics/counter.py",
        "owner": "system",
        "kind": "system",
        "last_modified": source.stat().st_mtime,
        "state_count": 2,
        "state_keys": ["count", "name"],
        "log_count": 2,
        "file_count": 0,
        "version_count": 2,
    }


def test_get_object_metadata_reports_user_owner(tmp_path):
    root = tmp_path / "objects"
    write_file(root / "users" / "42" / "deals.py", "def GET(request):\n    return {}\n")

    metadata = object_metadata.get_object_metadata(
        "u_42_deals",
        base_dir=tmp_path / "data",
        roots=[root],
    )

    assert metadata["source_path"] == "users/42/deals.py"
    assert metadata["owner"] == "42"
    assert metadata["kind"] == "user"


@pytest.mark.parametrize(
    "object_id",
    [
        "",
        "../outside",
        "basics/counter",
        "basics.counter",
        "object id",
        "object@station",
        "object;drop",
        "object\x00id",
        "a" * 65,
    ],
)
def test_get_object_metadata_rejects_invalid_object_ids(tmp_path, object_id):
    with pytest.raises(object_versions.InvalidObjectIdError):
        object_metadata.get_object_metadata(object_id, base_dir=tmp_path / "data")


def test_get_object_metadata_raises_for_missing_object(tmp_path):
    with pytest.raises(object_source.ObjectSourceNotFoundError):
        object_metadata.get_object_metadata(
            "missing_object",
            base_dir=tmp_path / "data",
            roots=[tmp_path / "objects"],
        )
