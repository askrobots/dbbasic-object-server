import hashlib

import pytest

import object_versions


def test_save_first_version_creates_metadata_and_content_file(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")

    version_id = manager.save_version(
        object_id="basics_counter",
        content="content v1",
        author="system",
        message="Initial version",
    )

    assert version_id == 1
    version_dir = tmp_path / "data" / "versions" / "basics_counter"
    metadata = version_dir / "metadata.tsv"
    content = version_dir / "v1.txt"

    assert metadata.exists()
    assert content.read_text() == "content v1"

    lines = metadata.read_text().strip().split("\n")
    assert lines[0] == "version_id\ttimestamp\tauthor\tmessage\thash\tcorrelation_id"
    assert lines[1].startswith("1\t")
    assert "\tsystem\tInitial version\t" in lines[1]


def test_save_version_records_correlation_id(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

    manager.save_version(
        "basics_counter",
        "content v1",
        "system",
        "Initial version",
        correlation_id=correlation_id,
    )

    version = manager.get_version("basics_counter", version_id=1)
    history = manager.get_history("basics_counter")

    assert version is not None
    assert version["correlation_id"] == correlation_id
    assert history[0]["correlation_id"] == correlation_id


def test_save_multiple_versions_increment_ids(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")

    v1 = manager.save_version("basics_counter", "content v1", "user", "v1")
    v2 = manager.save_version("basics_counter", "content v2", "user", "v2")
    v3 = manager.save_version("basics_counter", "content v3", "user", "v3")

    assert [v1, v2, v3] == [1, 2, 3]


def test_versions_are_independent_per_object(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")

    obj1_v1 = manager.save_version("object_one", "one v1", "user", "one")
    obj2_v1 = manager.save_version("object_two", "two v1", "user", "two")
    obj1_v2 = manager.save_version("object_one", "one v2", "user", "one again")

    assert obj1_v1 == 1
    assert obj2_v1 == 1
    assert obj1_v2 == 2


def test_get_specific_and_latest_version(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")

    manager.save_version("basics_counter", "content v1", "alice", "first")
    version_id = manager.save_version("basics_counter", "content v2", "bob", "second")

    latest = manager.get_version("basics_counter")
    specific = manager.get_version("basics_counter", version_id=1)

    assert latest is not None
    assert latest["version_id"] == version_id
    assert latest["content"] == "content v2"
    assert latest["author"] == "bob"
    assert specific is not None
    assert specific["version_id"] == 1
    assert specific["content"] == "content v1"
    assert specific["author"] == "alice"


def test_get_version_returns_none_for_missing_object_or_version(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    manager.save_version("basics_counter", "content", "user", "message")

    assert manager.get_version("missing_object") is None
    assert manager.get_version("basics_counter", version_id=999) is None


def test_history_is_newest_first_and_excludes_content(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    for i in range(1, 6):
        manager.save_version("basics_counter", f"content v{i}", f"user{i}", f"Version {i}")

    history = manager.get_history("basics_counter", limit=2, offset=1)

    assert [row["version_id"] for row in history] == [4, 3]
    assert all("content" not in row for row in history)


def test_hash_matches_saved_content(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    manager.save_version("basics_counter", "content v1", "user", "message")

    version = manager.get_version("basics_counter", version_id=1)

    assert version is not None
    assert version["hash"] == hashlib.sha256("content v1".encode()).hexdigest()


def test_rollback_creates_new_version_with_old_content(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    manager.save_version("basics_counter", "content v1", "user", "v1")
    manager.save_version("basics_counter", "content v2", "user", "v2")
    manager.save_version("basics_counter", "content v3", "user", "v3")

    new_version = manager.rollback(
        "basics_counter",
        to_version=1,
        author="rollback_user",
        message="Rollback to v1",
    )

    assert new_version == 4
    latest = manager.get_version("basics_counter")
    assert latest is not None
    assert latest["version_id"] == 4
    assert latest["content"] == "content v1"
    assert latest["author"] == "rollback_user"
    assert latest["message"] == "Rollback to v1"
    assert [row["version_id"] for row in manager.get_history("basics_counter")] == [4, 3, 2, 1]


def test_rollback_missing_version_raises(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    manager.save_version("basics_counter", "content", "user", "message")

    with pytest.raises(object_versions.VersionNotFoundError):
        manager.rollback("basics_counter", to_version=99, author="user", message="bad rollback")


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
    manager = object_versions.VersionManager(tmp_path / "data")

    with pytest.raises(object_versions.InvalidObjectIdError):
        manager.save_version(object_id, "content", "user", "message")

    assert not (tmp_path / "data" / "outside").exists()


def test_missing_metadata_returns_empty_history(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")

    assert manager.get_history("basics_counter") == []
    assert manager.get_version("basics_counter") is None


def test_malformed_metadata_rows_are_ignored(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    version_dir = tmp_path / "data" / "versions" / "basics_counter"
    version_dir.mkdir(parents=True)
    (version_dir / "metadata.tsv").write_text(
        "version_id\ttimestamp\tauthor\tmessage\thash\n"
        "bad\t2026-01-01T00:00:00\tuser\tbad row\tbad-hash\n"
        "1\t2026-01-01T00:00:01\tuser\tgood row\tgood-hash\n"
    )
    (version_dir / "v1.txt").write_text("good content")

    history = manager.get_history("basics_counter")
    version = manager.get_version("basics_counter", version_id=1)

    assert [row["version_id"] for row in history] == [1]
    assert version is not None
    assert version["content"] == "good content"


def test_legacy_metadata_without_correlation_id_still_reads(tmp_path):
    manager = object_versions.VersionManager(tmp_path / "data")
    version_dir = tmp_path / "data" / "versions" / "basics_counter"
    version_dir.mkdir(parents=True)
    (version_dir / "metadata.tsv").write_text(
        "version_id\ttimestamp\tauthor\tmessage\thash\n"
        "1\t2026-01-01T00:00:01\tuser\tlegacy row\tlegacy-hash\n"
    )
    (version_dir / "v1.txt").write_text("legacy content")

    history = manager.get_history("basics_counter")
    version = manager.get_version("basics_counter", version_id=1)

    assert history == [
        {
            "version_id": 1,
            "timestamp": "2026-01-01T00:00:01",
            "author": "user",
            "message": "legacy row",
            "hash": "legacy-hash",
        }
    ]
    assert version is not None
    assert version["content"] == "legacy content"
    assert "correlation_id" not in version
