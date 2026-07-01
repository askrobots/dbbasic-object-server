import json
from pathlib import Path

import pytest

import object_execution
import object_packages
import object_state
import python_object_runtime


def write_package(root, package_id, payload, files=()):
    package_dir = root / package_id
    package_dir.mkdir(parents=True)
    (package_dir / object_packages.MANIFEST_FILE).write_text(json.dumps(payload))
    for relative_path, content in files:
        path = package_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package_dir


def manifest(package_id="crm-starter", **overrides):
    payload = {
        "id": package_id,
        "name": "CRM Starter",
        "version": "0.1.0",
        "description": "Contacts and deals",
        "compatibility": {"object_server": ">=0.0.1"},
        "dependencies": [{"id": "hello-world", "version": ">=0.1.0"}],
        "objects": [{"id": "crm_contacts", "path": "objects/crm/contacts.py"}],
        "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
        "permissions": [{"path": "permissions/policy.json"}],
        "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
        "migrations": [{"id": "001_init", "path": "migrations/001_init.py"}],
    }
    payload.update(overrides)
    return payload


def test_list_packages_returns_manifest_summaries(tmp_path):
    packages_root = tmp_path / "packages"
    write_package(packages_root, "hello-world", manifest("hello-world"))
    write_package(packages_root, "crm-starter", manifest("crm-starter"))
    write_package(packages_root, "Bad.Name", manifest("Bad.Name"))

    packages = object_packages.list_packages(root=packages_root)

    assert [package["id"] for package in packages] == ["crm-starter", "hello-world"]
    assert packages[0]["status"] == "available"
    assert packages[0]["object_count"] == 1
    assert packages[0]["schema_count"] == 1
    assert packages[0]["dependency_count"] == 1


def test_get_package_normalizes_manifest(tmp_path):
    packages_root = tmp_path / "packages"
    write_package(packages_root, "crm-starter", manifest())

    package = object_packages.get_package("crm-starter", root=packages_root)

    assert package["id"] == "crm-starter"
    assert package["objects"] == [{"id": "crm_contacts", "path": "objects/crm/contacts.py"}]
    assert package["schemas"] == [{"collection": "contacts", "path": "schemas/contacts.json"}]
    assert package["dependencies"] == [{"id": "hello-world", "version": ">=0.1.0"}]


def test_dry_run_reports_create_replace_merge_apply_and_missing_files(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    (object_root / "crm").mkdir(parents=True)
    (object_root / "crm" / "contacts.py").write_text("def GET(request): return {}\n")
    (data_dir / "schemas").mkdir(parents=True)
    (data_dir / "schemas" / "contacts.json").write_text('{"fields":[]}\n')
    (data_dir / "collections" / "contacts").mkdir(parents=True)
    (data_dir / "collections" / "contacts" / "records.tsv").write_text("id\tname\nc1\tAlice\n")
    (data_dir / object_packages.PACKAGE_MIGRATIONS_DIR / "crm-starter").mkdir(parents=True)
    (data_dir / object_packages.PACKAGE_MIGRATIONS_DIR / "crm-starter" / "001_init.json").write_text("{}\n")

    write_package(
        packages_root,
        "crm-starter",
        manifest(),
        files=(
            ("objects/crm/contacts.py", "def GET(request): return {}\n"),
            ("schemas/contacts.json", '{"fields":[]}\n'),
            ("permissions/policy.json", "{}\n"),
            ("seed/contacts.tsv", "id\tname\nc2\tBob\n"),
        ),
    )

    plan = object_packages.dry_run_package(
        "crm-starter",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert plan["mode"] == "dry_run"
    assert plan["install_enabled"] is False
    assert plan["safe_to_install"] is False
    assert plan["objects"][0]["action"] == "replace"
    assert plan["schemas"][0]["action"] == "replace"
    assert plan["permissions"][0]["action"] == "merge"
    assert plan["seed"][0]["action"] == "merge"
    assert plan["migrations"][0]["action"] == "skip"
    assert plan["warnings"] == ["Missing package migration file: migrations/001_init.py"]


def test_dry_run_is_safe_when_declared_files_exist(tmp_path):
    packages_root = tmp_path / "packages"
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {}\n"),),
    )

    plan = object_packages.dry_run_package("hello-world", root=packages_root)

    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
    assert plan["objects"][0]["action"] == "create"


def test_repository_system_dashboard_package_installs_and_executes(tmp_path):
    packages_root = Path("packages")
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"

    package = object_packages.get_package("system-dashboard", root=packages_root)
    plan = object_packages.dry_run_package(
        "system-dashboard",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert package["objects"] == [
        {"id": "system_dashboard", "path": "objects/system/dashboard.py"}
    ]
    assert plan["safe_to_install"] is True
    assert plan["objects"][0]["action"] == "create"

    install = object_packages.install_package(
        "system-dashboard",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert install["objects"][0]["destination"] == "system/dashboard.py"
    assert (object_root / "system" / "dashboard.py").is_file()

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("system_dashboard"),
        roots=[object_root],
    )

    assert result.ok is True
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert "DBBASIC Object Dashboard" in result.result["body"]
    assert "packages/system-dashboard" in result.result["body"]
    assert object_state.get_object_state("system_dashboard", base_dir=data_dir)["served"] == 1


def test_install_package_creates_objects_schemas_and_seed(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
            "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
            "permissions": [],
            "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
            "migrations": [],
        },
        files=(
            ("objects/hello/world.py", "def GET(request): return {'status': 'ok'}\n"),
            ("schemas/contacts.json", '{"name":"contacts","fields":[{"name":"id","type":"text"}]}\n'),
            ("seed/contacts.tsv", "id\tname\nc1\tAlice\n"),
        ),
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert result["mode"] == "install"
    assert result["install_enabled"] is True
    assert result["objects"][0]["destination"] == "hello/world.py"
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'status': 'ok'}\n"
    schema = json.loads((data_dir / "schemas" / "contacts.json").read_text())
    assert schema["name"] == "contacts"
    assert schema["field_count"] == 1
    assert (data_dir / "collections" / "contacts" / "records.tsv").read_text() == "id\tname\nc1\tAlice\n"


def test_install_package_refuses_existing_objects_without_replace(tmp_path):
    packages_root = tmp_path / "packages"
    object_root = tmp_path / "objects"
    (object_root / "hello").mkdir(parents=True)
    (object_root / "hello" / "world.py").write_text("def GET(request): return {'old': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )

    with pytest.raises(object_packages.PackageInstallError, match="allow_replace=true"):
        object_packages.install_package(
            "hello-world",
            root=packages_root,
            object_roots=[object_root],
        )

    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'old': True}\n"


def test_install_package_replaces_objects_when_allowed(tmp_path):
    packages_root = tmp_path / "packages"
    object_root = tmp_path / "objects"
    (object_root / "hello").mkdir(parents=True)
    (object_root / "hello" / "world.py").write_text("def GET(request): return {'old': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["action"] == "replace"
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'new': True}\n"


def test_install_package_runs_before_write_hook_after_validation(tmp_path):
    packages_root = tmp_path / "packages"
    object_root = tmp_path / "objects"
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )
    calls = []

    def before_write(plan):
        calls.append(plan["package"]["id"])
        assert not (object_root / "hello" / "world.py").exists()
        return {"path": "restore-point.tar.gz"}

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        object_roots=[object_root],
        before_write=before_write,
    )

    assert calls == ["hello-world"]
    assert result["restore_point"] == {"path": "restore-point.tar.gz"}
    assert (object_root / "hello" / "world.py").is_file()


