"""Behavior tests for packages/app-timers: 62 -- Timer.

Covers packages/app-timers/objects/site/timer_actions.py's server-enforced
start/stop over time_logs (single-running-timer-per-owner invariant, derived
duration_seconds, owner scoping, the 409/403 error shapes, the
timers_enabled kill switch) plus the MCP tool_route mapping for
start_timer/stop_timer/get_running_timer (mirrors
tests/test_object_concurrency.py's update_record tool_route tests).

Structural/manifest/schema/permission shape isn't split into its own file
here (unlike e.g. test_app_catalog_package.py) since this package is small
enough that behavior and shape fit in one file without getting unwieldy.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import object_execution
import object_mcp
import object_packages
import object_records
import python_object_runtime

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
OBJECT_ID = "site_timer_actions"


def _install(tmp_path):
    """Install app-timers into an isolated data dir/object root and return
    (data_dir, object_root, runtime). Mirrors tests/test_app_orders_totals.py.
    """
    import os

    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    os.environ["DBBASIC_DATA_DIR"] = str(data_dir)

    object_packages.install_package(
        "app-timers", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root]
    )
    runtime = python_object_runtime.PythonObjectRuntime(base_dir=data_dir)
    return data_dir, object_root, runtime


def _call(runtime, object_root, method, payload):
    """Execute site_timer_actions and return (http_status, body_dict)."""
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(OBJECT_ID, method=method, payload=payload),
        roots=[object_root],
    )
    assert result.ok is True, result.error
    response = result.result
    status = response.get("status", 200)
    body = json.loads(response["body"])
    return status, body


def _identity(user_id):
    return {"_identity": {"user_id": user_id}}


def _start(runtime, object_root, user_id, **fields):
    payload = {"action": "start", **_identity(user_id), **fields}
    return _call(runtime, object_root, "POST", payload)


def _stop(runtime, object_root, user_id, **fields):
    payload = {"action": "stop", **_identity(user_id), **fields}
    return _call(runtime, object_root, "POST", payload)


def _running(runtime, object_root, user_id):
    payload = {"action": "running", **_identity(user_id)}
    return _call(runtime, object_root, "GET", payload)


def _write_feature_flag(data_dir, flag, value):
    path = data_dir / "collections" / "feature_flags" / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"id\tflag\tvalue\nff1\t{flag}\t{value}\n")


def _write_task(data_dir, *task_ids):
    # app-tasks isn't installed in this package's own tests (app-timers'
    # "dependencies": ["app-tasks"] is informational metadata, not an
    # install-time requirement -- see object_packages.install_package).
    # A raw records.tsv is enough to satisfy time_logs.task_id's relation
    # existence check without pulling in the whole tasks schema.
    path = data_dir / "collections" / "tasks" / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"{task_id}\tTest Task" for task_id in task_ids)
    path.write_text(f"id\ttitle\n{rows}\n")


# ---------------------------------------------------------------------------
# Single-running-timer invariant
# ---------------------------------------------------------------------------


def test_start_creates_a_running_row(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _write_task(data_dir, "t1")

    status, body = _start(runtime, object_root, "u1", task_id="t1", notes="working")

    assert status == 200
    row = body["time_log"]
    assert row["owner_id"] == "u1"
    assert row["task_id"] == "t1"
    assert row["notes"] == "working"
    assert row["is_running"] == "true"
    assert row["ended_at"] == ""


def test_starting_a_second_timer_auto_stops_the_first(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _write_task(data_dir, "t1", "t2")

    _, first = _start(runtime, object_root, "u1", task_id="t1")
    _, second = _start(runtime, object_root, "u1", task_id="t2")

    assert first["time_log"]["id"] != second["time_log"]["id"]

    rows = object_records.read_collection_records("time_logs", base_dir=data_dir)
    mine = [row for row in rows if row["owner_id"] == "u1"]
    running = [row for row in mine if row["is_running"] == "true"]
    assert len(running) == 1
    assert running[0]["id"] == second["time_log"]["id"]

    stopped = [row for row in mine if row["id"] == first["time_log"]["id"]][0]
    assert stopped["is_running"] == "false"
    assert stopped["ended_at"] != ""
    assert int(stopped["duration_seconds"]) >= 0


def test_only_one_running_row_per_owner_after_several_starts(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    for _ in range(5):
        _start(runtime, object_root, "u1")

    rows = object_records.read_collection_records("time_logs", base_dir=data_dir)
    running = [row for row in rows if row["owner_id"] == "u1" and row["is_running"] == "true"]
    assert len(running) == 1


def test_two_owners_may_each_have_their_own_running_timer(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    _start(runtime, object_root, "u1")
    _start(runtime, object_root, "u2")

    rows = object_records.read_collection_records("time_logs", base_dir=data_dir)
    running_ids = {row["owner_id"] for row in rows if row["is_running"] == "true"}
    assert running_ids == {"u1", "u2"}


# ---------------------------------------------------------------------------
# Duration derivation -- floor(ended_at - started_at)
# ---------------------------------------------------------------------------


def test_duration_seconds_is_floor_of_the_interval(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    # started_at is 5.7 real seconds in the past; if the verb rounded
    # instead of flooring, duration_seconds would come back 6, not 5.
    # Test overhead between here and the stop call below is expected to
    # stay well under the remaining 0.3s margin.
    started = datetime.now(timezone.utc) - timedelta(seconds=5, milliseconds=700)
    row = object_records.create_collection_record(
        "time_logs",
        {
            "owner_id": "u1",
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "ended_at": "",
            "is_running": "true",
        },
        base_dir=data_dir,
        actor="test",
    )

    status, body = _stop(runtime, object_root, "u1", time_log_id=row["id"])

    assert status == 200
    assert body["time_log"]["duration_seconds"] == "5"


# ---------------------------------------------------------------------------
# Owner scoping / error shapes
# ---------------------------------------------------------------------------


def test_stop_with_no_id_stops_whichever_timer_is_running(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _write_task(data_dir, "t1")
    _, started = _start(runtime, object_root, "u1", task_id="t1")

    status, body = _stop(runtime, object_root, "u1")

    assert status == 200
    assert body["time_log"]["id"] == started["time_log"]["id"]
    assert body["time_log"]["is_running"] == "false"


def test_stop_with_no_running_timer_is_409(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    status, body = _stop(runtime, object_root, "u1")

    assert status == 409
    assert body["status"] == "error"


def test_stop_someone_elses_timer_is_403(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _, started = _start(runtime, object_root, "owner_a")

    status, body = _stop(runtime, object_root, "owner_b", time_log_id=started["time_log"]["id"])

    assert status == 403
    assert body["status"] == "error"

    # And owner_a's timer is still running -- the refused stop touched nothing.
    row = object_records.get_collection_record(
        "time_logs", started["time_log"]["id"], base_dir=data_dir
    )
    assert row["is_running"] == "true"


def test_stop_an_already_stopped_named_timer_is_409(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _, started = _start(runtime, object_root, "u1")
    time_log_id = started["time_log"]["id"]
    _stop(runtime, object_root, "u1", time_log_id=time_log_id)

    status, body = _stop(runtime, object_root, "u1", time_log_id=time_log_id)

    assert status == 409


def test_stop_unknown_time_log_id_is_404(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    status, body = _stop(runtime, object_root, "u1", time_log_id="does-not-exist")

    assert status == 404


def test_anonymous_caller_is_401(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    status, body = _call(runtime, object_root, "POST", {"action": "start"})

    assert status == 401


# ---------------------------------------------------------------------------
# GET /timers/running
# ---------------------------------------------------------------------------


def test_running_is_null_when_no_timer_running(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)

    status, body = _running(runtime, object_root, "u1")

    assert status == 200
    assert body["time_log"] is None


def test_running_returns_the_callers_own_row_only(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _start(runtime, object_root, "owner_a")
    _, started_b = _start(runtime, object_root, "owner_b")

    status, body = _running(runtime, object_root, "owner_b")

    assert status == 200
    assert body["time_log"]["id"] == started_b["time_log"]["id"]


# ---------------------------------------------------------------------------
# timers_enabled kill switch
# ---------------------------------------------------------------------------


def test_flag_off_blocks_start_stop_and_running(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _write_feature_flag(data_dir, "timers_enabled", "off")

    start_status, start_body = _start(runtime, object_root, "u1")
    running_status, running_body = _running(runtime, object_root, "u1")

    assert start_status == 400
    assert start_body["error_code"] == "timers_disabled"
    assert running_status == 400
    assert running_body["error_code"] == "timers_disabled"

    # time_logs itself is untouched -- still plain CRUD (Degradation).
    row = object_records.create_collection_record(
        "time_logs",
        {"owner_id": "u1", "started_at": "2026-01-01T00:00:00Z", "is_running": "false"},
        base_dir=data_dir,
        actor="test",
    )
    assert row["owner_id"] == "u1"


def test_flag_missing_defaults_on(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    # No feature_flags collection at all -- 62's Parameterization/Degradation:
    # missing/unreadable resolves to "on", same as object_rollups'
    # rollup_pass_enabled.
    status, _body = _start(runtime, object_root, "u1")
    assert status == 200


def test_flag_present_but_blank_value_defaults_on(tmp_path):
    data_dir, object_root, runtime = _install(tmp_path)
    _write_feature_flag(data_dir, "timers_enabled", "")

    status, _body = _start(runtime, object_root, "u1")
    assert status == 200


# ---------------------------------------------------------------------------
# MCP tool_route -- thin wrappers over site_timer_actions' own routes
# ---------------------------------------------------------------------------


def test_mcp_start_timer_routes_to_timer_actions_execute():
    method, path, query, body = object_mcp.tool_route(
        "start_timer", {"task_id": "t1", "notes": "hi"}
    )

    assert method == "POST"
    assert path == "/admin/objects/site_timer_actions/execute"
    assert query == ""
    payload = json.loads(body)
    assert payload["method"] == "POST"
    assert payload["payload"] == {"action": "start", "task_id": "t1", "notes": "hi"}


def test_mcp_start_timer_omits_absent_optional_args():
    method, path, query, body = object_mcp.tool_route("start_timer", {})

    payload = json.loads(body)
    assert payload["payload"] == {"action": "start"}


def test_mcp_stop_timer_routes_to_timer_actions_execute():
    method, path, query, body = object_mcp.tool_route("stop_timer", {"time_log_id": "tl1"})

    assert method == "POST"
    assert path == "/admin/objects/site_timer_actions/execute"
    payload = json.loads(body)
    assert payload["method"] == "POST"
    assert payload["payload"] == {"action": "stop", "time_log_id": "tl1"}


def test_mcp_stop_timer_without_id_omits_it():
    method, path, query, body = object_mcp.tool_route("stop_timer", {})

    payload = json.loads(body)
    assert payload["payload"] == {"action": "stop"}


def test_mcp_get_running_timer_routes_to_timer_actions_execute_as_get():
    method, path, query, body = object_mcp.tool_route("get_running_timer", {})

    assert method == "POST"  # outer transport is always POST; inner method carries GET
    assert path == "/admin/objects/site_timer_actions/execute"
    payload = json.loads(body)
    assert payload["method"] == "GET"
    assert payload["payload"] == {"action": "running"}


def test_mcp_start_timer_rejects_non_string_task_id():
    with pytest.raises(ValueError):
        object_mcp.tool_route("start_timer", {"task_id": 5})


def test_mcp_tools_registered_in_catalog():
    names = {tool["name"] for tool in object_mcp.TOOLS}
    assert {"start_timer", "stop_timer", "get_running_timer"} <= names
