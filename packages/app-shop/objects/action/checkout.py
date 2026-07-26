"""action_checkout -- a basket becomes an order AND the bill for it.

POST {session_token, customer_email, customer_name?, confirm_prices?,
      preview?, customer_note?, gift_message?}

This is where browsing turns into a commitment, so it is where every
check happens at once:

- the products are still for sale,
- the prices still match what the shopper agreed to,
- the stock is there.

All of them are reported TOGETHER. Telling somebody about one problem,
letting them fix it, then revealing the next is how a checkout gets
abandoned -- and it is the easiest thing in the world to get wrong by
returning on the first failure.

Price disagreement is a hard stop with both numbers shown, not a silent
resolution in either direction. Accepting the new price on the shopper's
behalf is a bait-and-switch; honouring a three-week-old basket forever is
an open-ended liability. `confirm_prices: true` is the shopper saying yes
to the current prices, having seen them.

**Stock is checked here and moved nowhere.** The order is created as a
draft; stock moves and the order confirms when money actually arrives
(system_shop_fulfillment). Decrementing on checkout would make every
abandoned payment a phantom sale.

The oversell race is real and is not pretended away: two shoppers can
both pass this gate for the last unit. That is accepted deliberately at
this scale -- it surfaces at the stock move, which is visible and
refundable -- rather than paid for with a reservation ledger nobody has
needed yet. The same honest posture as hook_wallet_entries' documented
check-then-append race.

**The invoice is raised here too, already issued.** An order with no
invoice gives the buyer nothing to pay: the money is owed and there is no
document saying so and no door to walk through. The invoice is created
"sent" rather than "draft" because checkout IS the issuing act -- the
shopper has committed, and a draft would mean somebody in the back office
must press a button before the person standing at the counter can hand
over money. The response carries the pay link for the same reason.

**The order's own tracking token is minted here too.** This shop's
default sale is a guest checkout -- an email address and no account -- so
this response and the confirmation email are the only two places the
buyer will ever be handed a way back to their own order. Waiting for
system_order_portal_link (a post-commit reaction) would mean returning a
checkout response with no `track_path` in it, which is the one moment the
link is most obviously wanted. The handler tolerates a token that already
exists, so minting it early costs nothing and races with nothing.

If the invoice cannot be raised, the ORDER STILL STANDS. Losing a sale to
an invoicing hiccup is the expensive failure; an order without an invoice
is visible, and the period biller or an operator can raise one. So only
the invoice block is wrapped -- never the order.

**Tax and postage are charged here, or the shop is losing money and
breaking the law.** Four settings say how (all absent = a shop that
charges neither, which is exactly what it did before):

  shop.tax_rate_bps            basis points, 1500 = 15%; 0 = no tax
  shop.tax_shipping            is the postage part of the taxable base?
  shop.shipping_flat_cents     one flat charge; 0 = shipping disabled
  shop.shipping_free_over_cents  free over this basket size; 0 = never

The arithmetic itself is object_cart.checkout_totals and nothing here
recomputes any part of it -- this file only writes down what that fold
said. Retrofitting tax later would mean restating invoices that have
already been sent, which is why a flat honest rate now beats a clever
one afterwards.

**Postage is an invoice LINE, tax is an invoice TOTAL.** The line is
because a customer must be able to see what the delivery cost instead of
finding it welded into a number they cannot check, and because it keeps
the invoice's subtotal the sum of its own lines. The tax is a total
because invoices already carry a tax_cents field that means exactly
this, and inventing a "Sales tax" pseudo-line beside a real field would
give the same money two homes.

One seam is left open rather than papered over. invoice_totals, if an
operator ever turns DBBASIC_ENABLE_EVENT_HANDLERS on AND somebody edits
a line of one of these invoices through the API, restates tax as the sum
of the lines' own line_tax_cents -- and only the postage line carries
one, so such a restatement would drop the tax on the goods. Nothing
this file writes triggers that today (checkout writes through
object_records directly, which never reaches the dispatcher), and the
flag is off everywhere. The honest fixes are a decision for whoever owns
invoice_totals -- either it learns to leave an invoice-level tax alone,
or every taxable line carries its own rate -- and the second one costs a
cent of per-line rounding drift, which is exactly the error the single
rounding above exists to avoid. Written down here so the next person
finds it before a customer does.

The ORDER records tax_cents and a total_cents that includes both, but
its subtotal stays goods-only -- so postage on an order is currently
implied by total - subtotal - tax rather than stated. That is a known
gap: orders has no shipping_cents column yet. The moment one exists this
file stamps it (it checks the schema rather than assuming), and until
then the itemisation lives where a customer actually reads it, on the
invoice.

**Special instructions and a gift message: two fields, not a subsystem.**
customer_note is what the shopper needs the PACKER to know ("leave it
with the neighbour"), gift_message is what should be printed on the slip.
Both are optional, both are stamped on the cart, and both are stamped on
the ORDER through the same `_has_field` check shipping_cents uses -- the
picker and the packer read the order, and a note that lives only on a
basket is a note nobody in the warehouse ever sees. Until orders declares
those two columns the values stay on the cart rather than being written
into a column that does not exist, and the response says so
(`notes_on_cart_only`) instead of pretending the packer will get them:
the failure that prevents is a birthday message the shopper typed, the
shop charged for, and nobody ever printed.

**Per-line instructions travel; per-line deltas travel as MONEY.** A
basket line may carry `line_note` ("no onions") and `modifier_cents`
("oat milk +60c"). The note lands on the order line, where the kitchen
ticket reads it, and touches no total anywhere -- an instruction is not a
term of the sale. The delta is money, so it is inside every total this
file writes: object_cart.checkout_totals already folded it into
line_total_cents, the order line records it alongside the base price, and
the invoice line carries it in its EFFECTIVE unit price so the bill is
still an ordinary quantity x price document that multiplies out. Money
that reaches one total and not another is the failure this whole file is
arranged to prevent, which is why the delta is never a column sitting
beside a total that ignores it.

There is deliberately no gift FLAG. The packing slip carries no prices by
construction (site_packing_slip), so every parcel is already gift-safe,
and a flag somebody forgets to tick is exactly how a present arrives with
the amount paid stapled to it. The message is about warmth, not secrecy.

**A collection time is checked here and held by nothing.**
`fulfillment_method` (shipping | pickup | delivery | counter) and
`pickup_slot_id` are the two arguments that turn this into a counter
shop's checkout. When the method is `pickup` the slot must exist, be
open, be in the future, be past the shop's lead time, and have room --
and every one of those failures is reported ALONGSIDE the price, stock
and availability blockers above, in the same response, never returned
early on its own. That is not a style choice: a shopper told about a
dead slot, who fixes it, and is then told about a price change is a
shopper who has abandoned the basket, and this file is arranged the way
it is precisely to make that impossible.

A SHIPPING ORDER IS UNTOUCHED BY EVERY LINE OF IT. No method, or
`shipping`, means the gate reads no slots, adds no blockers, and writes
no new field -- orders.fulfillment_method defaults to `shipping`, so an
order raised by a shop that has never heard of pickup is byte-for-byte
the order it was before. That is the regression that matters most here
and it holds by construction rather than by a condition somebody has to
remember.

**The race is real and is not pretended away.** Two shoppers can both
pass this gate for the last place in a 6pm window, exactly as two can
both pass the stock gate for the last unit, and for the same reason: the
check is a read, nothing is reserved, and there is no lock. It is
accepted deliberately at this scale rather than paid for with a
reservation ledger nobody has needed yet -- the same honest posture
hook_wallet_entries documents for its own check-then-append. Two things
make it survivable. The loser finds out BEFORE PAYING, because the gate
runs before the order and the invoice exist. And the refusal NAMES THE
NEXT FREE SLOT, because "that time is full" sends somebody away while
"that time is full, the next one is 18:30" keeps the sale. When the race
is genuinely lost -- both increments land -- pickup_slots.orders_taken
comes to exceed capacity, and that is visible on the row it happened to,
which is what lets a shop ring somebody instead of finding out when they
turn up.

The bookability rule is duplicated here rather than imported from
app-pickup's action_pickup_slots, and the duplication is named in both
files: this package cannot execute another package's object as a
function, and a checkout that reached across for it would refuse orders
on a box where the pickup app is installed but its objects are not on
this root. Both read the same setting (`pickup.lead_minutes`) so the
picker and the gate cannot disagree about what "too soon" means.

**A DISCOUNT IS A PRICE. A GIFT CARD IS TENDER.** Two arguments arrive
here that both make a bill smaller, and everything about how they are
recorded follows from the fact that they are not the same kind of thing.

`promo_code` reduces what the goods COST. It therefore goes through
object_cart.checkout_totals like every other number on this sale, and it
comes off the taxable base BEFORE tax is computed -- which is not a
preference but what every jurisdiction levying a sales or value-added tax
requires, because tax is owed on the consideration and a seller's own
price reduction reduces it. On the invoice it is a LINE, negative, so the
invoice total stays the sum of its own lines and the customer can see
what came off instead of finding it welded into a number they cannot
check. The terms are STAMPED on the redemption row at the moment of use
(doctrine #1), so a promotion edited next month never restates what
somebody already got, and the arithmetic and the refusals both live in
object_promotions -- pure, tested without a data directory, the same
posture object_rates keeps for rate cards.

`gift_card_code` reduces what is OWED. A gift card is money the customer
already gave this shop, so it changes no price and no taxable base: the
order's total_cents stays the value of the sale, and the card is applied
after tax as `amount_due_cents`. Treating it as a discount would let a
shop sell $100 of goods and remit tax on $60. The debit is written
against the wallet through the EXISTING hook_wallet_entries gate, ASKED
IN PROCESS -- a trusted server-side write bypasses hooks by design, which
is exactly why the gate has to be consulted explicitly rather than
assumed -- so a card cannot overdraw, and the refusal a customer sees is
the same sentence that gate gives everybody else. The matching payment is
an ordinary payments row, because tender is a payment and this shop
already knows what one of those is.

**A preview writes NOTHING.** No redemption, no wallet debit, no payment,
no backorder. That is stated here because it is the one property of this
whole block that a refactor could quietly break: a preview exists so
somebody can see what they would owe, and a preview that burned a
single-use code or drained a gift card would be the shop charging people
for looking. The writes all live below the `preview` return, and the
`preview` return is above every one of them.

**Out of stock is now three answers, not one** -- see
object_cart.checkout_blockers. `refuse` (the default, and every product
that has never heard of the field) blocks exactly as it always has;
`allow` accepts the line, records a backorders row and marks the order
line so the warehouse does not send goods that are not there; `notify`
still refuses but writes down who wanted it. The default is what makes
this safe: a shop that has configured nothing behaves byte-for-byte as it
did before.

KNOWN GAP, stated rather than hidden: marking the ORDER LINE backordered
needs an order_lines column that app-orders does not declare yet, and
keeping a backordered line out of a shipment needs a condition in
app-shipping's action_create_shipment / system_order_fulfillment. Neither
package is this slice's to change. The stamp here is schema-aware (it
writes the field the day it exists and nothing before), the backorders
ROW carries the fact in the meantime, and tests/test_reorder.py holds
both halves as strict xfails -- the same thing tests/test_dropship.py did
for its two conditions, and for the same reason: a green test over a
shelf that silently ships is worse than a red one.
"""

