"""CHARACTERIZATION tests: does object_records.py's write path stay correct
under CONCURRENT writers/readers, and what is its actual concurrency-safety
boundary?

Pure characterization of EXISTING behavior. Nothing in object_records.py (or
any other production module) is modified by this file. Where a probe finds a
real gap, the assertion documents the correctness property a database-shaped
substrate SHOULD have and is left in place -- do not "fix" a finding here by
weakening the assertion to match the gap.

THE LOCK, READ FROM SOURCE FIRST (then verified empirically below):
`_records_file_lock` (object_records.py, ~line 2703) is NOT a
`threading.Lock` keyed by path (that would be a plausible, reasonable
mechanism, and is what this file's brief assumed going in) -- it is an
`fcntl.flock(LOCK_EX)` advisory lock held on a sidecar `.records.tsv.lock`
file, acquired fresh (a new `open("a")` file descriptor) on every
create/update/delete/compact call and released when the `with` block exits.
That distinction matters a great deal for this file's central question:

  - flock is a KERNEL-level lock associated with the *open file description*
    (not the process, and not the Python-level lock object), so it
    correctly serializes BOTH concurrent threads in the same process
    (each thread's own `with lock_path.open("a")` gets its own fd/open
    file description, and flock blocks across distinct descriptions even
    within one process) AND concurrent separate OS processes on the same
    machine sharing the same filesystem path -- there is no dependency on
    both callers being the same Python interpreter, unlike a
    `threading.Lock`, which cannot be observed at all outside its own
    process.
  - It is advisory, not mandatory: it only serializes writers that
    themselves call `_records_file_lock` (i.e. every writer inside this
    module). Nothing stops a process that opens records.tsv directly and
    writes to it without going through object_records.py.
  - Classic-mode full rewrites additionally write via a tempfile +
    `Path.replace()` (atomic rename) rather than in-place, so even a READER
    that holds no lock at all (readers never take `_records_file_lock` --
    see the CONCURRENCY block comment above _RECORDS_CACHE) always sees a
    complete pre- or post-write file, never a torn one -- the lock's job in
    that mode is only to serialize writers' read-modify-write cycles
    against EACH OTHER, not against readers.
  - Append-mode's fast path (`_append_records_rows`) is a genuine in-place
    mutation (`open(path, "a")` + `csv.writer.writerow` + flush) -- there is
    no atomic-rename safety net for this path; the flock is the ONLY thing
    preventing two writers' single `write()` calls from interleaving on
    disk. This is exactly the case this file's cross-process probes target.

So the headline question this file exists to answer empirically is not
"is the lock cross-thread only" (source review already answers that: no,
flock is not thread-scoped) but "does the flock-based mechanism actually
hold up under real concurrent contention, in both storage modes, across
both threads and separate OS processes" -- and separately, to characterize
honestly what current same-record concurrent UPDATE semantics are (last-
writer-wins; optimistic concurrency / compare-and-set is spec 63, not yet
built, so nothing here detects or rejects a lost concurrent update).

FINDINGS (all 13 tests below PASS, consistently across repeated runs; this
summary is the worst-first rollup -- worst first meaning "most surprising
if you assumed the naive per-process-only-lock model", not "most broken":
nothing here is broken):

  1. Cross-process DISTINCT-record writes, classic AND append mode: SAFE.
     4 separate OS processes (subprocess, not threads -- fresh interpreters,
     cold caches, sharing nothing but the files on disk), barrier-released
     to overlap in time, each creating 20 distinct records into the same
     collection: all 80 records survive, none garbled, in both modes. This
     is the headline result and it contradicts the naive assumption a
     `threading.Lock` mechanism would predict (that would provide ZERO
     cross-process exclusion): `_records_file_lock` is actually
     `fcntl.flock`, a kernel-level advisory lock keyed by the lock file's
     path, not by the calling interpreter -- so it DOES serialize
     independent processes, not just threads within one process.
  2. Cross-process SAME-record concurrent UPDATE: SAFE from corruption,
     last-writer-wins as expected. 4 processes updating one record
     concurrently always leave it holding exactly one clean submitted
     value, never a merge/garble -- confirming the lock's cross-process
     exclusion holds for the read-modify-write update path too, not just
     the append-only create path.
  3. Same-process (thread) DISTINCT-record writes, classic AND append,
     N=8 and N=32: SAFE. No lost or duplicated writes, no garbled rows.
  4. Same-process (thread) SAME-record concurrent UPDATE, both modes:
     SAFE from corruption, last-writer-wins. 16 threads updating one
     record concurrently always land on exactly one submitted value. This
     is the CURRENT concurrent-update guarantee, and it is a real gap:
     15 of 16 updates are silently discarded from live state with no error,
     no conflict signal, and no way for a caller to know its update was
     clobbered -- this is exactly the hole optimistic concurrency /
     compare-and-set (spec 63) is meant to fill. Notably, the discarded
     updates are NOT lost from the durable audit trail: object_record_
     changes' append-only JSONL log recorded all 16 update events (with
     all 16 distinct submitted values) even though get_collection_record
     shows only the winner -- so "last-writer-wins" is a live-state
     property, not a durability/logging one.
  5. Concurrent readers alongside a sustained writer, both modes: SAFE.
     Readers hold no lock at all (by design) and never raised, never saw a
     garbled row, across a writer creating 150 records while 4 reader
     threads hammered read_collection_records/get_collection_record/
     list_collection_records concurrently. A reader may transiently see a
     record count that lags the writer (expected, unasserted) but the file
     is never observed torn -- classic mode's atomic tempfile+replace and
     append mode's flush-after-write both hold up under concurrent reads.
  6. id->offset oidx sidecar under concurrent same-process appends: SAFE.
     After 24 threads concurrently create distinct records (forced through
     the cold-cache sidecar path via DBBASIC_RECORDS_CACHE_MAX_ROWS=0),
     every id resolves to its correct value via the offset-indexed by-id
     read, agreeing with a full fold -- the sidecar's documented
     disposability/self-heal ("any reader unable to make sense of it...
     rebuilds it") held up in practice under contention.

BOTTOM LINE: the lock is not a threading.Lock and not scoped to one
process -- it is a real fcntl.flock advisory file lock, and it is
effective across both threads AND separate OS processes (verified via
actual subprocesses, not simulated). For a multi-worker uvicorn/gunicorn
deployment (multiple OS processes sharing the same data directory), this
means concurrent writes are NOT the unsafe case some deployments would
fear -- corruption/lost-write risk was not observed in any probe here. The
one real, honest gap is same-record concurrent UPDATE semantics: pure
last-writer-wins, with silent overwrite of losing updates and no conflict
detection -- present identically whether the concurrent writers are threads
in one process or separate processes, and unrelated to the lock's
effectiveness. That gap is spec 63's to close, not a locking bug.

CAVEATS (not probed by, or outside the scope of, this file):
  - flock is advisory: a process that opens records.tsv directly without
    going through object_records.py's write path is not blocked by it.
  - flock's cross-process guarantee assumes a shared local filesystem with
    working flock semantics; it is well-known to be unreliable-to-absent
    over NFS and some network/container filesystem configurations, which
    were not exercised here (this ran against a local disk).
  - Only 4 concurrent processes / up to 32 concurrent threads were probed;
    this characterizes correctness under contention, not throughput or
    lock-wait latency at higher fan-out.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import object_record_changes
import object_records

pytestmark = pytest.mark.conformance

ID_FIELD = {"name": "id"}
VALUE_FIELD = {"name": "value", "type": "textarea"}

REPO_ROOT = str(Path(object_records.__file__).resolve().parent)


# --- setup helpers (mirror tests/test_embedded_json_lines_characterization.py) --


def write_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields}))
    return path


def write_append_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields, "storage": "append"}))
    return path


def _clear_caches() -> None:
    object_records._RECORDS_CACHE.clear()
    object_records._OIDX_CACHE.clear()


def _run_threads(target, count: int) -> list[BaseException | None]:
    """Run `count` copies of target(i, barrier) as threads, all released by
    a shared Barrier at (as close to) the same instant, to maximize actual
    lock contention rather than letting the OS schedule them apart. Returns
    one exception (or None) per thread, in thread-index order."""
    barrier = threading.Barrier(count)
    results: list[BaseException | None] = [None] * count

    def wrapper(i: int) -> None:
        try:
            target(i, barrier)
        except BaseException as exc:  # noqa: BLE001 -- captured, not swallowed
            results[i] = exc

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


# =============================================================================
# 1. SAME-PROCESS (threads) -- N DISTINCT records created concurrently
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
@pytest.mark.parametrize("n", [8, 32])
def test_same_process_concurrent_distinct_creates_no_lost_writes(tmp_path, storage, n):
    """N threads each CREATE a DISTINCT record id into ONE collection,
    released simultaneously by a Barrier. Property under test: the
    flock-based _records_file_lock genuinely serializes concurrent
    create_collection_record calls within one process (both classic's
    tempfile+replace path and append's in-place open("a") fast path), so
    no writer's row is lost and the file never ends up corrupt (unparseable
    or with a wrong row count) even though every thread's create does its
    own read-modify-write (duplicate-id check, then persist) inside the
    lock. RESULT: safe -- all N records survive, no exceptions, no
    duplicate/garbled rows."""
    data_dir = tmp_path / "data"
    collection = "widgets"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])

    def worker(i: int, barrier: threading.Barrier) -> None:
        barrier.wait(timeout=10)
        object_records.create_collection_record(
            collection,
            {"id": f"rec-{i:04d}", "value": f"thread-{i}"},
            base_dir=data_dir,
            roots=[],
        )

    errors = _run_threads(worker, n)
    assert errors == [None] * n, f"unexpected exceptions from concurrent creates: {errors}"

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    ids = sorted(r["id"] for r in all_records)
    expected = sorted(f"rec-{i:04d}" for i in range(n))
    assert ids == expected, (
        f"lost or duplicated writes under same-process concurrency: got {len(ids)} of "
        f"{n} expected records"
    )
    for r in all_records:
        i = int(r["id"].rsplit("-", 1)[1])
        assert r["value"] == f"thread-{i}", f"corrupted/cross-wired row: {r!r}"


# =============================================================================
# 2. SAME-PROCESS (threads) -- N writers UPDATING the SAME record concurrently
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_same_process_concurrent_same_record_update_is_last_writer_wins(tmp_path, storage):
    """N threads all UPDATE the SAME existing record id concurrently, each
    writing a distinct, easily-attributable value. There is no optimistic
    concurrency control in this codebase yet (compare-and-set is spec 63,
    not built) -- update_collection_record's read-modify-write is only
    serialized against OTHER writers by _records_file_lock, never checked
    against a caller-supplied "expected prior version". So the CORRECT,
    EXPECTED outcome here is last-writer-wins: the record's final value must
    be turned into EXACTLY ONE of the N submitted values (proving no
    interleaving/corruption of the row itself), while the other N-1 updates
    are silently overwritten with no error and no conflict signal to their
    caller -- that overwrite is precisely the gap spec 63 is meant to close.
    This test also confirms the durable change LOG (object_record_changes,
    a separate JSONL append) still records all N update events even though
    current-state only reflects the winner -- i.e. the update itself isn't
    lost from history, only from the live row. RESULT: safe from
    corruption, but N-1 of N updates are silently discarded from live
    state (expected LWW, not a bug -- the spec-63 gap, characterized
    honestly)."""
    data_dir = tmp_path / "data"
    collection = "counters"
    n = 16
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])

    object_records.create_collection_record(
        collection, {"id": "shared", "value": "seed"}, base_dir=data_dir, roots=[]
    )

    candidates = [f"writer-{i:04d}" for i in range(n)]

    def worker(i: int, barrier: threading.Barrier) -> None:
        barrier.wait(timeout=10)
        object_records.update_collection_record(
            collection, "shared", {"value": candidates[i]}, base_dir=data_dir, roots=[]
        )

    errors = _run_threads(worker, n)
    assert errors == [None] * n, f"unexpected exceptions from concurrent same-record updates: {errors}"

    _clear_caches()
    final = object_records.get_collection_record(collection, "shared", base_dir=data_dir, roots=[])
    assert final["value"] in candidates, (
        f"CORRUPTION (not LWW): final value {final['value']!r} is not any single submitted "
        f"value -- this would indicate a torn/interleaved write, not last-writer-wins"
    )

    changes = object_record_changes.list_record_changes(
        collection, record_id="shared", base_dir=data_dir, limit=1000
    )
    update_changes = [c for c in changes["changes"] if c.get("action") == "update"]
    assert len(update_changes) == n, (
        f"change log lost events under concurrency: expected {n} update entries, "
        f"got {len(update_changes)} -- the durable log itself should never drop a write "
        f"even though live state only keeps the last one"
    )
    logged_values = {c["after"]["value"] for c in update_changes if c.get("after")}
    assert logged_values == set(candidates), (
        "change log did not capture all N submitted values even though it recorded N events"
    )


# =============================================================================
# 3. Concurrent READERS alongside a sustained WRITER (readers hold no lock)
# =============================================================================


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_concurrent_readers_never_see_corruption_under_sustained_writes(tmp_path, storage):
    """Readers take NO lock at all (see the _RECORDS_CACHE CONCURRENCY block
    comment in object_records.py) -- they rely entirely on classic mode's
    atomic tempfile+replace, and append mode's append-then-flush plus the
    fact that csv.reader/the oidx scan are torn-tail-tolerant, to never
    observe a half-written file. One writer thread continuously creates new
    records while several reader threads continuously call
    read_collection_records / get_collection_record / list_collection_records
    concurrently, for a fixed number of iterations (not wall-clock time, to
    keep this fast and deterministic-ish). Property under test: a reader
    may see a record count that lags the writer (a benign, expected race --
    NOT asserted against), but must NEVER raise from a parse error and must
    NEVER return a row with a wrong/garbled value for an id it does return.
    RESULT: safe -- no reader exception, no garbled row observed across the
    run, for both storage modes."""
    data_dir = tmp_path / "data"
    collection = "stream"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])

    object_records.create_collection_record(
        collection, {"id": "seed-0000", "value": "seed"}, base_dir=data_dir, roots=[]
    )

    WRITE_COUNT = 150
    READ_ITERATIONS = 400
    stop = threading.Event()
    reader_errors: list[BaseException] = []
    lock = threading.Lock()

    def writer() -> None:
        for i in range(WRITE_COUNT):
            object_records.create_collection_record(
                collection,
                {"id": f"live-{i:04d}", "value": f"payload-{i:04d}"},
                base_dir=data_dir,
                roots=[],
            )
        stop.set()

    def reader() -> None:
        seen = 0
        while not stop.is_set() or seen < READ_ITERATIONS:
            try:
                all_records = object_records.read_collection_records(
                    collection, base_dir=data_dir, roots=[]
                )
                for r in all_records:
                    if r["id"] == "seed-0000":
                        assert r["value"] == "seed"
                    elif r["id"].startswith("live-"):
                        idx = r["id"].rsplit("-", 1)[1]
                        assert r["value"] == f"payload-{idx}", f"garbled row: {r!r}"
                one = object_records.get_collection_record(
                    collection, "seed-0000", base_dir=data_dir, roots=[]
                )
                assert one["value"] == "seed"
                object_records.list_collection_records(
                    collection, base_dir=data_dir, roots=[], limit=1000
                )
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    reader_errors.append(exc)
                return
            seen += 1
            if seen >= READ_ITERATIONS and stop.is_set():
                return

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(4)]
    writer_thread.start()
    for t in reader_threads:
        t.start()
    writer_thread.join(timeout=60)
    for t in reader_threads:
        t.join(timeout=60)

    assert not reader_errors, f"reader(s) observed corruption/exception: {reader_errors!r}"

    _clear_caches()
    final = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    assert len(final) == WRITE_COUNT + 1


# =============================================================================
# 4. CROSS-PROCESS -- the central question: is the lock effective across OS
#    processes, not just threads?
# =============================================================================


_WORKER_SCRIPT = '''\
import argparse
import json
import sys
import time
from pathlib import Path


def _wait_for_barrier(barrier_path):
    while not Path(barrier_path).exists():
        time.sleep(0.001)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root")
    parser.add_argument("data_dir")
    parser.add_argument("collection")
    parser.add_argument("mode", choices=["create_batch", "update_same"])
    parser.add_argument("payload")
    parser.add_argument("--barrier", default="")
    args = parser.parse_args()

    sys.path.insert(0, args.repo_root)
    import object_records

    payload = json.loads(args.payload)
    if args.barrier:
        _wait_for_barrier(args.barrier)

    if args.mode == "create_batch":
        id_prefix = payload["id_prefix"]
        count = payload["count"]
        for i in range(count):
            object_records.create_collection_record(
                args.collection,
                {"id": "%s-%04d" % (id_prefix, i), "value": id_prefix},
                base_dir=args.data_dir,
                roots=[],
            )
    elif args.mode == "update_same":
        object_records.update_collection_record(
            args.collection,
            payload["record_id"],
            {"value": payload["value"]},
            base_dir=args.data_dir,
            roots=[],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _write_worker_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "_cp_worker.py"
    script_path.write_text(_WORKER_SCRIPT)
    return script_path


def _launch(script_path: Path, *args: str, barrier: Path | None = None) -> subprocess.Popen:
    cmd = [sys.executable, str(script_path), *args]
    if barrier is not None:
        cmd += ["--barrier", str(barrier)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _release_barrier_soon(barrier_path: Path, delay: float = 0.3) -> None:
    """Give every launched subprocess time to get past interpreter startup
    and reach its barrier-poll loop, THEN drop the barrier file so all
    processes resume as close to simultaneously as OS scheduling allows --
    this is what makes the probe actually exercise contention on the flock
    instead of processes serializing naturally through staggered startup."""
    time.sleep(delay)
    barrier_path.write_text("go")


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_cross_process_concurrent_distinct_creates_no_lost_writes_or_corruption(
    tmp_path, storage
):
    """THE central probe: 4 separate OS processes (subprocess, not threads
    -- each a fresh Python interpreter with its own memory, its own cold
    _RECORDS_CACHE/_OIDX_CACHE, sharing nothing with the parent test process
    or each other except the records.tsv/.lock/.oidx files on disk), each
    creating 20 DISTINCT record ids into the SAME collection, released via
    a shared barrier file so their writes genuinely overlap in time.

    Property under test: is `_records_file_lock`'s fcntl.flock actually
    effective ACROSS processes -- i.e. does it prevent two independent
    processes' read-modify-write cycles (duplicate-id check + persist) from
    interleaving on disk -- or, as would be the case if the mechanism were
    a plain in-process `threading.Lock` (each process would have its OWN,
    mutually-invisible lock object, providing zero cross-process exclusion),
    can two processes corrupt or lose each other's writes?

    RESULT: safe. flock is a kernel-level advisory lock keyed by the lock
    file's path, not by the calling process/interpreter, so it correctly
    serializes all 4 processes' writes: every one of the 80 records
    survives, the file parses cleanly, and no row is garbled -- in BOTH
    classic mode (tempfile+replace per full rewrite) and append mode
    (in-place open("a") fast-path, which has NO atomic-rename safety net of
    its own and depends entirely on the lock for this property)."""
    data_dir = tmp_path / "data"
    collection = "cp_widgets"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])

    script_path = _write_worker_script(tmp_path)
    barrier_path = tmp_path / "barrier_distinct"
    num_procs = 4
    per_proc = 20

    procs = []
    for p in range(num_procs):
        payload = json.dumps({"id_prefix": f"proc{p}", "count": per_proc})
        procs.append(
            _launch(
                script_path,
                REPO_ROOT,
                str(data_dir),
                collection,
                "create_batch",
                payload,
                barrier=barrier_path,
            )
        )

    _release_barrier_soon(barrier_path)

    outputs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=60)
        outputs.append((proc.returncode, out))

    failures = [(rc, out) for rc, out in outputs if rc != 0]
    assert not failures, f"worker process(es) failed: {failures!r}"

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    ids = sorted(r["id"] for r in all_records)
    expected = sorted(
        f"proc{p}-{i:04d}" for p in range(num_procs) for i in range(per_proc)
    )
    assert ids == expected, (
        f"cross-process write loss/corruption: expected {len(expected)} records, "
        f"got {len(ids)} ({[i for i in expected if i not in ids][:5]} missing, "
        f"{[i for i in ids if i not in expected][:5]} unexpected)"
    )
    for r in all_records:
        prefix = r["id"].rsplit("-", 1)[0]
        assert r["value"] == prefix, f"cross-wired/corrupted row across processes: {r!r}"


@pytest.mark.parametrize("storage", ["classic", "append"])
def test_cross_process_concurrent_same_record_update_is_last_writer_wins_not_corruption(
    tmp_path, storage
):
    """Same question as the distinct-create probe above, but for the
    same-record UPDATE case ACROSS processes: 4 separate OS processes all
    update the SAME existing record concurrently (barrier-released). As in
    the same-process version of this test, there is no compare-and-set
    (spec 63) -- so the only claim under test is that the final value is
    EXACTLY ONE of the 4 submitted values (the lock prevented a torn/
    interleaved write), not that no update was overwritten (some updates
    ARE expected to be silently lost to LWW; that is unrelated to whether
    the lock itself works). RESULT: safe from corruption -- final value is
    always exactly one clean candidate string, never a merge/garble, in
    both storage modes; same LWW-loses-N-1-of-N semantics as the
    same-process case, now confirmed to hold across process boundaries
    too."""
    data_dir = tmp_path / "data"
    collection = "cp_counters"
    if storage == "classic":
        write_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    else:
        write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])

    object_records.create_collection_record(
        collection, {"id": "shared", "value": "seed"}, base_dir=data_dir, roots=[]
    )

    script_path = _write_worker_script(tmp_path)
    barrier_path = tmp_path / "barrier_update"
    num_procs = 4
    candidates = [f"proc-writer-{p}" for p in range(num_procs)]

    procs = []
    for p in range(num_procs):
        payload = json.dumps({"record_id": "shared", "value": candidates[p]})
        procs.append(
            _launch(
                script_path,
                REPO_ROOT,
                str(data_dir),
                collection,
                "update_same",
                payload,
                barrier=barrier_path,
            )
        )

    _release_barrier_soon(barrier_path)

    outputs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=60)
        outputs.append((proc.returncode, out))

    failures = [(rc, out) for rc, out in outputs if rc != 0]
    assert not failures, f"worker process(es) failed: {failures!r}"

    _clear_caches()
    final = object_records.get_collection_record(collection, "shared", base_dir=data_dir, roots=[])
    assert final["value"] in candidates, (
        f"CROSS-PROCESS CORRUPTION: final value {final['value']!r} is not any single "
        f"submitted value -- indicates the flock did NOT serialize these processes' "
        f"read-modify-write cycles"
    )


# =============================================================================
# 5. id->offset oidx sidecar under concurrent same-process appends
# =============================================================================


def test_oidx_sidecar_stays_coherent_under_concurrent_appends(tmp_path, monkeypatch):
    """Force every point op through the cold-cache id->offset sidecar path
    (DBBASIC_RECORDS_CACHE_MAX_ROWS=0, same trick
    test_embedded_json_lines_characterization.py's oidx tests use), then
    run N threads each creating a distinct record into one append-mode
    collection concurrently. Property under test: after the dust settles,
    does the sidecar (.records.oidx) -- rebuilt/caught-up independently by
    whichever thread happens to read it, under no lock at all on the READ
    side -- resolve every one of the N ids to the CORRECT row via the
    offset-indexed by-id path (get_collection_record), matching a full-fold
    read? The sidecar is documented as "100% disposable... any reader
    unable to make sense of it... rebuilds it with one sequential scan"
    (object_records.py ~line 90), so even if concurrent writers left it
    momentarily behind the file's actual size, a subsequent reader should
    self-heal rather than serve stale/wrong offsets. RESULT: safe -- every
    id resolves to its correct value via BOTH the by-id sidecar path and
    the full-fold path, and the two agree, after concurrent creation."""
    monkeypatch.setenv("DBBASIC_RECORDS_CACHE_MAX_ROWS", "0")
    data_dir = tmp_path / "data"
    collection = "oidx_stress"
    write_append_schema(data_dir, collection, [ID_FIELD, VALUE_FIELD])
    n = 24

    def worker(i: int, barrier: threading.Barrier) -> None:
        barrier.wait(timeout=10)
        object_records.create_collection_record(
            collection,
            {"id": f"oi-{i:04d}", "value": f"val-{i:04d}"},
            base_dir=data_dir,
            roots=[],
        )

    errors = _run_threads(worker, n)
    assert errors == [None] * n, f"unexpected exceptions: {errors}"

    _clear_caches()
    all_records = object_records.read_collection_records(collection, base_dir=data_dir, roots=[])
    assert sorted(r["id"] for r in all_records) == sorted(f"oi-{i:04d}" for i in range(n))

    for i in range(n):
        _clear_caches()
        by_id = object_records.get_collection_record(
            collection, f"oi-{i:04d}", base_dir=data_dir, roots=[]
        )
        assert by_id["value"] == f"val-{i:04d}", (
            f"oidx sidecar served wrong/stale data for oi-{i:04d} after concurrent "
            f"appends: got {by_id!r}"
        )

    sidecar_path = data_dir / "collections" / collection / object_records.OIDX_FILE
    assert sidecar_path.exists(), "sidecar was never (re)built despite over-threshold reads"
