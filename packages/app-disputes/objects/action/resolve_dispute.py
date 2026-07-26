"""action_resolve_dispute -- the one door where a dispute and the thing
that compensates it are written together.

POST {dispute_id, resolution_kind, amount_cents?, note?, wallet_id?,
      number?, today?}

This is the same argument action_disposition_return makes about returns
and refunds, and it is the reason this object exists rather than two
screens. Separating "close the dispute" from "issue the compensation" is
how they drift: a shop with a resolved queue and no refunds against it,
or refunds nobody can tie to a claim. Both facts come out of one human
decision -- "she's right, give her the money" -- so both records come out
of one call, and the dispute cannot be closed with one half done.

**The refund goes through the EXISTING gate.** hook_refunds already knows
what a payment can give back (its amount minus prior refunds, never a
bounced payment), and a second opinion about the same ceiling is a second
thing to get wrong -- worse, it would quote a customer a different number
depending on which door their claim came through. So the prospective
refund is handed to that hook in process and its refusal is surfaced
VERBATIM, then the record the hook handed back (stamped invoice_id and
all) is what gets written. A trusted server-side write bypasses hooks by
design, which is exactly why the hook has to be asked explicitly rather
than relied on.

**A credit refuses honestly when there is nowhere to put it.** Store
credit is a wallet entry, wallets are app-billing's, and app-billing is
not a dependency of this package on purpose: a shop that does not sell
credit should not be made to install a billing engine to record a
dispute. When the collection is absent the action says so and names the
app, rather than inventing a credit nobody can spend or -- worse --
closing the dispute as though one had been issued.

**A replacement is a real order, confirmed.** Not a draft: the order
schema's own words are that "a draft is not a commitment", and a
resolution that points at a document promising nothing is precisely the
failure hook_disputes exists to prevent. Its lines are copied from the
original at ZERO price, because the customer already paid on the order
being argued about and a replacement priced again would double-count
revenue in every fold that sums order totals. The link back to the
original is provenance in `notes` plus the dispute row itself, and NOT
orders.linked_order_id: that field means one specific thing -- the
drop-ship counterpart -- and action_dropship_order's stock rule reads it.
A field that means two things is a field a gate cannot read.

**Idempotency is the property this object is judged on.** An already
resolved dispute answers ok with a note and does nothing: no second
refund, no second replacement order, no second credit. Belt and braces
behind that, the refund's reason and the replacement order's notes both
carry `disputes/{id}`, and a replay finds its own marker. Two mechanisms
because the failure they guard against is a customer's money going back
twice, which is the one mistake nobody forgives and nobody notices for a
month.

The three rules about what a resolution must SAY are hook_disputes', and
this object holds them by ASKING that hook in process -- twice: once
before anything is written, for the endings that need no record, and once
with the finished reference in hand. Trusted server-side writes bypass
hooks by design, so the rule has to travel with the action; asking the
hook rather than restating it is what stops one gate growing two
vocabularies, exactly as the refund ceiling is asked of hook_refunds
rather than recomputed here.
"""

import os
from datetime import date, datetime, timezone

import object_execution
import object_ids
import object_records
import python_object_runtime

ACTOR = "action_resolve_dispute"

RESOLVED = "resolved"
WITHDRAWN = "withdrawn"

# The schema's own enum, restated only as a list of four values -- the
# RULES about them live in hook_disputes and are asked for, never copied.
RESOLUTION_KINDS = ("refund", "replacement", "credit", "no_action")
NEEDS_A_REASON = "no_action"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _cents(value):
    try:
        return int(_text(value) or "0")
    except ValueError:
        return 0


def _call(object_id, payload, *, method="POST"):
    """Run another installed object in process. Returns (result, error); a
    missing object is an error string, never an exception."""
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


def _ask_the_gate(record):
    """Put a prospective dispute row to hook_disputes and hand back its
    refusal, verbatim, or None.

    Fails CLOSED: a gate that cannot be consulted is not a gate that said
    yes, so an unreachable hook refuses the close rather than waving it
    through.
    """
    verdict, error = _call("hook_disputes",
                           {"action": "update", "collection": "disputes",
                            "record": record},
                           method="BEFORE_WRITE")
    if error:
        return {"status": 503,
                "error": (f"The dispute gate could not be consulted ({error}), "
                          f"so this dispute has not been closed. The rule about "
                          f"what a resolution must name lives in hook_disputes "
                          f"and this action refuses to decide without it.")}
    if isinstance(verdict, dict) and verdict.get("error"):
        return {"status": verdict.get("status", 400),
                "error": _text(verdict.get("error"))}
    return None


