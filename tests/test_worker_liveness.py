"""Is this server still doing its own work?

The agents board answered that for EXTERNAL agents and nobody was asking
it of the box itself. The evidence was already on disk: every scheduled
execution writes a scheduler_runs row (object_id, started_at, ok,
duration_ms, error), so "did the runner pass run in the last minute" has
been answerable all along -- just never surfaced.

So this is a fold, not a new heartbeat, and that is the design decision
worth defending. Adding a writer that stamped "the daemon is alive"
beside a log that already proves it would be a second, WEAKER account of
the same fact: a heartbeat says a process reached the line that writes
heartbeats, while a scheduler_runs row says the actual work ran and
whether it worked.
"""

import object_agents

NOW = "2026-07-31T14:00:00Z"


def run(object_id, started_at, *, ok="true", error="", duration_ms="10"):
    return {"object_id": object_id, "started_at": started_at, "ok": ok,
            "error": error, "duration_ms": duration_ms}


def task(object_id, *, schedule="* * * * *", status="active"):
    return {"object_id": object_id, "schedule": schedule, "status": status}


def fold(runs, tasks=None, **kw):
    return object_agents.worker_liveness(runs, tasks=tasks, now=NOW, **kw)


def by_id(result, object_id):
    return next(w for w in result["workers"] if w["object_id"] == object_id)


def test_a_pass_that_just_ran_is_live():
    result = fold([run("system_template_runner", "2026-07-31T13:59:30Z")])
    assert by_id(result, "system_template_runner")["liveness"] == "live"
    assert by_id(result, "system_template_runner")["last_run_ago"]


def test_a_pass_that_stopped_hours_ago_is_lost():
    result = fold([run("system_run_sweeper", "2026-07-31T09:00:00Z")])
    assert by_id(result, "system_run_sweeper")["liveness"] == "lost"


def test_only_the_most_recent_run_decides():
    """The log is append-only and full of history; liveness is about the
    newest row, not the first one found."""
    result = fold([run("p", "2026-07-30T01:00:00Z"),
                   run("p", "2026-07-31T13:59:00Z"),
                   run("p", "2026-07-31T02:00:00Z")])
    assert by_id(result, "p")["liveness"] == "live"


def test_a_task_that_has_never_run_is_visible_at_all():
    """THE case a fold over the run log cannot see by itself: an object
    with no rows is an ABSENCE, and absences are invisible to a fold over
    presences. A pass somebody scheduled that has quietly done nothing
    since the day it was added is the failure most worth catching."""
    result = fold([], tasks=[task("system_never_ran", schedule="0 4 * * *")])
    assert by_id(result, "system_never_ran")["liveness"] == "never"
    assert by_id(result, "system_never_ran")["schedule"] == "0 4 * * *"


def test_a_task_an_operator_switched_off_is_not_a_problem():
    """Same distinction liveness() draws for a paused agent: it is not
    silent, it is off, and rendering it as lost manufactures an alarm out
    of somebody's own decision."""
    result = fold([run("p", "2026-07-30T01:00:00Z")],
                  tasks=[task("p", status="paused")])
    assert by_id(result, "p")["liveness"] == "paused"


def test_a_pass_that_ran_and_threw_is_live_AND_failing():
    """The state a liveness badge alone would render as healthy, and it
    is worse than lost: the daemon is fine, the work is broken."""
    result = fold([run("p", "2026-07-31T13:59:00Z", ok="false",
                       error="boom")])
    row = by_id(result, "p")
    assert row["liveness"] == "live"
    assert row["failing"] is True
    assert row["last_error"] == "boom"
    assert result["failing"] == ["p"]


def test_an_error_string_alone_counts_as_failing():
    """ok can be absent on older rows; an error message is evidence
    enough and must not be shrugged off because a column was blank."""
    row = by_id(fold([run("p", "2026-07-31T13:59:00Z", ok="", error="nope")]), "p")
    assert row["failing"] is True


def test_worst_comes_first_and_failures_outrank_healthy_peers():
    """A board exists to surface what needs attention."""
    result = fold(
        [run("healthy", "2026-07-31T13:59:00Z"),
         run("broken", "2026-07-31T13:59:00Z", ok="false", error="x"),
         run("gone", "2026-07-31T01:00:00Z")],
        tasks=[task("unstarted")])
    order = [w["object_id"] for w in result["workers"]]
    assert order.index("unstarted") < order.index("healthy")
    assert order.index("gone") < order.index("healthy")
    assert order.index("broken") < order.index("healthy")


def test_the_declared_schedule_rides_along_for_context():
    """"lost" means nothing without knowing it was supposed to run
    minutely rather than yearly."""
    result = fold([run("p", "2026-07-31T09:00:00Z")],
                  tasks=[task("p", schedule="*/10 * * * *")])
    assert by_id(result, "p")["schedule"] == "*/10 * * * *"


def test_counts_and_totals_summarize_the_board():
    result = fold([run("a", "2026-07-31T13:59:00Z"),
                   run("b", "2026-07-31T01:00:00Z")],
                  tasks=[task("c")])
    assert result["total"] == 3
    assert result["live"] == 1
    assert result["counts"]["never"] == 1


def test_an_empty_box_folds_to_nothing_rather_than_erroring():
    assert fold([], tasks=[]) == {"workers": [], "counts": {}, "live": 0,
                                  "total": 0, "failing": []}


def test_the_liveness_rule_is_the_SAME_one_agents_use():
    """Two rules that could drift apart is how one board starts lying, so
    a worker and an agent that have both been silent for the same
    interval must get the same verdict."""
    silent = "2026-07-31T13:40:00Z"
    worker = by_id(fold([run("p", silent)]), "p")["liveness"]
    agent = object_agents.liveness({"status": "active", "heartbeat_at": silent},
                                   now=NOW)
    assert worker == agent
