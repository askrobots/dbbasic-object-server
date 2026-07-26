"""The rollup: counts computed on a timer, not on the front page.

`object_daemon.process_attention` runs every declared provider's COUNT
method and writes `attention_counts`, which the home page and the app
list read. The reason it is a pass and not a helper the page calls is
concrete: this box went to 675MB resident and started swapping because
something folded a big collection on a timer, and a home page that folds
a dozen collections on every render is that same mistake with a nicer
name. So the properties worth holding are about what the stored numbers
are allowed to SAY.

The one that matters most is the failure case. A provider that raises and
gets recorded as zero would report an empty queue when what happened is
that nobody looked -- a queue that looks handled, which is the worst
thing this table could do. A broken provider must look broken.
"""

import pathlib

import pytest

import object_daemon
import object_packages
import object_records

PACKAGES_ROOT = pathlib.Path(__file__).resolve().parents[1] / "packages"

COUNTS = "attention_counts"
SOURCES = "attention_sources"

GOOD = "def COUNT(request):\n    return {'count': 3, 'detail': '1 over a week old'}\n"
EMPTY = "def COUNT(request):\n    return {'count': 0}\n"
ANGRY = "def COUNT(request):\n    raise RuntimeError('the shed is on fire')\n"
NONSENSE = "def COUNT(request):\n    return {'count': 'lots'}\n"
MUTE = "def COUNT(request):\n    return {'count': 9}\n"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """app-nav installed into empty roots, with the environment pointed at
    them: the pass resolves providers through the same roots the server
    would use."""
    data = tmp_path / "data"
    objects = tmp_path / "objects"
    objects.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects))
    monkeypatch.delenv("DBBASIC_ATTENTION_INTERVAL_SECONDS", raising=False)

    object_packages.install_package(
        "app-nav", root=PACKAGES_ROOT, base_dir=data, object_roots=[objects])
    return {"data": data, "objects": objects}


def provider(box, object_id, source):
    (box["objects"] / f"{object_id}.py").write_text(source)


def declare(box, source_id, object_id, **fields):
    row = {"id": source_id, "package": "app-test", "object_id": object_id,
           "label": source_id.replace("_", " ").title(), "path": f"/{source_id}",
           "nav_id": "", "group": "Work", "severity": "normal",
           "operator_muted": "false"}
    row.update(fields)
    object_records.create_collection_record(
        SOURCES, row, base_dir=box["data"], actor="test")


def counts(box):
    try:
        rows = object_records.read_collection_records(COUNTS, base_dir=box["data"])
    except Exception:
        return {}
    return {row["id"]: row for row in rows}


def run(box):
    """One pass, with the interval marker cleared so it is always due.

    Every test but the interval one is about what the pass WRITES, and
    making each of them wait five minutes would be a slow way to test
    nothing.
    """
    marker = box["data"] / object_daemon.ATTENTION_MARKER_NAME
    if marker.exists():
        marker.unlink()
    return object_daemon.process_attention(base_dir=box["data"])


# --- the pass ------------------------------------------------------------------

def test_the_pass_writes_a_count_per_declared_queue(box):
    provider(box, "system_demo_attention", GOOD)
    declare(box, "demo_queue", "system_demo_attention",
            label="Demo queue", path="/demo", nav_id="home", severity="warning")

    result = run(box)
    assert result == {"sources": 1, "counted": 1, "failed": 0, "muted": 0}

    row = counts(box)["demo_queue"]
    assert row["count"] == "3"
    assert row["detail"] == "1 over a week old"
    assert row["source_id"] == "demo_queue"
    assert row["label"] == "Demo queue"
    assert row["path"] == "/demo"
    assert row["nav_id"] == "home"
    assert row["severity"] == "warning"
    assert row["error"] == ""
    assert row["computed_at"]


def test_a_second_pass_updates_rather_than_duplicating(box):
    provider(box, "system_demo_attention", GOOD)
    declare(box, "demo_queue", "system_demo_attention")
    run(box)
    provider(box, "system_demo_attention",
             "def COUNT(request):\n    return {'count': 1}\n")
    run(box)

    assert len(counts(box)) == 1
    assert counts(box)["demo_queue"]["count"] == "1"
    assert counts(box)["demo_queue"]["detail"] == ""   # the old sentence is gone


def test_a_zero_is_written_down_rather_than_left_unsaid(box):
    """The absence of a row means nobody has ever run the provider, which
    is a different fact from 'the queue is empty'. Only the surfaces
    decide that zero is not news."""
    provider(box, "system_quiet_attention", EMPTY)
    declare(box, "quiet_queue", "system_quiet_attention")

    run(box)
    assert counts(box)["quiet_queue"]["count"] == "0"


def test_one_bad_provider_does_not_blind_the_others(box):
    provider(box, "system_demo_attention", GOOD)
    provider(box, "system_angry_attention", ANGRY)
    declare(box, "demo_queue", "system_demo_attention")
    declare(box, "angry_queue", "system_angry_attention")

    result = run(box)
    assert (result["counted"], result["failed"]) == (1, 1)
    assert counts(box)["demo_queue"]["count"] == "3"


# --- a broken provider must look broken -----------------------------------------

