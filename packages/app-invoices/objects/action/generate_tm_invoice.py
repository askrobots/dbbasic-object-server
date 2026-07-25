"""action_generate_tm_invoice -- approved hours become one invoice.

POST {project_id?, customer_name?, through_date?, grouping?, dry_run?}

The last mile of time-and-materials billing, and deliberately the same
shape as the period biller: collect what is owed, raise ONE invoice, mark
the sources so a re-run raises nothing. Everything downstream is the
machinery that already ships -- totals rollup, portal link, send, aging,
dunning, payment, books.

Idempotency lives on the entries rather than in a dedup table. An entry
that already carries an invoice_id is never billed again, which means a
generator that crashed halfway through can simply be run again: the
entries it already stamped are skipped, the rest are picked up. Double
billing a client for the same hours is the failure a consultancy does not
get to explain twice.

What it will not do: rate anything. Rates were stamped at approval, back
when a human looked at the hours; re-deriving them here would let a rate
card edited last week silently reprice work approved last month. This
object only adds up numbers other people already agreed to.
"""

import os
from datetime import date, timedelta

import object_ids
import object_rates
import object_records

ACTOR = "action_generate_tm_invoice"

GROUPINGS = ("detail", "by_person", "by_task")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _int(value, default=0):
    try:
        return int(str(value or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _text(value):
    return str(value if value is not None else "").strip()


def _billable_entries(base, project_id, through):
    """Approved, unbilled hours for this project up to a date.

    An entry with an invoice_id is skipped no matter what its status says:
    the stamp is the fact, and trusting status alone would re-bill an
    entry somebody had hand-edited.
    """
    try:
        rows = object_records.read_collection_records("time_logs", base_dir=base)
    except Exception:
        return None
    out = []
    for row in rows:
        if _text(row.get("status")) != "approved":
            continue
        if _text(row.get("invoice_id")):
            continue
        if project_id and _text(row.get("project_id")) != project_id:
            continue
        worked = _text(row.get("started_at"))[:10]
        if through and worked and worked > through:
            continue
        if _int(row.get("amount_cents")) <= 0:
            continue
        out.append(row)
    return out


def _group_key(entry, grouping):
    if grouping == "by_person":
        return _text(entry.get("owner_id")) or "unassigned"
    if grouping == "by_task":
        return _text(entry.get("task_id")) or "unassigned"
    return _text(entry.get("id"))


def _describe(entries, grouping, base):
    """One line's worth of English.

    A T&M line names the work, the hours and the rate, because the hours
    are the whole argument for the number. A client who cannot see what
    they are paying for can only dispute the total.
    """
    first = entries[0]
    seconds = sum(_int(e.get("duration_seconds")) for e in entries)
    increment = 0  # already applied per entry at approval
    worked_hours = object_rates.hours(seconds, increment)
    rates = {_int(e.get("hourly_rate_cents")) for e in entries}
    rate_note = (f" at {rates.pop() / 100:.2f}/hr" if len(rates) == 1 else
                 " at mixed rates")

    if grouping == "by_person":
        who = _text(first.get("owner_id")) or "Unassigned"
        return f"{who}: {worked_hours} hours{rate_note}"
    if grouping == "by_task":
        task = _text(first.get("task_id")) or "Unassigned"
        title = _lookup(base, "tasks", task, "title") or task or "Work"
        return f"{title}: {worked_hours} hours{rate_note}"
    notes = _text(first.get("notes"))
    worked_on = _text(first.get("started_at"))[:10]
    label = notes or _lookup(base, "tasks", _text(first.get("task_id")), "title") or "Work"
    return f"{worked_on} {label}: {worked_hours} hours{rate_note}"


def _lookup(base, collection, record_id, field):
    if not record_id:
        return ""
    try:
        row = object_records.get_collection_record(collection, record_id, base_dir=base)
    except Exception:
        return ""
    return _text((row or {}).get(field))


def POST(request):
    base = _base_dir()
    project_id = _text(request.get("project_id"))
    through = _text(request.get("through_date"))[:10] or date.today().isoformat()
    dry_run = _truthy(request.get("dry_run"))
    grouping = _text(request.get("grouping")) or _setting(
        base, "billing.tm_line_grouping", "detail")
    if grouping not in GROUPINGS:
        return {"status": 400,
                "error": f"grouping must be one of {', '.join(GROUPINGS)}"}

    entries = _billable_entries(base, project_id, through)
    if entries is None:
        return {"ok": True, "skipped": "time tracking not installed (time_logs absent)"}
    if not entries:
        return {"ok": True, "invoiced": 0,
                "note": "no approved, unbilled time in range"}

    buckets = {}
    for entry in entries:
        buckets.setdefault(_group_key(entry, grouping), []).append(entry)

    total_cents = sum(_int(e.get("amount_cents")) for e in entries)
    if dry_run:
        return {"ok": True, "would_invoice": len(entries), "lines": len(buckets),
                "total_cents": total_cents, "through_date": through,
                "grouping": grouping}

    customer_name = (_text(request.get("customer_name"))
                     or _lookup(base, "projects", project_id, "name")
                     or "Customer")
    due_days = _int(_setting(base, "billing.invoice_due_days", "14"), 14)
    invoice_id = object_ids.new_uuid4()
    owner = _text(request.get("owner_id")) or _text(entries[0].get("owner_id"))
    marker = f"time_logs:{project_id or 'all'}:{through}"

    object_records.create_collection_record(
        "invoices",
        {
            "id": invoice_id,
            "number": f"TM-{(project_id or 'ALL')[:8]}-{through}",
            "customer_id": _text(request.get("customer_id")),
            "customer_name": customer_name,
            "customer_email": _text(request.get("customer_email")),
            "status": "draft",
            "issue_date": through,
            "due_date": (date.fromisoformat(through)
                         + timedelta(days=due_days)).isoformat(),
            "subtotal_cents": str(total_cents),
            "total_cents": str(total_cents),
            "notes": f"Generated by {ACTOR} [{marker}]",
            "owner_id": owner,
        },
        base_dir=base, actor=ACTOR)

    lines = 0
    billed = 0
    for key in sorted(buckets):
        group = buckets[key]
        amount = sum(_int(e.get("amount_cents")) for e in group)
        seconds = sum(_int(e.get("duration_seconds")) for e in group)
        object_records.create_collection_record(
            "invoice_lines",
            {
                "id": object_ids.new_uuid4(),
                "invoice_id": invoice_id,
                "description": _describe(group, grouping, base),
                "quantity": object_rates.hours(seconds),
                "unit_price_cents": str(_int(group[0].get("hourly_rate_cents"))),
                "line_total_cents": str(amount),
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        lines += 1
        for entry in group:
            # Stamp the entry BEFORE counting it billed: if this pass dies
            # here, the next run skips what is already stamped rather than
            # billing it twice.
            object_records.update_collection_record(
                "time_logs", entry["id"],
                {"status": "billed", "invoice_id": invoice_id},
                base_dir=base, actor=ACTOR)
            billed += 1

    return {"ok": True, "invoiced": 1, "invoice_id": invoice_id,
            "entries_billed": billed, "lines": lines,
            "total_cents": total_cents, "through_date": through,
            "grouping": grouping}
