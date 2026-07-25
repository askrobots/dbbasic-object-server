"""Rate cards: what an hour of human work costs, and which hour.

The pure half of time-and-materials billing, kept out of the objects so
it can be tested exhaustively without a data directory -- the same
posture as object_billing's rating and object_money's conversions.

Consulting hours are a usage metric with an approval gate. That is the
whole insight behind this file: metering, rating and invoicing are the
same spine as cloud billing, and the only structural difference is that a
human has to say "yes, bill that" before an hour becomes money. So the
rating half looks deliberately like object_billing -- integer minor
units, rounding stated where it happens, no I/O.

Two rules matter more than the arithmetic:

**Most specific wins.** A rate card can be scoped to a person on a
project, a project, a person, or nothing at all. Resolution walks from
narrowest to widest and stops, because the reason anyone writes a rate
for "Dana on the Acme rebuild" is that it must beat both the Acme rate
and Dana's standard rate.

**Never look forward.** A card applies from its valid_from onward, so the
rate in force is the newest one that had already started when the work
happened. Picking today's rate for last quarter's unbilled hours is how a
client gets an invoice that does not match the engagement letter they
signed.

The resolved rate is then STAMPED onto the entry at approval (doctrine
#1). Everything here is about choosing it correctly once; after that the
number belongs to the entry, and a rate change next quarter can never
reprice work someone already approved.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
from typing import Any

# Narrowest first. A card matches a scope only when every part of that
# scope is present on it AND agrees with the work; the first scope with
# any match wins outright, so a project rate is never averaged against a
# person rate -- one of them is the answer.
SCOPE_ORDER = ("person_project", "project", "person", "default")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _num(value: Any, default: int = 0) -> Decimal:
    try:
        return Decimal(_text(value) or str(default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _scope_of(card: dict) -> str:
    """Which scope a card occupies, read from what it actually carries.

    Derived rather than trusted from a scope_type column: a card naming
    both a person and a project IS a person-project rate, and letting the
    two disagree would mean a card that resolves differently from how it
    reads.
    """
    person = _text(card.get("person_id"))
    project = _text(card.get("project_id"))
    if person and project:
        return "person_project"
    if project:
        return "project"
    if person:
        return "person"
    return "default"


def _truthy(value: Any) -> bool:
    return _text(value).lower() in ("true", "1", "yes", "on")


def _active(card: dict) -> bool:
    # Absent is_active means active: a rate card written without the flag
    # is a rate somebody meant to use.
    raw = card.get("is_active")
    return True if raw in (None, "") else _truthy(raw)


def find_rate(cards: list[dict], *, on_date: str, person_id: str = "",
              project_id: str = "") -> dict | None:
    """The rate card in force for this work, or None.

    `on_date` is the date the work HAPPENED, not today: an entry approved
    late is still billed at the rate that applied when it was done.
    """
    day = _text(on_date)[:10]
    person = _text(person_id)
    project = _text(project_id)

    candidates: dict[str, list[dict]] = {scope: [] for scope in SCOPE_ORDER}
    for card in cards:
        if not _active(card):
            continue
        if _num(card.get("hourly_rate_cents"), -1) < 0:
            continue
        valid_from = _text(card.get("valid_from"))[:10]
        if day and valid_from and valid_from > day:
            continue                      # not yet in force -- never look forward
        scope = _scope_of(card)
        card_person = _text(card.get("person_id"))
        card_project = _text(card.get("project_id"))
        if card_person and card_person != person:
            continue
        if card_project and card_project != project:
            continue
        candidates[scope].append(card)

    for scope in SCOPE_ORDER:
        rows = candidates[scope]
        if not rows:
            continue
        # Newest applicable version wins within the scope. Rate cards
        # version forward and are never edited, so "the latest one that
        # had already started" is the whole of the lookup.
        return max(rows, key=lambda c: (_text(c.get("valid_from"))[:10],
                                        _text(c.get("id"))))
    return None


def billable_seconds(duration_seconds: Any, increment_minutes: Any = 0) -> int:
    """Seconds actually billed, after any minimum increment.

    Increment lives on the rate card rather than in a global setting
    because rounding to the next six minutes is a term of the engagement,
    not a preference of the server. It rounds UP -- that is what an
    increment means -- which is exactly why it must be something a client
    agreed to in writing rather than a default that quietly inflates
    every bill. Zero (the default) bills the seconds that happened.
    """
    seconds = _num(duration_seconds)
    if seconds <= 0:
        return 0
    step = _num(increment_minutes)
    if step <= 0:
        return int(seconds)
    step_seconds = step * 60
    blocks = (seconds / step_seconds).to_integral_value(rounding=ROUND_UP)
    return int(blocks * step_seconds)


def amount_cents(duration_seconds: Any, hourly_rate_cents: Any,
                 increment_minutes: Any = 0) -> int:
    """What this entry is worth, rounded half-up ONCE.

    Rounding per entry, not per hour and not per invoice: an entry is the
    thing a client disputes, so it has to be the thing whose arithmetic
    they can reproduce, and the invoice total must equal the sum of its
    own lines.
    """
    seconds = Decimal(billable_seconds(duration_seconds, increment_minutes))
    if seconds <= 0:
        return 0
    rate = _num(hourly_rate_cents)
    if rate <= 0:
        return 0
    amount = (seconds / Decimal(3600)) * rate
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


def rate_entry(entry: dict, cards: list[dict]) -> dict:
    """Resolve and price one time entry: {rate_cents, amount_cents,
    increment_minutes, rate_card_id, unrated_reason}.

    An entry with no applicable card comes back priced at nothing WITH a
    reason, never priced at zero in silence. Unbillable hours that look
    identical to hours nobody wrote a rate for is how revenue goes
    missing quietly.
    """
    card = find_rate(
        cards,
        on_date=_text(entry.get("worked_on") or entry.get("started_at")),
        person_id=_text(entry.get("owner_id")),
        project_id=_text(entry.get("project_id")),
    )
    if card is None:
        return {"rate_cents": 0, "amount_cents": 0, "increment_minutes": 0,
                "rate_card_id": "",
                "unrated_reason": "no rate card applies to this person, "
                                  "project and date"}
    rate = int(_num(card.get("hourly_rate_cents")))
    increment = int(_num(card.get("increment_minutes")))
    return {
        "rate_cents": rate,
        "increment_minutes": increment,
        "rate_card_id": _text(card.get("id")),
        "amount_cents": amount_cents(entry.get("duration_seconds"), rate, increment),
        "unrated_reason": "",
    }


def hours(duration_seconds: Any, increment_minutes: Any = 0) -> str:
    """Billable hours as a human reads them on an invoice line."""
    seconds = Decimal(billable_seconds(duration_seconds, increment_minutes))
    return str((seconds / Decimal(3600)).quantize(Decimal("0.01"),
                                                  rounding=ROUND_HALF_UP))
