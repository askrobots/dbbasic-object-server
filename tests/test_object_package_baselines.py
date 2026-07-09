import json

import pytest

import object_package_baselines
import object_packages
import object_schemas
import object_source


def write_package(root, package_id, payload, files=()):
    package_dir = root / package_id
    package_dir.mkdir(parents=True)
    (package_dir / object_packages.MANIFEST_FILE).write_text(json.dumps(payload))
    for relative_path, content in files:
        path = package_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package_dir


def manifest(package_id="hello-world", **overrides):
    payload = {
        "id": package_id,
        "name": "Hello World",
        "version": "0.1.0",
        "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
    }
    payload.update(overrides)
    return payload


def install_hello_world(tmp_path, *, version="0.1.0", allow_replace=False):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir(exist_ok=True)

    write_package(
        packages_root,
        "hello-world",
        manifest(version=version),
        files=(
            ("objects/hello/world.py", f"def GET(request): return {{'version': '{version}'}}\n"),
            ("schemas/contacts.json", '{"name":"contacts","fields":[{"name":"id","type":"text"}]}\n'),
        ),
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=allow_replace,
    )
    return packages_root, data_dir, object_root, result


def test_record_and_load_baseline_roundtrip(tmp_path):
    assert object_package_baselines.load_baseline("hello-world", base_dir=tmp_path) is None

    baseline = object_package_baselines.record_baseline(
        "hello-world",
        version="0.1.0",
        objects={"hello_world": "abc123"},
        schemas={"contacts": "def456"},
        base_dir=tmp_path,
    )

    assert baseline == {
        "package": "hello-world",
        "version": "0.1.0",
        "installed_at": None,
        "objects": {"hello_world": "abc123"},
        "schemas": {"contacts": "def456"},
    }

    loaded = object_package_baselines.load_baseline("hello-world", base_dir=tmp_path)
    assert loaded == baseline

    path = object_package_baselines.baseline_path("hello-world", base_dir=tmp_path)
    assert path == tmp_path / "package_baselines" / "hello-world.json"
    assert path.is_file()


def test_install_package_writes_baseline_with_shipped_hashes(tmp_path):
    packages_root, data_dir, object_root, result = install_hello_world(tmp_path)

    baseline = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert baseline is not None
    assert baseline["package"] == "hello-world"
    assert baseline["version"] == "0.1.0"

    live_object = object_source.get_object_source("hello_world", [object_root])
    assert baseline["objects"]["hello_world"] == object_package_baselines.sha256_text(live_object)

    live_schema = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert baseline["schemas"]["contacts"] == object_package_baselines.canonical_schema_hash(live_schema)


def test_package_status_after_fresh_install_is_pristine(tmp_path):
    packages_root, data_dir, object_root, result = install_hello_world(tmp_path)

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )

    assert status["installed"] is True
    assert status["installed_version"] == "0.1.0"
    assert status["customized"] is False
    assert len(status["artifacts"]) == 2
    assert all(artifact["state"] == "pristine" for artifact in status["artifacts"])


def test_package_status_marks_edited_object_customized(tmp_path):
    packages_root, data_dir, object_root, result = install_hello_world(tmp_path)

    (object_root / "hello" / "world.py").write_text("def GET(request): return {'edited': True}\n")

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )

    assert status["customized"] is True
    object_artifacts = [a for a in status["artifacts"] if a["kind"] == "object"]
    assert object_artifacts == [
        {"kind": "object", "id": "hello_world", "state": "customized", "overridden": False}
    ]
    schema_artifacts = [a for a in status["artifacts"] if a["kind"] == "schema"]
    assert schema_artifacts == [{"kind": "schema", "collection": "contacts", "state": "pristine"}]


def test_package_status_never_installed_reports_not_installed(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    write_package(
        packages_root,
        "hello-world",
        manifest(),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'status': 'ok'}\n"),
            ("schemas/contacts.json", '{"name":"contacts","fields":[]}\n'),
        ),
    )

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[tmp_path / "objects"]
    )

    assert status["installed"] is False
    assert status["customized"] is False
    assert status["artifacts"] == []


def test_reinstall_upgrade_restamps_baseline_to_new_version(tmp_path):
    packages_root, data_dir, object_root, first_install = install_hello_world(tmp_path, version="0.1.0")

    baseline_v1 = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert baseline_v1["version"] == "0.1.0"

    package_dir = packages_root / "hello-world"
    (package_dir / object_packages.MANIFEST_FILE).write_text(json.dumps(manifest(version="0.2.0")))
    (package_dir / "objects" / "hello" / "world.py").write_text(
        "def GET(request): return {'version': '0.2.0'}\n"
    )

    object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    baseline_v2 = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert baseline_v2["version"] == "0.2.0"

    live_object = object_source.get_object_source("hello_world", [object_root])
    assert baseline_v2["objects"]["hello_world"] == object_package_baselines.sha256_text(live_object)

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    assert status["installed_version"] == "0.2.0"
    assert status["customized"] is False
