import json

import pytest

import object_package_baselines
import object_reconciles
import object_schemas


def _object_record(base_dir, **overrides):
    payload = {
        "package": "app-articles",
        "target_version": "0.3.0",
        "baseline_version": "0.2.0",
        "artifact": {"kind": "object", "id": "site_articles"},
        "mine": "def GET(request): return {'v': 'mine'}\n",
        "theirs": "def GET(request): return {'v': 'theirs'}\n",
        "base_sha": "deadbeef",
        "base_dir": base_dir,
    }
    payload.update(overrides)
    return object_reconciles.create_reconcile(**payload)


def test_validate_reconcile_id():
    assert object_reconciles.validate_reconcile_id("a" * 32) is True
    assert object_reconciles.validate_reconcile_id("A" * 32) is False
    assert object_reconciles.validate_reconcile_id("a" * 31) is False
    assert object_reconciles.validate_reconcile_id("../../etc/passwd") is False
    assert object_reconciles.validate_reconcile_id(None) is False
    assert object_reconciles.validate_reconcile_id(12345) is False


def test_reconcile_path_rejects_invalid_id(tmp_path):
    with pytest.raises(ValueError):
        object_reconciles.reconcile_path("not-a-valid-id", base_dir=tmp_path)


def test_reconcile_path_shape(tmp_path):
    record = _object_record(tmp_path)
    path = object_reconciles.reconcile_path(record["id"], base_dir=tmp_path)
    assert path == tmp_path / "reconciles" / f"{record['id']}.json"
    assert path.is_file()


def test_create_get_roundtrip(tmp_path):
    record = _object_record(tmp_path)

    assert object_reconciles.validate_reconcile_id(record["id"])
    assert record["status"] == "pending"
    assert record["resolution"] is None
    assert record["package"] == "app-articles"
    assert record["artifact"] == {"kind": "object", "id": "site_articles"}

    loaded = object_reconciles.get_reconcile(record["id"], base_dir=tmp_path)
    assert loaded == record


def test_get_reconcile_missing_or_invalid_returns_none(tmp_path):
    assert object_reconciles.get_reconcile("a" * 32, base_dir=tmp_path) is None
    assert object_reconciles.get_reconcile("not-valid", base_dir=tmp_path) is None


def test_list_reconciles_empty_when_missing_dir(tmp_path):
    assert object_reconciles.list_reconciles(base_dir=tmp_path) == []
    assert object_reconciles.count_pending(base_dir=tmp_path) == 0


def test_list_and_count_filter_by_status_and_package(tmp_path):
    rec1 = _object_record(tmp_path, created_at="2026-01-01T00:00:00Z")
    rec2 = _object_record(
        tmp_path,
        package="app-notes",
        artifact={"kind": "object", "id": "site_notes"},
        created_at="2026-01-02T00:00:00Z",
    )

    all_records = object_reconciles.list_reconciles(base_dir=tmp_path)
    assert [r["id"] for r in all_records] == [rec1["id"], rec2["id"]]

    assert object_reconciles.count_pending(base_dir=tmp_path) == 2
    assert object_reconciles.count_pending(base_dir=tmp_path, package="app-articles") == 1
    assert object_reconciles.count_pending(base_dir=tmp_path, package="nonexistent") == 0

    by_package = object_reconciles.list_reconciles(base_dir=tmp_path, package="app-notes")
    assert [r["id"] for r in by_package] == [rec2["id"]]

    object_reconciles.resolve_reconcile(rec1["id"], "keep_mine", base_dir=tmp_path)
    assert object_reconciles.count_pending(base_dir=tmp_path) == 1
    resolved = object_reconciles.list_reconciles(base_dir=tmp_path, status="resolved")
    assert [r["id"] for r in resolved] == [rec1["id"]]


def test_resolve_reconcile_rejects_invalid_choice(tmp_path):
    record = _object_record(tmp_path)
    with pytest.raises(ValueError):
        object_reconciles.resolve_reconcile(record["id"], "bogus", base_dir=tmp_path)


def test_resolve_reconcile_missing_raises(tmp_path):
    with pytest.raises(ValueError):
        object_reconciles.resolve_reconcile("b" * 32, "keep_mine", base_dir=tmp_path)


