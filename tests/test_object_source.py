import pytest

import object_source
import object_versions


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_get_object_source_reads_existing_object(tmp_path):
    root = tmp_path / "objects"
    write_source(root / "basics" / "counter.py", "def GET(request):\n    return {'count': 1}\n")

    source = object_source.get_object_source("basics_counter", roots=[root])

    assert source == "def GET(request):\n    return {'count': 1}\n"


def test_update_object_source_writes_file_and_saves_version(tmp_path):
    root = tmp_path / "objects"
    source_path = write_source(root / "users" / "42" / "deals.py", "old code\n")
    manager = object_versions.VersionManager(tmp_path / "data")

    version_id = object_source.update_object_source(
        "u_42_deals",
        "new code\n",
        author="alice",
        message="Update deals",
        roots=[root],
        version_manager=manager,
    )

    assert version_id == 1
    assert source_path.read_text() == "new code\n"
    saved = manager.get_version("u_42_deals", version_id=1)
    assert saved is not None
    assert saved["content"] == "new code\n"
    assert saved["author"] == "alice"
    assert saved["message"] == "Update deals"


def test_update_object_source_appends_versions(tmp_path):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "v0\n")
    manager = object_versions.VersionManager(tmp_path / "data")

    v1 = object_source.update_object_source(
        "basics_counter",
        "v1\n",
        author="api",
        message="first",
        roots=[root],
        version_manager=manager,
    )
    v2 = object_source.update_object_source(
        "basics_counter",
        "v2\n",
        author="api",
        message="second",
        roots=[root],
        version_manager=manager,
    )

    assert [v1, v2] == [1, 2]
    assert source_path.read_text() == "v2\n"
    assert [row["version_id"] for row in manager.get_history("basics_counter")] == [2, 1]
    assert (tmp_path / "data" / "versions" / "basics_counter" / "metadata.tsv").exists()
    assert (tmp_path / "data" / "versions" / "basics_counter" / "v1.txt").read_text() == "v1\n"
    assert (tmp_path / "data" / "versions" / "basics_counter" / "v2.txt").read_text() == "v2\n"


def test_rollback_object_source_writes_old_content_as_new_latest_version(tmp_path):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "v0\n")
    manager = object_versions.VersionManager(tmp_path / "data")
    object_source.update_object_source(
        "basics_counter",
        "v1\n",
        author="api",
        message="first",
        roots=[root],
        version_manager=manager,
    )
    object_source.update_object_source(
        "basics_counter",
        "v2\n",
        author="api",
        message="second",
        roots=[root],
        version_manager=manager,
    )

    new_version_id = object_source.rollback_object_source(
        "basics_counter",
        to_version=1,
        author="rollback",
        message="Rollback to v1",
        roots=[root],
        version_manager=manager,
    )

    assert new_version_id == 3
    assert source_path.read_text() == "v1\n"
    latest = manager.get_version("basics_counter")
    assert latest is not None
    assert latest["version_id"] == 3
    assert latest["content"] == "v1\n"
    assert latest["author"] == "rollback"
    assert [row["version_id"] for row in manager.get_history("basics_counter")] == [3, 2, 1]


def test_rollback_missing_version_raises_and_leaves_source_unchanged(tmp_path):
    root = tmp_path / "objects"
    source_path = write_source(root / "basics" / "counter.py", "current\n")
    manager = object_versions.VersionManager(tmp_path / "data")
    manager.save_version("basics_counter", "v1\n", "api", "first")

    with pytest.raises(object_versions.VersionNotFoundError):
        object_source.rollback_object_source(
            "basics_counter",
            to_version=99,
            author="rollback",
            message="bad rollback",
            roots=[root],
            version_manager=manager,
        )

    assert source_path.read_text() == "current\n"


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
def test_invalid_object_ids_are_rejected(tmp_path, object_id):
    root = tmp_path / "objects"
    manager = object_versions.VersionManager(tmp_path / "data")

    with pytest.raises(object_versions.InvalidObjectIdError):
        object_source.get_object_source(object_id, roots=[root])

    with pytest.raises(object_versions.InvalidObjectIdError):
        object_source.update_object_source(
            object_id,
            "code",
            author="api",
            message="bad",
            roots=[root],
            version_manager=manager,
        )


def test_missing_object_source_raises(tmp_path):
    root = tmp_path / "objects"
    manager = object_versions.VersionManager(tmp_path / "data")

    with pytest.raises(object_source.ObjectSourceNotFoundError):
        object_source.get_object_source("missing_object", roots=[root])

    with pytest.raises(object_source.ObjectSourceNotFoundError):
        object_source.update_object_source(
            "missing_object",
            "code",
            author="api",
            message="missing",
            roots=[root],
            version_manager=manager,
        )