def test_a_provider_that_raises_records_the_error_and_keeps_the_count(box):
    """Zeroing it would report an empty queue when what happened is that
    nobody looked -- a queue that looks handled is the worst thing this
    table could say."""
    provider(box, "system_demo_attention", GOOD)
    declare(box, "demo_queue", "system_demo_attention")
    run(box)
    was = counts(box)["demo_queue"]

    provider(box, "system_demo_attention", ANGRY)
    result = run(box)

    assert result["failed"] == 1
    row = counts(box)["demo_queue"]
    assert row["count"] == "3"                     # the last real number survives
    assert "the shed is on fire" in row["error"]
    # And so does its timestamp: the age on the page describes the number,
    # not the attempt.
    assert row["computed_at"] == was["computed_at"]


def test_a_provider_that_answers_with_nonsense_is_a_failure_not_a_zero(box):
    provider(box, "system_demo_attention", GOOD)
    declare(box, "demo_queue", "system_demo_attention")
    run(box)

    provider(box, "system_demo_attention", NONSENSE)
    assert run(box)["failed"] == 1
    row = counts(box)["demo_queue"]
    assert row["count"] == "3"
    assert "expected an integer" in row["error"]


def test_a_provider_that_never_worked_records_the_error_against_zero(box):
    """No previous number to keep is not a reason to invent one, and the
    error is what says the zero is not an answer."""
    provider(box, "system_angry_attention", ANGRY)
    declare(box, "angry_queue", "system_angry_attention")
    run(box)

    row = counts(box)["angry_queue"]
    assert row["count"] == "0"
    assert row["error"]
    assert row["computed_at"] == ""


def test_a_missing_provider_object_is_an_error_not_a_silent_zero(box):
    declare(box, "ghost_queue", "system_nowhere_attention")
    assert run(box)["failed"] == 1
    assert counts(box)["ghost_queue"]["error"]


def test_a_recovered_provider_clears_its_error(box):
    provider(box, "system_demo_attention", ANGRY)
    declare(box, "demo_queue", "system_demo_attention")
    run(box)
    provider(box, "system_demo_attention", GOOD)
    run(box)

    row = counts(box)["demo_queue"]
    assert row["error"] == ""
    assert row["count"] == "3"


# --- the operator's mute -----------------------------------------------------------

def test_a_muted_source_is_not_run_and_leaves_no_stale_number(box):
    """A muted queue should cost nothing and show nothing, and leaving
    the last number behind would show a stale one."""
    provider(box, "system_mute_attention", MUTE)
    declare(box, "mute_queue", "system_mute_attention")
    run(box)
    assert counts(box)["mute_queue"]["count"] == "9"

    object_records.update_collection_record(
        SOURCES, "mute_queue", {"operator_muted": "true"},
        base_dir=box["data"], actor="operator")
    result = run(box)

    assert result == {"sources": 1, "counted": 0, "failed": 0, "muted": 1}
    assert "mute_queue" not in counts(box)


# --- when to run at all ---------------------------------------------------------

def test_the_interval_marker_is_honoured(box, monkeypatch):
    """A count that is five minutes stale is fine; a pass that re-runs a
    dozen providers on every one-second poll is not."""
    provider(box, "system_demo_attention", GOOD)
    declare(box, "demo_queue", "system_demo_attention")

    assert run(box) is not None                    # first call: no marker yet
    assert object_daemon.process_attention(base_dir=box["data"]) is None

    monkeypatch.setenv("DBBASIC_ATTENTION_INTERVAL_SECONDS", "0")
    assert object_daemon.process_attention(base_dir=box["data"]) is not None


def test_nothing_declared_is_not_a_pass(box):
    assert object_daemon.process_attention(base_dir=box["data"]) is None
    assert not (box["data"] / object_daemon.ATTENTION_MARKER_NAME).exists()


# --- the providers this repository actually ships ----------------------------------

def _shipped_providers():
    import json
    for manifest_path in sorted(PACKAGES_ROOT.glob("*/dbbasic-package.json")):
        manifest = json.loads(manifest_path.read_text())
        paths = {entry["id"]: entry["path"] for entry in manifest.get("objects", [])}
        for entry in manifest.get("attention") or []:
            yield (manifest["id"], entry["object_id"],
                   manifest_path.parent / paths[entry["object_id"]])


@pytest.mark.parametrize("package_id, object_id, source_path",
                         list(_shipped_providers()),
                         ids=lambda value: str(value)[:40])
def test_every_shipped_provider_reads_zero_on_an_empty_box(
        package_id, object_id, source_path, tmp_path, monkeypatch):
    """A deployment that never installed the app must not produce an error
    every five minutes. A provider degrades to zero when its collections
    are absent; only a genuine failure raises."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data))

    module = {}
    exec(compile(source_path.read_text(), str(source_path), "exec"), module)
    assert module["COUNT"]({}) == {"count": 0}


def test_a_box_without_the_home_app_runs_nothing_and_says_nothing(tmp_path, monkeypatch):
    """The pass must not raise, log, or create files on a deployment that
    never installed app-nav."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data))
    assert object_daemon.process_attention(base_dir=data) is None
