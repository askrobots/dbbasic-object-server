"""Promotions: a code, what it takes off, and every reason it cannot.

The pure half of discounting, kept out of the objects so it can be tested
exhaustively without a data directory -- the same posture as
object_rates, object_billing, object_money and object_cart. Nothing here
reads a file, writes a row or knows what a shop is; the object renders
and writes, this module decides.

**A code is a version, not a setting.** Promotions resolve the way rate
cards do (object_rates.find_rate): the terms in force are the newest ones
that had ALREADY STARTED, and resolution never looks forward. A code
whose terms change in September must not restate August's baskets, and a
September row must not discount an August order because somebody typed
the same word. Two rows may therefore share a code -- that is a re-run of
a campaign on new terms, and it is the ordinary case, not an error.

**The terms are stamped at redemption.** What a promotion said when
somebody used it belongs to the redemption, exactly as a rate belongs to
the approved time entry and a price belongs to the invoice line. Editing
the promotion next month then changes what the NEXT shopper gets and
nothing else, which is the only behaviour a shop can defend when a
customer emails asking why their receipt says 20% and the page says 10%.
`terms()` is what the writer copies onto the row.

**The count of redemptions is a fold over the redemptions.** promotions
carries a redemptions_used caption, and it is a rollup over the same rows
this module counts -- the identical argument wallets.balance_minor makes
about wallet_entries. A gate that trusted the caption would authorise a
1001st use of a 1000-use code the moment that caption went stale, so
`blockers` counts the rows.

**Every reason at once.** `blockers` returns a LIST and never returns
early. A shopper told their code has expired, who then finds a new one
and is told the basket is too small, and then that they have already used
it, is a shopper who has left. This is the same rule action_checkout
follows for prices, stock and collection times, and it is easy to break
by writing the obvious `if ...: return`.

**Rounded once.** A percentage is applied to the whole discountable base
and rounded a single time, for the reason object_cart.tax_cents states
about tax: rounding per line accumulates a cent of error per row that
nobody can reconcile, and the single rounding is the only version a
customer can check with a calculator.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

# What a promotion can do. `value` means basis points for percent (2000 =
# 20%), integer cents for fixed, and nothing at all for free_shipping --
# the postage is whatever the postage is, and a "free shipping worth up to
# $5" promotion is a fixed discount wearing a costume.
KINDS = ("percent", "fixed", "free_shipping")

STACKING = ("never", "with_others")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _num(value: Any, default: int = 0) -> Decimal:
    try:
        return Decimal(_text(value) or str(default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    return _text(value).lower() in ("true", "1", "yes", "on")


def normalize_code(value: Any) -> str:
    """A code as it is COMPARED: trimmed and upper-cased.

    Case-insensitive at redemption because a code is something a human
    retypes off a postcard, an email footer or a podcast advert, and
    refusing `spring20` because the shop typed `SPRING20` is a shop
    losing a sale to its own shift key. Stored as the seller wrote it,
    compared as this function says.
    """
    return _text(value).upper()


def _active(promo: dict) -> bool:
    # Absent is_active means active: a promotion written without the flag
    # is one somebody meant to run, same reading object_rates gives a rate
    # card.
    raw = promo.get("is_active")
    return True if raw in (None, "") else _truthy(raw)


def _started(promo: dict, day: str) -> bool:
    starts = _text(promo.get("starts_on"))[:10]
    return not (starts and day and starts > day)


def _ended(promo: dict, day: str) -> bool:
    ends = _text(promo.get("ends_on"))[:10]
    return bool(ends and day and ends < day)


def resolve(code: Any, promotions: list[dict], *, on_date: Any) -> dict | None:
    """The promotion this code names on this day, or None.

    NEVER LOOKS FORWARD. Where several rows share a code -- a campaign
    re-run on new terms -- the answer is the newest one that had already
    started on `on_date`, exactly as object_rates picks the newest card
    already in force. Picking the future row would discount today's
    basket at terms nobody has published yet.

    When NO row has started, the soonest-starting one is returned anyway
    rather than None, and that is deliberate: `blockers` has to be able
    to say "that code starts on the 3rd of June" instead of "no such
    code". A refusal that names the date keeps the customer; a refusal
    that denies the code exists sends them to support.

    Matching is case-insensitive (see normalize_code). Expiry is NOT
    filtered here for the same reason a future start is not: an expired
    code must be refused with its end date, and a resolver that returned
    None could only say the code was wrong.
    """
    wanted = normalize_code(code)
    if not wanted:
        return None
    day = _text(on_date)[:10]

    matches = [row for row in promotions
               if normalize_code(row.get("code")) == wanted]
    if not matches:
        return None

    def _order(row: dict) -> tuple[str, str]:
        return (_text(row.get("starts_on"))[:10], _text(row.get("id")))

    in_force = [row for row in matches if _started(row, day)]
    if in_force:
        return max(in_force, key=_order)
    # Nothing has started yet: hand back the one that starts soonest so the
    # refusal can name its date.
    return min(matches, key=_order)


def applies_to(promo: dict | None) -> str:
    """Which half of the bill this promotion comes off: "goods" or
    "shipping".

    It matters because tax does. A discount on goods reduces the taxable
    base; free postage reduces the postage, and where a jurisdiction taxes
    delivery it reduces that tax too. One string, read once, so
    object_cart.checkout_totals does not have to know what a promotion is.
    """
    if promo and _text(promo.get("kind")) == "free_shipping":
        return "shipping"
    return "goods"


def discount_for(subtotal_cents: Any, shipping_cents: Any,
                 promo: dict | None) -> int:
    """What this promotion takes off, in whole cents, ROUNDED ONCE.

    Applied to the whole base rather than per line, for the reason
    object_cart.tax_cents gives about tax: four separately-rounded lines
    accumulate an error nobody can reconcile against the percentage
    printed on the receipt.

    CLAMPED TO THE BASE IT COMES OFF, always. A $50 code against a $30
    basket takes off $30 and not a cent more -- a shop does not owe money
    to somebody for shopping, and a negative total would propagate into an
    invoice, a payment and eventually a refund of money nobody paid. The
    clamp lives here rather than in the caller so that every caller gets
    it, including the ones written later.
    """
    if not promo:
        return 0
    kind = _text(promo.get("kind"))
    subtotal = max(0, _int(subtotal_cents))
    postage = max(0, _int(shipping_cents))

    if kind == "free_shipping":
        return postage
    if kind == "fixed":
        return min(max(0, _int(promo.get("value"))), subtotal)
    if kind == "percent":
        bps = max(0, _int(promo.get("value")))
        if not bps or not subtotal:
            return 0
        cut = (Decimal(subtotal) * Decimal(bps) / Decimal(10000)).to_integral_value(
            rounding=ROUND_HALF_UP)
        return min(int(cut), subtotal)
    # An unknown kind discounts nothing. A promotion whose terms cannot be
    # read must not guess in the customer's favour or the shop's.
    return 0


def redemptions_of(promo: dict | None, redemptions: Iterable[dict]) -> list[dict]:
    """Every row recording a use of this promotion.

    Folded, never counted from promotions.redemptions_used: that number is
    a rollup over exactly these rows, and a gate that reads a derived
    caption it could recompute is a gate that authorises the 1001st use of
    a 1000-use code the moment the caption goes stale. Identical argument
    to hook_wallet_entries summing the ledger instead of reading the
    wallet's balance.
    """
    if not promo:
        return []
    promotion_id = _text(promo.get("id"))
    return [row for row in redemptions
            if _text(row.get("promotion_id")) == promotion_id]


def customer_redemptions(promo: dict | None, redemptions: Iterable[dict],
                         customer_email: Any) -> list[dict]:
    """This customer's uses of this promotion, matched case-insensitively
    on the email -- the same address typed with a capital letter is the
    same person, and a per-customer limit that Shift keys around is not a
    limit."""
    email = _text(customer_email).lower()
    if not email:
        return []
    return [row for row in redemptions_of(promo, redemptions)
            if _text(row.get("customer_email")).lower() == email]


def blockers(promo: dict | None, subtotal_cents: Any, customer_email: Any,
             redemptions: Iterable[dict], *, on_date: Any = "",
             code: Any = "", others: Iterable[Any] = ()) -> list[str]:
    """EVERY reason this code cannot be used, gathered in one pass.

    A list of sentences, never a first-failure return. The full-report
    rule is the same one action_checkout follows for prices, stock and
    collection times, and it exists because a checkout that reveals one
    problem at a time is a checkout people abandon.

    Each sentence carries the NUMBER or the DATE that caused it. "This
    code has expired" makes somebody write in; "this code ended on
    2026-06-30" ends the conversation.
    """
    typed = normalize_code(code)
    if promo is None:
        return [f"There is no promotion with the code '{typed}'." if typed
                else "No promotion code was given."]

    day = _text(on_date)[:10]
    subtotal = max(0, _int(subtotal_cents))
    used = redemptions_of(promo, redemptions)
    shown = normalize_code(promo.get("code")) or typed
    reasons: list[str] = []

    if not _active(promo):
        reasons.append(f"The code {shown} is not active.")

    starts = _text(promo.get("starts_on"))[:10]
    if not _started(promo, day):
        reasons.append(f"The code {shown} does not start until {starts}.")
    if _ended(promo, day):
        reasons.append(f"The code {shown} ended on "
                       f"{_text(promo.get('ends_on'))[:10]}.")

    minimum = max(0, _int(promo.get("minimum_spend_cents")))
    if minimum and subtotal < minimum:
        reasons.append(f"The code {shown} needs a basket of at least "
                       f"{minimum / 100:.2f}; this one is {subtotal / 100:.2f}.")

    cap = max(0, _int(promo.get("max_redemptions")))
    if cap and len(used) >= cap:
        reasons.append(f"The code {shown} has been used {len(used)} times and "
                       f"its limit is {cap}.")

    per_customer = max(0, _int(promo.get("per_customer_limit")))
    if per_customer:
        mine = customer_redemptions(promo, used, customer_email)
        if len(mine) >= per_customer:
            reasons.append(
                f"The code {shown} has already been used {len(mine)} times by "
                f"{_text(customer_email)} and allows {per_customer} per "
                f"customer.")

    stacking = _text(promo.get("stacking")) or "never"
    combined = [normalize_code(other) for other in others
                if normalize_code(other) and normalize_code(other) != shown]
    if combined and stacking == "never":
        reasons.append(f"The code {shown} cannot be combined with "
                       f"{', '.join(sorted(set(combined)))}.")

    return reasons


def terms(promo: dict | None) -> dict:
    """What gets STAMPED on the redemption row, so a later edit to the
    promotion can never restate what somebody already got.

    Doctrine #1, the same act as stamping a rate on an approved time entry
    and a price on an invoice line. The redemption is the record of an
    agreement; the promotion is only where the agreement was published.
    """
    if not promo:
        return {}
    return {
        "promotion_id": _text(promo.get("id")),
        "code_used": normalize_code(promo.get("code")),
        "kind": _text(promo.get("kind")),
        "value": str(_int(promo.get("value"))),
        "minimum_spend_cents": str(_int(promo.get("minimum_spend_cents"))),
        "stacking": _text(promo.get("stacking")) or "never",
    }
