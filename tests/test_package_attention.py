"""What needs a human ships with the app that knows.

Every gate on this server ends in a pile somebody has to look at, and the
system has been computing those piles and discarding them for as long as
it has existed -- `system_scan_processor` returns a key literally named
`needing_a_human` that no surface has ever read. The `attention` manifest
section is where a package finally says what its pile MEANS, and these
are the properties that make saying it safe to do repeatedly.

They are the same properties `nav` holds, for the same reasons: an
operator's decision outlives an upgrade, a plan that says "unchanged"
writes nothing, a second package cannot quietly take over a definition,
and a package still installs when the home-screen app is absent. The one
addition is the blocking check -- a counter aimed at an object that will
not exist reads zero forever, and a queue that reads zero forever is
indistinguishable from a queue that is empty. That silent failure is the
entire reason this section exists, so it refuses to install.
"""

import json
import pathlib

import pytest

import object_packages
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
APP_NAV = PACKAGES / "app-nav"
SOURCES_SCHEMA = json.loads((APP_NAV / "schemas" / "attention_sources.json").read_text())

MANIFEST = {
    "id": "app-demo",
    "name": "Demo",
    "version": "1.0.0",
    "objects": [{"id": "system_demo_attention",
                 "path": "objects/system/demo_attention.py"}],
    "schemas": [{"collection": "attention_sources", "path": "schemas/attention_sources.json"}],
    "attention": [{"id": "demo_queue", "object_id": "system_demo_attention",
                   "label": "Demo queue", "path": "/demo?status=waiting",
                   "nav_id": "demo", "group": "Work", "severity": "warning"}],
}

SOURCE = "def COUNT(request):\n    return {'count': 2, 'detail': 'both today'}\n"


@pytest.fixture
def package(tmp_path):
    """A one-provider package that also ships the attention_sources
    schema, so the registry exists for it to declare into."""
    root = tmp_path / "packages"
    pkg = root / "app-demo"
    (pkg / "objects" / "system").mkdir(parents=True)
    (pkg / "objects" / "system" / "demo_attention.py").write_text(SOURCE)
    (pkg / "schemas").mkdir()
    (pkg / "schemas" / "attention_sources.json").write_text(json.dumps(SOURCES_SCHEMA))
    (pkg / "dbbasic-package.json").write_text(json.dumps(MANIFEST))
    objects = tmp_path / "objects"
    objects.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"root": root, "dir": pkg, "objects": objects, "data": data}


def write_manifest(package, **overrides):
    manifest = {**MANIFEST, **overrides}
    (package["dir"] / "dbbasic-package.json").write_text(json.dumps(manifest))


def install(package, **kwargs):
    return object_packages.install_package(
        "app-demo", root=package["root"], base_dir=package["data"],
        object_roots=[package["objects"]], **kwargs)


def plan(package):
    return object_packages.dry_run_package(
        "app-demo", root=package["root"], base_dir=package["data"],
        object_roots=[package["objects"]])


def sources(package):
    try:
        rows = object_records.read_collection_records(
            "attention_sources", base_dir=package["data"])
    except Exception:
        return {}
    return {row["id"]: row for row in rows}


def set_source(fixture, source_id, **changes):
    """An operator editing a row by hand -- `fixture` rather than
    `package` because `package` is itself one of the row's columns."""
    object_records.update_collection_record(
        "attention_sources", source_id, changes,
        base_dir=fixture["data"], actor="operator")


# --- declaring ----------------------------------------------------------------

def test_installing_a_package_registers_its_queue(package):
    result = install(package)
    assert [entry["status"] for entry in result["attention"]] == ["written"]

    row = sources(package)["demo_queue"]
    assert row["object_id"] == "system_demo_attention"
    assert row["label"] == "Demo queue"
    assert row["path"] == "/demo?status=waiting"
    assert row["nav_id"] == "demo"
    assert row["group"] == "Work"
    assert row["severity"] == "warning"
    assert row["package"] == "app-demo"        # provenance, stamped by install
    assert row["operator_muted"] == "false"


def test_the_plan_says_what_it_would_declare_before_it_does(package):
    assert plan(package)["attention"] == [{
        "id": "demo_queue", "object_id": "system_demo_attention",
        "label": "Demo queue", "path": "/demo?status=waiting", "nav_id": "demo",
        "group": "Work", "severity": "warning",
        "exists": False, "action": "create"}]
    assert plan(package)["package"]["attention_count"] == 1
    assert sources(package) == {}              # a dry run writes nothing


def test_a_package_with_no_attention_touches_no_records(package):
    write_manifest(package, attention=[])
    result = install(package)
    assert result["attention"] == []
    assert sources(package) == {}


