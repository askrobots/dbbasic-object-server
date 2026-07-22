import json
from pathlib import Path

import pytest

import object_execution
import object_namespace
import object_package_baselines
import object_packages
import object_reconciles
import object_record_changes
import object_records
import object_schemas
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


def update_package(root, package_id, payload, files=()):
    """Overwrite an already-written package's manifest and files (a "new version")."""
    package_dir = root / package_id
    (package_dir / object_packages.MANIFEST_FILE).write_text(json.dumps(payload))
    for relative_path, content in files:
        path = package_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package_dir


def _reconcile_manifest(version):
    return {
        "id": "hello-world",
        "name": "Hello World",
        "version": version,
        "objects": [{"id": "hello_world", "path": "objects/hello/world.py"}],
        "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
    }


def _install_reconcile_fixture(tmp_path, *, edit_object=False, edit_schema=False):
    """Install hello-world v0.1.0, optionally customize the live object/schema."""
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    write_package(
        packages_root,
        "hello-world",
        _reconcile_manifest("0.1.0"),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'v': 1}\n"),
            (
                "schemas/contacts.json",
                json.dumps({"name": "contacts", "fields": [{"name": "id", "type": "text"}]}),
            ),
        ),
    )
    object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    if edit_object:
        (object_root / "hello" / "world.py").write_text("def GET(request): return {'v': 'customized'}\n")
    if edit_schema:
        # Change "id"'s type (diverging from the shipped base) as well as
        # adding a new field, so a later package change to "id" is a real
        # field-level collision rather than a union-mergeable diff.
        object_schemas.replace_schema(
            "contacts",
            {
                "name": "contacts",
                "fields": [
                    {"name": "id", "type": "number"},
                    {"name": "x_priority", "type": "text"},
                ],
            },
            base_dir=data_dir,
        )

    return packages_root, data_dir, object_root


def _bump_reconcile_package(packages_root, *, version, object_code=None, schema_fields=None):
    package_dir = packages_root / "hello-world"
    files = []
    if object_code is not None:
        files.append(("objects/hello/world.py", object_code))
    if schema_fields is not None:
        files.append(("schemas/contacts.json", json.dumps({"name": "contacts", "fields": schema_fields})))
    update_package(packages_root, "hello-world", _reconcile_manifest(version), files=files)


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
            ("permissions/policy.json", '{"rules": []}\n'),
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
    assert plan["seed"][0]["action"] == "skip"
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
    assert "DBBASIC Dashboard" in result.result["body"]
    assert "sign in</a> for live metrics" in result.result["body"]
    assert object_state.get_object_state("system_dashboard", base_dir=data_dir)["served"] == 1


