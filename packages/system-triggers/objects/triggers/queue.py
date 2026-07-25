"""queue -- the daemon's deferred-work queue.

object_daemon.process_queue reads `msg_*` keys out of THIS object's state
on every poll and executes the target object each message names. The queue
primitive already supports everything deferred work normally needs:

    visible_after    run no earlier than this epoch second (a DELAY)
    priority_level   higher goes first
    expires_at       drop it if it never became runnable in time
    max_attempts     retry with exponential backoff (2**attempts seconds),
                     then park the message as failed rather than looping
    queue_name       independent named queues

Enqueue with object_daemon_control.enqueue_message() or POST
/daemon/queue/messages. Status walks pending -> processing ->
completed | failed | expired, and the message keeps its attempt count, so
a dead job is inspectable instead of vanishing.

Like the scheduler object this is inert on purpose -- the work is data in
its state, and the contract lives in object_daemon.process_queue. Without
this file the daemon simply reports "Queue: no queue object" and every
enqueued message waits forever, which is why it ships in a package.

What this queue is NOT: a worker pool. Messages execute inside the daemon's
poll loop, so a job that takes thirty seconds delays that daemon's other
passes by thirty seconds. That is fine for follow-on writes, notifications,
recomputes and short fetches; heavy or long work (image processing, OCR,
model calls) wants its own process and should say so plainly rather than
quietly starving the aging pass.
"""


def GET(request):
    return {"ok": True,
            "note": "msg_* entries in this object's state drive "
                    "object_daemon.process_queue; enqueue via "
                    "/daemon/queue/messages"}


def POST(request):
    return {"ok": True}
