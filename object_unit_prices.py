"""Media pricing: what one image or one second of video costs.

`ai_prices` prices a token: input_per_million / output_per_million, and
object_ai.compute_cost_cents multiplies. Media cannot be expressed in
that shape at all, and pretending otherwise is how a $6.00 run gets
quoted at zero.

    images   (model, quality, size)  -> cents per image
    video    (model, size)           -> cents per second

Two things follow from those tuples, and both are the point of this
module existing separately rather than as another column on ai_prices:

**A price is a LOOKUP, not a rate.** There is no arithmetic that turns
gpt-image-2 at 1024x1024 into gpt-image-2 at 1536x1024; they are
different products with different prices, published as a table. So this
module looks up a row and refuses when there is no row, rather than
interpolating, defaulting, or falling back to a cheaper neighbour --
every one of which would quote a price the provider will not honour.

**The quote must exist BEFORE the call.** A media run is held against the
wallet at queue time, so the price has to be knowable from the request
alone: model, quality, size, and (for video) duration. That is why
`quote` takes a spec dict rather than a provider response. A run whose
price cannot be computed in advance is a run that cannot be held, and it
must be refused at submission rather than discovered afterwards -- the
whole reason holds exist.

Integer minor units throughout (doctrine #10). Video is the interesting
case: per-second pricing times a fractional duration must not lose
money to truncation, so seconds are carried as Decimal and the product
is rounded HALF UP to the cent at the end, once.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

UNIT_PRICES_COLLECTION = "unit_prices"

#: What each kind is priced per. `image` is a flat per-item price;
#: `video` multiplies by duration_seconds.
PER_ITEM_KINDS = ("image",)
PER_SECOND_KINDS = ("video", "audio")

#: The dimensions that identify a price row, per kind. Order matters
#: only for the message a refusal prints.
KIND_DIMENSIONS = {
    "image": ("model", "quality", "size"),
    "video": ("model", "size"),
    "audio": ("model",),
}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _decimal(value: Any) -> Decimal | None:
    text = _text(value)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def find_price(rows: Iterable[dict], spec: dict) -> dict | None:
    """The price row matching a spec's dimensions, or None.

    Matching is exact on every dimension the kind declares. A row whose
    dimension is BLANK is a wildcard for that dimension -- which is how a
    provider that charges one price for every size is expressed without
    enumerating sizes -- but an exact match always wins over a wildcard,
    so adding a specific row later overrides the catch-all without
    having to delete it.
    """
    kind = _text(spec.get("kind"))
    dimensions = KIND_DIMENSIONS.get(kind)
    if dimensions is None:
        return None

    exact, wildcard = None, None
    for row in rows or ():
        if _text(row.get("kind")) != kind:
            continue
        specificity = 0
        matched = True
        for name in dimensions:
            wanted = _text(spec.get(name))
            have = _text(row.get(name))
            if not have:
                continue                     # wildcard on this dimension
            if have != wanted:
                matched = False
                break
            specificity += 1
        if not matched:
            continue
        if specificity == len(dimensions):
            exact = row
            break
        if wildcard is None or specificity > wildcard[0]:
            wildcard = (specificity, row)
    if exact is not None:
        return exact
    return wildcard[1] if wildcard else None


def quote(rows: Iterable[dict], spec: dict) -> dict:
    """Price one media request in advance.

    Returns {"price_cents": int, "price_row_id": str, "detail": str} or
    {"error": str} -- and the error is deliberately a value rather than
    an exception, because "we cannot price this" is a normal answer that
    a submission path must turn into a 409 naming what to configure, not
    an exceptional one.
    """
    kind = _text(spec.get("kind"))
    if kind not in KIND_DIMENSIONS:
        return {"error": (f"No pricing for media kind {kind!r}. Known kinds: "
                          f"{', '.join(sorted(KIND_DIMENSIONS))}.")}

    quantity = _decimal(spec.get("quantity"))
    if quantity is None:
        quantity = Decimal("1")
    if quantity <= 0:
        return {"error": "quantity must be positive"}

    row = find_price(rows, spec)
    if row is None:
        named = ", ".join(f"{name}={_text(spec.get(name)) or '?'}"
                          for name in KIND_DIMENSIONS[kind])
        return {"error": (f"No unit price for {kind} ({named}). Add a "
                          f"{UNIT_PRICES_COLLECTION} row before running this "
                          f"-- a media run is held against the wallet at "
                          f"submission, so an unpriced model cannot be run "
                          f"rather than being billed after the fact.")}

    unit = _decimal(row.get("unit_price_cents"))
    if unit is None or unit < 0:
        return {"error": (f"Unit price row {row.get('id')!r} has no usable "
                          f"unit_price_cents.")}

    if kind in PER_SECOND_KINDS:
        seconds = _decimal(spec.get("duration_seconds"))
        if seconds is None or seconds <= 0:
            return {"error": (f"{kind} is priced per second, so "
                              f"duration_seconds is required to quote it in "
                              f"advance.")}
        total = unit * seconds * quantity
        detail = (f"{seconds}s x {quantity} @ {unit}c/s")
    else:
        total = unit * quantity
        detail = f"{quantity} x {unit}c"

    cents = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {"price_cents": cents,
            "price_row_id": _text(row.get("id")),
            "detail": detail}