def test_repository_admin_write_probe_package_installs_and_writes_records(tmp_path):
    packages_root = Path("packages")
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"

    package = object_packages.get_package("admin-write-probe", root=packages_root)
    plan = object_packages.dry_run_package(
        "admin-write-probe",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert package["objects"] == [
        {"id": "system_write_probe", "path": "objects/system/write_probe.py"}
    ]
    assert package["schemas"] == [
        {"collection": "dbbasic_probe", "path": "schemas/dbbasic_probe.json"}
    ]
    assert package["seed"] == [
        {"collection": "dbbasic_probe", "path": "seed/dbbasic_probe.tsv"}
    ]
    assert plan["safe_to_install"] is True
    assert plan["objects"][0]["action"] == "create"
    assert plan["schemas"][0]["action"] == "create"
    assert plan["seed"][0]["action"] == "create"

    install = object_packages.install_package(
        "admin-write-probe",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert install["objects"][0]["destination"] == "system/write_probe.py"
    assert install["schemas"][0]["destination"] == "schemas/dbbasic_probe.json"
    assert install["seed"][0]["destination"] == "collections/dbbasic_probe/records.tsv"
    assert (object_root / "system" / "write_probe.py").is_file()

    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("system_write_probe"),
        roots=[object_root],
    )

    assert result.ok is True
    assert result.result["content_type"] == "text/html; charset=utf-8"
    assert "DBBASIC Write Probe" in result.result["body"]
    assert "packages/admin-write-probe" in result.result["body"]
    assert object_state.get_object_state("system_write_probe", base_dir=data_dir)["served"] == 1

    record = object_records.create_collection_record(
        "dbbasic_probe",
        {
            "id": "probe_test",
            "note": "created by package test",
            "status": "created",
            "updated_at": "2026-07-01T00:00:00+00:00",
        },
        base_dir=data_dir,
    )
    assert record["status"] == "created"

    updated = object_records.update_collection_record(
        "dbbasic_probe",
        "probe_test",
        {
            "note": "updated by package test",
            "status": "updated",
        },
        base_dir=data_dir,
    )
    assert updated["note"] == "updated by package test"
    assert updated["status"] == "updated"

    deleted = object_records.delete_collection_record(
        "dbbasic_probe",
        "probe_test",
        base_dir=data_dir,
    )
    assert deleted["id"] == "probe_test"
    assert object_records.read_collection_records("dbbasic_probe", base_dir=data_dir) == []


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


def test_install_package_attributes_seed_records(tmp_path):
    """Seed writes bypass object_records.create_collection_record (the whole

    records.tsv lands as one atomic byte copy), so unlike a normal record
    write nothing emits a change automatically -- universal attribution
    still requires one, attributed to the installing package.
    """
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
            "objects": [],
            "schemas": [{"collection": "contacts", "path": "schemas/contacts.json"}],
            "permissions": [],
            "seed": [{"collection": "contacts", "path": "seed/contacts.tsv"}],
            "migrations": [],
        },
        files=(
            ("schemas/contacts.json", '{"name":"contacts","fields":[{"name":"id","type":"text"}]}\n'),
            ("seed/contacts.tsv", "id\tname\nc1\tAlice\nc2\tBob\n"),
        ),
    )

    object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    changes = object_record_changes.list_record_changes("contacts", base_dir=data_dir)["changes"]
    assert {change["record_id"] for change in changes} == {"c1", "c2"}
    assert all(change["action"] == "create" for change in changes)
    assert all(change["actor"] == "package-install:hello-world" for change in changes)


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
    # The pre-existing file here was never installed by this package (no
    # baseline recorded for it), so the reconcile engine has no basis to call
    # it "pristine" and would park it as a conflict rather than guess. This
    # is the "replaces when allowed" path re-armed under Phase 2: allow_replace
    # clears the create/replace blocker, and force is the explicit "discard
    # whatever is there" signal (see docs/upgrade-and-customization.md Rule 1).
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
        base_dir=tmp_path / "data",
        object_roots=[object_root],
        allow_replace=True,
        force=True,
    )

    assert result["objects"][0]["action"] == "replace"
    assert result["objects"][0]["status"] == "forced"
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'new': True}\n"


def test_install_package_parks_conflict_when_replace_has_no_baseline(tmp_path):
    # Same setup as above, but WITHOUT force: an untracked existing file plus
    # differing shipped content is a conflict, not a blind overwrite. The
    # install still succeeds; the live file is left untouched and a
    # pending-reconcile record is created instead.
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
        base_dir=tmp_path / "data",
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["action"] == "replace"
    assert result["objects"][0]["status"] == "conflict"
    assert "reconcile_id" in result["objects"][0]
    assert result["reconciles"] == [result["objects"][0]["reconcile_id"]]
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'old': True}\n"


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
            ("permissions/policy.json", '{"rules": []}\n'),
            ("seed/contacts.tsv", "id\tname\nc2\tBob\n"),
            ("migrations/001_init.py", "def run(): pass\n"),
        ),
    )

    with pytest.raises(object_packages.PackageInstallError) as exc:
        object_packages.install_package("crm-starter", root=packages_root)

    assert "migration execution" in str(exc.value)