def test_resolve_reconcile_already_resolved_raises(tmp_path):
    record = _object_record(tmp_path)
    object_reconciles.resolve_reconcile(record["id"], "keep_mine", base_dir=tmp_path)
    with pytest.raises(ValueError):
        object_reconciles.resolve_reconcile(record["id"], "keep_mine", base_dir=tmp_path)


def test_resolve_take_theirs_writes_live_object_and_stamps_baseline(tmp_path):
    object_root = tmp_path / "objects"
    (object_root / "site").mkdir(parents=True)
    (object_root / "site" / "articles.py").write_text("def GET(request): return {'v': 'mine'}\n")
    data_dir = tmp_path / "data"

    record = _object_record(data_dir)

    resolved = object_reconciles.resolve_reconcile(
        record["id"],
        "take_theirs",
        base_dir=data_dir,
        object_roots=[object_root],
        resolved_at="2026-07-08T00:00:00Z",
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == {"choice": "take_theirs", "resolved_at": "2026-07-08T00:00:00Z"}
    assert (object_root / "site" / "articles.py").read_text() == "def GET(request): return {'v': 'theirs'}\n"

    baseline = object_package_baselines.load_baseline("app-articles", base_dir=data_dir)
    assert baseline["version"] == "0.3.0"
    assert baseline["objects"]["site_articles"] == object_package_baselines.sha256_text(
        "def GET(request): return {'v': 'theirs'}\n"
    )


def test_resolve_keep_mine_leaves_live_object_and_stamps_baseline(tmp_path):
    object_root = tmp_path / "objects"
    (object_root / "site").mkdir(parents=True)
    (object_root / "site" / "articles.py").write_text("def GET(request): return {'v': 'mine'}\n")
    data_dir = tmp_path / "data"

    record = _object_record(data_dir)

    resolved = object_reconciles.resolve_reconcile(
        record["id"],
        "keep_mine",
        base_dir=data_dir,
        object_roots=[object_root],
        resolved_at="2026-07-08T00:00:00Z",
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolution"]["choice"] == "keep_mine"
    # Live content is untouched.
    assert (object_root / "site" / "articles.py").read_text() == "def GET(request): return {'v': 'mine'}\n"

    # The baseline is still stamped to sha_theirs/target_version, so live now
    # reads as customized against the new upstream baseline.
    baseline = object_package_baselines.load_baseline("app-articles", base_dir=data_dir)
    assert baseline["version"] == "0.3.0"
    assert baseline["objects"]["site_articles"] == object_package_baselines.sha256_text(
        "def GET(request): return {'v': 'theirs'}\n"
    )


def test_resolve_schema_take_theirs_replaces_live_schema(tmp_path):
    data_dir = tmp_path / "data"
    mine_schema = object_schemas.normalize_schema(
        "contacts",
        {"name": "contacts", "fields": [{"name": "id", "type": "text"}]},
        source="manual",
    )
    object_schemas.replace_schema("contacts", mine_schema, base_dir=data_dir)
    theirs_schema = object_schemas.normalize_schema(
        "contacts",
        {
            "name": "contacts",
            "fields": [
                {"name": "id", "type": "text"},
                {"name": "email", "type": "text"},
            ],
        },
        source="manual",
    )

    record = object_reconciles.create_reconcile(
        package="app-crm",
        target_version="0.2.0",
        baseline_version="0.1.0",
        artifact={"kind": "schema", "collection": "contacts"},
        mine=json.dumps(mine_schema, indent=2, sort_keys=True),
        theirs=json.dumps(theirs_schema, indent=2, sort_keys=True),
        base_sha=None,
        base_dir=data_dir,
    )

    resolved = object_reconciles.resolve_reconcile(
        record["id"], "take_theirs", base_dir=data_dir, resolved_at="2026-07-08T00:00:00Z"
    )

    assert resolved["status"] == "resolved"
    live = object_schemas.get_schema("contacts", base_dir=data_dir)
    assert {f["name"] for f in live["fields"]} == {"id", "email"}

    baseline = object_package_baselines.load_baseline("app-crm", base_dir=data_dir)
    assert baseline["schemas"]["contacts"] == object_package_baselines.canonical_schema_hash(theirs_schema)
    assert baseline["version"] == "0.2.0"