def test_the_defaults_are_apps_and_normal(package):
    write_manifest(package, attention=[{
        "id": "demo_queue", "object_id": "system_demo_attention",
        "label": "Demo queue", "path": "/demo"}])
    install(package)

    row = sources(package)["demo_queue"]
    assert row["group"] == "Apps"
    assert row["severity"] == "normal"
    assert row["nav_id"] == ""                 # a queue may have no door


# --- surviving an upgrade -------------------------------------------------------

def test_reinstalling_preserves_the_operators_mute(package):
    """The package declares what a queue MEANS; the operator decides
    whether this deployment wants to be told about it. An upgrade that
    silently un-mutes a counter somebody turned off is the same incident
    as one that restarts a paused nightly pass."""
    install(package)
    set_source(package, "demo_queue", operator_muted="true")

    write_manifest(package, version="1.1.0",
                   attention=[{**MANIFEST["attention"][0], "label": "Renamed"}])
    install(package, allow_replace=True)

    row = sources(package)["demo_queue"]
    assert row["operator_muted"] == "true"     # untouched
    assert row["label"] == "Renamed"           # but the package's word is restated


def test_reinstalling_updates_a_changed_declaration(package):
    install(package)
    write_manifest(package, version="1.1.0", attention=[{
        **MANIFEST["attention"][0], "label": "Renamed", "path": "/elsewhere",
        "nav_id": "", "group": "System", "severity": "urgent"}])
    result = install(package, allow_replace=True)

    assert result["attention"][0]["action"] == "update"
    assert result["attention"][0]["status"] == "updated"
    row = sources(package)["demo_queue"]
    assert (row["label"], row["path"], row["nav_id"], row["group"], row["severity"]) == (
        "Renamed", "/elsewhere", "", "System", "urgent")


def test_an_unchanged_entry_reports_unchanged(package):
    install(package)
    result = install(package, allow_replace=True)
    assert result["attention"][0]["status"] == "unchanged"
    assert result["attention"][0]["action"] == "unchanged"


def test_a_plan_that_says_unchanged_writes_nothing_at_all(package):
    """Every package-owned field is compared, severity and nav_id
    included: a dry run that under-reports is worse than no dry run."""
    install(package)
    before = sources(package)["demo_queue"]
    install(package, allow_replace=True)
    assert sources(package)["demo_queue"] == before  # byte-for-byte, created_at included

    write_manifest(package, attention=[{**MANIFEST["attention"][0],
                                        "severity": "urgent"}])
    assert plan(package)["attention"][0]["action"] == "update"
    install(package, allow_replace=True)
    assert sources(package)["demo_queue"]["severity"] == "urgent"


# --- refusing what could not work ------------------------------------------------

def test_two_packages_cannot_own_one_definition(package):
    """Two packages writing one row is two answers to what needs a human,
    where whichever installed last silently wins."""
    install(package)
    set_source(package, "demo_queue", package="app-other")
    with pytest.raises(object_packages.PackageInstallError) as exc:
        install(package, allow_replace=True)
    assert "app-other" in str(exc.value)


def test_a_counter_pointing_at_nothing_blocks_the_install(package):
    """A provider that will not exist reads zero forever, and a queue that
    reads zero forever is indistinguishable from an empty one. That silent
    failure is the reason this whole section exists."""
    write_manifest(package, attention=[{**MANIFEST["attention"][0],
                                        "object_id": "system_not_shipped"}])
    with pytest.raises(object_packages.PackageInstallError) as exc:
        install(package)
    assert "system_not_shipped" in str(exc.value)
    assert sources(package) == {}


def test_a_counter_may_point_at_an_object_already_on_the_server(package):
    """A package is allowed to count somebody else's queue, as long as the
    provider is genuinely resolvable when the plan is made."""
    (package["objects"] / "system_elsewhere.py").write_text(SOURCE)
    write_manifest(package, attention=[{**MANIFEST["attention"][0],
                                        "object_id": "system_elsewhere"}])
    result = install(package)
    assert result["attention"][0]["status"] == "written"


def test_two_queues_may_point_at_one_list(package):
    """Deliberately NOT a collision: an orders page is legitimately both
    'to pick' and 'to invoice', and refusing that would be a false
    blocker."""
    write_manifest(package, attention=[
        MANIFEST["attention"][0],
        {**MANIFEST["attention"][0], "id": "second_queue", "label": "Also"},
    ])
    result = install(package)
    assert [entry["status"] for entry in result["attention"]] == ["written", "written"]