def test_install_package_merges_permission_rules_with_provenance(tmp_path):
    import object_permission_store

    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    fragment = json.dumps(
        {
            "rules": [
                {
                    "effect": "allow",
                    "principal": "public",
                    "actions": ["execute"],
                    "object_id": "crm_contacts",
                    "reason": "public contacts page",
                },
                {
                    "effect": "allow",
                    "principal": "registered",
                    "actions": ["read", "create"],
                    "collection": "contacts",
                },
            ]
        }
    )
    payload = manifest(dependencies=[], migrations=[], seed=[])
    write_package(
        packages_root,
        "crm-starter",
        payload,
        files=(
            ("objects/crm/contacts.py", "def GET(request): return {}\n"),
            ("schemas/contacts.json", '{"fields":[]}\n'),
            ("permissions/policy.json", fragment),
        ),
    )

    plan = object_packages.dry_run_package(
        "crm-starter", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    install = object_packages.install_package(
        "crm-starter", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    policy = object_permission_store.load_policy(data_dir)

    assert plan["permissions"][0] == {
        "path": "permissions/policy.json",
        "action": "merge",
        "exists": True,
        "rules": 2,
        "new_rules": 2,
    }
    assert install["permissions"][0]["status"] == "merged"
    assert install["permissions"][0]["new_rules"] == 2
    assert len(policy.rules) == 2
    assert all(rule.package == "crm-starter" for rule in policy.rules)
    assert policy.rules[0].object_id == "crm_contacts"

    reinstall = object_packages.install_package(
        "crm-starter",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )
    policy_after = object_permission_store.load_policy(data_dir)

    assert reinstall["permissions"][0]["new_rules"] == 0
    assert len(policy_after.rules) == 2


def test_install_package_rejects_invalid_permission_fragment(tmp_path):
    packages_root = tmp_path / "packages"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    payload = manifest(dependencies=[], migrations=[], schemas=[], seed=[])
    write_package(
        packages_root,
        "crm-starter",
        payload,
        files=(
            ("objects/crm/contacts.py", "def GET(request): return {}\n"),
            ("permissions/policy.json", '{"rules": [{"effect": "allow"}]}\n'),
        ),
    )

    with pytest.raises(object_packages.PackageInstallError) as exc:
        object_packages.install_package(
            "crm-starter",
            root=packages_root,
            base_dir=tmp_path / "data",
            object_roots=[object_root],
        )

    assert "permission rule is invalid" in str(exc.value)


def test_install_package_merges_seed_by_id_preserving_existing(tmp_path):
    # Seeding a collection that already holds records MERGES by id: existing
    # rows survive untouched, and seed rows whose id isn't present are added.
    # This is what lets several packages each seed a shared collection
    # (views/site_routes) and lets a new package version ship new default rows,
    # while never clobbering live data ("upgrade the app, keep the data").
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

    result = object_packages.install_package(
        "seeded", root=packages_root, base_dir=data_dir, allow_replace=True
    )

    seed_entry = result["seed"][0]
    assert seed_entry["status"] == "merged"
    assert seed_entry["installed"] is True
    assert seed_entry["added"] == 1
    # Existing row (Alice) preserved AND the new seed row (Bob) merged in by id.
    records = object_records.read_collection_records("contacts", base_dir=data_dir)
    assert {r["name"] for r in records} == {"Alice", "Bob"}
    # Re-installing adds nothing (ids already present).
    again = object_packages.install_package(
        "seeded", root=packages_root, base_dir=data_dir, allow_replace=True
    )
    assert again["seed"][0]["status"] == "skipped" and again["seed"][0]["added"] == 0


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


# --- Phase 2: reconcile-on-upgrade decision table ---------------------------
#
# See docs/upgrade-and-customization.md (Rule 1). Each test below exercises
# one row of the three-way-compare table for objects (and, at the end,
# schemas): pristine vs customized, shipped unchanged vs shipped changed.


def test_reconcile_fresh_install_status_written(tmp_path):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    write_package(
        packages_root,
        "hello-world",
        _reconcile_manifest("0.1.0"),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'v': 1}\n"),
            (
                "schemas/contacts.json",
                json.dumps({"name": "contacts", "fields": [{"name": "id", "type": "text"}]}),
            ),
        ),
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
    )

    assert result["objects"][0]["status"] == "written"
    assert result["schemas"][0]["status"] == "written"
    assert result["reconciles"] == []


def test_reconcile_unchanged_reinstall_is_noop(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path)

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["status"] == "unchanged"
    assert result["schemas"][0]["status"] == "unchanged"
    assert result["reconciles"] == []
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'v': 1}\n"