def _rows(collection, base):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _order_of(base, dispute):
    order_id = _text(dispute.get("order_id"))
    if not order_id:
        return {}
    try:
        return object_records.get_collection_record("orders", order_id,
                                                    base_dir=base)
    except Exception:
        return {}


def _payment_for(base, dispute, order):
    """The payment this refund compensates.

    A payment dispute names its own payment and that wins outright -- the
    customer is arguing about one charge and refunding a different one
    would be a new problem. Otherwise the order points at an invoice and
    payments point at the same invoice, which is the join app-shop's
    checkout leaves behind; the earliest non-bounced payment wins,
    deterministically. Splitting one refund across several payments is
    deliberately NOT built here for the same reason app-returns does not
    build it: it needs a rule about which money goes back first, and
    inventing that rule inside a disputes action would be picking an
    accounting policy by accident.
    """
    payment_id = _text(dispute.get("payment_id"))
    if payment_id:
        try:
            return object_records.get_collection_record("payments", payment_id,
                                                        base_dir=base)
        except Exception:
            return None

    invoice_id = _text(order.get("invoice_id"))
    if not invoice_id:
        return None
    payments = _rows("payments", base)
    if payments is None:
        return None
    candidates = [row for row in payments
                  if _text(row.get("invoice_id")) == invoice_id
                  and _text(row.get("status") or "received") != "bounced"]
    candidates.sort(key=lambda row: (_text(row.get("received_on")),
                                     _text(row.get("id"))))
    return candidates[0] if candidates else None


def _wallet_for(base, request, dispute, order):
    """Whose credit this is. Explicit wallet_id wins; otherwise the
    customer's own wallet, matched on the wallet's owner.

    Deliberately no invention: a shop that sells to guests has no wallet
    for most of its customers, and issuing credit into a wallet nobody
    owns would be a number the customer can never spend.
    """
    wallets = _rows("wallets", base)
    if wallets is None:
        return None, "wallets"
    wanted = _text(request.get("wallet_id"))
    if wanted:
        for row in wallets:
            if _text(row.get("id")) == wanted:
                return row, ""
        return None, "no_such_wallet"
    keys = {_text(dispute.get("customer_email")),
            _text(order.get("customer_email")),
            _text(order.get("customer_id"))}
    keys.discard("")
    for row in wallets:
        if _text(row.get("owner_id")) in keys:
            return row, ""
    return None, "no_wallet_for_customer"


def _marker(dispute_id):
    return f"disputes/{dispute_id}"


def _already_refunded(base, dispute_id):
    """Belt and braces behind the status check: a refund carrying this
    dispute's marker means the money already went back, whatever the
    dispute row says."""
    for row in _rows("refunds", base) or []:
        if _marker(dispute_id) in _text(row.get("reason")):
            return row
    return None


def _already_replaced(base, dispute_id):
    for row in _rows("orders", base) or []:
        if _marker(dispute_id) in _text(row.get("notes")):
            return row
    return None


def _already_credited(base, dispute_id):
    for row in _rows("wallet_entries", base) or []:
        if _marker(dispute_id) in _text(row.get("generated_from")):
            return row
    return None


