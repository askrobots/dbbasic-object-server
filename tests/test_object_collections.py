from pathlib import Path

import pytest

import object_collections
import object_logs
import object_permission_store
import object_permissions


def write_source(path: Path, content: str = "def GET(request):\n    return {}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_state(data_dir: Path, object_id: str) -> None:
    state_file = data_dir / "state" / object_id / "state.tsv"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("count\t2\n")


def write_file(data_dir: Path, object_id: str, filename: str, content: bytes = b"hello") -> None:
    file_path = data_dir / "files" / object_id / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(content)


def write_records(data_dir: Path, collection: str, content: str = "id\tname\nr1\tAda\n") -> None:
    records_file = data_dir / "collections" / collection / "records.tsv"
    records_file.parent.mkdir(parents=True, exist_ok=True)
    records_file.write_text(content)


def save_policy(data_dir: Path, policy: object_permissions.PermissionPolicy) -> None:
    object_permission_store.save_policy(policy, base_dir=data_dir)


def by_name(collections: list[dict]) -> dict[str, dict]:
    return {item["name"]: item for item in collections}


def test_list_collections_derives_summaries_from_sources_state_logs_files_and_policy(tmp_path):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "site" / "home.py")
    write_source(root / "site" / "about.py")
    write_source(root / "users" / "42" / "deals.py")
    write_source(root / "users" / "42" / "deals_report.py")
    write_state(data_dir, "site_home")
    object_logs.append_object_log("site_home", "INFO", "served", base_dir=data_dir)
    write_file(data_dir, "site_home", "card.txt")
    write_records(data_dir, "site")
    save_policy(
        data_dir,
        object_permissions.PermissionPolicy(
            access_mode="role_based",
            rules=(
                object_permissions.PermissionRule.allow(
                    "role:admin",
                    {object_permissions.READ, object_permissions.EXECUTE},
                    collection="site",
                ),
                object_permissions.PermissionRule.deny(
                    "public",
                    {object_permissions.UPDATE},
                    collection="site",
                ),
                object_permissions.PermissionRule.allow(
                    "subscription:pro",
                    {object_permissions.READ},
                    collection="deals",
                ),
            ),
        ),
    )

    collections = by_name(
        object_collections.list_collections(base_dir=data_dir, roots=[root])
    )

    assert sorted(collections) == ["deals", "site"]
    assert collections["site"] == {
        "name": "site",
        "object_count": 2,
        "file_count": 1,
        "state_object_count": 1,
        "log_object_count": 1,
        "has_records": True,
        "owners": ["system"],
        "kinds": {"system": 2},
        "permission": {
            "access_mode": "role_based",
            "rule_count": 2,
            "allow_count": 1,
            "deny_count": 1,
            "actions": ["execute", "read", "update"],
            "principals": ["public", "role:admin"],
        },
    }
    assert collections["deals"]["object_count"] == 2
    assert collections["deals"]["owners"] == ["42"]
    assert collections["deals"]["kinds"] == {"user": 2}
    assert collections["deals"]["permission"]["principals"] == ["subscription:pro"]


def test_get_collection_includes_object_details(tmp_path):
    root = tmp_path / "objects"
    data_dir = tmp_path / "data"
    write_source(root / "apps" / "widget_counter.py")
    write_state(data_dir, "apps_widget_counter")
    write_file(data_dir, "apps_widget_counter", "nested/widget.json", b"{}")

    collection = object_collections.get_collection("apps", base_dir=data_dir, roots=[root])

    assert collection["name"] == "apps"
    assert collection["object_count"] == 1
    assert collection["has_records"] is False
    assert collection["objects"] == [
        {
            "object_id": "apps_widget_counter",
            "path": "apps/widget_counter.py",
            "owner": "system",
            "kind": "system",
            "state_count": 1,
            "has_logs": False,
            "file_count": 1,
        }
    ]


def test_permission_only_collection_is_listed(tmp_path):
    data_dir = tmp_path / "data"
    root = tmp_path / "objects"
    save_policy(
        data_dir,
        object_permissions.PermissionPolicy(
            rules=(
                object_permissions.PermissionRule.allow(
                    "role:billing",
                    {object_permissions.READ},
                    collection="invoices",
                ),
            ),
        ),
    )

    collections = object_collections.list_collections(base_dir=data_dir, roots=[root])

    assert collections == [
        {
            "name": "invoices",
            "object_count": 0,
            "file_count": 0,
            "state_object_count": 0,
            "log_object_count": 0,
            "has_records": False,
            "owners": [],
            "kinds": {},
            "permission": {
                "access_mode": "role_based",
                "rule_count": 1,
                "allow_count": 1,
                "deny_count": 0,
                "actions": ["read"],
                "principals": ["role:billing"],
            },
        }
    ]


def test_record_only_collection_is_listed(tmp_path):
    data_dir = tmp_path / "data"
    root = tmp_path / "objects"
    write_records(data_dir, "contacts")

    collections = object_collections.list_collections(base_dir=data_dir, roots=[root])

    assert collections == [
        {
            "name": "contacts",
            "object_count": 0,
            "file_count": 0,
            "state_object_count": 0,
            "log_object_count": 0,
            "has_records": True,
            "owners": [],
            "kinds": {},
            "permission": {
                "access_mode": "role_based",
                "rule_count": 0,
                "allow_count": 0,
                "deny_count": 0,
                "actions": [],
                "principals": [],
            },
        }
    ]


def test_get_collection_rejects_unsafe_names(tmp_path):
    with pytest.raises(object_collections.InvalidCollectionNameError):
        object_collections.get_collection("../bad", base_dir=tmp_path / "data", roots=[])


def test_get_collection_rejects_missing_collections(tmp_path):
    with pytest.raises(object_collections.CollectionNotFoundError):
        object_collections.get_collection("missing", base_dir=tmp_path / "data", roots=[])
