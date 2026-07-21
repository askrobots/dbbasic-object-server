"""65 multi-entity slice 4: the Setup Accounts action -- seed an entity's chart
of accounts from its mode (a faithful port of the predecessor's
Entity.create_default_accounts / the setup_finance_accounts MCP tool). Idempotent,
owner-gated.
"""

import json
import os
import tempfile
import types
from pathlib import Path

import object_mcp
import object_packages
import object_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_FINANCE_DIR = PACKAGES_ROOT / "app-finance"


def _install(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for pkg in ("app-entities", "app-finance", "app-projects"):
        object_packages.install_package(
            pkg, root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root], allow_replace=True,
        )
    return data_dir


def _load_object(data_dir, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    src = (APP_FINANCE_DIR / "objects" / "site" / "setup_accounts.py").read_text()
    mod = types.ModuleType("setup_accounts_under_test")
    mod._logger = types.SimpleNamespace(info=lambda *a, **k: None)
    exec(compile(src, "setup_accounts.py", "exec"), mod.__dict__)
    return mod


def _post(mod, user_id, entity_id):
    req = {"entity_id": entity_id}
    if user_id is not None:
        req["_identity"] = {"user_id": user_id}
    r = mod.POST(req)
    return r.get("status", 200), json.loads(r["body"])


def _entity(data_dir, owner, mode):
    return object_records.create_collection_record(
        "entities", {"name": mode.title(), "owner_id": owner, "mode": mode},
        base_dir=data_dir, actor="test",
    )["id"]


def _accounts_for(data_dir, entity_id):
    return [a for a in object_records.read_collection_records("fin_accounts", base_dir=data_dir)
            if a.get("entity_id") == entity_id]


def test_each_mode_seeds_its_chart_owned_and_scoped(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    mod = _load_object(data_dir, monkeypatch)
    for mode, count in (("simple", 2), ("standard", 15), ("double_entry", 29)):
        eid = _entity(data_dir, "7", mode)
        status, body = _post(mod, "7", eid)
        assert status == 200 and body["mode"] == mode and body["created"] == count
        accts = _accounts_for(data_dir, eid)
        assert len(accts) == count
        # every account is owned by the caller and scoped to this entity
        assert all(a["owner_id"] == "7" and a["entity_id"] == eid for a in accts)
        # a known account carries its predecessor code
        assert all(a["account_type"] in {"asset", "liability", "equity", "income", "expense"} for a in accts)


def test_idempotent_second_run_creates_nothing(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    mod = _load_object(data_dir, monkeypatch)
    eid = _entity(data_dir, "7", "standard")
    _post(mod, "7", eid)
    status, body = _post(mod, "7", eid)
    assert status == 200 and body["created"] == 0 and body["skipped"] == 15
    assert len(_accounts_for(data_dir, eid)) == 15  # no duplicates


def test_owner_gated(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    mod = _load_object(data_dir, monkeypatch)
    eid = _entity(data_dir, "7", "standard")
    # another owner -> 403, nothing created
    status, _ = _post(mod, "8", eid)
    assert status == 403
    assert _accounts_for(data_dir, eid) == []
    # anonymous -> 401
    status, _ = _post(mod, None, eid)
    assert status == 401


def test_unknown_entity_and_missing_id(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    mod = _load_object(data_dir, monkeypatch)
    assert _post(mod, "7", "00000000-0000-4000-8000-000000000000")[0] == 404
    assert mod.POST({"_identity": {"user_id": "7"}})["status"] == 400  # no entity_id


def test_package_wires_the_object_route_and_rule():
    package = object_packages.get_package("app-finance", root=PACKAGES_ROOT)
    assert "site_setup_accounts" in {o["id"] for o in package["objects"]}
    assert "site_routes" in {e["collection"] for e in package["seed"]}
    # dependencies are normalized to {id, version} dicts by object_packages.
    assert any(d.get("id") == "app-entities" for d in package.get("dependencies", []))
    rules = json.loads((APP_FINANCE_DIR / "permissions" / "rules.json").read_text())["rules"]
    assert any(r.get("object_id") == "site_setup_accounts" and r.get("actions") == ["execute"]
               for r in rules)
    import csv
    routes = list(csv.DictReader(open(APP_FINANCE_DIR / "seed" / "site_routes.tsv"), delimiter="\t"))
    assert routes[0]["pattern"] == "/finance/setup-accounts"
    assert routes[0]["object_id"] == "site_setup_accounts"


def test_mcp_setup_finance_accounts_verb_routes_to_the_object():
    method, path, _query, body = object_mcp.tool_route(
        "setup_finance_accounts", {"entity_id": "E1"},
    )
    assert method == "POST"
    assert path == "/admin/objects/site_setup_accounts/execute"
    assert json.loads(body)["payload"] == {"entity_id": "E1"}


def test_mcp_setup_finance_accounts_requires_entity_id():
    import pytest

    with pytest.raises(ValueError):
        object_mcp.tool_route("setup_finance_accounts", {})
