"""system_gift_card_issue -- the customer paid for a gift card, so the
gift card now exists.

HANDLES payment writes. When money settles an invoice raised for an order
that contained a gift-card product, this opens ONE WALLET PER LINE with a
bearer code and credits it with what was paid for it.

**A gift card is not a new kind of thing.** It is a wallet with a code,
credited by an ordinary sale and debited at checkout through the gate
that already refuses to let anybody spend what is not there
(hook_wallet_entries). Everything a gift-card feature needs -- a balance
that cannot drift from its movements, a spend check that is authoritative,
a statement the holder can be shown -- app-billing already had, and
building a gift_cards table with its own balance column beside it would
be reconstructing the predecessor's drift bug in a corner nobody watches,
for money that arrives as a bearer instrument and is therefore the least
forgiving place to be wrong.

**Selling one is an ordinary product sale.** products.is_gift_card marks
the catalogue row; nothing else about the sale is special. It has a
price, it goes in a basket, it is invoiced and it is paid for, and the
FULFILMENT of it is this handler crediting a wallet rather than a picker
putting something in a box. The face value is the line total, so a
customer who buys three $25 cards gets three cards, and a shop that sells
a card at a discount credits what was actually charged rather than what
the card claims to be worth. (A shop that wants "pay $45, get $50" prices
the product at 45 and needs a face-value field; that is a decision for
whoever has that shop, and it is deliberately not invented here.)

**One wallet per LINE, not per order.** A gift card is a thing somebody
hands to somebody -- three cards on one order are three presents for
three people, and merging them into one balance makes two of those
presents impossible to give.

**It waits for the money.** Nothing is credited until the invoice is
SETTLED, folded from the payments themselves rather than read off the
invoice's own rollup, for the reason system_shop_fulfillment documents:
this handler runs ON the write that created the payment, so the invoice
has not yet been told about the money that triggered it, and trusting the
rollup would mean the first payment issues nothing and the second issues
everything.

Placement follows docs/logic-decisions.md #6 -- a REACTION, post-commit,
best-effort, never blocking the payment. A shop that cannot open a wallet
must still be able to take the money; the gap is visible and fixable,
whereas a refused payment is a lost sale.

**Idempotency by provenance** (#7), and it matters more here than almost
anywhere: a replayed payment event that issued a second card would be the
shop giving away money. Every credit stamps generated_from with
`gift_card/{order_line_id}`, and this handler looks for that marker
before it writes anything. At-least-once delivery therefore credits
exactly once, with no dedup table.
"""

import os
import secrets

import object_ids
import object_records

ACTOR = "system_gift_card_issue"

HANDLES = [
    "payments.record.created",
    "payments.record.updated",
]

MARKER = "gift_card/"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _rows(collection, base):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _payment_for(request, base):
    record = request.get("record")
    if isinstance(record, dict) and record.get("id"):
        return record
    payment_id = _text(request.get("record_id") or request.get("id"))
    if not payment_id:
        return None
    try:
        return object_records.get_collection_record("payments", payment_id,
                                                     base_dir=base)
    except Exception:
        return None


def _invoice_is_settled(base, invoice_id):
    """(True, "") when the bill is fully paid.

    The payments are summed HERE rather than read off the invoice's
    amount_paid rollup, for the reason in the module docstring: this runs
    on the write that created the payment, so the rollup has not seen it
    yet.
    """
    if not invoice_id:
        return False, "no invoice"
    try:
        invoice = object_records.get_collection_record("invoices", invoice_id,
                                                        base_dir=base)
    except Exception:
        return False, "invoice unreadable"
    total = _int(invoice.get("total_cents"))
    if _text(invoice.get("status")) == "paid":
        return True, ""
    if total <= 0:
        return True, ""

    paid = 0
    for row in _rows("payments", base) or []:
        if (_text(row.get("invoice_id")) == invoice_id
                and _text(row.get("status")) == "received"):
            paid += _int(row.get("amount_cents"))
    for row in _rows("refunds", base) or []:
        if _text(row.get("invoice_id")) == invoice_id:
            paid -= _int(row.get("amount_cents"))
    if paid >= total:
        return True, ""
    return False, f"invoice not settled: {paid} of {total} paid"


def _order_for_invoice(base, invoice_id):
    if not invoice_id:
        return None
    for order in _rows("orders", base) or []:
        if _text(order.get("invoice_id")) == invoice_id:
            return order
    return None


