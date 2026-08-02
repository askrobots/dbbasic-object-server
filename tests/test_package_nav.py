"""Navigation ships with the app that owns the door.

Before this, the same list of apps was maintained by hand in three
places -- the app switcher's JS array, the home page's tile grid, and a
search-hit URL map -- and they had already drifted: 25 entries against
21, and between them no mention of the eight newest apps. Nothing failed
when they drifted; the front door simply advertised a server that no
longer existed, which is why nobody noticed.

So the properties worth holding are about SURVIVING an install rather
than performing one: an operator's `hidden` outlives an upgrade, a plan
that says "unchanged" writes nothing, a second package cannot quietly
take over somebody's door, and a package still installs when the nav app
is absent entirely. The last test in this file is the one that stops the
drift coming back.
"""

import json
import pathlib

import pytest

import object_packages
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
NAV_SCHEMA = json.loads((PACKAGES / "app-nav" / "schemas" / "nav_entries.json").read_text())

MANIFEST = {
    "id": "app-demo",
    "name": "Demo",
    "version": "1.0.0",
    "objects": [{"id": "site_demo", "path": "objects/site/demo.py"}],
    "schemas": [{"collection": "nav_entries", "path": "schemas/nav_entries.json"}],
    "nav": [{"id": "demo", "label": "Demo", "path": "/demo",
             "blurb": "A door.", "group": "Work", "order": 10}],
}

SOURCE = "def GET(request):\n    return {'ok': True}\n"


@pytest.fixture
def package(tmp_path):
    """A one-page, one-door package that also ships the nav_entries
    schema, so the registry exists for it to register into."""
    root = tmp_path / "packages"
    pkg = root / "app-demo"
    (pkg / "objects" / "site").mkdir(parents=True)
    (pkg / "objects" / "site" / "demo.py").write_text(SOURCE)
    (pkg / "schemas").mkdir()
    (pkg / "schemas" / "nav_entries.json").write_text(json.dumps(NAV_SCHEMA))
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


def entries(package):
    try:
        rows = object_records.read_collection_records("nav_entries", base_dir=package["data"])
    except Exception:
        return {}
    return {row["id"]: row for row in rows}


def set_entry(fixture, nav_id, **changes):
    """An operator editing a row by hand -- `fixture` rather than
    `package` because `package` is itself one of the row's columns."""
    object_records.update_collection_record(
        "nav_entries", nav_id, changes, base_dir=fixture["data"], actor="operator")


# --- registering -------------------------------------------------------------

def test_installing_a_package_registers_its_door(package):
    result = install(package)
    assert [entry["status"] for entry in result["nav"]] == ["written"]

    row = entries(package)["demo"]
    assert row["label"] == "Demo"
    assert row["path"] == "/demo"
    assert row["blurb"] == "A door."
    assert row["group"] == "Work"
    assert row["surface"] == "member"          # the default tier
    assert row["order"] == "10"
    assert row["package"] == "app-demo"        # provenance, stamped by install
    assert row["operator_hidden"] == "false"


def test_the_plan_says_what_it_would_register_before_it_does(package):
    assert plan(package)["nav"] == [{"id": "demo", "label": "Demo", "path": "/demo",
                                     "group": "Work", "surface": "member",
                                     "exists": False, "action": "create"}]
    assert plan(package)["package"]["nav_count"] == 1
    assert entries(package) == {}              # a dry run writes nothing


def test_a_package_with_no_nav_touches_no_records(package):
    write_manifest(package, nav=[])
    result = install(package)
    assert result["nav"] == []
    assert entries(package) == {}


# --- surviving an upgrade ------------------------------------------------------

def test_reinstalling_preserves_the_operators_hidden(package):
    """The package declares what the app IS; the operator decides what
    they want to SEE. An upgrade that silently puts back a door somebody
    deliberately removed is the same incident as one that restarts a
    paused nightly pass."""
    install(package)
    set_entry(package, "demo", operator_hidden="true")

    write_manifest(package, version="1.1.0",
                   nav=[{**MANIFEST["nav"][0], "label": "Demo App"}])
    install(package, allow_replace=True)

    row = entries(package)["demo"]
    assert row["operator_hidden"] == "true"    # untouched
    assert row["label"] == "Demo App"          # but the package's word is restated


