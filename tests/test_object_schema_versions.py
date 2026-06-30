import json

import pytest

import object_schema_versions


def schema_json(name="contacts", fields=None):
    return object_schema_versions.schema_version_content(
        {
            "name": name,
            "title": name.title(),
            "source": "manual",
            "version": 1,
            "fields": fields or [],
            "field_count": len(fields or []),
        }
    )


def test_schema_versions_save_and_read_latest(tmp_path):
    manager = object_schema_versions.SchemaVersionManager(tmp_path / "data")

    version_id = manager.save_version(
        "contacts",
        schema_json(fields=[{"name": "email", "type": "email", "required": False}]),
        author="admin",
        message="Add email",
    )

    saved = manager.get_version("contacts")

    assert version_id == 1
    assert saved is not None
    assert saved["version_id"] == 1
    assert saved["author"] == "admin"
    assert saved["message"] == "Add email"
    assert json.loads(saved["content"])["fields"][0]["name"] == "email"


def test_schema_versions_history_is_newest_first_without_content(tmp_path):
    manager = object_schema_versions.SchemaVersionManager(tmp_path / "data")

    manager.save_version("contacts", schema_json(fields=[{"name": "email"}]), "admin", "first")
    manager.save_version("contacts", schema_json(fields=[{"name": "phone"}]), "admin", "second")

    history = manager.get_history("contacts")
    limited = manager.get_history("contacts", limit=1)

    assert [version["version_id"] for version in history] == [2, 1]
    assert [version["message"] for version in history] == ["second", "first"]
    assert all("content" not in version for version in history)
    assert [version["version_id"] for version in limited] == [2]


def test_schema_versions_rollback_creates_new_version(tmp_path):
    manager = object_schema_versions.SchemaVersionManager(tmp_path / "data")
    first = schema_json(fields=[{"name": "email"}])
    second = schema_json(fields=[{"name": "phone"}])

    manager.save_version("contacts", first, "admin", "first")
    manager.save_version("contacts", second, "admin", "second")
    new_version_id = manager.rollback("contacts", 1, "admin", "rollback")

    latest = manager.get_version("contacts")

    assert new_version_id == 3
    assert latest is not None
    assert latest["version_id"] == 3
    assert latest["message"] == "rollback"
    assert latest["content"] == first


def test_schema_versions_reject_invalid_schema_name(tmp_path):
    manager = object_schema_versions.SchemaVersionManager(tmp_path / "data")

    with pytest.raises(object_schema_versions.InvalidSchemaNameError):
        manager.save_version("bad.name", schema_json(), "admin", "bad")


def test_schema_versions_raise_for_missing_rollback_target(tmp_path):
    manager = object_schema_versions.SchemaVersionManager(tmp_path / "data")

    with pytest.raises(object_schema_versions.SchemaVersionNotFoundError):
        manager.rollback("contacts", 99, "admin", "missing")
