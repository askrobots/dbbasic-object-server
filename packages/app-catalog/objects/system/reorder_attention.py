"""system_reorder_attention -- how many shelves are waiting on a decision.

COUNT {} -> {count, detail}

The open reorder suggestions, and nothing else. `ordered` means somebody
went and bought some; `dismissed` means somebody looked and said no, which
is a real answer and has to be available -- a count that could not be
dismissed would stay permanently lit and teach people to stop reading the
whole band it sits in.

Severity `warning` rather than `normal` in the manifest: unlike a receipt
sitting in a queue somebody put it in, nobody chose to be here, and the
consequence of ignoring it is running out of something a customer wants
to buy. It is not `urgent` -- that is reserved for the server reporting
that it stopped doing its own work, and a low shelf is a business problem
rather than a broken machine.

The detail names the WORST one rather than a total, because "12 products
to reorder" is a number and "12 products to reorder, one is 6 below its
point" is a reason to open the page.

Degrades to zero when reorder_suggestions is absent rather than raising,
the same posture system_shipment_attention and system_scan_attention
take: a rollup pass should not log an error every five minutes about a
collection nobody installed.
"""

import os

import object_records

ACTOR = "system_reorder_attention"

WAITING_STATUS = "open"


def _text(value):
    return str(value if value is not None else "").strip()


def _number(value):
    try:
        return float(_text(value) or 0)
    except (TypeError, ValueError):
        return 0.0


def COUNT(request):
    base = os.environ.get("DBBASIC_DATA_DIR", "data")
    try:
        rows = object_records.read_collection_records("reorder_suggestions",
                                                       base_dir=base)
    except Exception:
        return {"count": 0}

    waiting = [row for row in rows
               if _text(row.get("status")) == WAITING_STATUS]
    if not waiting:
        return {"count": 0}

    gaps = [_number(row.get("reorder_point")) - _number(row.get("on_hand"))
            for row in waiting]
    worst = max(gaps)
    if worst <= 0:
        return {"count": len(waiting)}
    return {"count": len(waiting),
            "detail": f"one is {worst:g} below its point"}
