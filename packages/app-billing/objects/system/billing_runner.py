"""system_billing_runner -- close periods, raise invoices, move the ladder.

POST {today?, dry_run?} -- the daily pass that turns subscriptions into
money owed. Three jobs, in this order:

1. **Trials that ended** become active (or move on unpaid, below).
2. **Periods that closed** raise ONE invoice for the period just ended
   and advance the subscription's dates. Idempotent by provenance:
   generated_from = "subscriptions/{id}:{period_start}", so a biller that
   runs twice, or is re-run after a crash, cannot double-bill. That
   property is not a nicety -- double-billing is the failure customers
   never forgive, and a dedup table would be one more thing to get wrong.
3. **Unpaid invoices age the subscription**: past due beyond a grace
   window moves active -> past_due, and further silence -> suspended.
   Paying restores service, because the ladder must be climbable in both
   directions or a paying customer stays cut off.

What it deliberately does NOT do: cancel anything on its own beyond
suspension, prorate arithmetic inside a line (a plan change closes the
period early and opens a new one -- see the spec), or bill wallet-mode
subscriptions, which have no periodic invoice by definition; their money
moved when the usage did.

Everything downstream is the machinery that already ships: the invoice
gets its portal token, its dunning schedule, its books entry when paid.
The biller's whole job is to decide that money is owed and say so once.
"""

import json
import os
from datetime import date, timedelta

import object_billing
import object_ids
import object_records

ACTOR = "system_billing_runner"

DEFAULT_GRACE_DAYS = 7
DEFAULT_SUSPEND_AFTER_DAYS = 30


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


def _advance(day_iso, period):
    """Same day next month/year, clamped -- the 31st of a 30-day month
    bills on the 30th rather than skipping a cycle."""
    try:
        y, m, d = (int(x) for x in str(day_iso)[:10].split("-"))
    except (ValueError, AttributeError):
        return ""
    if period == "year":
        for day in (d, 28):
            try:
                return date(y + 1, m, day).isoformat()
            except ValueError:
                continue
        return ""
    month = m + 1
    year = y + (1 if month > 12 else 0)
    month = 1 if month > 12 else month
    for day in (d, 30, 29, 28):
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def _existing_invoice(base, marker):
    try:
        for row in object_records.read_collection_records("invoices", base_dir=base):
            if row.get("notes") and marker in str(row.get("notes")):
                return row
    except Exception:
        return True  # cannot tell -> do not risk a second invoice
    return None


def _usage_lines(base, subscription, plan, period_start, period_end):
    """Rated overage for the period, from SUMMARIES not raw events.

    Allowances and tiers are properties of a period's total, so this can
    only happen at close -- rating each event as it landed would charge
    the first calls at retail and never apply the included quantity.
    """
    prices = object_billing.parse_prices(plan.get("prices"))
    if not prices:
        return [], 0, [], []
    try:
        rows = object_records.read_collection_records("usage_summaries", base_dir=base)
    except Exception:
        return [], 0, [], []
    mine = [r for r in rows
            if r.get("subscription_id") == subscription.get("id")
            and str(r.get("period_start") or "")[:10] == period_start
            and not _truthy(r.get("invoiced"))]
    rated = object_billing.rate_period(mine, prices)
    return rated["lines"], rated["total_minor"], rated["unpriced"], [r["id"] for r in mine]


def _raise_invoice(base, subscription, plan, period_start, period_end, today):
    """One invoice for the period that just ended: the base fee, plus any
    rated usage overage."""
    marker = f"subscriptions/{subscription['id']}:{period_start}"
    existing = _existing_invoice(base, marker)
    if existing:
        return {"skipped": "already invoiced", "marker": marker}

    base_minor = _int(plan.get("base_minor"))
    usage_lines, usage_minor, unpriced, summary_ids = _usage_lines(
        base, subscription, plan, period_start, period_end)
    if base_minor <= 0 and usage_minor <= 0:
        return {"skipped": "nothing to bill for this period",
                **({"unpriced_metrics": unpriced} if unpriced else {})}
    total_minor = base_minor + usage_minor

    grace = _int(_setting(base, "billing.invoice_due_days", "14"), 14)
    invoice_id = object_ids.new_uuid4()
    number = f"SUB-{str(subscription['id'])[:8]}-{period_start[:7]}"
    object_records.create_collection_record(
        "invoices",
        {
            "id": invoice_id,
            "number": number,
            "customer_id": subscription.get("customer_id", ""),
            "customer_name": subscription.get("customer_name") or "Customer",
            "customer_email": subscription.get("customer_email", ""),
            "status": "sent",
            "issue_date": today,
            "due_date": (date.fromisoformat(today) + timedelta(days=grace)).isoformat(),
            "subtotal_cents": str(total_minor),
            "total_cents": str(total_minor),
            # Provenance lives in notes because invoices carry no
            # generated_from column; the marker is what makes a re-run a
            # no-op, so it must be written where it can be found again.
            "notes": f"Generated by {ACTOR} [{marker}]",
            "owner_id": subscription.get("owner_id", ""),
        },
        base_dir=base, actor=ACTOR)
    try:
        if base_minor > 0:
            object_records.create_collection_record(
                "invoice_lines",
                {
                    "id": object_ids.new_uuid4(),
                    "invoice_id": invoice_id,
                    "description": f"{plan.get('name') or 'Subscription'} "
                                   f"({period_start} to {period_end})",
                    "quantity": "1",
                    "unit_price_cents": str(base_minor),
                    "line_total_cents": str(base_minor),
                    "owner_id": subscription.get("owner_id", ""),
                },
                base_dir=base, actor=ACTOR)
        for line in usage_lines:
            # The line says what was used, what the plan included, and what
            # the overage cost -- a bill a customer can check rather than
            # a single opaque "usage" figure they can only dispute.
            object_records.create_collection_record(
                "invoice_lines",
                {
                    "id": object_ids.new_uuid4(),
                    "invoice_id": invoice_id,
                    "description": (f"{line['metric']}: {line['quantity']} used, "
                                    f"{line['included']} included, "
                                    f"{line['overage']} billable"),
                    "quantity": str(line["overage"]),
                    "unit_price_cents": str(line["unit_minor"] or 0),
                    "line_total_cents": str(line["amount_minor"]),
                    "owner_id": subscription.get("owner_id", ""),
                },
                base_dir=base, actor=ACTOR)
        for summary_id in summary_ids:
            object_records.update_collection_record(
                "usage_summaries", summary_id, {"invoiced": "true"},
                base_dir=base, actor=ACTOR)
    except Exception as exc:
        return {"invoice_id": invoice_id, "warning": f"line not written: {str(exc)[:100]}"}
    return {"invoice_id": invoice_id, "number": number, "amount_minor": total_minor,
            "base_minor": base_minor, "usage_minor": usage_minor,
            **({"unpriced_metrics": unpriced} if unpriced else {})}


