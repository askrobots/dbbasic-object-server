import asyncio
import time
from pathlib import Path

import pytest

import object_execution
import object_worker_pool
from object_execution import ObjectExecutionRequest


def write_object(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


# ---------------------------------------------------------------------------
# Round-trip parity with execute_python_object_subprocess
# ---------------------------------------------------------------------------


def test_pool_execute_returns_success_result(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "worker.py",
        "def GET(request):\n    return {'value': request['value']}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            return await pool.execute(
                ObjectExecutionRequest("basics_worker", payload={"value": 7}),
                timeout_seconds=5,
                roots=[root],
            )
        finally:
            await pool.shutdown()

    result = asyncio.run(scenario())

    assert result.ok
    assert result.object_id == "basics_worker"
    assert result.method == "GET"
    assert result.path == root / "basics" / "worker.py"
    assert result.result == {"value": 7}
    assert result.error is None
    assert result.started_at.endswith("Z")
    assert result.finished_at.endswith("Z")


def test_pool_execute_matches_subprocess_success_shape(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "worker.py",
        "def GET(request):\n    return {'value': request['value']}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            return await pool.execute(
                ObjectExecutionRequest("basics_worker", payload={"value": 7}),
                timeout_seconds=5,
                roots=[root],
            )
        finally:
            await pool.shutdown()

    pool_result = asyncio.run(scenario())
    subprocess_result = object_execution.execute_python_object_subprocess(
        ObjectExecutionRequest("basics_worker", payload={"value": 7}),
        roots=[root],
        timeout_seconds=5,
    )

    assert pool_result.ok == subprocess_result.ok
    assert pool_result.result == subprocess_result.result
    assert pool_result.object_id == subprocess_result.object_id
    assert pool_result.method == subprocess_result.method
    assert pool_result.path == subprocess_result.path


def test_pool_execute_object_raises_preserves_error_type_and_message(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "boom.py",
        "def GET(request):\n    raise ValueError('kaboom')\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            return await pool.execute(
                ObjectExecutionRequest("basics_boom"),
                timeout_seconds=5,
                roots=[root],
            )
        finally:
            await pool.shutdown()

    result = asyncio.run(scenario())

    assert not result.ok
    assert result.error is not None
    # PythonObject.execute wraps the underlying exception in
    # ObjectMethodExecutionError -- same behavior as the in-process and
    # spawn-per-request paths, so match that shape here too.
    assert result.error.type == "ObjectMethodExecutionError"
    assert "kaboom" in result.error.message
    assert "ValueError" in result.error.message


def test_pool_execute_missing_method_returns_method_not_supported(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "readonly.py",
        "def GET(request):\n    return {'ok': True}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            return await pool.execute(
                ObjectExecutionRequest("basics_readonly", method="POST"),
                timeout_seconds=5,
                roots=[root],
            )
        finally:
            await pool.shutdown()

    result = asyncio.run(scenario())

    assert not result.ok
    assert result.error is not None
    assert result.error.type == "MethodNotSupportedError"
    assert "POST" in result.error.message


# ---------------------------------------------------------------------------
# Live edit: same warm worker must observe source rewrites between requests
# ---------------------------------------------------------------------------


def test_pool_live_edit_same_worker_sees_new_source(tmp_path):
    root = tmp_path / "objects"
    source = write_object(
        root / "basics" / "live.py",
        "def GET(request):\n    return {'version': 1}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)  # single worker: guarantees same process
        try:
            first = await pool.execute(
                ObjectExecutionRequest("basics_live"),
                timeout_seconds=5,
                roots=[root],
            )
            source.write_text("def GET(request):\n    return {'version': 2}\n")
            second = await pool.execute(
                ObjectExecutionRequest("basics_live"),
                timeout_seconds=5,
                roots=[root],
            )
            return first, second
        finally:
            await pool.shutdown()

    first, second = asyncio.run(scenario())

    assert first.ok and first.result == {"version": 1}
    assert second.ok and second.result == {"version": 2}


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_pool_execute_timeout_matches_subprocess_error_shape(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "slow.py",
        "import time\n\ndef GET(request):\n    time.sleep(5)\n    return {'ok': True}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            timed_out = await pool.execute(
                ObjectExecutionRequest("basics_slow"),
                timeout_seconds=0.5,
                roots=[root],
            )
            # Pool should self-heal: a fresh request on the same pool works.
            write_object(
                root / "basics" / "fast.py",
                "def GET(request):\n    return {'ok': True}\n",
            )
            recovered = await pool.execute(
                ObjectExecutionRequest("basics_fast"),
                timeout_seconds=5,
                roots=[root],
            )
            return timed_out, recovered
        finally:
            await pool.shutdown()

    timed_out, recovered = asyncio.run(scenario())

    assert not timed_out.ok
    assert timed_out.error is not None
    assert timed_out.error.type == object_execution.TIMEOUT_ERROR_TYPE
    assert timed_out.error.message == "GET timed out for object basics_slow after 0.5 seconds"

    assert recovered.ok
    assert recovered.result == {"ok": True}


# ---------------------------------------------------------------------------
# Crash
# ---------------------------------------------------------------------------


def test_pool_execute_worker_crash_self_heals(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "crash.py",
        "import os\n\ndef GET(request):\n    os._exit(1)\n",
    )
    write_object(
        root / "basics" / "fine.py",
        "def GET(request):\n    return {'fine': True}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            crashed = await pool.execute(
                ObjectExecutionRequest("basics_crash"),
                timeout_seconds=5,
                roots=[root],
            )
            recovered = await pool.execute(
                ObjectExecutionRequest("basics_fine"),
                timeout_seconds=5,
                roots=[root],
            )
            return crashed, recovered
        finally:
            await pool.shutdown()

    crashed, recovered = asyncio.run(scenario())

    assert not crashed.ok
    assert crashed.error is not None
    assert crashed.error.type == object_execution.WORKER_ERROR_TYPE
    assert "exit code" in crashed.error.message

    assert recovered.ok
    assert recovered.result == {"fine": True}


# ---------------------------------------------------------------------------
# Concurrency: loop must not serialize concurrent executions
# ---------------------------------------------------------------------------


def test_pool_size_two_runs_two_requests_concurrently(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "sleepy.py",
        "import time\n\ndef GET(request):\n    time.sleep(0.3)\n    return {'ok': True}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=2)
        try:
            started = time.perf_counter()
            results = await asyncio.gather(
                pool.execute(
                    ObjectExecutionRequest("basics_sleepy"),
                    timeout_seconds=5,
                    roots=[root],
                ),
                pool.execute(
                    ObjectExecutionRequest("basics_sleepy"),
                    timeout_seconds=5,
                    roots=[root],
                ),
            )
            elapsed = time.perf_counter() - started
            return results, elapsed
        finally:
            await pool.shutdown()

    results, elapsed = asyncio.run(scenario())

    assert all(r.ok for r in results)
    # Serialized execution would take >= 0.6s; concurrent execution on a
    # 2-worker pool should land close to a single 0.3s sleep.
    assert elapsed < 0.55, f"expected overlap, took {elapsed:.3f}s"


def test_pool_three_requests_through_size_two_pool_all_complete(tmp_path):
    root = tmp_path / "objects"
    write_object(
        root / "basics" / "sleepy.py",
        "import time\n\ndef GET(request):\n    time.sleep(0.3)\n    return {'ok': True}\n",
    )

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=2)
        try:
            started = time.perf_counter()
            results = await asyncio.gather(
                *[
                    pool.execute(
                        ObjectExecutionRequest("basics_sleepy"),
                        timeout_seconds=5,
                        roots=[root],
                    )
                    for _ in range(3)
                ]
            )
            elapsed = time.perf_counter() - started
            return results, elapsed
        finally:
            await pool.shutdown()

    results, elapsed = asyncio.run(scenario())

    assert all(r.ok for r in results)
    # Two run concurrently (~0.3s), the third queues for a free worker
    # (~another 0.3s): expect ~0.3-0.5s total, well under fully serialized
    # (3 * 0.3s = 0.9s).
    assert elapsed < 0.75, f"expected queueing not full serialization, took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Object-not-found parity
# ---------------------------------------------------------------------------


def test_pool_execute_missing_object_returns_not_found(tmp_path):
    root = tmp_path / "objects"
    root.mkdir(parents=True, exist_ok=True)

    async def scenario():
        pool = object_worker_pool.WorkerPool(size=1)
        try:
            return await pool.execute(
                ObjectExecutionRequest("basics_missing"),
                timeout_seconds=5,
                roots=[root],
            )
        finally:
            await pool.shutdown()

    result = asyncio.run(scenario())

    assert not result.ok
    assert result.error is not None
    assert result.error.type == "ObjectNotFoundError"


def test_worker_pool_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        object_worker_pool.WorkerPool(size=0)
    with pytest.raises(ValueError):
        object_worker_pool.WorkerPool(size=-1)