def _new_code(existing):
    """A bearer code nobody already holds.

    Long enough not to be guessed off a shop's own order numbers, and
    checked against the wallets that exist rather than trusted to be
    unique by arithmetic -- hook_wallets refuses a duplicate anyway, and a
    handler that produced one would fail the write instead of the card.
    """
    for _ in range(8):
        code = f"GC-{secrets.token_hex(6).upper()}"
        if code not in existing:
            return code
    return f"GC-{secrets.token_hex(10).upper()}"


def EVENT(request):
    base = _base_dir()
    payment = _payment_for(request, base)
    if not payment:
        return {"ok": True, "skipped": "no payment in the event"}
    if _text(payment.get("status")) != "received":
        return {"ok": True, "skipped": "payment not received"}

    invoice_id = _text(payment.get("invoice_id"))
    order = _order_for_invoice(base, invoice_id)
    if order is None:
        # Most payments have nothing to do with gift cards. Saying so
        # plainly beats a silent return that looks identical to a bug.
        return {"ok": True, "skipped": "no order for this payment"}

    lines = [row for row in (_rows("order_lines", base) or [])
             if _text(row.get("order_id")) == _text(order.get("id"))]
    products = {}
    for line in lines:
        product_id = _text(line.get("product_id"))
        if not product_id or product_id in products:
            continue
        try:
            products[product_id] = object_records.get_collection_record(
                "products", product_id, base_dir=base)
        except Exception:
            products[product_id] = {}

    sellable = [line for line in lines
                if _truthy(products.get(_text(line.get("product_id")), {})
                           .get("is_gift_card"))]
    if not sellable:
        return {"ok": True, "skipped": "no gift card on this order"}

    settled, reason = _invoice_is_settled(base, invoice_id)
    if not settled:
        # The card is money. It is issued when the money is here, not when
        # somebody has promised it.
        return {"ok": True, "skipped": reason, "order_id": order["id"]}

    entries = _rows("wallet_entries", base)
    if entries is None:
        return {"ok": True, "order_id": order["id"], "issued": 0,
                "warning": ("this order sold a gift card and there is no "
                            "wallet ledger on this server to credit; the "
                            "customer has paid for nothing")}
    already = {_text(row.get("generated_from")) for row in entries}
    wallets = _rows("wallets", base) or []
    taken = {_text(row.get("code")).upper() for row in wallets if row.get("code")}

    issued = []
    warnings = []
    for line in sellable:
        provenance = f"{MARKER}{_text(line.get('id'))}"
        if provenance in already:
            continue                 # a replay; the card already exists
        amount = _int(line.get("line_total_cents"))
        if amount <= 0:
            warnings.append(f"order line {_text(line.get('id'))} sold a gift "
                            f"card worth nothing; no wallet was opened")
            continue
        code = _new_code(taken)
        taken.add(code)
        wallet_id = object_ids.new_uuid4()
        try:
            object_records.create_collection_record(
                "wallets",
                {"id": wallet_id,
                 # The SHOP owns the container; the CODE is what spends it.
                 # A guest who was given this card has no account here, and
                 # owning it to somebody's email would make the present
                 # unusable by the person it was for.
                 "owner_id": _text(order.get("owner_id")),
                 "kind": "gift_card",
                 "code": code,
                 "is_active": "true"},
                base_dir=base, actor=ACTOR)
            object_records.create_collection_record(
                "wallet_entries",
                {"id": object_ids.new_uuid4(),
                 "wallet_id": wallet_id,
                 # Positive: money arriving. Credits are never gated by
                 # hook_wallet_entries, which is why this write needs no
                 # permission from it.
                 "amount_minor": str(amount),
                 "kind": "topup",
                 "description": f"Gift card {code} purchased on order "
                                f"{_text(order.get('number')) or order['id']}",
                 "reference": f"orders/{order['id']}",
                 "generated_from": provenance,
                 "owner_id": _text(order.get("owner_id"))},
                base_dir=base, actor=ACTOR)
        except Exception as exc:
            warnings.append(f"gift card for order line {_text(line.get('id'))} "
                            f"could not be issued: {str(exc)[:120]}")
            continue
        issued.append({"wallet_id": wallet_id, "code": code,
                       "amount_minor": amount,
                       "order_line_id": _text(line.get("id"))})

    result = {"ok": True, "order_id": order["id"], "issued": len(issued),
              "cards": issued}
    if warnings:
        result["warnings"] = warnings
    if not issued and not warnings:
        result["note"] = "already issued; a replay credits nothing twice"
    return result


# EVENT is the verb the change dispatcher calls handlers with; POST stays
# as an alias so an operator can poke the handler by hand over HTTP, the
# same pair system_shop_fulfillment keeps.
POST = EVENT
