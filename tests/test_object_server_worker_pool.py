"""Server-level integration tests for the DBBASIC_WORKER_POOL_SIZE path.

object_worker_pool.WorkerPool itself is covered end-to-end in
tests/test_object_worker_pool.py. These tests exercise the object_server.py
wiring: the _execute_object_method call site, the _get_worker_pool /
_shutdown_worker_pool lifecycle helpers, and the admin capabilities payload.

Note on event loops: asyncio subprocess transports are bound to the loop
that created them, and every test here that actually spawns pool workers
runs its HTTP calls *and* the final object_server._shutdown_worker_pool()
inside one asyncio.run() (see run_scenario below), rather than opening a
fresh loop per call the way tests/test_object_server.py's request() helper
does. That mirrors how a real server actually uses the pool -- uvicorn runs
one event loop for the process's whole lifetime -- and it lets shutdown()
gracefully await each worker's exit instead of falling back to its
best-effort cross-loop pid kill.
"""
import asyncio
import json
import time as time_module

import object_correlation
import object_execution
import object_server
import object_worker_pool

TEST_ADMIN_TOKEN = "unit-test-only-admin-token"


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def auth_headers():
    return [("authorization", f"Token {TEST_ADMIN_TOKEN}")]


def enable_admin_token(monkeypatch):
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", TEST_ADMIN_TOKEN)


async def asgi_request(path, method="GET", query_string="", body=b"", headers=None):
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope_headers = [(b"accept", b"application/json")]
    for name, value in headers or []:
        if isinstance(name, str):
            name = name.encode("latin-1")
        if isinstance(value, str):
            value = value.encode("latin-1")
        scope_headers.append((name, value))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string.encode("utf-8"),
        "headers": scope_headers,
        "client": ("127.0.0.1", 12345),
    }
    await object_server.app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body_parts = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    payload = b"".join(body_parts)
    return start["status"], dict(start["headers"]), json.loads(payload.decode("utf-8"))


def request(path, method="GET", query_string="", body=b"", headers=None):
    """Plain single-call helper for tests that never touch the pool."""
    return asyncio.run(
        asgi_request(path, method=method, query_string=query_string, body=body, headers=headers)
    )


def run_scenario(coro_factory):
    """Run an async test body and shut the pool down on the SAME loop.

    coro_factory is a zero-arg callable returning the coroutine to run (not
    the coroutine itself, so it's only created inside the new event loop).
    """

    async def wrapper():
        try:
            return await coro_factory()
        finally:
            await object_server._shutdown_worker_pool()

    return asyncio.run(wrapper())


# ---------------------------------------------------------------------------
# Disabled by default
# ---------------------------------------------------------------------------


