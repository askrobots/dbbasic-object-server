"""Schedules ship with the app that needs them.

Before this, every recurring pass on a running server had been typed into
the scheduler's state by hand and appeared nowhere in the repository: a
rebuilt box came back with no nightly work and said nothing about it. The
properties worth holding are therefore about SURVIVING an install, not
just performing one -- run history is kept, a pause is honoured, and a
schedule pointing at nothing is refused rather than written.
"""

import json
import pathlib

import pytest

import object_packages
import object_state

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"

MANIFEST = {
    "id": "app-demo",
    "name": "Demo",
    "version": "1.0.0",
    "objects": [{"id": "system_nightly", "path": "objects/system/nightly.py"}],
    "schedules": [{"id": "nightly_pass", "object_id": "system_nightly",
                   "schedule": "10 6 * * *",
                   "description": "The nightly pass."}],
}

SOURCE = "def POST(request):\n    return {'ok': True}\n"


@pytest.fixture
def package(tmp_path):
    """A one-object, one-schedule package plus empty install roots."""
    root = tmp_path / "packages"
    pkg = root / "app-demo"
    (pkg / "objects" / "system").mkdir(parents=True)
    (pkg / "objects" / "system" / "nightly.py").write_text(SOURCE)
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


def tasks(package):
    state = object_state.get_object_state("scheduler", base_dir=package["data"])
    return {k[len("task_"):]: json.loads(v)
            for k, v in state.items() if k.startswith("task_")}


def set_task(package, schedule_id, task):
    manager = object_state.ObjectStateManager("scheduler", base_dir=package["data"])
    manager.set(f"task_{schedule_id}", json.dumps(task))


# --- installing ------------------------------------------------------------

def test_installing_a_package_puts_its_schedule_on_the_board(package):
    result = install(package)
    assert [s["status"] for s in result["schedules"]] == ["written"]

    task = tasks(package)["nightly_pass"]
    assert task["object_id"] == "system_nightly"
    assert task["schedule"] == "10 6 * * *"
    assert task["method"] == "POST"
    assert task["status"] == "active"
    # next_run is the daemon's to compute; the package does not guess it.
    assert "next_run" not in task


def test_the_plan_says_what_it_would_schedule_before_it_does(package):
    plan = object_packages.dry_run_package(
        "app-demo", root=package["root"], base_dir=package["data"],
        object_roots=[package["objects"]])
    assert plan["schedules"] == [{"id": "nightly_pass", "object_id": "system_nightly",
                                  "schedule": "10 6 * * *", "type": "cron",
                                  "exists": False, "action": "create"}]
    assert plan["package"]["schedule_count"] == 1
    assert tasks(package) == {}          # a dry run writes nothing


def test_a_package_with_no_schedules_touches_no_state(package):
    write_manifest(package, schedules=[])
    result = install(package)
    assert result["schedules"] == []
    assert not (package["data"] / "state" / "scheduler").exists()


# --- surviving an upgrade ----------------------------------------------------

def test_reinstalling_keeps_the_run_history(package):
    """An upgrade that forgets when a pass last worked makes 'is this
    running?' unanswerable."""
    install(package)
    set_task(package, "nightly_pass", {**tasks(package)["nightly_pass"],
                                       "last_run": 1784959800, "run_count": 12,
                                       "next_run": 1785046200})
    install(package, allow_replace=True)

    task = tasks(package)["nightly_pass"]
    assert task["run_count"] == 12 and task["last_run"] == 1784959800
    assert task["next_run"] == 1785046200      # unchanged cron keeps tonight's firing


def test_a_paused_task_stays_paused(package):
    """The package declares what SHOULD run; an operator decides what does
    right now. A reinstall that restarts a deliberately-stopped pass is how
    an upgrade becomes an incident."""
    install(package)
    set_task(package, "nightly_pass", {**tasks(package)["nightly_pass"],
                                       "status": "paused"})
    result = install(package, allow_replace=True)

    assert tasks(package)["nightly_pass"]["status"] == "paused"
    assert result["schedules"][0]["task_status"] == "paused"


def test_changing_the_cron_reschedules_instead_of_leaving_a_stale_firing(package):
    install(package)
    set_task(package, "nightly_pass", {**tasks(package)["nightly_pass"],
                                       "next_run": 1785046200, "run_count": 3})
    write_manifest(package, version="1.1.0",
                   schedules=[{**MANIFEST["schedules"][0], "schedule": "30 2 * * *"}])
    result = install(package, allow_replace=True)

    task = tasks(package)["nightly_pass"]
    assert task["schedule"] == "30 2 * * *"
    assert task["next_run"] is None            # the daemon recomputes it
    assert task["run_count"] == 3              # but history is not reset
    assert result["schedules"][0]["action"] == "update"


def test_an_unchanged_schedule_reports_unchanged(package):
    install(package)
    result = install(package, allow_replace=True)
    assert result["schedules"][0]["status"] == "unchanged"
    assert result["schedules"][0]["action"] == "unchanged"


def test_a_hand_made_task_is_adopted_not_duplicated(package):
    """Every schedule on the demo box was hand-entered before packages
    could declare them; installing must take ownership of that row rather
    than run the same pass twice."""
    set_task(package, "nightly_pass",
             {"id": "nightly_pass", "object_id": "system_nightly", "method": "POST",
              "payload": {}, "schedule": "10 6 * * *", "type": "cron",
              "status": "active", "run_count": 40})
    install(package)
    assert len(tasks(package)) == 1
    assert tasks(package)["nightly_pass"]["run_count"] == 40