def test_reconcile_pristine_fast_forwards_on_upgrade(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=False)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["status"] == "updated"
    assert result["reconciles"] == []
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'v': 2}\n"

    baseline = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert baseline["version"] == "0.2.0"
    assert baseline["objects"]["hello_world"] == object_package_baselines.sha256_text(
        "def GET(request): return {'v': 2}\n"
    )


def test_reconcile_customized_and_shipped_unchanged_keeps_customization(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["status"] == "kept"
    assert result["reconciles"] == []
    assert (
        object_root / "hello" / "world.py"
    ).read_text() == "def GET(request): return {'v': 'customized'}\n"


def test_reconcile_customized_and_shipped_changed_parks_conflict(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["objects"][0]["status"] == "conflict"
    assert "reconcile_id" in result["objects"][0]
    assert result["reconciles"] == [result["objects"][0]["reconcile_id"]]
    # The install itself never raises: the conflict is parked, not blocking.
    assert (
        object_root / "hello" / "world.py"
    ).read_text() == "def GET(request): return {'v': 'customized'}\n"

    reconcile = object_reconciles.get_reconcile(result["objects"][0]["reconcile_id"], base_dir=data_dir)
    assert reconcile["status"] == "pending"
    assert reconcile["package"] == "hello-world"
    assert reconcile["target_version"] == "0.2.0"
    assert reconcile["baseline_version"] == "0.1.0"
    assert reconcile["artifact"] == {"kind": "object", "id": "hello_world"}
    assert reconcile["mine"] == "def GET(request): return {'v': 'customized'}\n"
    assert reconcile["theirs"] == "def GET(request): return {'v': 2}\n"


def test_reconcile_force_overwrites_conflict(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
        force=True,
    )

    assert result["objects"][0]["status"] == "forced"
    assert result["reconciles"] == []
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'v': 2}\n"

    baseline = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert baseline["objects"]["hello_world"] == object_package_baselines.sha256_text(
        "def GET(request): return {'v': 2}\n"
    )


def test_reconcile_resolve_take_theirs_writes_live_and_baseline(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )
    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )
    reconcile_id = result["objects"][0]["reconcile_id"]

    resolved = object_reconciles.resolve_reconcile(
        reconcile_id,
        "take_theirs",
        base_dir=data_dir,
        object_roots=[object_root],
        resolved_at="2026-07-08T00:00:00Z",
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == {"choice": "take_theirs", "resolved_at": "2026-07-08T00:00:00Z"}
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'v': 2}\n"

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    assert status["pending_reconciles"] == 0
    object_artifact = next(a for a in status["artifacts"] if a["kind"] == "object")
    assert object_artifact["state"] == "pristine"