import os
import secrets
from datetime import date, datetime, timedelta

import object_cart
import object_execution
import object_ids
import object_promotions
import object_records
import object_schemas
import object_stock
import python_object_runtime

ACTOR = "action_checkout"

DEFAULT_DUE_DAYS = 14


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _has_field(base, collection, field):
    """Does this collection's schema declare `field` yet?

    Asked rather than assumed because orders is due to gain a
    shipping_cents column and this file wants to fill it the day it
    appears, without a migration and without writing a field that does
    not exist into a shop that has not upgraded. Any failure to read the
    schema answers "no": a missing column costs an itemisation, a
    rejected write would cost the order.
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=base)
    except Exception:
        return False
    return any(f.get("name") == field for f in schema.get("fields") or [])


def _invoice_description(line):
    """The line as the payer should read it.

    A latte at 4.00 billed at 4.60 with nothing saying why is the kind of
    small unexplained difference that turns into a phone call, so a line
    carrying a delta says so in its own description. Only when there IS
    one: every existing invoice line reads exactly as it did.
    """
    description = _text(line.get("description"))
    modifier = int(line.get("modifier_cents") or 0)
    if not modifier:
        return description
    return f"{description} (+{modifier / 100:.2f})"


def _tax_and_shipping_settings(base):
    """The four numbers that decide what this sale costs beyond the goods.

    All default to 0/off, so a shop that has configured nothing charges
    nothing extra and its invoices look exactly as they did yesterday --
    absent configuration must read as "this shop does not do that", never
    as a broken tax line.
    """
    return {
        "tax_rate_bps": _int(_setting(base, "shop.tax_rate_bps", 0)),
        "tax_shipping": _truthy(_setting(base, "shop.tax_shipping", "")),
        "shipping_flat_cents": _int(_setting(base, "shop.shipping_flat_cents", 0)),
        "free_over_cents": _int(_setting(base, "shop.shipping_free_over_cents", 0)),
    }


def _pay_path_for_order(base, order_id):
    """The pay link an already-checked-out order was given, if it has one.

    Everything here is best-effort on purpose: a retry must hand back the
    SAME door rather than mint a second invoice, but an order whose invoice
    or token cannot be found is still a perfectly good order. The caller
    gets no link instead of an error.
    """
    try:
        order = object_records.get_collection_record("orders", order_id, base_dir=base)
        invoice_id = _text(order.get("invoice_id"))
        if not invoice_id:
            return ""
        invoice = object_records.get_collection_record("invoices", invoice_id,
                                                       base_dir=base)
        token = _text(invoice.get("portal_token"))
    except Exception:
        return ""
    return f"/pay/{token}" if token else ""


def _track_path_for_order(base, order_id):
    """The tracking link an already-checked-out order was given.

    Same best-effort posture as _pay_path_for_order, and for the same
    reason: a shopper who double-clicked must get the SAME door back, and
    an order whose token cannot be read is still an order.
    """
    try:
        order = object_records.get_collection_record("orders", order_id, base_dir=base)
        token = _text(order.get("portal_token"))
    except Exception:
        return ""
    return f"/orders/track/{token}" if token else ""


def _products(base, product_ids):
    out = {}
    for product_id in product_ids:
        try:
            row = object_records.get_collection_record(
                "products", product_id, base_dir=base)
        except Exception:
            row = None
        if row:
            out[product_id] = row
    return out


def _on_hand(base, products):
    """Levels for the products this basket touches, and which of them the
    shop actually tracks.

    A service or a digital download has no stock level, and treating "no
    level recorded" as "none available" would refuse to sell the things
    that are always available.
    """
    # products.product_type: physical | digital | service | subscription |
    # asset. Only the first and last are things that sit on a shelf and
    # can run out; a download or an hour of work never does.
    tracked = {pid for pid, row in products.items()
               if _text(row.get("product_type")) in ("physical", "asset", "")}
    levels = {}
    for product_id in tracked:
        try:
            levels[product_id] = object_stock.total_quantity(
                product_id, base_dir=base)
        except Exception:
            continue
    return levels, tracked


# --- the collection-time gate ------------------------------------------------
#
# Everything from here to POST is inert for a shipping order: _pickup_gate
# returns before it reads anything at all unless the request names a
# method that is not `shipping`.

FULFILLMENT_METHODS = ("shipping", "pickup", "delivery", "counter")

DEFAULT_LEAD_MINUTES = 30


def _moment(value):
    """An ISO datetime as naive local wall-clock, or None.

    Same normalisation app-pickup's own objects perform, and for the same
    reason: every time in this repo is the shop's own clock, and comparing
    one aware value against a naive one raises -- which would turn a
    single oddly-typed slot into a checkout that refuses everybody.
    """
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _slot_has_room(slot):
    return _int(slot.get("orders_taken"), 0) < _int(slot.get("capacity"), 0)


def _next_free_slot(slots, *, earliest, location, exclude_id=""):
    """The soonest window somebody could actually be given instead.

    Searched at the SAME location as the one they asked for, because
    offering a customer a time at the other branch is not an offer. Ties
    are broken by start time and nothing else -- the next free slot is
    simply the next one.
    """
    candidates = [slot for slot in slots
                  if _text(slot.get("id")) != exclude_id
                  and _truthy(slot.get("is_open") or "true")
                  and _text(slot.get("location_id")) == location
                  and _slot_has_room(slot)]
    dated = [(moment, slot) for moment, slot in
             ((_moment(slot.get("starts_at")), slot) for slot in candidates)
             if moment is not None and moment >= earliest]
    if not dated:
        return None
    dated.sort(key=lambda pair: pair[0])
    return dated[0][1]


def _and_the_next_one(slot):
    """The half of a refusal that keeps the sale.

    "That time is full" sends somebody away. "That time is full, the next
    one is 18:30" is the same fact with somewhere to go, and it costs one
    sentence.
    """
    if slot is None:
        return (" There is no other free collection time on the board -- "
                "contact the shop.")
    return f" The next free collection time is {_text(slot.get('starts_at'))}."


def _pickup_gate(base, request):
    """Everything the collection time can be wrong about, gathered at once.

    Returns {method, slot, problems, next_free}. `problems` is a list of
    sentences, and it is a LIST rather than a first-failure return for
    exactly the reason the price/stock checks above are: the caller folds
    it in with theirs and reports the lot together.

    A blank or `shipping` method returns immediately, having read nothing:
    a shop that does not do pickup must not pay a collection scan per
    checkout, and more importantly must not be able to acquire a pickup
    refusal by accident.
    """
    method = _text(request.get("fulfillment_method")).lower()
    empty = {"method": "", "slot": None, "problems": [], "next_free": None}
    if not method or method == "shipping":
        return empty

    if method not in FULFILLMENT_METHODS:
        return {**empty, "method": method,
                "problems": [f"'{method}' is not a way this shop can fulfil an "
                             f"order. Choose one of: "
                             f"{', '.join(FULFILLMENT_METHODS)}."]}

    if method != "pickup":
        # delivery and counter are stated facts about how this order is
        # fulfilled and book nothing: there is no slot board for the van or
        # the till, and inventing one here would be a rule nobody asked for.
        return {**empty, "method": method}

    slot_id = _text(request.get("pickup_slot_id"))
    now = _moment(request.get("now")) or datetime.now()
    lead = max(0, _int(_setting(base, "pickup.lead_minutes", ""),
                       DEFAULT_LEAD_MINUTES))
    earliest = now + timedelta(minutes=lead)

    try:
        slots = object_records.read_collection_records("pickup_slots",
                                                       base_dir=base)
    except Exception:
        # The pickup app is not installed on this box. Refusing is the only
        # honest answer -- the shopper asked for a collection time and this
        # shop has no board of them -- and it is a refusal rather than a
        # silent downgrade to shipping, because quietly posting somebody
        # their lunch is worse than telling them no.
        return {**empty, "method": method,
                "problems": ["This shop is not taking collection orders "
                             "(no pickup slots are configured)."]}

    if not slot_id:
        free = _next_free_slot(slots, earliest=earliest, location="")
        return {**empty, "method": method, "next_free": free,
                "problems": ["Choose a collection time." + _and_the_next_one(free)]}

    slot = next((row for row in slots if _text(row.get("id")) == slot_id), None)
    if slot is None:
        free = _next_free_slot(slots, earliest=earliest, location="")
        return {**empty, "method": method, "next_free": free,
                "problems": ["That collection time is not on the board any "
                             "more." + _and_the_next_one(free)]}

    location = _text(slot.get("location_id"))
    problems = []
    starts = _moment(slot.get("starts_at"))

    if not _truthy(slot.get("is_open") or "true"):
        problems.append("The shop is not taking orders for that collection "
                        "time.")
    if starts is None:
        problems.append("That collection time cannot be read.")
    elif starts <= now:
        problems.append(f"That collection time ({_text(slot.get('starts_at'))}) "
                        f"has already passed.")
    elif starts < earliest:
        # The one refusal that should be impossible to reach through a
        # picker, and is checked anyway: action_pickup_slots never OFFERS a
        # slot inside the lead time, so arriving here means the id was
        # typed, kept from an old page, or guessed.
        problems.append(f"That collection time is too soon -- this shop needs "
                        f"{lead} minutes' notice.")
    if not _slot_has_room(slot):
        problems.append(f"That collection time is full: it takes "
                        f"{_int(slot.get('capacity'), 0)} orders and "
                        f"{_int(slot.get('orders_taken'), 0)} have been taken.")

    free = None
    if problems:
        free = _next_free_slot(slots, earliest=earliest, location=location,
                               exclude_id=slot_id)
        problems[-1] += _and_the_next_one(free)
    return {"method": method, "slot": slot, "problems": problems,
            "next_free": free}


# --- the money gates ---------------------------------------------------------
#
# Both are inert unless the request names a code, for the same reason
# _pickup_gate is inert for a shipping order: a shop that runs no
# promotions and sells no gift cards must not pay a collection scan per
# checkout, and must not be able to acquire a refusal by accident.


def _rows(base, collection):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _call(object_id, payload, *, method="POST"):
    """Run another installed object in process -- the same mechanism
    system_shop_fulfillment uses to reach app-shipping, and
    action_resolve_dispute uses to reach hook_refunds.

    Returns (result, error). A missing object (the app is simply not
    installed) is an error string, never an exception.
    """
    try:
        runtime = python_object_runtime.PythonObjectRuntime()
        outcome = object_execution.execute_object(
            runtime,
            object_execution.ObjectExecutionRequest(
                object_id, method=method, payload=payload))
    except Exception as exc:
        return None, str(exc)[:200]
    if not outcome.ok:
        message = getattr(outcome.error, "message", "") if outcome.error else ""
        return None, _text(message)[:200] or f"{object_id} failed"
    return outcome.result, ""


def _promotion_gate(base, request, subtotal_cents, postage_cents, today):
    """Everything a discount code can be wrong about, gathered at once.

    Returns {code, promo, problems, discount_cents, applies_to}. `problems`
    is a LIST, from object_promotions.blockers, and it is folded in with
    the price, stock and collection-time problems below rather than
    returned on its own -- a shopper told their code expired, who fixes
    that and is then told the basket is too small, has left.

    No code returns immediately having read nothing.
    """
    empty = {"code": "", "promo": None, "problems": [], "discount_cents": 0,
             "applies_to": "goods"}
    code = object_promotions.normalize_code(request.get("promo_code"))
    if not code:
        return empty

    promotions = _rows(base, "promotions")
    if promotions is None:
        # No promotions app on this box. An honest refusal beats silently
        # ignoring a code the shopper believes they are getting.
        return {**empty, "code": code,
                "problems": [f"There is no promotion with the code '{code}'."]}
    redemptions = _rows(base, "promotion_redemptions")
    if redemptions is None:
        # The limits cannot be counted, and a limit that cannot be counted
        # has not been met. Refusing is the safe direction: a code honoured
        # once too often is money, a code refused once is an email.
        return {**empty, "code": code,
                "problems": ["That code cannot be checked right now (the "
                             "redemption log is unreadable)."]}

    promo = object_promotions.resolve(code, promotions, on_date=today)
    problems = object_promotions.blockers(
        promo, subtotal_cents, _text(request.get("customer_email")),
        redemptions, on_date=today, code=code)
    if problems:
        return {**empty, "code": code, "promo": promo, "problems": problems}
    return {"code": code, "promo": promo, "problems": [],
            "discount_cents": object_promotions.discount_for(
                subtotal_cents, postage_cents, promo),
            "applies_to": object_promotions.applies_to(promo)}


def _gift_card_gate(base, request):
    """The card, its balance, and every reason it cannot be spent.

    Returns {code, wallet, problems, available_cents}. The balance is
    SUMMED FROM THE ENTRIES rather than read off wallets.balance_minor,
    for the reason hook_wallet_entries states about its own gate: that
    number is a rollup, and a decision that leans on a derived value it
    could recompute is a decision that authorises a debit against money
    which does not exist the moment the rollup goes stale.

    This only decides how much to OFFER. The authoritative refusal comes
    from hook_wallet_entries at the moment of the debit, which is asked
    in process below -- there is exactly one gate on this ledger and it
    is not this function.
    """
    empty = {"code": "", "wallet": None, "problems": [], "available_cents": 0}
    code = _text(request.get("gift_card_code")).upper()
    if not code:
        return empty

    wallets = _rows(base, "wallets")
    if wallets is None:
        return {**empty, "code": code,
                "problems": [f"There is no gift card with the code '{code}' "
                             f"(this shop has no wallets)."]}
    wallet = next((row for row in wallets
                   if _text(row.get("code")).upper() == code), None)
    if wallet is None:
        return {**empty, "code": code,
                "problems": [f"There is no gift card with the code '{code}'."]}

    problems = []
    if not _truthy(wallet.get("is_active") or "true"):
        problems.append(f"The gift card {code} is not active.")

    entries = _rows(base, "wallet_entries")
    if entries is None:
        return {**empty, "code": code, "wallet": wallet,
                "problems": problems + [
                    f"The balance of gift card {code} cannot be read, so it "
                    f"cannot be spent right now."]}
    balance = sum(_int(row.get("amount_minor"))
                  for row in entries
                  if _text(row.get("wallet_id")) == _text(wallet.get("id")))
    if balance <= 0:
        problems.append(f"The gift card {code} has nothing left on it.")

    return {"code": code, "wallet": wallet, "problems": problems,
            "available_cents": max(0, balance)}


def _spend_the_card(base, wallet, amount_cents, *, order_id, invoice_id,
                    today, owner_id):
    """Debit the wallet and record the tender. Returns (applied, error).

    **THE GATE IS ASKED, NOT ASSUMED.** hook_wallet_entries is the one
    authority on whether a wallet can afford a debit -- it sums the
    entries rather than trusting the balance rollup, and it documents its
    own check-then-append race honestly. A trusted server-side write like
    this one BYPASSES hooks by design, so the only way to be under that
    gate is to ask it in process and surface its refusal verbatim, which
    is exactly what action_resolve_dispute does with hook_refunds. A
    second opinion written here would be a second ceiling to get wrong,
    and would quote a customer a different answer depending on which door
    their spend came through.

    Fails CLOSED: a gate that cannot be consulted has not said yes.

    **The order of the two writes, and the compensation.** The debit is
    taken first and the payment recorded second, because a payment
    against an untouched card is the shop giving away goods. If the
    payment cannot be written the debit is REVERSED by a compensating
    credit -- the ledger's own correction mechanism, never an edit or a
    delete, exactly as refunds correct payments and reversing journals
    correct postings. The customer's balance ends where it started and
    the checkout says what happened.
    """
    wallet_id = _text(wallet.get("id"))
    entry = {
        "id": object_ids.new_uuid4(),
        "wallet_id": wallet_id,
        # Negative: money leaving. The sign convention is the ledger's.
        "amount_minor": str(-int(amount_cents)),
        "kind": "debit",
        "description": f"Spent at checkout on order {order_id[:8].upper()}",
        "reference": f"orders/{order_id}",
        # Provenance, so a replayed checkout finds its own marker instead
        # of charging the card twice (docs/logic-decisions.md #7).
        "generated_from": f"checkout/{order_id}",
        "owner_id": _text(wallet.get("owner_id")) or owner_id,
    }

    verdict, error = _call("hook_wallet_entries",
                           {"action": "create", "collection": "wallet_entries",
                            "record": entry},
                           method="BEFORE_WRITE")
    if error:
        return 0, (f"the wallet gate could not be consulted ({error}), so the "
                   f"gift card was not charged")
    if isinstance(verdict, dict) and verdict.get("error"):
        return 0, _text(verdict.get("error"))

    try:
        object_records.create_collection_record(
            "wallet_entries", entry, base_dir=base, actor=ACTOR)
    except Exception as exc:
        return 0, f"the gift card could not be charged: {str(exc)[:120]}"

    try:
        object_records.create_collection_record(
            "payments",
            {
                "id": object_ids.new_uuid4(),
                "invoice_id": invoice_id,
                "amount_cents": str(int(amount_cents)),
                # `other` because payments.method has no wallet entry and
                # app-payments' enum is not this slice's to widen. The
                # reference names the wallet, so what this was is findable
                # rather than merely labelled.
                "method": "other",
                "received_on": today,
                "reference": f"wallets/{wallet_id}",
                "status": "received",
                "notes": (f"Gift card {_text(wallet.get('code'))} redeemed by "
                          f"{ACTOR} [orders/{order_id}]"),
                "owner_id": owner_id,
            },
            base_dir=base, actor=ACTOR)
    except Exception as exc:
        # Put the money back. A compensating entry, never an edit: the
        # ledger is append-only and a mistaken charge is corrected by a
        # record that says so.
        try:
            object_records.create_collection_record(
                "wallet_entries",
                {"id": object_ids.new_uuid4(),
                 "wallet_id": wallet_id,
                 "amount_minor": str(int(amount_cents)),
                 "kind": "adjustment",
                 "description": ("Reversed: the payment against this order "
                                 "could not be recorded"),
                 "reference": f"orders/{order_id}",
                 "generated_from": f"checkout_reversal/{order_id}",
                 "owner_id": _text(wallet.get("owner_id")) or owner_id},
                base_dir=base, actor=ACTOR)
        except Exception:
            return int(amount_cents), (
                f"the gift card was charged but the payment could not be "
                f"recorded ({str(exc)[:100]}) AND the charge could not be "
                f"reversed -- this wallet is short and needs a human")
        return 0, (f"the payment could not be recorded "
                   f"({str(exc)[:120]}); the gift card was charged and the "
                   f"charge has been reversed")

    return int(amount_cents), ""


def _record_backorders(base, lines, order_id, line_ids, email, today, cart):
    """A row per line the shop agreed to owe. Returns what it wrote.

    Best-effort by construction: the sale has already happened by the time
    this runs, and a backorders collection that is absent (a shop still on
    app-catalog 0.6) must cost the warehouse a queue entry rather than
    cost the shopper their order.
    """
    if not lines:
        return []
    written = []
    for line in lines:
        row_id = object_ids.new_uuid4()
        try:
            object_records.create_collection_record(
                "backorders",
                {"id": row_id,
                 "product_id": line["product_id"],
                 "kind": "backorder",
                 "order_id": order_id,
                 "order_line_id": line_ids.get(line["cart_item_id"], ""),
                 # What was SHORT, not what was ordered: the rest of the
                 # line is ordinary stock that goes out today.
                 "quantity": _text(line.get("short_by")),
                 "customer_email": email,
                 "status": "open",
                 "requested_on": today,
                 "owner_id": _text(cart.get("owner_id"))},
                base_dir=base, actor=ACTOR)
        except Exception:
            continue
        written.append({"backorder_id": row_id,
                        "product_id": line["product_id"],
                        "description": _text(line.get("description")),
                        "short_by": _text(line.get("short_by"))})
    return written


def _record_notify_interest(base, lines, products, email, today, cart):
    """Who wanted what the shop just refused to sell them.

    Written on the REFUSAL path, which is the only moment this fact
    exists. Refusing while remembering beats refusing and forgetting,
    which is what the shop did before and what loses the same customer
    twice.

    Needs an address: a record of interest with no way to reach the
    interested party is a record of nothing, so a checkout with no email
    writes none and the response says so by simply not listing any.

    Idempotent on (product, email) while a row is still open, so a shopper
    who presses the button five times is one person waiting, not five.
    """
    if not lines or not email:
        return []
    existing = _rows(base, "backorders")
    if existing is None:
        return []
    already = {(_text(row.get("product_id")),
                _text(row.get("customer_email")).lower())
               for row in existing
               if _text(row.get("kind")) == "notify"
               and _text(row.get("status")) == "open"}

    written = []
    for line in lines:
        key = (line["product_id"], email.lower())
        if key in already:
            continue
        already.add(key)
        row_id = object_ids.new_uuid4()
        try:
            object_records.create_collection_record(
                "backorders",
                {"id": row_id,
                 "product_id": line["product_id"],
                 "kind": "notify",
                 # No order: nothing was sold, which is the whole
                 # distinction between this row and a backorder.
                 "order_id": "",
                 "quantity": _text(line.get("short_by")),
                 "customer_email": email,
                 "status": "open",
                 "requested_on": today,
                 "owner_id": _text(cart.get("owner_id"))},
                base_dir=base, actor=ACTOR)
        except Exception:
            continue
        written.append({"backorder_id": row_id,
                        "product_id": line["product_id"],
                        "description": _text(line.get("description"))})
    return written


def _slot_refusal(pickup):
    """The pickup half of a 409, shaped so a storefront can render the next
    free time as a button rather than parsing it back out of a sentence."""
    payload = {"pickup_problems": pickup["problems"]}
    free = pickup.get("next_free")
    if free is not None:
        payload["next_free_slot"] = {
            "id": free["id"],
            "starts_at": _text(free.get("starts_at")),
            "ends_at": _text(free.get("ends_at")),
            "places_left": max(0, _int(free.get("capacity"), 0)
                               - _int(free.get("orders_taken"), 0)),
        }
    return payload


def POST(request):
    base = _base_dir()
    token = _text(request.get("session_token"))
    if not token:
        return {"status": 400, "error": "session_token is required"}

    try:
        carts = object_records.read_collection_records("carts", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "shop not installed (carts absent)"}

    cart = next((c for c in carts if _text(c.get("session_token")) == token
                 and _text(c.get("status")) == "open"), None)
    if cart is None:
        settled = next((c for c in carts
                        if _text(c.get("session_token")) == token
                        and _text(c.get("checked_out_order_id"))), None)
        if settled:
            # One cart, one order, however many times a browser retries --
            # and the same pay link back each time, because a shopper who
            # double-clicked still needs the door, not a second bill.
            order_id = _text(settled["checked_out_order_id"])
            duplicate = {"ok": True, "duplicate": True, "order_id": order_id,
                         "note": "this basket has already been checked out"}
            pay_path = _pay_path_for_order(base, order_id)
            if pay_path:
                duplicate["pay_path"] = pay_path
            track_path = _track_path_for_order(base, order_id)
            if track_path:
                duplicate["track_path"] = track_path
            return duplicate
        return {"status": 404, "error": "No open basket for this session."}

    try:
        all_items = object_records.read_collection_records("cart_items", base_dir=base)
    except Exception:
        all_items = []
    items = [i for i in all_items if _text(i.get("cart_id")) == cart["id"]]

    products = _products(base, {_text(i.get("product_id")) for i in items})
    levels, tracked = _on_hand(base, products)
    blockers = object_cart.checkout_blockers(items, products, levels, tracked=tracked)
    # Gathered HERE, beside the price and stock checks, and reported with
    # them below -- never on its own and never first. For a shipping order
    # this reads nothing and finds nothing; see _pickup_gate.
    pickup = _pickup_gate(base, request)

    if blockers["empty"]:
        return {"status": 400, "error": "There is nothing in this basket.",
                **(_slot_refusal(pickup) if pickup["problems"] else {})}

    if blockers["price_changes"] and not _truthy(request.get("confirm_prices")):
        return {"status": 409,
                "error": "Some prices changed while this basket was open.",
                "price_changes": blockers["price_changes"],
                "note": "Show both numbers and send confirm_prices=true once "
                        "the shopper has agreed to the current ones.",
                **(_slot_refusal(pickup) if pickup["problems"] else {})}

    # Prices confirmed: adopt the live ones so the order records what the
    # shopper actually agreed to, not the number they first saw. Moved
    # ABOVE the remaining refusals because the money gates below have to
    # judge a discount code's minimum spend against the basket the shopper
    # is actually agreeing to -- a code checked against a stale subtotal is
    # a code that passes here and fails on the invoice.
    if blockers["price_changes"] and _truthy(request.get("confirm_prices")):
        for item in items:
            product = products.get(_text(item.get("product_id")))
            if product:
                item["unit_price_cents"] = str(product.get("price_cents") or 0)

    today = _text(request.get("today")) or date.today().isoformat()
    charges = _tax_and_shipping_settings(base)
    email = _text(request.get("customer_email"))
    # The sale before any code or card touches it. The promotion's minimum
    # spend is judged on GOODS -- the number a shopper can see on the
    # basket page and check for themselves -- and the free-postage
    # threshold on the same pre-discount subtotal, so "free over $50" means
    # what the banner says rather than what a code left behind.
    quoted = object_cart.checkout_totals(items, **charges)
    promo = _promotion_gate(base, request, quoted["subtotal_cents"],
                            quoted["shipping_cents"], today)
    card = _gift_card_gate(base, request)
    money_problems = promo["problems"] + card["problems"]

    if (blockers["inactive"] or blockers["unavailable"] or pickup["problems"]
            or money_problems):
        # One 409 carrying everything wrong with this checkout. The
        # sentence changes depending on what is actually broken, because
        # "some items cannot be ordered right now" over a perfectly good
        # basket whose only problem is a full 6pm slot is a message that
        # sends the shopper looking in the wrong place.
        #
        # The code and the card are refused HERE, beside the stock and the
        # slot, and never on their own: a shopper told their code expired,
        # who finds another and is then told an item is out of stock, is a
        # shopper who has left.
        items_wrong = bool(blockers["inactive"] or blockers["unavailable"])
        if items_wrong and pickup["problems"]:
            error = ("Some items cannot be ordered right now, and the "
                     "collection time is no longer available.")
        elif items_wrong:
            error = "Some items cannot be ordered right now."
        elif pickup["problems"]:
            error = " ".join(pickup["problems"])
        else:
            error = " ".join(money_problems)
        refusal = {"status": 409,
                   "error": error,
                   "unavailable": blockers["unavailable"],
                   "inactive": blockers["inactive"],
                   **(_slot_refusal(pickup) if pickup["problems"] else {})}
        if money_problems:
            # Both lists, separately, so a storefront can put each sentence
            # under the box it belongs to instead of next to the basket.
            refusal["money_problems"] = money_problems
            if promo["problems"]:
                refusal["promo_problems"] = promo["problems"]
            if card["problems"]:
                refusal["gift_card_problems"] = card["problems"]
        # Somebody wanted something the shop refused to sell them, and the
        # seller asked to be told about it (products.backorder_policy =
        # notify). Written on the REFUSAL path deliberately: this is the
        # only moment the interest exists, and forgetting it is how the
        # same customer is lost twice.
        #
        # Not on a PREVIEW, though. A preview is a look, and the rule that
        # a preview writes nothing has to hold on the refusal path too or
        # it does not hold at all -- a storefront that quotes the basket
        # before submitting it would otherwise file a waiting list entry
        # for everybody who glanced at an empty shelf.
        if not _truthy(request.get("preview")):
            recorded = _record_notify_interest(base, blockers["notify"],
                                               products, email, today, cart)
            if recorded:
                refusal["interest_recorded"] = recorded
        return refusal

    summary = object_cart.checkout_totals(
        items, **charges,
        # A price reduction: off the taxable base, before tax.
        discount_cents=promo["discount_cents"],
        discount_on=promo["applies_to"],
        # Tender: after tax, changing no price and no taxable base.
        credit_cents=card["available_cents"])

    if _truthy(request.get("preview")):
        # The whole breakdown, because a preview exists so somebody can
        # see what they are about to owe -- a subtotal alone would hide
        # exactly the two numbers this preview was extended to show.
        #
        # AND IT WRITES NOTHING. No redemption row, no wallet debit, no
        # payment, no backorder. A preview that burned a single-use code
        # or drained a gift card would be a shop charging people for
        # looking, and every write in this object is below this return
        # precisely so that stays true.
        quote = {"ok": True, "preview": True, "cart_id": cart["id"],
                 "subtotal_cents": summary["subtotal_cents"],
                 "shipping_cents": summary["shipping_cents"],
                 "shipping_free": summary["shipping_free"],
                 "discount_cents": summary["discount_cents"],
                 "tax_cents": summary["tax_cents"],
                 "total_cents": summary["total_cents"],
                 "credit_applied_cents": summary["credit_applied_cents"],
                 "amount_due_cents": summary["amount_due_cents"],
                 "lines": summary["lines"],
                 "price_changes": blockers["price_changes"]}
        if promo["code"]:
            quote["promo_code"] = promo["code"]
        if card["code"]:
            quote["gift_card_code"] = card["code"]
        if blockers["backordered"]:
            quote["backordered"] = blockers["backordered"]
        return quote

    if not email:
        return {"status": 400,
                "error": "An email address is needed to send the receipt to."}

    order_id = object_ids.new_uuid4()
    # The order's own capability token, minted HERE and not left to
    # system_order_portal_link, for exactly the reason the invoice's token
    # below is minted here: that handler is a reaction -- post-commit and
    # best-effort -- which is right for an order somebody will email about
    # later and useless to the shopper standing at the counter now. THIS
    # response is what has to carry the door, and the confirmation email
    # composed off the very next write needs a token that already exists.
    # The handler only mints when a token is absent, so one that already
    # exists is something it skips rather than fights over.
    track_token = secrets.token_urlsafe(32)
    order_row = {
        "id": order_id,
        "doc_type": "sale",
        "number": f"WEB-{order_id[:8].upper()}",
        "customer_name": _text(request.get("customer_name")) or email,
        "customer_email": email,
        "currency": _text(cart.get("currency")) or "USD",
        "status": "draft",
        "order_date": today,
        # Subtotal is goods, per the schema's own definition (the sum of
        # the order's lines). Postage is not a line of this order, so it
        # cannot be in its subtotal -- it is in the total, which is what
        # the buyer owes.
        "subtotal_cents": str(summary["subtotal_cents"]),
        "tax_cents": str(summary["tax_cents"]),
        "total_cents": str(summary["total_cents"]),
        "notes": f"Web checkout [carts/{cart['id']}]",
        "owner_id": _text(cart.get("owner_id")),
    }
    if summary["shipping_cents"] and _has_field(base, "orders", "shipping_cents"):
        order_row["shipping_cents"] = str(summary["shipping_cents"])

    # Same schema-aware posture: a shop still on orders v4 has no
    # portal_token column, and writing one would be rejected -- which
    # would cost the shopper the ORDER, not just the link. On such a shop
    # the response simply carries no track_path, which is honest.
    tracks = _has_field(base, "orders", "portal_token")
    if tracks:
        order_row["portal_token"] = track_token

    # The two merchandising fields, on the order the moment orders declares
    # them -- see the module docstring. Same schema-aware posture as
    # shipping_cents above: a missing column costs the packer a note (which
    # the cart still holds and this response still reports), a rejected
    # write would cost the shopper their order.
    notes = {"customer_note": _text(request.get("customer_note")),
             "gift_message": _text(request.get("gift_message"))}
    carried_on_cart = []
    for field, value in notes.items():
        if not value:
            continue
        if _has_field(base, "orders", field):
            order_row[field] = value
        else:
            carried_on_cart.append(field)

    # How this order reaches the customer, and when we said it would be
    # ready. Same schema-aware posture as everything above: a shop still on
    # orders v6 has none of these columns, and writing one would cost the
    # shopper the ORDER rather than the timestamp.
    #
    # requested_at and promised_at are stamped SEPARATELY and deliberately
    # from different places. The promise is the slot's start -- the moment
    # the shop committed to, and the moment the customer will walk in. The
    # request is what they ASKED for, which is the same thing when they
    # simply picked a slot and is genuinely different when a storefront
    # passes the time they originally wanted and could not have. Collapsing
    # them would erase every "you wanted 6:00, we said 6:20", which is the
    # only evidence a shop ever gets that its 6pm is too small.
    slot = pickup["slot"]
    if pickup["method"] and _has_field(base, "orders", "fulfillment_method"):
        order_row["fulfillment_method"] = pickup["method"]
    if slot is not None:
        promised = _text(slot.get("starts_at"))
        requested = _text(request.get("requested_at")) or promised
        if _has_field(base, "orders", "promised_at"):
            order_row["promised_at"] = promised
        if _has_field(base, "orders", "requested_at"):
            order_row["requested_at"] = requested
        # Which window this order took, in the one place this repo puts
        # provenance that has no column of its own -- the same marker
        # pattern the invoice below and app-billing's runner both use.
        # orders needs no pickup_slot_id field for it: promised_at already
        # answers "when", and a second copy of the link would be one more
        # thing that can disagree with the slot's own count.
        order_row["notes"] = (f"{order_row['notes']} "
                              f"[pickup_slots/{slot['id']}]")

    # portal_token is schema read_only so no client can choose its own; a
    # server-side writer passes preserve_read_only, which is exactly the
    # narrow escape hatch that flag exists for -- the same call the
    # invoice create below already makes.
    object_records.create_collection_record(
        "orders", order_row, base_dir=base, actor=ACTOR,
        preserve_read_only=True)

    # The per-line instruction and its delta, carried straight through --
    # see the module docstring. Same schema-aware posture as everything
    # else here: an app-orders that predates the columns costs the cook a
    # note, a rejected write would cost the shopper the order.
    line_extras = [field for field in ("line_note", "modifier_cents")
                   if _has_field(base, "order_lines", field)]
    # Which lines the shop agreed to owe rather than to hand over today.
    # The MARK belongs on the order line, because the picker and the packer
    # read the order and a fact that lives only on a basket is a fact the
    # warehouse never sees -- but order_lines does not declare the column
    # yet (app-orders is not this slice's to change), so the same
    # schema-aware posture every other field here uses applies: it is
    # written the day it exists, and until then the backorders ROW below
    # carries it. See the module docstring's KNOWN GAP.
    backordered_items = {row["cart_item_id"]: row
                         for row in blockers["backordered"]}
    marks_backorders = _has_field(base, "order_lines", "backordered")
    line_ids = {}
    for line in summary["lines"]:
        order_line = {
            "id": object_ids.new_uuid4(),
            "order_id": order_id,
            "product_id": line["product_id"],
            "description": line["description"],
            "quantity": line["quantity"],
            "unit_price_cents": str(line["unit_price_cents"]),
            "line_total_cents": str(line["line_total_cents"]),
            "owner_id": _text(cart.get("owner_id")),
        }
        if "line_note" in line_extras:
            order_line["line_note"] = _text(line.get("line_note"))
        if "modifier_cents" in line_extras:
            order_line["modifier_cents"] = str(line.get("modifier_cents") or 0)
        if marks_backorders and line["cart_item_id"] in backordered_items:
            order_line["backordered"] = "true"
        object_records.create_collection_record(
            "order_lines", order_line, base_dir=base, actor=ACTOR)
        line_ids[line["cart_item_id"]] = order_line["id"]

    # The backorders themselves, once the order exists to point at. Written
    # here rather than beside the blockers because a row naming an order id
    # that was never created would be a queue full of ghosts.
    backordered = _record_backorders(base, blockers["backordered"], order_id,
                                     line_ids, email, today, cart)

    # The redemption, with the TERMS STAMPED ON IT. Written once the order
    # exists, because a redemption naming an order that was never created
    # is a used-up code against nothing. Wrapped, because a code that
    # cannot be recorded must not cost the shopper the order they just
    # placed -- the discount is already in the totals above, and a missing
    # redemption row is visible (and its absence undercounts, which is the
    # forgiving direction) where a lost order is not.
    redemption_id = ""
    redemption_error = ""
    if promo["promo"] and summary["discount_cents"]:
        redemption_id = object_ids.new_uuid4()
        try:
            object_records.create_collection_record(
                "promotion_redemptions",
                {
                    "id": redemption_id,
                    # Everything object_promotions.terms() hands back is
                    # the promotion AS IT WAS: kind, value, minimum and
                    # stacking. Editing the promotion next month therefore
                    # changes what the next shopper gets and restates
                    # nothing anybody has already been given (doctrine #1).
                    **object_promotions.terms(promo["promo"]),
                    "order_id": order_id,
                    "customer_email": email,
                    # The money that actually came off, recorded rather
                    # than rederivable: a number recomputed later from
                    # terms that have since moved is exactly the drift the
                    # stamp exists to prevent.
                    "discount_cents": str(summary["discount_cents"]),
                    "redeemed_at": datetime.now().isoformat(timespec="seconds"),
                    "owner_id": _text(cart.get("owner_id")),
                },
                base_dir=base, actor=ACTOR)
        except Exception as exc:
            redemption_id = ""
            redemption_error = str(exc)[:200]

    # Stamp the cart BEFORE anything else can retry: the stamp is what
    # makes a second click a no-op rather than a second order.
    stamp = {"status": "checked_out", "checked_out_order_id": order_id,
             "customer_email": email,
             "customer_name": _text(request.get("customer_name"))}
    # Always on the cart, whether or not orders took them: this is where
    # the shopper typed them, and it is the only copy that survives a shop
    # whose orders schema has not caught up yet.
    stamp.update({field: value for field, value in notes.items() if value})
    object_records.update_collection_record(
        "carts", cart["id"], stamp, base_dir=base, actor=ACTOR)

    # The window's own count, incremented once the order exists. Read,
    # add one, write -- with no lock, because there is none to take here,
    # and this is the exact point at which two shoppers racing for the
    # last place both win: each read 3 of 4 and each writes 4, or worse,
    # each writes 5. That is the documented, accepted race (see the module
    # docstring), and the write is deliberately NOT clamped to capacity:
    # clamping would make an oversell invisible, and the whole reason this
    # number is stored on the row is so a shop can SEE that it happened
    # and ring somebody. Wrapped, because a slot that cannot be counted
    # must never cost a shopper the order they have already placed.
    if slot is not None:
        try:
            current = object_records.get_collection_record(
                "pickup_slots", slot["id"], base_dir=base)
            object_records.update_collection_record(
                "pickup_slots", slot["id"],
                {"orders_taken": str(_int(current.get("orders_taken"), 0) + 1)},
                base_dir=base, actor=ACTOR)
        except Exception:
            pass

    # From here on the sale is already made. Everything below is wrapped so
    # that an invoicing problem costs the shopper a pay link, not the order
    # they just placed -- see the module docstring.
    invoice_id = ""
    pay_path = ""
    invoice_error = ""
    try:
        invoice_id = object_ids.new_uuid4()
        due_days = _int(_setting(base, "billing.invoice_due_days", DEFAULT_DUE_DAYS),
                        DEFAULT_DUE_DAYS)
        # The token is minted HERE rather than left to
        # system_invoice_portal_link. That handler is a reaction --
        # post-commit and best-effort -- which is right for an invoice that
        # will be emailed, and useless to a shopper standing at the counter
        # now: THIS response is what has to carry the door. The handler only
        # mints when a token is absent, so a token that already exists is
        # something it tolerates rather than fights over.
        token = secrets.token_urlsafe(24)
        object_records.create_collection_record(
            "invoices",
            {
                "id": invoice_id,
                # Same number as the order: one human reference for the
                # whole sale, so "WEB-1A2B3C4D" means one thing to the
                # shopper, the packer and the bookkeeper alike.
                "number": f"WEB-{order_id[:8].upper()}",
                "customer_name": _text(request.get("customer_name")) or email,
                "customer_email": email,
                "currency": _text(cart.get("currency")) or "USD",
                "status": "sent",
                "issue_date": today,
                "due_date": (date.fromisoformat(today)
                             + timedelta(days=due_days)).isoformat(),
                # The invoice's subtotal IS the sum of its own lines --
                # goods plus the postage line below -- and its total is
                # that plus tax. Exactly the arithmetic the schema
                # describes, so invoice_totals restating it one day finds
                # the same numbers rather than an argument.
                "subtotal_cents": str(summary["subtotal_cents"]
                                      + summary["shipping_cents"]
                                      - summary["discount_cents"]),
                "tax_cents": str(summary["tax_cents"]),
                "total_cents": str(summary["total_cents"]),
                # Provenance in notes, the house pattern: invoices carry no
                # generated_from column, so the marker goes where it can be
                # found again by anyone asking where this bill came from.
                "notes": f"Generated by {ACTOR} [orders/{order_id}]",
                "source_order_id": order_id,
                "portal_token": token,
                "owner_id": _text(cart.get("owner_id")),
            },
            # portal_token is schema read_only so no client can choose its
            # own; a server-side writer passes preserve_read_only, which is
            # exactly the narrow escape hatch that flag exists for.
            base_dir=base, actor=ACTOR, preserve_read_only=True)

        for line in summary["lines"]:
            # The invoice line's unit price is the EFFECTIVE one -- the
            # catalogue price plus the line's modifier -- so the invoice
            # stays an ordinary quantity x price document that adds up
            # when anybody, or invoice_totals, multiplies it out. Carrying
            # the delta as a separate column here instead would mean the
            # one restatement this file already documents (invoice_totals
            # recomputing a line) would quietly drop it, and the customer
            # would be billed less than the shop is about to make. The
            # NOTE is not on the invoice at all: it is an instruction to
            # the kitchen, not a term of the sale.
            modifier = int(line.get("modifier_cents") or 0)
            object_records.create_collection_record(
                "invoice_lines",
                {
                    "id": object_ids.new_uuid4(),
                    "invoice_id": invoice_id,
                    "description": _invoice_description(line),
                    "quantity": line["quantity"],
                    "unit_price_cents": str(line["unit_price_cents"] + modifier),
                    "line_total_cents": str(line["line_total_cents"]),
                    "owner_id": _text(cart.get("owner_id")),
                },
                base_dir=base, actor=ACTOR)

        if summary["shipping_cents"]:
            # Postage as its own line, only when there is postage to
            # charge: a shop that does not post things must not grow a
            # "Shipping 0.00" row on every bill.
            shipping_line = {
                "id": object_ids.new_uuid4(),
                "invoice_id": invoice_id,
                "description": "Shipping",
                "quantity": "1",
                "unit_price_cents": str(summary["shipping_cents"]),
                "line_total_cents": str(summary["shipping_cents"]),
                "owner_id": _text(cart.get("owner_id")),
            }
            if charges["tax_shipping"] and charges["tax_rate_bps"]:
                # Where postage is taxable, the line says so and carries
                # its own share -- somebody auditing "was delivery taxed?"
                # should find the answer on the line, not have to
                # reverse-engineer it out of the invoice total.
                shipping_line["tax_rate_bps"] = str(charges["tax_rate_bps"])
                shipping_line["line_tax_cents"] = str(object_cart.tax_cents(
                    summary["shipping_cents"], charges["tax_rate_bps"]))
            object_records.create_collection_record(
                "invoice_lines", shipping_line, base_dir=base, actor=ACTOR)

        if summary["discount_cents"]:
            # THE DISCOUNT IS A LINE, and it is negative. Not a field
            # beside the total, and not silently baked into the goods
            # prices: the invoice's subtotal stays the sum of its own
            # lines, so anybody -- a customer, an accountant,
            # invoice_totals recomputing it one day -- adds the rows up
            # and gets the number printed at the bottom. Baking it into
            # the unit prices instead would leave a bill nobody can
            # reconcile against the code the shopper was given, and a
            # field beside the total would be money with two homes.
            #
            # It names the CODE, because "Discount -6.00" invites the
            # phone call that "Discount (SPRING20) -6.00" answers.
            object_records.create_collection_record(
                "invoice_lines",
                {
                    "id": object_ids.new_uuid4(),
                    "invoice_id": invoice_id,
                    "description": (f"Discount ({promo['code']})"
                                    if promo["code"] else "Discount"),
                    "quantity": "1",
                    "unit_price_cents": str(-summary["discount_cents"]),
                    "line_total_cents": str(-summary["discount_cents"]),
                    "owner_id": _text(cart.get("owner_id")),
                },
                base_dir=base, actor=ACTOR)

        # The stamp is the join system_shop_fulfillment walks backwards:
        # a payment knows its invoice, and this is the only thing that says
        # which order that invoice was for.
        object_records.update_collection_record(
            "orders", order_id, {"invoice_id": invoice_id},
            base_dir=base, actor=ACTOR)
        pay_path = f"/pay/{token}"
    except Exception as exc:
        invoice_error = str(exc)[:200]

    # The card is spent LAST, and only against a bill that exists. Tender
    # applied to nothing is money taken off a customer for an invoice
    # nobody raised, so an invoicing failure above means the balance stays
    # where it is and the shopper is told why.
    card_applied = 0
    card_error = ""
    if card["wallet"] and summary["credit_applied_cents"] and invoice_id:
        card_applied, card_error = _spend_the_card(
            base, card["wallet"], summary["credit_applied_cents"],
            order_id=order_id, invoice_id=invoice_id, today=today,
            owner_id=_text(cart.get("owner_id")))
    if card["wallet"] and summary["credit_applied_cents"] and not invoice_id:
        card_error = ("the invoice could not be raised, so the gift card was "
                      "not charged; its balance is untouched")
    if card_error and not card_applied:
        # The card did not pay, so the customer owes the whole bill. Say so
        # in the totals rather than only in a warning nobody parses.
        summary = {**summary, "credit_applied_cents": 0,
                   "amount_due_cents": summary["total_cents"]}

    # total_cents is the GRAND total: what the payer owes, including
    # postage and tax. A response whose "total" was the goods subtotal
    # would be the number a payment gets built from, and the shop would
    # quietly collect the wrong amount.
    result = {"ok": True, "order_id": order_id, "cart_id": cart["id"],
              "subtotal_cents": summary["subtotal_cents"],
              "shipping_cents": summary["shipping_cents"],
              "shipping_free": summary["shipping_free"],
              "discount_cents": summary["discount_cents"],
              "tax_cents": summary["tax_cents"],
              "total_cents": summary["total_cents"],
              "credit_applied_cents": summary["credit_applied_cents"],
              "amount_due_cents": summary["amount_due_cents"],
              "lines": len(summary["lines"]),
              "status_of_order": "draft",
              "note": "order raised; stock moves and the order confirms when "
                      "payment arrives"}
    if promo["code"]:
        result["promo_code"] = promo["code"]
        if redemption_id:
            result["redemption_id"] = redemption_id
        if redemption_error:
            # Reported, never swallowed: the discount was given and the
            # count of it did not land, so a limit is now one use looser
            # than the shop thinks and somebody has to know.
            result["redemption_error"] = redemption_error
    if card["code"]:
        result["gift_card_code"] = card["code"]
        result["gift_card_applied_cents"] = card_applied
        if card_error:
            result["gift_card_error"] = card_error
    if backordered:
        # Named in the response because the shopper has just bought
        # something that is not in the building, and a confirmation that
        # did not say so is how a parcel becomes a complaint.
        result["backordered"] = backordered
        result["note"] = ("order raised; some lines are backordered and ship "
                          "when stock arrives. Stock moves and the order "
                          "confirms when payment arrives")
    if pickup["method"]:
        # Only on an order that actually chose a method. A shipping
        # checkout's response is exactly the dict it was before pickup
        # existed -- no empty "fulfillment_method": "shipping" key, no
        # blank promised_at -- because a caller that has never sent these
        # arguments must not have to learn them.
        result["fulfillment_method"] = pickup["method"]
        if slot is not None:
            result["pickup_slot_id"] = slot["id"]
            result["promised_at"] = _text(slot.get("starts_at"))
    if tracks:
        # The customer's own door, alongside the pay link and for the same
        # reason: this shop sells to guests, so the response and the
        # confirmation email are the only two places the buyer will ever
        # be handed a way back to their own order.
        result["track_path"] = f"/orders/track/{track_token}"
    if carried_on_cart:
        # Reported, not swallowed. Somebody has to know that what the
        # shopper typed did not reach the order the packer will read.
        result["notes_on_cart_only"] = carried_on_cart
    if invoice_error:
        # Say what went wrong rather than returning a cheerful ok: somebody
        # has to raise this bill, and they can only do that if the failure
        # was reported instead of swallowed.
        result["invoice_error"] = invoice_error
        result["note"] = ("order raised, but the invoice could not be: "
                          f"{invoice_error}. The order stands and the invoice "
                          "can be raised again.")
    else:
        result["invoice_id"] = invoice_id
        result["pay_path"] = pay_path
    return result
