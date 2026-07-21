"""03 external connectors: the generic reconcile engine (sync lifecycle,
retry/backoff planning, dynamic loading), manifest discovery, and the daemon
pass end to end against a real (fake) connector package.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import object_connectors
import object_daemon
import object_packages
import object_records

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


# ---- selection + planning (pure) ------------------------------------------

def test_is_due_selects_active_statuses_and_respects_backoff():
    assert object_connectors.is_due({"sync_status": "pending"}, NOW)
    assert object_connectors.is_due({"sync_status": ""}, NOW)                  # unset == pending
    assert object_connectors.is_due({"sync_status": "pending_delete"}, NOW)
    assert not object_connectors.is_due({"sync_status": "synced"}, NOW)
    assert not object_connectors.is_due({"sync_status": "deleted"}, NOW)
    assert not object_connectors.is_due({"sync_status": "dead"}, NOW)
    # backoff gate
    assert not object_connectors.is_due({"sync_status": "pending", "sync_next_at": "2026-07-21T12:01:00Z"}, NOW)
    assert object_connectors.is_due({"sync_status": "pending", "sync_next_at": "2026-07-21T11:59:00Z"}, NOW)


def test_backoff_exponential_capped():
    cfg = object_connectors.ConnectorConfig(retry_base=60, retry_max=3600)
    assert object_connectors.backoff_seconds(1, cfg) == 60
    assert object_connectors.backoff_seconds(3, cfg) == 240
    assert object_connectors.backoff_seconds(99, cfg) == 3600


def test_plan_sync_success_create_and_delete():
    cfg = object_connectors.ConnectorConfig()
    synced = object_connectors.plan_sync({"sync_status": "pending"}, {"ok": True}, cfg, now=NOW)
    assert synced["sync_status"] == "synced" and synced["sync_attempts"] == "0" and synced["sync_error"] == ""
    # a successful reconcile of a delete tombstones
    deleted = object_connectors.plan_sync({"sync_status": "pending_delete"}, {"ok": True}, cfg, now=NOW)
    assert deleted["sync_status"] == "deleted"


def test_plan_sync_transient_keeps_direction_and_backs_off():
    cfg = object_connectors.ConnectorConfig(max_attempts=5)
    out = object_connectors.plan_sync(
        {"sync_status": "pending_delete", "sync_attempts": "0"},
        {"ok": False, "error": "network"}, cfg, now=NOW)
    assert out["sync_status"] == "pending_delete"  # still a delete target on retry
    assert out["sync_attempts"] == "1" and out["sync_error"] == "network"
    assert out["sync_next_at"] > "2026-07-21T12:00"


def test_plan_sync_permanent_and_exhausted_go_dead():
    cfg = object_connectors.ConnectorConfig(max_attempts=3)
    perm = object_connectors.plan_sync({"sync_status": "pending"}, {"ok": False, "permanent": True, "error": "bad"}, cfg, now=NOW)
    assert perm["sync_status"] == "dead"
    exhausted = object_connectors.plan_sync({"sync_status": "pending", "sync_attempts": "2"}, {"ok": False, "error": "x"}, cfg, now=NOW)
    assert exhausted["sync_status"] == "dead" and exhausted["sync_attempts"] == "3"


# ---- dynamic loader -------------------------------------------------------

def test_load_connector_loads_entry_and_rejects_bad(tmp_path):
    mod = tmp_path / "c.py"
    mod.write_text("def reconcile(record, *, base_dir):\n    return {'ok': True, 'seen': record['id']}\n")
    fn = object_connectors.load_connector(mod, "reconcile")
    assert fn({"id": "x"}, base_dir="/nope") == {"ok": True, "seen": "x"}

    try:
        object_connectors.load_connector(mod, "missing_entry")
        assert False, "expected ConnectorLoadError"
    except object_connectors.ConnectorLoadError:
        pass
    try:
        object_connectors.load_connector(tmp_path / "nope.py")
        assert False, "expected ConnectorLoadError"
    except object_connectors.ConnectorLoadError:
        pass


# ---- manifest discovery ---------------------------------------------------

RECONCILE_SRC = """
def reconcile(record, *, base_dir):
    name = record.get("name")
    if name == "boom":
        return {"ok": False, "error": "temporary boom"}
    if name == "fatal":
        return {"ok": False, "error": "bad request", "permanent": True}
    return {"ok": True}