def test_reinstalling_updates_a_changed_label(package):
    install(package)
    write_manifest(package, version="1.1.0",
                   nav=[{**MANIFEST["nav"][0], "label": "Renamed", "path": "/renamed",
                         "group": "System", "surface": "operator", "order": 5}])
    result = install(package, allow_replace=True)

    assert result["nav"][0]["action"] == "update"
    assert result["nav"][0]["status"] == "updated"
    row = entries(package)["demo"]
    assert (row["label"], row["path"], row["group"], row["surface"], row["order"]) == (
        "Renamed", "/renamed", "System", "operator", "5")


def test_an_unchanged_entry_reports_unchanged(package):
    install(package)
    result = install(package, allow_replace=True)
    assert result["nav"][0]["status"] == "unchanged"
    assert result["nav"][0]["action"] == "unchanged"


def test_a_plan_that_says_unchanged_writes_nothing_at_all(package):
    """Every package-owned field is compared, blurb and order included:
    a dry run that under-reports is worse than no dry run."""
    install(package)
    write_manifest(package, nav=[{**MANIFEST["nav"][0], "blurb": "Reworded."}])
    assert plan(package)["nav"][0]["action"] == "update"

    result = install(package, allow_replace=True)
    assert result["nav"][0]["status"] == "updated"
    assert entries(package)["demo"]["blurb"] == "Reworded."


def test_an_unchanged_reinstall_does_not_rewrite_the_row(package):
    install(package)
    before = entries(package)["demo"]
    install(package, allow_replace=True)
    assert entries(package)["demo"] == before   # byte-for-byte, created_at included


def test_a_hand_made_entry_is_adopted_not_duplicated(package):
    """Somebody adding a door by hand before its package declared one
    must be taken over, not shadowed by a second row for the same id."""
    install(package)
    set_entry(package, "demo", package="", label="Typed By Hand")
    install(package, allow_replace=True)

    assert len(entries(package)) == 1
    assert entries(package)["demo"]["package"] == "app-demo"


# --- refusing what could not work ----------------------------------------------

def test_two_packages_cannot_fight_over_one_door(package):
    """Whichever installed last would silently win, and the loser's menu
    entry would point somewhere its own package never chose."""
    install(package)
    set_entry(package, "demo", package="app-other")
    with pytest.raises(object_packages.PackageInstallError) as exc:
        install(package, allow_replace=True)
    assert "app-other" in str(exc.value)


def test_a_path_another_package_already_claims_blocks_the_install(package):
    install(package)
    object_records.create_collection_record(
        "nav_entries",
        {"id": "rival", "package": "app-other", "label": "Rival", "path": "/second",
         "group": "Work", "surface": "member", "order": "10"},
        base_dir=package["data"], actor="test")
    write_manifest(package, nav=[{**MANIFEST["nav"][0], "path": "/second"}])

    with pytest.raises(object_packages.PackageInstallError) as exc:
        install(package, allow_replace=True)
    assert "/second" in str(exc.value)


@pytest.mark.parametrize("bad, reason", [
    ({"id": "two words", "label": "X", "path": "/x"}, "nav id"),
    ({"id": "demo", "label": "X", "path": "demo"}, "start with"),
    ({"id": "demo", "label": "X", "path": "/two words"}, "whitespace"),
    ({"id": "demo", "label": "X", "path": "/x\nmore"}, "whitespace"),
    ({"id": "demo", "label": "X", "path": "/x", "surface": "everyone"},
     "public|member|operator|hidden"),
    ({"id": "demo", "label": "X", "path": "/x", "order": "first"}, "integer"),
    ({"id": "demo", "path": "/x"}, "requires 'label'"),
    ({"id": "demo", "label": "X"}, "requires 'path'"),
])
def test_a_door_that_could_not_work_is_refused_at_install(package, bad, reason):
    write_manifest(package, nav=[bad])
    with pytest.raises(object_packages.InvalidPackageManifestError) as exc:
        install(package)
    assert reason in str(exc.value)
    assert entries(package) == {}


def test_two_nav_entries_cannot_share_an_id(package):
    write_manifest(package, nav=[MANIFEST["nav"][0],
                                 {**MANIFEST["nav"][0], "path": "/other"}])
    with pytest.raises(object_packages.InvalidPackageManifestError):
        install(package)


# --- when the nav app is not installed -------------------------------------------

def test_a_missing_registry_skips_gracefully_instead_of_failing(package):
    """A package must never fail to install because the navigation app is
    absent. It says so in the result rather than pretending it wrote."""
    write_manifest(package, schemas=[])
    result = install(package)

    assert [entry["status"] for entry in result["nav"]] == ["skipped"]
    assert "nav_entries" in result["nav"][0]["reason"]
    assert not (package["data"] / "collections" / "nav_entries").exists()