def test_worker_pool_disabled_by_default_uses_subprocess_path(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "fast.py",
        "def GET(request):\n    return {'mode': 'object_source'}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.delenv(object_worker_pool.WORKER_POOL_SIZE_ENV, raising=False)
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")

    calls = []

    def fake_subprocess(exec_request, roots=None, *, timeout_seconds, raise_on_error=False):
        calls.append((exec_request.object_id, timeout_seconds))
        return object_execution.ObjectExecutionResult(
            object_id=exec_request.object_id,
            method=exec_request.normalized_method(),
            path=exec_request.path,
            ok=True,
            result={"mode": "subprocess"},
            error=None,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:00Z",
            duration_ms=1.0,
        )

    monkeypatch.setattr(object_execution, "execute_python_object_subprocess", fake_subprocess)

    status, _, payload = request("/objects/basics_fast")

    assert status == 200
    assert payload == {"mode": "subprocess"}
    assert calls == [("basics_fast", 5.0)]
    # The pool must never have been constructed at all.
    assert object_server._worker_pool is None


def test_trusted_in_process_object_still_bypasses_pool(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "fast.py",
        "def GET(request):\n    return {'mode': 'in_process'}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_server.TRUSTED_IN_PROCESS_OBJECTS_ENV, "basics_fast")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "1")

    status, _, payload = request("/objects/basics_fast")

    assert status == 200
    assert payload == {"mode": "in_process"}
    # Trusted objects never touch the pool, so it should never spin up.
    assert object_server._worker_pool is None


# ---------------------------------------------------------------------------
# Capabilities payload (no pool workers spawned either way)
# ---------------------------------------------------------------------------


def test_admin_status_reports_worker_pool_disabled_by_default(monkeypatch):
    monkeypatch.delenv(object_worker_pool.WORKER_POOL_SIZE_ENV, raising=False)
    enable_admin_token(monkeypatch)

    status, _, payload = request("/admin/status", headers=auth_headers())

    assert status == 200
    assert payload["capabilities"]["worker_pool"] == {
        "enabled": False,
        "size": 0,
        "env": "DBBASIC_WORKER_POOL_SIZE",
    }


def test_admin_status_reports_worker_pool_enabled_with_size(monkeypatch):
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "4")
    enable_admin_token(monkeypatch)

    status, _, payload = request("/admin/status", headers=auth_headers())

    assert status == 200
    assert payload["capabilities"]["worker_pool"] == {
        "enabled": True,
        "size": 4,
        "env": "DBBASIC_WORKER_POOL_SIZE",
    }


# ---------------------------------------------------------------------------
# Enabled: pool path taken instead of subprocess (spawns real workers)
# ---------------------------------------------------------------------------


def test_worker_pool_enabled_executes_via_pool_not_subprocess(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "fast.py",
        "def GET(request):\n    return {'mode': 'pool'}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "1")

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("pool is enabled -- subprocess-per-request must not run")

    monkeypatch.setattr(object_execution, "execute_python_object_subprocess", fail_subprocess)

    status, _, payload = run_scenario(lambda: asgi_request("/objects/basics_fast"))

    assert status == 200
    assert payload == {"mode": "pool"}


def test_worker_pool_execution_error_reports_through_normal_error_path(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "1")

    status, _, payload = run_scenario(lambda: asgi_request("/objects/basics_broken"))

    assert status == 500
    assert payload["status"] == "error"
    assert "RuntimeError: boom" in payload["error"]


def test_worker_pool_timeout_returns_504_matching_subprocess_message(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "slow.py",
        "import time\n\ndef GET(request):\n    time.sleep(5)\n    return {'ok': True}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "0.5")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "1")

    status, _, payload = run_scenario(lambda: asgi_request("/objects/basics_slow"))

    assert status == 504
    assert payload["status"] == "error"
    assert payload["error"] == "Execution failed: GET timed out for object basics_slow after 0.5 seconds"
    assert object_correlation.normalize_correlation_id(payload["correlation_id"]) == payload[
        "correlation_id"
    ]


# ---------------------------------------------------------------------------
# Live edit through the real server call site, same warm worker
# ---------------------------------------------------------------------------


def test_worker_pool_live_edit_survives_across_requests_on_same_worker(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    source = write_source(
        root / "basics" / "live.py",
        "def GET(request):\n    return {'version': 1}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "1")  # single worker: same process both times

    async def scenario():
        first = await asgi_request("/objects/basics_live")
        source.write_text("def GET(request):\n    return {'version': 2}\n")
        second = await asgi_request("/objects/basics_live")
        return first, second

    first, second = run_scenario(scenario)
    status1, _, payload1 = first
    status2, _, payload2 = second

    assert status1 == 200 and payload1 == {"version": 1}
    assert status2 == 200 and payload2 == {"version": 2}


# ---------------------------------------------------------------------------
# Concurrency: pool path must not serialize the event loop
# ---------------------------------------------------------------------------


def test_worker_pool_concurrent_requests_do_not_block_event_loop(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    write_source(
        root / "basics" / "sleepy.py",
        "import time\n\ndef GET(request):\n    time.sleep(0.3)\n    return {'ok': True}\n",
    )
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv(object_server.OBJECT_TIMEOUT_SECONDS_ENV, "5")
    monkeypatch.setenv(object_worker_pool.WORKER_POOL_SIZE_ENV, "2")
    monkeypatch.setenv(object_server.MAX_CONCURRENT_EXECUTIONS_ENV, "2")

    async def scenario():
        started = time_module.perf_counter()
        results = await asyncio.gather(
            asgi_request("/objects/basics_sleepy"),
            asgi_request("/objects/basics_sleepy"),
        )
        elapsed = time_module.perf_counter() - started
        return results, elapsed

    results, elapsed = run_scenario(scenario)

    assert all(status == 200 and payload == {"ok": True} for status, _, payload in results)
    assert elapsed < 0.55, f"expected overlap, took {elapsed:.3f}s"