def test_install_package_does_not_run_before_write_hook_when_blocked(tmp_path):
    packages_root = tmp_path / "packages"
    object_root = tmp_path / "objects"
    (object_root / "hello").mkdir(parents=True)
    (object_root / "hello" / "world.py").write_text("def GET(request): return {'old': True}\n")
    write_package(
        packages_root,
        "hello-world",
        {
            "id": "hello-world",
            "name": "Hello World",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        },
        files=(("objects/hello/world.py", "def GET(request): return {'new': True}\n"),),
    )
    calls = []

    with pytest.raises(object_packages.PackageInstallError):
        object_packages.install_package(
            "hello-world",
            root=packages_root,
            object_roots=[object_root],
            before_write=lambda plan: calls.append(plan["package"]["id"]),
        )

    assert calls == []
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'old': True}\n"


def test_install_package_rejects_unsupported_permission_and_migration_writes(tmp_path):
    packages_root = tmp_path / "packages"
    write_package(
        packages_root,
        "crm-starter",
        manifest(),
        files=(
            ("objects/crm/contacts.py", "def GET(request): return {}\n"),
            ("schemas/contacts.json", '{"fields":[]}\n'),
            ("permissions/policy.json", "{}\n"),
            ("seed/contacts.tsv", "id\tname\nc2\tBob\n"),
            ("migrations/001_init.py", "def run(): pass\n"),
        ),
    )

    with pytest.raises(object_packages.PackageInstallError) as exc:
        object_packages.install_package("crm-starter", root=packages_root)

    assert "permission installs" in str(exc.value)
    assert "migration execution" in str(exc.value)


def test_install_package_refuses_existing_seed_data(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    (data_dir / "collections" / "contacts").mkdir(parents=True)
    (data_dir / "collections" / "contacts" / "records.tsv").write_text("id\tname\nc1\tAlice\n")
    write_package(
        packages_root,
        "seeded",
        {
            "id": "seeded",
            "name": "Seeded",
            "version": "0.1.0",
            "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
        },
        files=(("seed/contacts.tsv", "id\tname\nc2\tBob\n"),),
    )

    with pytest.raises(object_packages.PackageInstallError, match="Seed data already exists"):
        object_packages.install_package("seeded", root=packages_root, base_dir=data_dir)

    assert (data_dir / "collections" / "contacts" / "records.tsv").read_text() == "id\tname\nc1\tAlice\n"


def test_install_package_validates_schema_before_writing_objects(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    write_package(
        packages_root,
        "bad-schema",
        {
            "id": "bad-schema",
            "name": "Bad Schema",
            "version": "0.1.0",
            "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
            "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
        },
        files=(
            ("objects/hello/world.py", "def GET(request): return {'status': 'ok'}\n"),
            ("schemas/contacts.json", '{"name":"other","fields":[]}\n'),
        ),
    )

    with pytest.raises(object_packages.PackageInstallError, match="schema is invalid"):
        object_packages.install_package(
            "bad-schema",
            root=packages_root,
            base_dir=data_dir,
            object_roots=[object_root],
        )

    assert not (object_root / "hello" / "world.py").exists()


def test_package_manifest_rejects_unsafe_paths_and_ids(tmp_path):
    packages_root = tmp_path / "packages"
    write_package(
        packages_root,
        "bad-package",
        manifest("bad-package", objects=[{"id": "bad/id", "path": "../secret.py"}]),
    )

    with pytest.raises(object_packages.InvalidPackageManifestError):
        object_packages.get_package("bad-package", root=packages_root)

    with pytest.raises(object_packages.InvalidPackageIdError):
        object_packages.get_package("../bad", root=packages_root)


def test_missing_package_raises(tmp_path):
    with pytest.raises(object_packages.PackageNotFoundError):
        object_packages.get_package("missing", root=tmp_path / "packages")
