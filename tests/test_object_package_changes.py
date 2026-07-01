import json
from uuid import UUID

import pytest

import object_package_changes
import object_packages


def test_append_package_change_writes_jsonl_and_lists_newest_first(tmp_path):
    data_dir = tmp_path / "data"

    first = object_package_changes.append_package_change(
        package_id="hello-world",
        action="dry_run",
        package_version="0.1.0",
        actor="admin",
        details={"safe_to_install": True, "objects": {"create": 1}},
        base_dir=data_dir,
    )
    second = object_package_changes.append_package_change(
        package_id="hello-world",
        action="install_requested",
        package_version="0.1.0",
        actor="admin",
        base_dir=data_dir,
    )

    path = data_dir / "package_changes" / "hello-world" / "changes.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]

    assert rows == [first, second]
    assert first["message"] == "Dry run package install"
    assert first["details"] == {"safe_to_install": True, "objects": {"create": 1}}
    assert UUID(first["change_id"]).version == 4
    assert UUID(second["change_id"]).version == 4
    assert "+" not in first["change_id"]
    assert ":" not in first["change_id"]
    assert "." not in first["change_id"]

    payload = object_package_changes.list_package_changes("hello-world", base_dir=data_dir)

    assert payload["package_id"] == "hello-world"
    assert payload["count"] == 2
    assert payload["total"] == 2
    assert [change["action"] for change in payload["changes"]] == [
        "install_requested",
        "dry_run",
    ]


def test_list_package_changes_paginates(tmp_path):
    data_dir = tmp_path / "data"
    for action in ("dry_run", "install_requested", "failed"):
        object_package_changes.append_package_change(
            package_id="hello-world",
            action=action,
            package_version="0.1.0",
            base_dir=data_dir,
        )

    payload = object_package_changes.list_package_changes(
        "hello-world",
        base_dir=data_dir,
        limit=1,
        offset=1,
    )

    assert payload["count"] == 1
    assert payload["total"] == 3
    assert payload["has_more"] is True
    assert payload["changes"][0]["action"] == "install_requested"


def test_get_package_change_returns_one_entry_by_id(tmp_path):
    data_dir = tmp_path / "data"
    first = object_package_changes.append_package_change(
        package_id="hello-world",
        action="restore_requested",
        package_version="0.1.0",
        base_dir=data_dir,
    )
    object_package_changes.append_package_change(
        package_id="hello-world",
        action="rolled_back",
        package_version="0.1.0",
        base_dir=data_dir,
    )

    assert (
        object_package_changes.get_package_change(
            "hello-world",
            first["change_id"],
            base_dir=data_dir,
        )
        == first
    )
    assert object_package_changes.get_package_change(
        "hello-world",
        "missing-change",
        base_dir=data_dir,
    ) is None


def test_package_changes_return_empty_for_valid_package_without_log(tmp_path):
    payload = object_package_changes.list_package_changes(
        "hello-world",
        base_dir=tmp_path / "data",
    )

    assert payload["changes"] == []
    assert payload["count"] == 0
    assert payload["total"] == 0


def test_package_changes_reject_invalid_names_actions_and_details(tmp_path):
    with pytest.raises(object_packages.InvalidPackageIdError):
        object_package_changes.list_package_changes("../bad", base_dir=tmp_path / "data")

    with pytest.raises(object_package_changes.InvalidPackageChangeError):
        object_package_changes.append_package_change(
            package_id="hello-world",
            action="publish",
            base_dir=tmp_path / "data",
        )

    with pytest.raises(object_package_changes.InvalidPackageChangeError):
        object_package_changes.append_package_change(
            package_id="hello-world",
            action="dry_run",
            details=[],
            base_dir=tmp_path / "data",
        )


def test_package_changes_ignore_corrupt_lines(tmp_path):
    data_dir = tmp_path / "data"
    path = data_dir / "package_changes" / "hello-world" / "changes.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not-json\n"
        '{"action":"dry_run","package_id":"hello-world"}\n'
        "[]\n"
    )

    payload = object_package_changes.list_package_changes("hello-world", base_dir=data_dir)

    assert payload["changes"] == [{"action": "dry_run", "package_id": "hello-world"}]


def test_dry_run_change_details_compacts_plan():
    details = object_package_changes.dry_run_change_details(
        {
            "package": {"id": "hello-world", "name": "Hello World", "version": "0.1.0"},
            "safe_to_install": True,
            "install_enabled": False,
            "objects": [{"action": "create"}, {"action": "replace"}],
            "schemas": [{"action": "create"}],
            "permissions": [],
            "seed": [{"action": "merge"}],
            "migrations": [{"action": "apply"}, {"action": "apply"}],
            "warnings": ["review first"],
        }
    )

    assert details == {
        "package": {"id": "hello-world", "name": "Hello World", "version": "0.1.0"},
        "safe_to_install": True,
        "install_enabled": False,
        "objects": {"create": 1, "replace": 1},
        "schemas": {"create": 1},
        "permissions": {},
        "seed": {"merge": 1},
        "migrations": {"apply": 2},
        "warnings": ["review first"],
    }
