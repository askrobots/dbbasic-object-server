"""system_review_attention -- runs where a person has to decide the money.

COUNT {} -> {count, detail}

Every row here is a run submitted to a provider whose outcome was never
learned, parked at `needs_review` with its hold still on. The sweeper put
it there deliberately: releasing would give away what the provider may
already have charged, and charging would bill a customer for work that
may never have arrived, so the machine stopped rather than guess.

Which makes this the one queue on the box where **doing nothing costs
somebody money continuously.** A backorder makes a customer wait; an
unresolved run holds their balance down against a job nobody will ever
settle. That is why the detail leads with the amount held rather than the
age: "3 waiting" is a number, "3 waiting, $18.00 held" is the reason to
open the page, and it is the same number the customer is looking at in
their own balance.

Severity `warning`, matching the rest of the "nobody chose to be in this
queue" family, and deliberately not `urgent` -- urgent is reserved for
the server reporting that it stopped doing its own work. Here the server
did exactly what it should.

Degrades to zero when template_runs is absent, the same posture every
other provider takes: a rollup pass should not log an error every five
minutes about a collection nobody installed.
"""

import os

import object_records
import object_template_runs

ACTOR = "system_review_attention"
RUNS = "template_runs"


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _money(cents):
    sign = "-" if cents < 0 else ""
    amount = abs(int(cents))
    return f"{sign}${amount // 100}.{amount % 100:02d}"


def COUNT(request):
    try:
        runs = object_records.read_collection_records(RUNS, base_dir=_base_dir())
    except Exception:
        return {"count": 0}

    waiting = [run for run in runs
               if _text(run.get("status"))
               == object_template_runs.NEEDS_REVIEW_STATUS]
    if not waiting:
        return {"count": 0}

    held = sum(_int(run.get("price_cents")) for run in waiting)
    detail = f"{len(waiting)} waiting"
    if held:
        detail += f", {_money(held)} held"
    return {"count": len(waiting), "detail": detail}