# --- refusing what could not run ----------------------------------------------

def test_a_schedule_aimed_at_a_missing_object_blocks_the_install(package):
    """The silent failure this whole feature exists to end: a task that
    fires nightly into nothing."""
    write_manifest(package, schedules=[{"id": "nightly_pass",
                                        "object_id": "system_absent",
                                        "schedule": "10 6 * * *"}])
    with pytest.raises(object_packages.PackageInstallError) as exc:
        install(package)
    assert "system_absent" in str(exc.value)
    assert tasks(package) == {}


def test_a_schedule_may_target_an_object_already_on_the_server(package):
    (package["objects"] / "system").mkdir()
    (package["objects"] / "system" / "other.py").write_text(SOURCE)
    write_manifest(package, schedules=[{"id": "nightly_pass",
                                        "object_id": "system_other",
                                        "schedule": "10 6 * * *"}])
    assert install(package)["schedules"][0]["status"] == "written"


@pytest.mark.parametrize("bad, reason", [
    ({"id": "nightly pass", "object_id": "system_nightly", "schedule": "10 6 * * *"},
     "schedule id"),
    ({"id": "nightly_pass", "object_id": "system_nightly", "schedule": "not a cron"},
     "cron"),
    ({"id": "nightly_pass", "object_id": "system_nightly", "schedule": "10 6 * * *",
      "type": "hourly"}, "cron|onetime"),
    ({"id": "nightly_pass", "object_id": "system_nightly", "schedule": "10 6 * * *",
      "method": "TRACE"}, "method"),
    ({"id": "nightly_pass", "object_id": "system_nightly", "schedule": "10 6 * * *",
      "payload": "everything"}, "payload"),
])
def test_a_schedule_that_could_never_fire_is_refused_at_install(package, bad, reason):
    """The daemon treats an unparseable expression as 'no next run' and
    moves on in silence, so this has to be caught here or not at all."""
    write_manifest(package, schedules=[bad])
    with pytest.raises(object_packages.InvalidPackageManifestError) as exc:
        install(package)
    assert reason in str(exc.value)


def test_two_schedules_cannot_share_an_id(package):
    write_manifest(package, schedules=[MANIFEST["schedules"][0],
                                       {**MANIFEST["schedules"][0],
                                        "schedule": "0 3 * * *"}])
    with pytest.raises(object_packages.InvalidPackageManifestError):
        install(package)


def test_a_onetime_schedule_takes_a_timestamp(package):
    write_manifest(package, schedules=[{"id": "one_off", "object_id": "system_nightly",
                                        "type": "onetime",
                                        "schedule": "2026-08-01T06:00:00Z"}])
    assert install(package)["schedules"][0]["status"] == "written"
    write_manifest(package, schedules=[{"id": "one_off", "object_id": "system_nightly",
                                        "type": "onetime", "schedule": "soon"}])
    with pytest.raises(object_packages.InvalidPackageManifestError):
        install(package)


# --- the real packages ----------------------------------------------------------

def test_every_shipped_schedule_names_an_object_its_package_installs():
    """A schedule and the object it calls must travel together, or a
    rebuilt box gets one without the other."""
    found = 0
    for manifest_path in sorted(PACKAGES.glob("*/dbbasic-package.json")):
        manifest = json.loads(manifest_path.read_text())
        schedules = manifest.get("schedules") or []
        if not schedules:
            continue
        provided = {entry["id"] for entry in manifest.get("objects", [])}
        for schedule in schedules:
            assert schedule["object_id"] in provided, (
                f"{manifest_path.parent.name}: {schedule['id']} targets "
                f"{schedule['object_id']}, which it does not install")
            found += 1
    assert found >= 7, "the demo box's recurring passes should all be declared"


def test_shipped_schedules_do_not_collide_on_an_id_or_a_minute():
    """Two packages writing the same task_ key would silently overwrite
    each other; two heavy passes on the same minute would merely be rude."""
    seen_ids: dict[str, str] = {}
    seen_slots: dict[str, str] = {}
    for manifest_path in sorted(PACKAGES.glob("*/dbbasic-package.json")):
        manifest = json.loads(manifest_path.read_text())
        for schedule in manifest.get("schedules") or []:
            package_id = manifest["id"]
            assert schedule["id"] not in seen_ids, (
                f"{schedule['id']} declared by both {seen_ids.get(schedule['id'])} "
                f"and {package_id}")
            seen_ids[schedule["id"]] = package_id
            if schedule.get("type", "cron") != "cron":
                continue
            assert schedule["schedule"] not in seen_slots, (
                f"{schedule['schedule']} used by both "
                f"{seen_slots[schedule['schedule']]} and {schedule['id']}")
            seen_slots[schedule["schedule"]] = schedule["id"]


def test_billing_folds_usage_before_it_rates_the_period():
    """Ordering is load-bearing: a period closing today must be rated
    against usage that has already been folded into summaries."""
    manifest = json.loads((PACKAGES / "app-billing" / "dbbasic-package.json").read_text())
    minute_of = {s["object_id"]: (lambda p: int(p[1]) * 60 + int(p[0]))(
        s["schedule"].split()) for s in manifest["schedules"]}
    assert minute_of["system_usage_rollup"] < minute_of["system_billing_runner"]