@pytest.mark.parametrize("bad, reason", [
    ({"id": "two words", "object_id": "system_demo_attention", "label": "X",
      "path": "/x"}, "attention id"),
    ({"id": "demo_queue", "object_id": "Not An Id", "label": "X", "path": "/x"},
     "attention object_id"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X",
      "path": "x"}, "start with"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X",
      "path": "/two words"}, "whitespace"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X",
      "path": "/x\nmore"}, "whitespace"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X",
      "path": "/x", "severity": "screaming"}, "normal|warning|urgent"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X",
      "path": "/x", "nav_id": "two words"}, "attention nav_id"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "path": "/x"},
     "requires 'label'"),
    ({"id": "demo_queue", "object_id": "system_demo_attention", "label": "X"},
     "requires 'path'"),
    ({"id": "demo_queue", "label": "X", "path": "/x"}, "requires 'object_id'"),
])
def test_a_declaration_that_could_not_work_is_refused_at_install(package, bad, reason):
    write_manifest(package, attention=[bad])
    with pytest.raises(object_packages.InvalidPackageManifestError) as exc:
        install(package)
    assert reason in str(exc.value)
    assert sources(package) == {}


def test_two_attention_entries_cannot_share_an_id(package):
    write_manifest(package, attention=[MANIFEST["attention"][0],
                                       {**MANIFEST["attention"][0], "path": "/other"}])
    with pytest.raises(object_packages.InvalidPackageManifestError):
        install(package)


# --- when the home-screen app is not installed -------------------------------------

def test_a_missing_registry_skips_gracefully_instead_of_failing(package):
    """A package must never fail to install because the home screen is
    absent. It says so in the result rather than pretending it wrote."""
    write_manifest(package, schemas=[])
    result = install(package)

    assert [entry["status"] for entry in result["attention"]] == ["skipped"]
    assert "attention_sources" in result["attention"][0]["reason"]
    assert not (package["data"] / "collections" / "attention_sources").exists()


def test_a_missing_registry_still_plans_the_entry(package):
    write_manifest(package, schemas=[])
    assert plan(package)["attention"][0]["action"] == "create"
    assert plan(package)["safe_to_install"] is True


# --- the real packages --------------------------------------------------------------

def _manifests():
    return {path.parent.name: json.loads(path.read_text())
            for path in sorted(PACKAGES.glob("*/dbbasic-package.json"))}


def test_shipped_attention_sources_do_not_collide_on_an_id():
    seen: dict[str, str] = {}
    for package_id, manifest in _manifests().items():
        for entry in manifest.get("attention") or []:
            assert entry["id"] not in seen, (
                f"{entry['id']} declared by both {seen.get(entry['id'])} "
                f"and {package_id}")
            seen[entry["id"]] = package_id
    assert len(seen) >= 9, "the box's known queues should all be declared"


def test_every_shipped_declaration_names_a_provider_its_package_ships():
    """A counter aimed at an object nobody installs would block its own
    package's install; this catches it at review time instead."""
    for package_id, manifest in _manifests().items():
        shipped = {entry["id"] for entry in manifest.get("objects", [])}
        for entry in manifest.get("attention") or []:
            assert entry["object_id"] in shipped, (
                f"{package_id} counts {entry['object_id']} but does not ship it")
            assert entry["object_id"].endswith("_attention"), entry["object_id"]


def test_every_shipped_provider_exposes_count_and_nothing_else():
    """COUNT is the contract with the daemon's rollup pass. A provider
    that answered on GET or POST instead would read as a permanent zero,
    which is exactly the silent failure this layer exists to end."""
    for package_id, manifest in _manifests().items():
        paths = {entry["id"]: entry["path"] for entry in manifest.get("objects", [])}
        for entry in manifest.get("attention") or []:
            source = (PACKAGES / package_id / paths[entry["object_id"]]).read_text()
            assert "def COUNT(request):" in source, package_id
            assert "def POST(request):" not in source, package_id


def test_every_shipped_declaration_normalizes_and_uses_the_house_groups():
    groups = {"Work", "Publishing", "Money", "Commerce", "Warehouse", "System"}
    nav_ids = {entry["id"]
               for manifest in _manifests().values()
               for entry in manifest.get("nav") or []}
    for package_id in _manifests():
        package = object_packages.get_package(package_id, root=PACKAGES)
        for entry in package["attention"]:
            assert entry["group"] in groups, f"{package_id}: {entry['group']}"
            assert entry["path"].startswith("/")
            if entry["nav_id"]:
                assert entry["nav_id"] in nav_ids, (
                    f"{package_id}: {entry['id']} decorates a door nobody declares")


def test_only_a_broken_machine_is_urgent():
    """Severity is a scale nobody calibrates if everything claims the top
    of it. `urgent` is reserved for the server reporting that it stopped
    doing its own work, not for work that is merely late."""
    urgent = {entry["id"]
              for manifest in _manifests().values()
              for entry in manifest.get("attention") or []
              if entry.get("severity") == "urgent"}
    assert urgent == {"scheduler_failures"}


def test_app_nav_owns_the_two_collections_and_declares_no_queue():
    """The home screen holds the registry and the rollup; it has no
    domain of its own, so it has nothing that needs a human."""
    manifest = _manifests()["app-nav"]
    collections = {entry["collection"] for entry in manifest["schemas"]}
    assert {"attention_sources", "attention_counts"} <= collections
    assert not manifest.get("attention")