def test_reconcile_resolve_keep_mine_preserves_live_and_updates_baseline(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )
    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )
    reconcile_id = result["objects"][0]["reconcile_id"]

    resolved = object_reconciles.resolve_reconcile(
        reconcile_id,
        "keep_mine",
        base_dir=data_dir,
        object_roots=[object_root],
        resolved_at="2026-07-08T00:00:00Z",
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolution"]["choice"] == "keep_mine"
    # Live content is untouched — the operator's customization survives.
    assert (
        object_root / "hello" / "world.py"
    ).read_text() == "def GET(request): return {'v': 'customized'}\n"

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    assert status["pending_reconciles"] == 0
    # Baseline now reads sha_theirs, so live (still "mine") reads customized
    # against the new upstream baseline — the operator's choice is durable.
    object_artifact = next(a for a in status["artifacts"] if a["kind"] == "object")
    assert object_artifact["state"] == "customized"


def test_reconcile_schema_conflict_and_resolve(tmp_path):
    # Base ships id:text. The operator changes id's type to "number" *and*
    # the upgrade changes id's type to "boolean": the same field diverged on
    # both sides, so field-union merge cannot resolve it and it must still
    # park as a conflict (the "email" addition alone would have merged).
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_schema=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        schema_fields=[
            {"name": "id", "type": "boolean"},
            {"name": "email", "type": "text"},
        ],
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["schemas"][0]["status"] == "conflict"
    assert result["schemas"][0]["collisions"] == ["id"]
    reconcile_id = result["schemas"][0]["reconcile_id"]
    assert result["reconciles"] == [reconcile_id]

    live_before = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert live_before["field_count"] == 2
    assert {f["name"] for f in live_before["fields"]} == {"id", "x_priority"}

    reconcile_record = object_reconciles.get_reconcile(reconcile_id, base_dir=data_dir)
    assert reconcile_record["collisions"] == ["id"]

    resolved = object_reconciles.resolve_reconcile(
        reconcile_id,
        "take_theirs",
        base_dir=data_dir,
        object_roots=[object_root],
        resolved_at="2026-07-08T00:00:00Z",
    )
    assert resolved["status"] == "resolved"

    live_after = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert {f["name"] for f in live_after["fields"]} == {"id", "email"}

    baseline_after = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert {f["name"] for f in baseline_after["schema_bodies"]["contacts"]["fields"]} == {"id", "email"}

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    schema_artifact = next(a for a in status["artifacts"] if a["kind"] == "schema")
    assert schema_artifact["state"] == "pristine"
    assert status["pending_reconciles"] == 0


def test_reconcile_schema_merges_additive_changes_without_conflict(tmp_path):
    # Phase 4a / Rule 3: schemas are additive, so an operator-added field and
    # a package-added field on the same base should both survive an upgrade
    # via field-union merge, with no reconcile parked.
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()

    write_package(
        packages_root,
        "hello-world",
        _reconcile_manifest("0.1.0"),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'v': 1}\n"),
            (
                "schemas/contacts.json",
                json.dumps(
                    {
                        "name": "contacts",
                        "fields": [
                            {"name": "id", "type": "text"},
                            {"name": "name", "type": "text"},
                        ],
                    }
                ),
            ),
        ),
    )
    object_packages.install_package(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )

    # Operator adds a local field directly to the live schema.
    object_schemas.replace_schema(
        "contacts",
        {
            "name": "contacts",
            "fields": [
                {"name": "id", "type": "text"},
                {"name": "name", "type": "text"},
                {"name": "x_priority", "type": "text"},
            ],
        },
        base_dir=data_dir,
    )

    # Upgrade ships its own new field, on the same base.
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        schema_fields=[
            {"name": "id", "type": "text"},
            {"name": "name", "type": "text"},
            {"name": "status", "type": "text"},
        ],
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["schemas"][0]["status"] == "merged"
    assert "reconcile_id" not in result["schemas"][0]
    assert result["reconciles"] == []

    live = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert {f["name"] for f in live["fields"]} == {"id", "name", "x_priority", "status"}

    baseline = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert {f["name"] for f in baseline["schema_bodies"]["contacts"]["fields"]} == {"id", "name", "status"}


def test_reconcile_schema_without_baseline_body_falls_back_to_conflict(tmp_path):
    # Backward-compat: a baseline recorded before this feature existed has no
    # "schema_bodies" entry. A both-changed schema must still fall back to
    # the old park-a-conflict behavior instead of crashing.
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_schema=True)

    baseline_file = object_package_baselines.baseline_path("hello-world", base_dir=data_dir)
    baseline = json.loads(baseline_file.read_text())
    del baseline["schema_bodies"]
    baseline_file.write_text(json.dumps(baseline))

    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        schema_fields=[
            {"name": "id", "type": "text"},
            {"name": "email", "type": "text"},
        ],
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    assert result["schemas"][0]["status"] == "conflict"
    assert "reconcile_id" in result["schemas"][0]
    live = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert {f["name"] for f in live["fields"]} == {"id", "x_priority"}


def test_reconcile_schema_resolve_keep_mine_updates_baseline_schema_body(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_schema=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        schema_fields=[
            {"name": "id", "type": "boolean"},
            {"name": "email", "type": "text"},
        ],
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )
    reconcile_id = result["schemas"][0]["reconcile_id"]

    resolved = object_reconciles.resolve_reconcile(
        reconcile_id,
        "keep_mine",
        base_dir=data_dir,
        object_roots=[object_root],
    )
    assert resolved["status"] == "resolved"

    # Live keeps the operator's version...
    live = object_schemas.get_schema("contacts", base_dir=data_dir, roots=[object_root])
    assert {f["name"] for f in live["fields"]} == {"id", "x_priority"}

    # ...but the baseline body advances to the new upstream shape, so a
    # future upgrade diffs against what was actually shipped.
    baseline = object_package_baselines.load_baseline("hello-world", base_dir=data_dir)
    assert {f["name"] for f in baseline["schema_bodies"]["contacts"]["fields"]} == {"id", "email"}


