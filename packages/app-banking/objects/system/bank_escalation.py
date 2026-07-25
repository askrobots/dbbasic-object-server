"""system_bank_escalation -- turn a quietly-growing unreconciled tail into
work someone is accountable for (plan/bank-import-reconciliation-spec.md
section 5, "THE SIGNAL").

Matching (system_bank_matcher) and resolving (action_resolve_bank_line) both
depend on a human noticing an open item and acting on it. Nothing forces
that noticing to happen -- a bank line can sit at match_status=unmatched or
suggested forever, and a payment can sit recorded in the books with no bank
line ever confirming the cash actually arrived. Both are silent by default;
this runner is what makes them loud.

Two directions of trouble, and the spec is explicit that BOTH matter:

1. **Bank lines the books cannot explain** -- a bank_line still
   unmatched/suggested more than reconcile.escalate_after_days after
   posted_on. Money moved and there is no book record saying why.
2. **Book records the bank never confirmed** -- a payments row (status
   received) older than the same window with no bank_line matched_to it.
   This is the direction people forget, and the spec calls it the more
   serious one: it can mean a payment was recorded that never actually
   arrived, which is exactly the shape of a fraud loss or a bookkeeping
   error that compounds every day it goes unnoticed.

Each escalation becomes ONE row in the tasks collection, never two for the
same item. tasks.json (app-tasks) has no generated_from field the way
fin_journals does -- compose_posted_journal's idempotency-by-provenance
posture is mirrored here through the one free-form field the schema
offers: metadata is stamped with {"generated_from": "bank_lines/{id}"} (or
"payments/{id}"), and every run scans existing tasks for that marker before
creating another. Same posture as the composers, different field because
tasks was never built to be a composer target.

Settings (app_settings, code-side defaults so an absent row is safe):

    reconcile.escalate_after_days   how many days of silence before a line
                                     or payment becomes a task (10)
    reconcile.escalate_to           user id to hold accountable; blank means
                                     the item's own owner_id ("")
    reconcile.escalation_enabled    kill switch for the whole runner ("true")

Best-effort like system_bank_matcher and system_invoice_aging: a single bad
row is skipped (recorded in results with an error) rather than aborting the
run, and the whole runner is a no-op -- not a crash -- on a server where
banking or tasks was never installed. Reconciliation must keep working (the
matcher, the resolution verbs) even where nothing is around to receive an
escalation.
"""

import json
import os
from datetime import date, timedelta

import object_ids
import object_records

ACTOR = "system_bank_escalation"

DEFAULT_AFTER_DAYS = 10
DEFAULT_ENABLED = "true"


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


def _int_setting(base, key, default):
    try:
        return int(_setting(base, key, default))
    except (TypeError, ValueError):
        return default


def _bool_setting(base, key, default):
    return str(_setting(base, key, default)).strip().lower() not in ("0", "false", "no", "off", "")