def _unpaid_days(base, subscription, today):
    """Days past due of the oldest unpaid invoice for this subscriber."""
    worst = 0
    marker = f"subscriptions/{subscription['id']}:"
    try:
        rows = object_records.read_collection_records("invoices", base_dir=base)
    except Exception:
        return 0
    for row in rows:
        if marker not in str(row.get("notes") or ""):
            continue
        if row.get("status") in ("paid", "void"):
            continue
        due = str(row.get("due_date") or "")[:10]
        if not due or due >= today:
            continue
        try:
            days = (date.fromisoformat(today) - date.fromisoformat(due)).days
        except ValueError:
            continue
        worst = max(worst, days)
    return worst


def POST(request):
    base = _base_dir()
    today = str(request.get("today") or date.today().isoformat())
    dry_run = _truthy(request.get("dry_run"))

    try:
        subscriptions = object_records.read_collection_records("subscriptions", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "billing not installed (subscriptions absent)"}
    try:
        plans = {p["id"]: p for p in
                 object_records.read_collection_records("billing_plans", base_dir=base)}
    except Exception:
        plans = {}

    grace = _int(_setting(base, "billing.past_due_grace_days", DEFAULT_GRACE_DAYS),
                 DEFAULT_GRACE_DAYS)
    suspend_after = _int(_setting(base, "billing.suspend_after_days",
                                  DEFAULT_SUSPEND_AFTER_DAYS), DEFAULT_SUSPEND_AFTER_DAYS)

    invoiced = advanced = laddered = 0
    results = []
    for sub in subscriptions:
        sub_id = sub.get("id")
        status = sub.get("status") or "trialing"
        if status == "canceled":
            continue
        plan = plans.get(sub.get("plan_id")) or {}
        mode = sub.get("billing_mode") or "subscription"

        # 1. trial expiry
        trial_end = str(sub.get("trial_ends_on") or "")[:10]
        if status == "trialing" and trial_end and trial_end <= today:
            if not dry_run:
                object_records.update_collection_record(
                    "subscriptions", sub_id, {"status": "active"},
                    base_dir=base, actor=ACTOR)
            status = "active"
            laddered += 1
            results.append({"subscription": sub_id, "trial_ended": trial_end})

        # 2. close the period -- wallet mode never gets a periodic invoice,
        #    because its money already moved when the usage did.
        period_end = str(sub.get("current_period_end") or "")[:10]
        if (mode in ("subscription", "hybrid") and status in ("active", "past_due")
                and period_end and period_end <= today):
            period_start = str(sub.get("current_period_start") or "")[:10] or period_end
            if dry_run:
                results.append({"subscription": sub_id, "would_invoice_period": period_start})
            else:
                outcome = _raise_invoice(base, sub, plan, period_start, period_end, today)
                results.append({"subscription": sub_id, **outcome})
                if outcome.get("invoice_id"):
                    invoiced += 1
                next_end = _advance(period_end, plan.get("period") or "month")
                if next_end:
                    object_records.update_collection_record(
                        "subscriptions", sub_id,
                        {"current_period_start": period_end,
                         "current_period_end": next_end},
                        base_dir=base, actor=ACTOR)
                    advanced += 1
                if _truthy(sub.get("cancel_at_period_end")):
                    object_records.update_collection_record(
                        "subscriptions", sub_id, {"status": "canceled"},
                        base_dir=base, actor=ACTOR)
                    results.append({"subscription": sub_id,
                                    "canceled": "at period end, as requested"})
                    continue

        # 3. the ladder: unpaid invoices age the subscription
        overdue_days = _unpaid_days(base, sub, today)
        if not dry_run and overdue_days:
            if status == "active" and overdue_days > grace:
                object_records.update_collection_record(
                    "subscriptions", sub_id, {"status": "past_due"},
                    base_dir=base, actor=ACTOR)
                laddered += 1
                results.append({"subscription": sub_id, "past_due_after_days": overdue_days})
            elif status == "past_due" and overdue_days > suspend_after:
                object_records.update_collection_record(
                    "subscriptions", sub_id, {"status": "suspended"},
                    base_dir=base, actor=ACTOR)
                laddered += 1
                results.append({"subscription": sub_id, "suspended_after_days": overdue_days})

    return {"ok": True, "today": today, "invoiced": invoiced, "advanced": advanced,
            "status_moves": laddered, "results": results}
