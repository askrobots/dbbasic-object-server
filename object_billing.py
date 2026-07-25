"""Rating: turning measured usage into money owed.

The pure half of metered billing, kept out of the runner so it can be
tested exhaustively without a data directory -- same posture as
object_finance's folds and object_money's conversions.

Three rules shape everything here, and each exists because of a way real
vendors get bills wrong:

**Rate at period close, never per event.** Allowances and tiers are
properties of a PERIOD's total, not of an individual call. Rating each
event as it lands means the first thousand calls get charged at the
retail rate and the allowance never applies, which is precisely how
customers end up disputing invoices they cannot reconstruct.

**Rate from summaries, never from raw events.** A summary is a period
bucket; a month of per-request rows is not something a bill should be
re-derived from every time someone opens a page. This boundary is also
the scale valve: the write side can change (batching, an accumulator)
without touching a single line of pricing logic.

**Stamp what it cost US alongside what we charge.** usage_events carry a
cost, so margin is a fold over stored facts rather than a spreadsheet
someone maintains separately. A billing system that knows revenue but not
cost cannot answer "which customer loses us money", which is the first
question anyone asks once the AI bills arrive.

Integer minor units throughout; Decimal only where a rate multiplication
needs it, and rounding is stated at the point it happens.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def parse_prices(raw: Any) -> dict[str, dict]:
    """A plan's price JSON -> {metric: {included, unit_minor, tiers?}}.

    Unparseable pricing yields NO prices rather than an exception: a
    malformed plan must fail as "nothing to charge for", never as a
    crashed billing run that leaves half a customer base uninvoiced.
    The runner reports it; the pass keeps going.
    """
    if isinstance(raw, dict):
        candidate = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            candidate = json.loads(text)
        except (ValueError, TypeError):
            return {}
    if not isinstance(candidate, dict):
        return {}
    out = {}
    for metric, spec in candidate.items():
        if isinstance(spec, dict):
            out[str(metric)] = spec
    return out


def _num(value, default=0):
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def rate_metric(quantity: Any, spec: dict) -> dict:
    """Charge for one metric's period total under one plan's terms.

    Returns {"billable", "included", "overage", "unit_minor", "amount_minor",
    "tier_detail"}. `included` is consumed before anything is charged, and
    tiers (when present) price successive bands of the OVERAGE, which is
    the shape every metered vendor actually publishes:

        tiers: [{"upto": 1000, "unit_minor": 2}, {"unit_minor": 1}]

    The last tier may omit `upto`, meaning "everything above". Rounding is
    half-up, applied ONCE to the metric's total rather than per tier:
    rounding each band separately is how a bill stops matching the sum of
    its own line items.
    """
    total = _num(quantity)
    included = _num(spec.get("included"))
    overage = total - included
    if overage <= 0:
        return {"billable": total, "included": included, "overage": Decimal(0),
                "unit_minor": _num(spec.get("unit_minor")), "amount_minor": 0,
                "tier_detail": []}

    tiers = spec.get("tiers")
    if isinstance(tiers, list) and tiers:
        remaining = overage
        amount = Decimal(0)
        detail = []
        consumed = Decimal(0)
        for tier in tiers:
            if remaining <= 0:
                break
            if not isinstance(tier, dict):
                continue
            unit = _num(tier.get("unit_minor"))
            upto = tier.get("upto")
            if upto in (None, ""):
                band = remaining
            else:
                band = min(remaining, max(_num(upto) - consumed, Decimal(0)))
            if band <= 0:
                continue
            amount += band * unit
            detail.append({"band": str(band), "unit_minor": str(unit)})
            consumed += band
            remaining -= band
        if remaining > 0 and detail:
            # Tiers that do not cover the whole overage bill the remainder
            # at the last stated rate rather than silently free.
            unit = _num(tiers[-1].get("unit_minor") if isinstance(tiers[-1], dict) else 0)
            amount += remaining * unit
            detail.append({"band": str(remaining), "unit_minor": str(unit),
                           "note": "beyond the last tier, charged at its rate"})
        return {"billable": total, "included": included, "overage": overage,
                "unit_minor": None, "tier_detail": detail,
                "amount_minor": int(amount.to_integral_value(rounding=ROUND_HALF_UP))}

    unit = _num(spec.get("unit_minor"))
    amount = overage * unit
    return {"billable": total, "included": included, "overage": overage,
            "unit_minor": unit, "tier_detail": [],
            "amount_minor": int(amount.to_integral_value(rounding=ROUND_HALF_UP))}


def rate_period(summaries: list[dict], prices: dict[str, dict]) -> dict:
    """Every metric's charge for one period.

    Returns {"lines": [...], "total_minor": int, "unpriced": [metrics]}.
    A metric with usage but no price lands in `unpriced` and is charged
    NOTHING -- and is reported, because silently billing for something a
    plan never named is worse than the revenue is worth, while silently
    dropping it hides a pricing gap the operator needs to close.
    """
    lines = []
    unpriced = []
    total = 0
    for summary in sorted(summaries, key=lambda s: str(s.get("metric") or "")):
        metric = str(summary.get("metric") or "")
        if not metric:
            continue
        spec = prices.get(metric)
        if not isinstance(spec, dict):
            if _num(summary.get("quantity")) > 0:
                unpriced.append(metric)
            continue
        rated = rate_metric(summary.get("quantity"), spec)
        if rated["amount_minor"] <= 0:
            continue
        lines.append({
            "metric": metric,
            "quantity": str(rated["billable"]),
            "included": str(rated["included"]),
            "overage": str(rated["overage"]),
            "unit_minor": (None if rated["unit_minor"] is None
                           else str(rated["unit_minor"])),
            "amount_minor": rated["amount_minor"],
            "tier_detail": rated["tier_detail"],
        })
        total += rated["amount_minor"]
    return {"lines": lines, "total_minor": total, "unpriced": unpriced}


def margin(revenue_minor: int, cost_minor: int) -> dict:
    """What was earned against what it cost -- the question a billing
    system that only knows revenue can never answer."""
    revenue = int(revenue_minor or 0)
    cost = int(cost_minor or 0)
    gross = revenue - cost
    pct = None
    if revenue:
        pct = round((gross / revenue) * 100, 2)
    return {"revenue_minor": revenue, "cost_minor": cost,
            "gross_minor": gross, "gross_pct": pct}