"""


def _make_pkg(root, pkg_id, *, collection, reconcile_src=RECONCILE_SRC, module="connectors/fake.py"):
    d = root / pkg_id
    (d / "connectors").mkdir(parents=True)
    (d / "schemas").mkdir(parents=True)
    (d / module).write_text(reconcile_src)
    (d / "schemas" / f"{collection}.json").write_text(json.dumps({
        "name": collection, "title": collection, "version": 1,
        "fields": [
            {"name": "id"}, {"name": "owner_id", "type": "text"}, {"name": "name", "type": "text"},
            {"name": "sync_status", "type": "text"}, {"name": "sync_attempts", "type": "number"},
            {"name": "sync_error", "type": "text"}, {"name": "sync_next_at", "type": "datetime"},
            {"name": "created_at", "type": "datetime", "read_only": True},
            {"name": "updated_at", "type": "datetime", "read_only": True},
        ],
    }))
    (d / "dbbasic-package.json").write_text(json.dumps({
        "id": pkg_id, "name": pkg_id, "version": "0.1.0",
        "compatibility": {"dbbasic_object_server": ">=0.1.0"},
        "objects": [], "schemas": [{"collection": collection, "path": f"schemas/{collection}.json"}],
        "permissions": [], "seed": [], "migrations": [],
        "connectors": [{"collection": collection, "module": module, "entry": "reconcile"}],
    }))
    return d


def test_manifest_normalizes_connectors(tmp_path):
    root = tmp_path / "packages"; root.mkdir()
    _make_pkg(root, "app-fake", collection="widgets")
    package = object_packages.get_package("app-fake", root=root)
    assert package["connectors"] == [{"collection": "widgets", "module": "connectors/fake.py", "entry": "reconcile"}]
    assert object_packages._package_summary(package)["connector_count"] == 1


def test_iter_connectors_resolves_module_and_skips_bad(tmp_path):
    root = tmp_path / "packages"; root.mkdir()
    _make_pkg(root, "app-fake", collection="widgets")
    decls = object_packages.iter_connectors(root=root)
    assert len(decls) == 1
    decl = decls[0]
    assert decl["package_id"] == "app-fake" and decl["collection"] == "widgets"
    assert Path(decl["module"]).is_file() and decl["module"].endswith("connectors/fake.py")

    # a declaration whose module file is missing is skipped, not raised
    bad = _make_pkg(root, "app-broken", collection="gadgets")
    (bad / "connectors" / "fake.py").unlink()
    ids = {d["package_id"] for d in object_packages.iter_connectors(root=root)}
    assert "app-broken" not in ids and "app-fake" in ids


# ---- daemon pass end to end ----------------------------------------------

def test_process_connectors_reconciles_each_outcome(tmp_path, monkeypatch):
    pkg_root = tmp_path / "packages"; pkg_root.mkdir()
    _make_pkg(pkg_root, "app-fake", collection="widgets")
    data_dir = tmp_path / "data"
    obj = tmp_path / "objects"; obj.mkdir()
    object_packages.install_package("app-fake", root=pkg_root, base_dir=data_dir,
                                    object_roots=[obj], allow_replace=True)

    for name in ("ok-one", "boom", "fatal"):
        object_records.create_collection_record(
            "widgets", {"owner_id": "1", "name": name, "sync_status": "pending"},
            base_dir=data_dir, actor="t")
    object_records.create_collection_record(
        "widgets", {"owner_id": "1", "name": "gone", "sync_status": "pending_delete"},
        base_dir=data_dir, actor="t")

    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(pkg_root))
    monkeypatch.setenv("DBBASIC_PRIVATE_PACKAGES_DIR", str(tmp_path / "no-private"))

    result = object_daemon.process_connectors(base_dir=data_dir)
    assert result == {"reconciled": 4, "synced": 2, "dead": 1}

    rows = {r["name"]: r for r in object_records.read_collection_records("widgets", base_dir=data_dir)}
    assert rows["ok-one"]["sync_status"] == "synced"
    assert rows["gone"]["sync_status"] == "deleted"
    assert rows["fatal"]["sync_status"] == "dead"
    assert rows["boom"]["sync_status"] == "pending"        # transient -> stays a target
    assert rows["boom"]["sync_attempts"] == "1"
    assert "boom" in rows["boom"]["sync_error"] and rows["boom"]["sync_next_at"]

    # re-run: synced/deleted/dead are terminal; only boom is due (its backoff is
    # in the future now), so nothing reconciles this tick
    assert object_daemon.process_connectors(base_dir=data_dir) == {"reconciled": 0, "synced": 0, "dead": 0}


def test_process_connectors_none_when_nothing_declared(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("DBBASIC_PRIVATE_PACKAGES_DIR", str(tmp_path / "empty2"))
    assert object_daemon.process_connectors(base_dir=tmp_path / "data") is None
