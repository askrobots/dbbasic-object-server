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


# === wallet money reaching the books ==========================================
#
# The gap this closes: wallet_entries recorded every movement of customer
# money and NONE of it reached fin_journals. system_books (app-payments)
# composes journals for payments/refunds/invoices; nothing did the same
# for the prepaid wallet, so a box that charged real money showed zero
# revenue on the P&L and neither the cash nor the customer liability on
# the balance sheet.
#
# THE ACCOUNTING POINT, which is easy to get flatteringly wrong: **a
# top-up is not revenue.** Money taken before the service is rendered is
# money owed back -- a LIABILITY -- and it becomes revenue only when the
# work is done. Booking top-ups as revenue overstates income by
# everything sitting unspent in customer wallets and understates what is
# owed by the same amount. So the pair is:
#
#   top-up   DR cash              CR customer funds   (deferred, not earned)
#   debit    DR customer funds    CR revenue          (earned, now)
#
# Holds compose NOTHING, and that is doctrine rather than an omission: a
# hold is not a debit that got undone, it is money that was never spent.
# No economic event occurs when funds are ring-fenced inside the same
# liability, so no entry should exist -- and the reconciliation below
# knows to add outstanding holds back, which is the difference between a
# report that is right and a report that cries wolf whenever a run is in
# flight.

WALLET_NO_ENTRY_KINDS = ("hold", "release")

WALLET_ACCOUNT_KEYS = (
    "cash", "customer_funds", "revenue", "promo_expense", "adjustment",
)


def wallet_posting(kind: Any, amount_minor: Any, accounts: dict) -> dict:
    """Map one wallet entry to a DR/CR pair, or refuse with a reason.

    Pure: no I/O, no clock, no settings lookup -- the caller resolves
    account ids and this decides the accounting. Returns either
    {"skip": reason} or {"debit", "credit", "amount_minor"} with a
    POSITIVE amount (the direction is carried by which account is which,
    never by a sign, so a negative never reaches a journal line).

    `accounts` maps the WALLET_ACCOUNT_KEYS names to fin_accounts ids;
    an unconfigured account is a skip, never a guess, because posting
    real money to a plausible-looking account is worse than not posting.
    """
    kind_text = str(kind or "").strip()
    try:
        amount = int(amount_minor)
    except (TypeError, ValueError):
        return {"skip": f"unusable amount {amount_minor!r}"}

    if kind_text in WALLET_NO_ENTRY_KINDS:
        return {"skip": "a hold is money that was never spent, not a debit "
                        "that got undone -- ring-fencing funds inside the "
                        "same liability is not an economic event"}
    if amount == 0:
        return {"skip": "zero amount"}

    def resolved(*names):
        missing = [n for n in names if not str(accounts.get(n) or "").strip()]
        if missing:
            return None, ("accounts unconfigured: "
                          + ", ".join(f"billing.journal.{n}_account" for n in missing))
        return [str(accounts[n]).strip() for n in names], None

    if kind_text in ("topup", "auto_topup"):
        # Cash in, service owed. Deliberately NOT revenue.
        got, why = resolved("cash", "customer_funds")
        if why:
            return {"skip": why}
        cash, funds = got
        if amount < 0:
            # A negative top-up is a withdrawal: cash leaves, the debt shrinks.
            return {"debit": funds, "credit": cash, "amount_minor": -amount}
        return {"debit": cash, "credit": funds, "amount_minor": amount}

    if kind_text == "debit":
        # The moment the service is rendered: the liability shrinks and
        # the revenue is finally real.
        got, why = resolved("customer_funds", "revenue")
        if why:
            return {"skip": why}
        funds, revenue = got
        if amount > 0:
            return {"skip": "a positive debit is not a debit"}
        return {"debit": funds, "credit": revenue, "amount_minor": -amount}

    if kind_text == "refund":
        # Credit returned to the wallet un-earns revenue; it does not move
        # cash, because the money never left the wallet in the first place.
        got, why = resolved("customer_funds", "revenue")
        if why:
            return {"skip": why}
        funds, revenue = got
        if amount < 0:
            return {"skip": "a negative refund is not a refund"}
        return {"debit": revenue, "credit": funds, "amount_minor": amount}

    if kind_text == "promo":
        # Credit we granted. It costs us even though no cash moved --
        # which is exactly why promotional credit belongs on the P&L
        # rather than quietly inflating the liability from nowhere.
        got, why = resolved("promo_expense", "customer_funds")
        if why:
            return {"skip": why}
        expense, funds = got
        if amount < 0:
            return {"debit": funds, "credit": expense, "amount_minor": -amount}
        return {"debit": expense, "credit": funds, "amount_minor": amount}

    if kind_text == "adjustment":
        # Deliberately its own account rather than folded into revenue:
        # an adjustment is somebody correcting something, and burying
        # those in revenue is how a P&L stops being auditable.
        got, why = resolved("adjustment", "customer_funds")
        if why:
            return {"skip": why}
        adjust, funds = got
        if amount < 0:
            return {"debit": funds, "credit": adjust, "amount_minor": -amount}
        return {"debit": adjust, "credit": funds, "amount_minor": amount}

    return {"skip": f"no accounting policy for wallet entry kind {kind_text!r}"}


def outstanding_holds_minor(entries) -> int:
    """How much is ring-fenced right now, as a POSITIVE number.

    Holds are negative entries and releases positive ones, so a
    hold that has been released cancels itself and only funds still in
    flight survive the fold.
    """
    total = 0
    for row in entries or []:
        if str(row.get("kind") or "") not in WALLET_NO_ENTRY_KINDS:
            continue
        try:
            total += int(row.get("amount_minor") or 0)
        except (TypeError, ValueError):
            continue
    return -total


def customer_funds_reconciliation(
    wallet_balances_minor, booked_debit_minor: int, booked_credit_minor: int,
    *, outstanding_holds: int = 0,
) -> dict:
    """Does what customers are owed match what the books say we owe?

    The invariant that makes prepaid balances honest: sum every wallet,
    add back what is merely ring-fenced, and it must equal the credit
    balance of the customer-funds liability account.

    The holds term is what stops this being a false alarm generator. A
    wallet with an open hold reports a balance BELOW what the customer is
    actually owed -- the money is theirs until the run settles -- while
    the books, correctly, composed nothing for the hold. Without adding
    holds back, every reconciliation run during an in-flight template run
    reports a discrepancy that is not one.
    """
    held = sum(int(b or 0) for b in wallet_balances_minor or [])
    owed = held + int(outstanding_holds or 0)
    booked = int(booked_credit_minor or 0) - int(booked_debit_minor or 0)
    difference = owed - booked
    return {
        "wallet_balances_minor": held,
        "outstanding_holds_minor": int(outstanding_holds or 0),
        "owed_to_customers_minor": owed,
        "booked_liability_minor": booked,
        "difference_minor": difference,
        "balanced": difference == 0,
    }