def _parse_date(value):
    try:
        y, m, d = (int(x) for x in str(value)[:10].split("-"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        return None


def _existing_markers(tasks):
    """generated_from values already claimed by an escalation task.

    tasks.metadata is a free-form JSON string (same convention as
    app-templates' schema/default_values); a row this runner did not write,
    or wrote before this field existed, simply has nothing to parse and is
    skipped rather than treated as a collision.
    """
    markers = set()
    for task in tasks:
        raw = task.get("metadata") or ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        marker = data.get("generated_from") if isinstance(data, dict) else None
        if marker:
            markers.add(marker)
    return markers


def _confirmed_payment_refs(lines):
    """Payment refs some bank line already claims via matched_to.

    matched_to is set on match (match_status=matched) and stays set through
    resolution (e.g. action_resolve_bank_line's nsf path), so this is a
    property of the line regardless of its current match_status.
    """
    refs = set()
    for line in lines:
        target = (line.get("matched_to") or "").strip()
        if target.startswith("payments/"):
            refs.add(target)
    return refs


def _create_task(base, *, marker, title, description, urgency, owner_id):
    payload = {
        "id": object_ids.new_uuid4(),
        "title": title[:200],
        "description": description,
        "urgency": urgency,
        "owner_id": owner_id or "",
        "metadata": json.dumps({"generated_from": marker}),
    }
    return object_records.create_collection_record(
        "tasks", payload, base_dir=base, actor=ACTOR)


def POST(request):
    base = _base_dir()
    today = _parse_date(request.get("today")) or date.today()
    only_account = str(request.get("bank_account_id") or "").strip()

    if not _bool_setting(base, "reconcile.escalation_enabled", DEFAULT_ENABLED):
        return {"ok": True, "skipped": "escalation disabled (reconcile.escalation_enabled)"}

    after_days = max(0, _int_setting(base, "reconcile.escalate_after_days", DEFAULT_AFTER_DAYS))
    escalate_to = _setting(base, "reconcile.escalate_to", "")

    try:
        existing_tasks = object_records.read_collection_records("tasks", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "tasks not installed"}
    already_escalated = _existing_markers(existing_tasks)

    try:
        lines = object_records.read_collection_records("bank_lines", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "banking not installed (bank_lines absent)"}

    try:
        payments = object_records.read_collection_records("payments", base_dir=base)
    except Exception:
        payments = []

    confirmed = _confirmed_payment_refs(lines)

    scanned = escalated = already_open = 0
    results = []

    # Direction 1: bank lines the books cannot explain.
    for line in lines:
        status = line.get("match_status") or "unmatched"
        if status not in ("unmatched", "suggested"):
            continue
        if only_account and line.get("bank_account_id") != only_account:
            continue
        scanned += 1
        posted = _parse_date(line.get("posted_on"))
        if posted is None or (today - posted).days < after_days:
            continue

        marker = f"bank_lines/{line['id']}"
        if marker in already_escalated:
            already_open += 1
            results.append({"kind": "bank_line", "id": line["id"], "outcome": "already_open"})
            continue

        amount = line.get("amount_cents") or "0"
        desc = line.get("description") or "(no description)"
        title = f"Unexplained bank line: {desc} ({amount} cents, {line.get('posted_on')})"
        body = (
            f"Bank line {line['id']} on account {line.get('bank_account_id')} posted "
            f"{line.get('posted_on')} for {amount} cents (\"{desc}\") has been "
            f"{status} for {(today - posted).days} days with no matching book record. "
            "Confirm a match, or resolve it as a fee/interest/transfer/nsf/timing item."
        )
        target_owner = escalate_to or line.get("owner_id") or ""
        try:
            _create_task(base, marker=marker, title=title, description=body,
                         urgency="high", owner_id=target_owner)
        except Exception as exc:
            results.append({"kind": "bank_line", "id": line["id"], "error": str(exc)[:160]})
            continue
        escalated += 1
        results.append({"kind": "bank_line", "id": line["id"], "outcome": "escalated",
                        "days_old": (today - posted).days})

    # Direction 2: book records the bank never confirmed -- the forgotten,
    # more serious direction (a payment recorded that may never have landed).
    for payment in payments:
        if (payment.get("status") or "received") != "received":
            continue
        if f"payments/{payment['id']}" in confirmed:
            continue
        scanned += 1
        received = _parse_date(payment.get("received_on"))
        if received is None or (today - received).days < after_days:
            continue

        marker = f"payments/{payment['id']}"
        if marker in already_escalated:
            already_open += 1
            results.append({"kind": "payment", "id": payment["id"], "outcome": "already_open"})
            continue

        amount = payment.get("amount_cents") or "0"
        title = (f"Unconfirmed payment: {amount} cents received "
                 f"{payment.get('received_on')} (invoice {payment.get('invoice_id') or '?'})")
        body = (
            f"Payment {payment['id']} for {amount} cents against invoice "
            f"{payment.get('invoice_id') or '?'} (method {payment.get('method') or '?'}, "
            f"reference \"{payment.get('reference') or ''}\") was recorded as received on "
            f"{payment.get('received_on')} -- {(today - received).days} days ago -- but no "
            "bank line has confirmed the cash actually arrived. Verify against the bank "
            "statement before trusting this balance."
        )
        target_owner = escalate_to or payment.get("owner_id") or ""
        try:
            _create_task(base, marker=marker, title=title, description=body,
                         urgency="critical", owner_id=target_owner)
        except Exception as exc:
            results.append({"kind": "payment", "id": payment["id"], "error": str(exc)[:160]})
            continue
        escalated += 1
        results.append({"kind": "payment", "id": payment["id"], "outcome": "escalated",
                        "days_old": (today - received).days})

    return {"ok": True, "scanned": scanned, "escalated": escalated,
            "already_open": already_open, "after_days": after_days,
            "today": today.isoformat(), "results": results}
