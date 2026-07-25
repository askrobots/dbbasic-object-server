"""scheduler -- the daemon's task board.

object_daemon.process_scheduler reads `task_*` keys out of THIS object's
state on every poll. Each value is JSON:

    {"id", "object_id", "method", "payload",
     "schedule", "type": "cron"|"onetime", "status": "active"|"paused"}

The daemon stamps next_run/last_run/run_count back onto the same key, and
records every execution as a scheduler_runs row (system-dashboard) so a
run's outcome is queryable rather than buried in a log.

The object is deliberately inert: scheduling is DATA in its state, and the
contract lives in object_daemon.process_scheduler, not here. It ships in a
package rather than being hand-placed on a server because a trigger object
that exists only on one box is a scheduler that dies silently the next
time that box is rebuilt.

The same argument applies to the tasks themselves, so DO NOT hand-write
rows here. A package declares its recurring passes in its manifest's
`schedules` section and the installer writes them onto this board (see
docs/package-authoring.md); a schedule and the object it calls then travel
together, survive a rebuild, and are reviewable as code. An install
preserves run history and never un-pauses a task an operator stopped, so
declaring one is safe to repeat. Hand-entered tasks still work -- and are
adopted rather than duplicated when a package later declares the same id --
but they are exactly the invisible state this whole board was meant to
stop being.
"""


def GET(request):
    return {"ok": True,
            "note": "task_* entries in this object's state drive "
                    "object_daemon.process_scheduler; see /scheduler for the board"}


def POST(request):
    return {"ok": True}
