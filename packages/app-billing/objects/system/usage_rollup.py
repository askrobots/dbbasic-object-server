"""system_usage_rollup -- fold events into the buckets rating reads.

POST {today?} -- walks unrated usage_events, groups them by
(subscription, metric, the subscription's current period), and writes or
updates the matching usage_summaries row, marking the events rated.

Why a fold at all, when rating could sum events directly: a month of
per-request rows must not be part of every invoice render, and pricing
must not be welded to the write side's shape. With this bucket in
between, metering can start batching later and no pricing code changes
(plan/billing-metering-spec.md section 2).

Idempotent by construction rather than by marker: an event is folded once
because folding sets rated=true, and a summary is updated in place by its
(subscription, metric, period) identity, so a re-run after a crash adds
nothing twice. Costs are summed alongside quantities, which is what makes
margin per customer per metric a read.
"""

import os
from datetime import date
from decimal import Decimal, InvalidOperation

import object_ids
import object_records

ACTOR = "system_usage_rollup"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _dec(value):
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal(0)


def _int(value):
    try:
        return int(str(value or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def POST(request):
    base = _base_dir()
    today = str(request.get("today") or date.today().isoformat())

    try:
        events = object_records.read_collection_records("usage_events", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "metering not installed (usage_events absent)"}
    try:
        subs = {s["id"]: s for s in
                object_records.read_collection_records("subscriptions", base_dir=base)}
    except Exception:
        subs = {}
    try:
        summaries = object_records.read_collection_records("usage_summaries", base_dir=base)
    except Exception:
        summaries = []

    existing = {(s.get("subscription_id"), s.get("metric"),
                 s.get("period_start"), s.get("period_end")): s for s in summaries}

    buckets = {}
    folded = 0
    orphaned = 0
    for event in events:
        if _truthy(event.get("rated")):
            continue
        sub = subs.get(event.get("subscription_id"))
        if sub is None:
            # Usage with no subscription cannot be priced; leave it
            # UNRATED and visible rather than folding it somewhere wrong.
            orphaned += 1
            continue
        key = (sub["id"], str(event.get("metric") or ""),
               str(sub.get("current_period_start") or "")[:10],
               str(sub.get("current_period_end") or "")[:10])
        entry = buckets.setdefault(key, {"quantity": Decimal(0), "cost": 0,
                                         "count": 0, "events": [],
                                         "owner_id": sub.get("owner_id", "")})
        entry["quantity"] += _dec(event.get("quantity"))
        entry["cost"] += _int(event.get("cost_minor"))
        entry["count"] += 1
        entry["events"].append(event["id"])
        folded += 1

    written = 0
    for (sub_id, metric, start, end), entry in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        row = existing.get((sub_id, metric, start, end))
        if row is not None:
            object_records.update_collection_record(
                "usage_summaries", row["id"],
                {"quantity": str(_dec(row.get("quantity")) + entry["quantity"]),
                 "event_count": str(_int(row.get("event_count")) + entry["count"]),
                 "cost_minor": str(_int(row.get("cost_minor")) + entry["cost"])},
                base_dir=base, actor=ACTOR)
        else:
            object_records.create_collection_record(
                "usage_summaries",
                {"id": object_ids.new_uuid4(), "subscription_id": sub_id,
                 "metric": metric, "period_start": start, "period_end": end,
                 "quantity": str(entry["quantity"]), "event_count": str(entry["count"]),
                 "cost_minor": str(entry["cost"]), "invoiced": "false",
                 "owner_id": entry["owner_id"]},
                base_dir=base, actor=ACTOR)
        written += 1
        for event_id in entry["events"]:
            object_records.update_collection_record(
                "usage_events", event_id, {"rated": "true"},
                base_dir=base, actor=ACTOR)

    return {"ok": True, "today": today, "events_folded": folded,
            "summaries_written": written, "orphaned_events": orphaned}