def _copy_lines(base, from_order_id, to_order_id, owner):
    """The goods, again, at no charge.

    Zero price is the decision worth naming: the customer already paid on
    the order being argued about, so a replacement priced at the catalog
    rate would show up as a second sale in every fold that sums order
    totals, and the shop would appear to have earned money it is in fact
    giving away.
    """
    created = []
    for line in _rows("order_lines", base) or []:
        if _text(line.get("order_id")) != from_order_id:
            continue
        line_id = object_ids.new_uuid4()
        object_records.create_collection_record(
            "order_lines",
            {
                "id": line_id,
                "order_id": to_order_id,
                "product_id": _text(line.get("product_id")),
                "description": _text(line.get("description")),
                "quantity": _text(line.get("quantity")) or "1",
                "unit_price_cents": "0",
                "line_total_cents": "0",
                "tax_rate_bps": "0",
                "line_tax_cents": "0",
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
        created.append(line_id)
    return created


def POST(request):
    base = _base_dir()
    dispute_id = _text(request.get("dispute_id"))
    if not dispute_id:
        return {"status": 400, "error": "dispute_id is required"}

    try:
        dispute = object_records.get_collection_record("disputes", dispute_id,
                                                       base_dir=base)
    except Exception:
        return {"status": 404, "error": f"No such dispute: {dispute_id}"}

    status = _text(dispute.get("status"))
    if status == RESOLVED:
        # The single most important behaviour in this object. A retried
        # request, a double-clicked button, a replayed queue entry: none of
        # them may send a customer's money back twice or raise a second
        # replacement order. Observable state, not a lock
        # (docs/logic-decisions.md #7).
        return {"ok": True, "dispute_id": dispute_id,
                "resolution_kind": _text(dispute.get("resolution_kind")),
                "resolution_ref": _text(dispute.get("resolution_ref")),
                "refund_id": "", "order_id": "", "wallet_entry_id": "",
                "note": "this dispute was already resolved; no second refund, "
                        "replacement or credit was issued"}

    if status == WITHDRAWN:
        return {"status": 409,
                "error": (f"Dispute {dispute_id} was withdrawn. The customer "
                          f"dropped the claim, and compensating a claim "
                          f"nobody is making would be a payment with no "
                          f"argument behind it -- raise a new dispute if they "
                          f"have come back.")}

    kind = _text(request.get("resolution_kind"))
    note = _text(request.get("note"))
    if kind not in RESOLUTION_KINDS:
        return {"status": 400,
                "error": (f"Unknown resolution_kind {kind or '(blank)'!r}; "
                          f"expected one of {', '.join(RESOLUTION_KINDS)}.")}

    # The same three rules the hook enforces, asked of the hook's own
    # checker rather than restated -- trusted server-side writes bypass
    # hooks, so the rule has to travel with the action, and a second
    # wording of it would give one gate two vocabularies. The ref is
    # filled in below once the compensating record exists; only the
    # kind-and-reason half can be checked this early, which is the half
    # that must fail BEFORE anything is written.
    if kind == NEEDS_A_REASON:
        refusal = _ask_the_gate({"id": dispute_id, "status": RESOLVED,
                                 "resolution_kind": kind,
                                 "resolution_note": note})
        if refusal:
            return refusal

    order = _order_of(base, dispute)
    owner = (_text(dispute.get("owner_id"))
             or _text(order.get("owner_id"))
             or _text((request.get("_identity") or {}).get("user_id")))
    actor_user = (_text((request.get("_identity") or {}).get("user_id"))
                  or owner)
    when = _text(request.get("today")) or date.today().isoformat()

    resolution_ref = ""
    refund_id = ""
    replacement_id = ""
    entry_id = ""
    amount = _cents(request.get("amount_cents"))

    # --- the compensating record, composed BEFORE the dispute is closed ---
    # Statuses follow facts and never lead them: a dispute marked resolved
    # while its refund is still being argued about by a gate is exactly the
    # lie this package exists to prevent.
    if kind == "refund":
        existing = _already_refunded(base, dispute_id)
        if existing is not None:
            refund_id = _text(existing.get("id"))
            resolution_ref = f"refunds/{refund_id}"
        else:
            if amount <= 0:
                return {"status": 400,
                        "error": ("A refund of nothing is not a refund: pass "
                                  "amount_cents, or resolve this as no_action "
                                  "with a reason if no money is going back.")}
            payment = _payment_for(base, dispute, order)
            if payment is None:
                return {"status": 409,
                        "error": ("No payment to refund against. Money can "
                                  "only go back the way it came, so a refund "
                                  "needs the payment it compensates -- point "
                                  "the dispute at a payment_id, or resolve it "
                                  "as a credit or a replacement.")}
            candidate = {
                "id": object_ids.new_uuid4(),
                "payment_id": _text(payment.get("id")),
                "amount_cents": str(amount),
                "reason": f"{_marker(dispute_id)} {note}".strip()[:300],
                "refunded_on": when,
                "owner_id": owner,
            }
            # DEFER to the existing ceiling rather than growing a second
            # opinion about it, and surface its words unchanged so the
            # customer is quoted the same number whichever door they came
            # through.
            verdict, error = _call("hook_refunds",
                                   {"action": "create", "collection": "refunds",
                                    "record": candidate},
                                   method="BEFORE_WRITE")
            if error:
                return {"status": 503,
                        "error": (f"The refund gate could not be consulted "
                                  f"({error}), so no money is going anywhere "
                                  f"and the dispute is still open. Refunds are "
                                  f"app-payments' arithmetic and this action "
                                  f"refuses to guess at it.")}
            if isinstance(verdict, dict) and verdict.get("error"):
                return {"status": verdict.get("status", 409),
                        "error": _text(verdict.get("error")),
                        "refund": _text(verdict.get("error"))}
            if isinstance(verdict, dict) and isinstance(verdict.get("record"),
                                                        dict):
                candidate = verdict["record"]
            object_records.create_collection_record("refunds", candidate,
                                                    base_dir=base, actor=ACTOR)
            refund_id = _text(candidate.get("id"))
            resolution_ref = f"refunds/{refund_id}"

    elif kind == "replacement":
        existing = _already_replaced(base, dispute_id)
        if existing is not None:
            replacement_id = _text(existing.get("id"))
            resolution_ref = f"orders/{replacement_id}"
        else:
            if not order:
                return {"status": 409,
                        "error": ("A replacement needs the order it replaces. "
                                  "This dispute names no order_id, so there "
                                  "are no lines to send again and no customer "
                                  "to send them to.")}
            replacement_id = object_ids.new_uuid4()
            number = (_text(request.get("number"))
                      or f"{_text(order.get('number'))}-R")
            object_records.create_collection_record(
                "orders",
                {
                    "id": replacement_id,
                    "doc_type": "sale",
                    "number": number,
                    "customer_id": _text(order.get("customer_id")),
                    "customer_name": _text(order.get("customer_name")),
                    "customer_email": _text(order.get("customer_email")),
                    "currency": _text(order.get("currency")) or "USD",
                    # Confirmed, not draft. The order schema's own words are
                    # that a draft is not a commitment, and a resolution
                    # pointing at a document that promises nothing is the
                    # failure hook_disputes exists to prevent.
                    "status": "confirmed",
                    "order_date": when,
                    "ship_to_name": _text(order.get("ship_to_name")),
                    "ship_to_address": _text(order.get("ship_to_address")),
                    "customer_note": _text(order.get("customer_note")),
                    "notes": (f"Generated by {ACTOR} [{_marker(dispute_id)} "
                              f"orders/{order['id']}] -- no-charge replacement"),
                    "owner_id": owner,
                },
                base_dir=base, actor=ACTOR)
            _copy_lines(base, order["id"], replacement_id, owner)
            resolution_ref = f"orders/{replacement_id}"

    elif kind == "credit":
        existing = _already_credited(base, dispute_id)
        if existing is not None:
            entry_id = _text(existing.get("id"))
            resolution_ref = f"wallet_entries/{entry_id}"
        else:
            if amount <= 0:
                return {"status": 400,
                        "error": ("A credit of nothing is not a credit: pass "
                                  "amount_cents, or resolve this as no_action "
                                  "with a reason.")}
            if _rows("wallet_entries", base) is None:
                return {"status": 409,
                        "error": ("There is no wallet on this server to credit: "
                                  "store credit is a wallet entry and wallets "
                                  "come from app-billing, which is not "
                                  "installed. Refusing rather than closing this "
                                  "dispute as though credit had been issued -- "
                                  "refund it, replace it, or say no_action and "
                                  "why."),
                        "missing_app": "app-billing"}
            wallet, problem = _wallet_for(base, request, dispute, order)
            if wallet is None:
                return {"status": 409,
                        "error": (f"No wallet to credit ({problem}). A credit "
                                  f"has to land somewhere the customer can "
                                  f"spend it: pass wallet_id, or open a wallet "
                                  f"for this customer first. A credit issued "
                                  f"into a wallet nobody owns is a number that "
                                  f"never becomes money."),
                        "missing_wallet": problem}
            entry_id = object_ids.new_uuid4()
            object_records.create_collection_record(
                "wallet_entries",
                {
                    "id": entry_id,
                    "wallet_id": _text(wallet.get("id")),
                    # Positive: credit is money going TO the customer, and
                    # the sign convention is the ledger's, not ours.
                    "amount_minor": str(amount),
                    "kind": "adjustment",
                    "description": note or f"Dispute {dispute_id} resolved as "
                                           f"store credit",
                    "reference": _marker(dispute_id),
                    "generated_from": _marker(dispute_id),
                    "owner_id": _text(wallet.get("owner_id")) or owner,
                },
                base_dir=base, actor=ACTOR)
            resolution_ref = f"wallet_entries/{entry_id}"

    # --- the paperwork, last ------------------------------------------------
    patch = {
        "status": RESOLVED,
        "resolution_kind": kind,
        "resolution_ref": resolution_ref,
        "resolved_by": actor_user,
        "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if note:
        patch["resolution_note"] = note

    # The gate, asked one final time with the ref now filled in. It cannot
    # refuse a resolution this action just composed, and that is exactly
    # the point: if it ever does, the two have drifted and the dispute
    # stays open rather than closing over a record nobody can find.
    refusal = _ask_the_gate({**dispute, **patch})
    if refusal:
        return {**refusal,
                "resolution_ref": resolution_ref,
                "note": "the compensating record was written but the dispute "
                        "was NOT closed; it is still open for somebody to "
                        "finish"}

    object_records.update_collection_record("disputes", dispute_id, patch,
                                            base_dir=base, actor=ACTOR)

    return {"ok": True, "dispute_id": dispute_id, "resolution_kind": kind,
            "resolution_ref": resolution_ref, "refund_id": refund_id,
            "order_id": replacement_id, "wallet_entry_id": entry_id,
            "amount_cents": str(amount) if amount > 0 else "0",
            "status_of_dispute": RESOLVED, "date": when}