def test_a_missing_registry_still_plans_the_entry(package):
    write_manifest(package, schemas=[])
    assert plan(package)["nav"][0]["action"] == "create"
    assert plan(package)["safe_to_install"] is True


# --- the real packages ------------------------------------------------------------

def _manifests():
    return {path.parent.name: json.loads(path.read_text())
            for path in sorted(PACKAGES.glob("*/dbbasic-package.json"))}


def test_every_package_that_ships_a_page_declares_a_door():
    """The test that stops the drift coming back.

    A package that serves a URL and registers nothing is exactly how the
    hand-maintained lists fell four apps behind without anybody noticing.
    A package whose site_* object is a widget or a per-record document
    rather than a front page says so with `nav_optional`, which makes
    "this app has no front door" a reviewable claim instead of an
    omission.
    """
    missing = []
    for package_id, manifest in _manifests().items():
        ships_page = any(entry["id"].startswith("site_")
                         for entry in manifest.get("objects", []))
        ships_routes = any(entry["collection"] == "site_routes"
                           for entry in manifest.get("seed", []))
        if not (ships_page or ships_routes):
            continue
        if manifest.get("nav") or manifest.get("nav_optional"):
            continue
        missing.append(package_id)
    assert not missing, (
        f"these packages serve a URL and register no nav entry: {missing}. "
        "Declare `nav`, or `nav_optional: true` if the page is not a door.")


def test_shipped_nav_entries_do_not_collide_on_an_id_or_a_path():
    """Two packages writing the same nav_entries row would silently
    overwrite each other; two claiming one path is two doors on one room."""
    seen_ids: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for package_id, manifest in _manifests().items():
        for entry in manifest.get("nav") or []:
            assert entry["id"] not in seen_ids, (
                f"{entry['id']} declared by both {seen_ids.get(entry['id'])} "
                f"and {package_id}")
            seen_ids[entry["id"]] = package_id
            assert entry["path"] not in seen_paths, (
                f"{entry['path']} claimed by both {seen_paths[entry['path']]} "
                f"and {package_id}")
            seen_paths[entry["path"]] = package_id
    assert len(seen_ids) >= 30, "the box's doors should all be declared"


def test_shipped_nav_entries_normalize_and_use_the_house_groups():
    """Six groups, decided once. A seventh would not break anything --
    `group` is free text on purpose -- but it would mean somebody invented
    a heading rather than filing under one that exists."""
    groups = {"Work", "Publishing", "Money", "Commerce", "Warehouse", "System"}
    for package_id in _manifests():
        package = object_packages.get_package(package_id, root=PACKAGES)
        for entry in package["nav"]:
            assert entry["group"] in groups, f"{package_id}: {entry['group']}"
            assert entry["blurb"], f"{package_id}: {entry['id']} has no blurb"
            assert entry["path"].startswith("/")


def test_the_public_tier_is_only_the_pages_a_visitor_should_meet():
    """A `public` surface is a menu decision, not a grant -- but a door
    offered to somebody who will be bounced by permissions is still a
    broken promise, so the list is small and deliberate.

    `privacy` earns its place the same way the other two do: app-privacy
    grants `public` execute on site_privacy, and a privacy policy behind
    a sign-in is not a privacy policy.

    `notary` earns it by the same argument turned up one notch. An
    attestation that only its submitter can look up is not an attestation,
    it is a favour -- the whole value of lodging a digest with an
    independent party is that a THIRD person can check it without an
    account here or a relationship with the operator, and a door they
    cannot find is a door they do not have. app-notary grants `public`
    execute on site_notary to match. Note that the OPERATOR-facing
    `notarizations` list in the same manifest is deliberately not on this
    tier: reading the whole log is a different question from checking one
    digest you were already given.

    `docs` is the plainest case of all: developer documentation behind a
    sign-in is not documentation. app-docs grants `public` execute on all
    three of its page objects and `public` read on doc_pages to match.
    """
    public = {entry["id"]
              for manifest in _manifests().values()
              for entry in manifest.get("nav") or []
              if entry.get("surface") == "public"}
    assert public == {"home", "shop", "privacy", "notary", "docs"}


def test_app_nav_declares_its_own_front_door():
    manifest = _manifests()["app-nav"]
    assert manifest["nav"] == [{
        "id": "home", "label": "Home", "path": "/",
        "blurb": "The switchboard: every app this server installs",
        "surface": "public", "group": "Work", "order": 0}]