def test_reconcile_package_status_counts_pending_reconciles(tmp_path):
    packages_root, data_dir, object_root = _install_reconcile_fixture(tmp_path, edit_object=True)
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )

    object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=[object_root],
        allow_replace=True,
    )

    status = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    assert status["pending_reconciles"] == 1


# --- Phase 3: override objects (conflict-free customization, Rule 2) --------
#
# See docs/upgrade-and-customization.md (Rule 2). An override shadows a
# package object by id without touching the package copy, so install/
# reconcile logic (which always targets get_base_object_roots()) never sees
# a conflict for an overridden object, and the package copy stays
# upgradeable.


def test_package_status_reports_overridden_flag(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    override_root = tmp_path / "overrides"
    monkeypatch.delenv("DBBASIC_OVERRIDES_DIR", raising=False)
    write_package(
        packages_root,
        "hello-world",
        _reconcile_manifest("0.1.0"),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'v': 1}\n"),
            (
                "schemas/contacts.json",
                json.dumps({"name": "contacts", "fields": [{"name": "id", "type": "text"}]}),
            ),
        ),
    )
    object_packages.install_package(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )

    status_before = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    object_artifact = next(a for a in status_before["artifacts"] if a["kind"] == "object")
    assert object_artifact["overridden"] is False

    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))
    override_file = override_root / "hello" / "world.py"
    override_file.parent.mkdir(parents=True)
    override_file.write_text("def GET(request): return {'v': 'overridden'}\n")

    status_after = object_packages.package_status(
        "hello-world", root=packages_root, base_dir=data_dir, object_roots=[object_root]
    )
    object_artifact_after = next(a for a in status_after["artifacts"] if a["kind"] == "object")
    assert object_artifact_after["overridden"] is True
    # pristine/customized state is computed from the base-root copy only —
    # the override never influences it.
    assert object_artifact_after["state"] == "pristine"


def test_upgrade_fast_forwards_base_copy_without_conflict_when_overridden(tmp_path, monkeypatch):
    packages_root = tmp_path / "packages"
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(object_root))
    monkeypatch.delenv("DBBASIC_OVERRIDES_DIR", raising=False)

    write_package(
        packages_root,
        "hello-world",
        _reconcile_manifest("0.1.0"),
        files=(
            ("objects/hello/world.py", "def GET(request): return {'v': 1}\n"),
            (
                "schemas/contacts.json",
                json.dumps({"name": "contacts", "fields": [{"name": "id", "type": "text"}]}),
            ),
        ),
    )

    object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=object_namespace.get_base_object_roots(),
    )

    # Enable overrides and customize hello_world via an override. The
    # package copy under object_root is never touched by this.
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))
    override_file = object_namespace.override_path("hello_world")
    override_file.parent.mkdir(parents=True)
    override_file.write_text("def GET(request): return {'v': 'overridden'}\n")
    assert object_namespace.resolve_object_id("hello_world") == override_file

    # Ship an upgrade with different shipped object content.
    _bump_reconcile_package(
        packages_root,
        version="0.2.0",
        object_code="def GET(request): return {'v': 2}\n",
    )

    result = object_packages.install_package(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=object_namespace.get_base_object_roots(),
        allow_replace=True,
    )

    # The base/package copy fast-forwards cleanly: no conflict, because the
    # base copy (not the override) was pristine going into the upgrade.
    assert result["objects"][0]["status"] == "updated"
    assert result["reconciles"] == []
    assert (object_root / "hello" / "world.py").read_text() == "def GET(request): return {'v': 2}\n"

    # The override is completely untouched by the upgrade.
    assert override_file.read_text() == "def GET(request): return {'v': 'overridden'}\n"

    status = object_packages.package_status(
        "hello-world",
        root=packages_root,
        base_dir=data_dir,
        object_roots=object_namespace.get_base_object_roots(),
    )
    object_artifact = next(a for a in status["artifacts"] if a["kind"] == "object")
    assert object_artifact["state"] == "pristine"
    assert object_artifact["overridden"] is True
