"""action_disposition_return -- somebody opens the box and decides.

POST {shipment_id, lines: [{shipment_line_id, disposition, quantity,
      note?}], refund?: "full"|"partial"|"none", refund_cents?, today?}

This is the ONE place a return and its refund compose. Not because
combining them is tidy, but because separating them is how they drift: a
shop that restocks on Monday and refunds on Thursday, through two screens
with two audiences, eventually has restocked goods nobody paid back for
and refunds against goods still sitting in the returns bin. Both facts
come out of one human decision -- "this mug is fine, give her the money"
-- so both records come out of one call, and the RMA cannot be closed
with one half done.

**The parcel must be `received` first.** Dispositioning a shipment still
in transit is a guess about what is in a box nobody has opened. The
refusal names the status it found, because the usual cause is somebody
working ahead of the van rather than somebody doing something wrong.

**Disposal is still a MOVE.** The tempting shortcut -- restocked goods
compose a move, binned ones compose nothing -- is exactly how shrinkage
hides: the units left inventory in a way somebody has to be able to
audit, and a system where "we threw it away" is the one outcome that
leaves no trace is a system whose loss reports are guaranteed to be
optimistic. So a disposal writes a real stock move with a real reason
(`damage` when the RMA says the customer received it damaged, `waste`
otherwise) leaving the location the goods were sold to, with no
destination, which is the loss shape hook_stock_moves already enforces.
The loss taxonomy in stock_moves v3 exists precisely so that "what
happened to it" is a fact rather than an adjustment nobody can explain.

**Where the goods move FROM.** The sale move sent these units to
shop.customer_location, so that is where they still are as far as the
ledger is concerned, and that is where the return move takes them from --
restock walks them back to the shelf with reason `return`, disposal takes
them out of the system entirely. plan/fulfillment-logistics-spec.md
sketches a returns STAGING location with a move on arrival and a second
move at disposition; that would need the arrival handler to compose stock
before anybody has decided what the goods are, which is the one thing
this slice must not do (system_return_posting deliberately touches no
stock at all). One move at the moment of the decision says the same thing
with half the rows and no intermediate state that lies while somebody is
at lunch. returns.stock_location and returns.customer_location override
the shop's settings for a shop that really does bench its returns
somewhere separate.

**A misconfigured location REFUSES here**, which is the opposite of what
system_order_fulfillment and system_receipt_posting do, and the
difference is deliberate. Those are event handlers reacting to a fact
that already happened: the parcel went, the pallet landed, and a missing
setting must not cost anybody that record -- they warn, and the event can
be replayed once the setting exists. This is a one-shot action that ENDS
in a terminal state; completing it with the moves silently missing would
destroy the only chance to record them, because a dispositioned shipment
never comes back for a second pass. Refusing is recoverable in five
seconds and losing the moves is not. A shop with no stock collection at
all is a different case -- nothing to move, not a misconfiguration -- and
proceeds with a note.

**The refund ceiling is NOT re-implemented here.** hook_refunds already
knows what a payment can give back (amount minus prior refunds, never a
bounced payment), and a second opinion about the same ceiling is a second
thing to get wrong. This action calls that hook in process with the
refund it is about to write and surfaces its refusal verbatim -- so the
number a customer is quoted comes from the same arithmetic whatever door
it arrived through -- then writes the record the hook handed back, stamped
invoice_id and all, because a trusted server-side write would otherwise
bypass the hook entirely.

`refund: none` is a first-class, SILENT outcome. A restocking-fee-only
return, a replacement instead of a refund, a goodwill restock: all
ordinary. Warning about them would teach operators to click past
warnings, which is expensive the day one matters.

**Idempotency is the property this object is judged on.** A dispositioned
shipment answers ok with a note and does nothing -- no second set of
moves, no second refund. Belt and braces behind that, each move carries
`returns/{shipment_id}:line/{line_id}` and a replay finds its own marker.
Two mechanisms because the failure they guard against is somebody's money
being sent back twice.
"""

import os
from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

import object_execution
import object_ids
import object_records
import python_object_runtime

ACTOR = "action_disposition_return"

DISPOSITIONS = ("restock", "dispose")
REFUND_MODES = ("full", "partial", "none")

# Goods are on our dock and unjudged. Anything else is either too early
# (still with the customer or the carrier) or already done.
READY_STATUS = "received"
DONE_STATUS = "dispositioned"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _quantity(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return None


def _number(value):
    return format(value.normalize(), "f")


def _cents(value):
    try:
        return int(_text(value) or "0")
    except ValueError:
        return 0


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings (docs/logic-decisions.md #4)."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


def _call(object_id, payload, *, method="POST"):
    """Run another installed object in process. Returns (result, error);
    a missing object is an error string, never an exception."""
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


def _rma_for(base, shipment_id):
    try:
        rows = object_records.read_collection_records("return_authorizations",
                                                      base_dir=base)
    except Exception:
        return None
    for row in rows:
        if _text(row.get("shipment_id")) == shipment_id:
            return row
    return None


def _lines_of(base, shipment_id):
    try:
        rows = object_records.read_collection_records("shipment_lines",
                                                      base_dir=base)
    except Exception:
        return []
    return [row for row in rows if _text(row.get("shipment_id")) == shipment_id]


def _order_line(base, order_line_id):
    if not order_line_id:
        return {}
    try:
        return object_records.get_collection_record("order_lines",
                                                    order_line_id,
                                                    base_dir=base)
    except Exception:
        return {}


def _payment_for(base, order):
    """The payment this refund compensates.

    The order points at an invoice and payments point at the same invoice,
    which is the join app-shop's checkout leaves behind. The earliest
    non-bounced payment wins, deterministically, and splitting one refund
    across several payments is deliberately NOT built: it is a real case
    (a deposit plus a balance), it needs a rule about which money goes
    back first, and inventing that rule inside a returns action would be
    picking an accounting policy by accident. Until it exists, the
    operator raises the second refund by hand, which is visible.
    """
    invoice_id = _text(order.get("invoice_id"))
    if not invoice_id:
        return None
    try:
        payments = object_records.read_collection_records("payments",
                                                          base_dir=base)
    except Exception:
        return None
    candidates = [row for row in payments
                  if _text(row.get("invoice_id")) == invoice_id
                  and _text(row.get("status") or "received") != "bounced"]
    candidates.sort(key=lambda row: (_text(row.get("received_on")),
                                     _text(row.get("id"))))
    return candidates[0] if candidates else None


def _refundable_for_lines(base, wanted):
    """What the customer actually paid for the units coming back.

    Line money plus the tax that was charged ON that line, computed the
    same way order_totals computed it in the first place (floor, integer
    cents, bps) -- so a full refund gives back exactly what the customer
    handed over for those goods rather than a number that looks close.
    Shipping is NOT refunded: the carriage was performed, the box really
    did travel, and a shop that wants to be more generous than that is
    making a commercial decision it should make explicitly with a partial
    refund of its own choosing.
    """
    total = 0
    for line, quantity, _disposition, _note in wanted:
        order_line = _order_line(base, _text(line.get("order_line_id")))
        unit = _cents(order_line.get("unit_price_cents"))
        money = int((quantity * Decimal(unit)).to_integral_value(
            rounding=ROUND_FLOOR))
        rate = _cents(order_line.get("tax_rate_bps"))
        total += money + (money * rate // 10000)
    return total


def POST(request):
    base = _base_dir()
    shipment_id = _text(request.get("shipment_id"))
    if not shipment_id:
        return {"status": 400, "error": "shipment_id is required"}

    try:
        shipment = object_records.get_collection_record("shipments",
                                                        shipment_id,
                                                        base_dir=base)
    except Exception:
        return {"status": 404, "error": f"No such shipment: {shipment_id}"}

    if _text(shipment.get("direction")) != "inbound":
        return {"status": 409,
                "error": ("That is an outbound shipment. Dispositioning is "
                          "what you do to goods that came BACK; a parcel on "
                          "its way to a customer has nothing to decide about "
                          "yet.")}

    rma = _rma_for(base, shipment_id)
    status = _text(shipment.get("status"))

    if status == DONE_STATUS:
        # The single most important behaviour in this object. A retried
        # request, a double-clicked button, a replayed queue entry: none of
        # them may move goods a second time or send a customer's money back
        # twice. Observable state, not a lock (docs/logic-decisions.md #7).
        return {"ok": True, "shipment_id": shipment_id,
                "return_id": _text((rma or {}).get("id")),
                "moved": 0, "refund_id": "",
                "refund_ref": _text((rma or {}).get("refund_ref")),
                "note": "this return was already dispositioned; nothing moved "
                        "and no second refund was issued"}

    if status != READY_STATUS:
        return {"status": 409,
                "error": (f"This return is {status}, not received. Goods have "
                          f"to be physically on the dock before anybody can "
                          f"say what they are -- dispositioning a parcel still "
                          f"in transit is a guess about the contents of a box "
                          f"nobody has opened.")}

    lines = {row["id"]: row for row in _lines_of(base, shipment_id)}
    requested = request.get("lines")
    blockers = {"unknown_lines": [], "over_disposition": [],
                "bad_quantities": [], "bad_disposition": [], "refund": ""}

    wanted = []
    if not isinstance(requested, list) or not requested:
        blockers["bad_quantities"].append(
            {"shipment_line_id": "",
             "reason": "name what each returned line is: there is deliberately "
                       "no default, because guessing restock would put damaged "
                       "goods back on the shelf and guessing dispose would bin "
                       "sellable stock"})
    else:
        for entry in requested:
            if not isinstance(entry, dict):
                blockers["bad_quantities"].append(
                    {"shipment_line_id": "", "reason": "line is not an object"})
                continue
            line_id = _text(entry.get("shipment_line_id"))
            if line_id not in lines:
                blockers["unknown_lines"].append(line_id)
                continue
            disposition = _text(entry.get("disposition"))
            if disposition not in DISPOSITIONS:
                blockers["bad_disposition"].append(
                    {"shipment_line_id": line_id,
                     "disposition": disposition,
                     "reason": f"expected one of {', '.join(DISPOSITIONS)}"})
                continue
            quantity = _quantity(entry.get("quantity"))
            if quantity is None or quantity <= 0:
                blockers["bad_quantities"].append(
                    {"shipment_line_id": line_id,
                     "quantity": _text(entry.get("quantity")),
                     "reason": "a disposition must name a positive quantity"})
                continue
            came_back = _quantity(lines[line_id].get("quantity")) or Decimal(0)
            if quantity > came_back:
                blockers["over_disposition"].append({
                    "shipment_line_id": line_id,
                    "description": _text(lines[line_id].get("description")),
                    "came_back": _number(came_back),
                    "asked_for": _number(quantity),
                })
                continue
            wanted.append((lines[line_id], quantity, disposition,
                           _text(entry.get("note"))))

    refund_mode = _text(request.get("refund")) or "none"
    if refund_mode not in REFUND_MODES:
        blockers["refund"] = (f"Unknown refund {refund_mode!r}; expected one "
                              f"of {', '.join(REFUND_MODES)}.")

    # --- locations, checked BEFORE anything is written -------------------
    try:
        moves = object_records.read_collection_records("stock_moves",
                                                       base_dir=base)
        stock_installed = True
    except Exception:
        moves, stock_installed = [], False

    from_location = (_setting(base, "returns.customer_location")
                     or _setting(base, "shop.customer_location"))
    to_location = (_setting(base, "returns.stock_location")
                   or _setting(base, "shop.stock_location"))
    movable = [entry for entry in wanted
               if _text(entry[0].get("product_id"))]
    if stock_installed and movable and not from_location:
        blockers["bad_quantities"].append(
            {"shipment_line_id": "",
             "reason": "shop.customer_location (or returns.customer_location) "
                       "is not configured, so there is nowhere for these goods "
                       "to come back FROM; refusing rather than closing the "
                       "return with its moves silently missing, because a "
                       "dispositioned shipment never comes back for a second "
                       "pass"})
    if (stock_installed and not to_location
            and any(entry[2] == "restock" for entry in movable)):
        blockers["bad_quantities"].append(
            {"shipment_line_id": "",
             "reason": "shop.stock_location (or returns.stock_location) is not "
                       "configured, so a restock has no shelf to go back to; "
                       "refusing rather than closing the return with its moves "
                       "silently missing"})

    # --- the refund, priced and CHECKED before any goods move ------------
    refund_record = None
    refund_amount = 0
    if refund_mode != "none" and not any(blockers.values()):
        try:
            order = object_records.get_collection_record(
                "orders", _text(shipment.get("order_id")), base_dir=base)
        except Exception:
            order = {}
        if refund_mode == "full":
            refund_amount = _refundable_for_lines(base, wanted)
        else:
            refund_amount = _cents(request.get("refund_cents"))
        if refund_amount <= 0:
            blockers["refund"] = (
                "A refund of nothing is not a refund: pass refund_cents for a "
                "partial, or refund: none if no money is going back (which is "
                "a perfectly ordinary outcome and needs no excuse).")
        else:
            payment = _payment_for(base, order)
            if payment is None:
                blockers["refund"] = (
                    "No payment on this order to refund against. Money can "
                    "only go back the way it came, so a refund needs the "
                    "payment it compensates -- an unpaid order is corrected "
                    "by amending the invoice, not by refunding it.")
            else:
                candidate = {
                    "id": object_ids.new_uuid4(),
                    "payment_id": payment["id"],
                    "amount_cents": str(refund_amount),
                    "reason": (f"Return {_text((rma or {}).get('id'))} "
                               f"({_text((rma or {}).get('reason')) or 'return'})"
                               ).strip(),
                    "refunded_on": (_text(request.get("today"))
                                    or date.today().isoformat()),
                    "owner_id": _text(shipment.get("owner_id")),
                }
                # DEFER to the existing ceiling rather than growing a second
                # opinion about it. A trusted server-side write bypasses
                # hooks, so the hook is asked explicitly and its answer --
                # refusal or stamped record -- is what happens next.
                verdict, error = _call("hook_refunds",
                                       {"action": "create",
                                        "collection": "refunds",
                                        "record": candidate},
                                       method="BEFORE_WRITE")
                if error:
                    blockers["refund"] = (
                        f"The refund gate could not be consulted ({error}), so "
                        f"no money is going anywhere and nothing has been "
                        f"dispositioned. Refunds are app-payments' arithmetic "
                        f"and this action refuses to guess at it.")
                elif isinstance(verdict, dict) and verdict.get("error"):
                    # Surfaced verbatim: the customer must be quoted the same
                    # number whichever door the refund came through.
                    blockers["refund"] = _text(verdict.get("error"))
                elif isinstance(verdict, dict) and isinstance(verdict.get("record"), dict):
                    refund_record = verdict["record"]
                else:
                    refund_record = candidate

    if any(blockers.values()):
        return {"status": 409,
                "error": "This return cannot be dispositioned yet.",
                **blockers}

    # --- the goods ---------------------------------------------------------
    reason_of_return = _text((rma or {}).get("reason"))
    when = _text(request.get("today")) or date.today().isoformat()
    existing = [_text(move.get("reference")) for move in moves]
    moved = 0
    composed = []
    for line, quantity, disposition, note in wanted:
        product_id = _text(line.get("product_id"))
        if not product_id or not stock_installed:
            # A service, a free-text line, or a shop with no stock app: there
            # was never anything on a shelf to put back or to bin.
            composed.append({"shipment_line_id": line["id"],
                             "disposition": disposition,
                             "quantity": _number(quantity), "moved": False})
            continue
        marker = f"returns/{shipment_id}:line/{line['id']}"
        if any(marker in reference for reference in existing):
            composed.append({"shipment_line_id": line["id"],
                             "disposition": disposition,
                             "quantity": _number(quantity), "moved": False})
            continue
        move = {
            "id": object_ids.new_uuid4(),
            "product_id": product_id,
            "from_location_id": from_location,
            "quantity": _number(quantity),
            "reference": f"{marker} {disposition}".strip(),
            "occurred_at": when,
            "owner_id": _text(shipment.get("owner_id")),
            "entity_id": _text(shipment.get("entity_id")),
        }
        if disposition == "restock":
            move["to_location_id"] = to_location
            move["reason"] = "return"
        else:
            # A loss move: from a real location, to nowhere. `damage` when
            # the customer told us it arrived broken -- that is a different
            # argument (with a carrier or a supplier) than something we
            # simply cannot resell.
            move["reason"] = "damage" if reason_of_return == "damaged" else "waste"
        if note:
            move["reference"] = f"{move['reference']} {note}"[:300]
        object_records.create_collection_record("stock_moves", move,
                                                base_dir=base, actor=ACTOR)
        existing.append(marker)
        moved += 1
        composed.append({"shipment_line_id": line["id"],
                         "disposition": disposition,
                         "quantity": _number(quantity),
                         "reason": move["reason"], "moved": True})

    # --- the money ---------------------------------------------------------
    refund_id = ""
    refund_ref = ""
    if refund_record is not None:
        object_records.create_collection_record("refunds", refund_record,
                                                base_dir=base, actor=ACTOR)
        refund_id = _text(refund_record.get("id"))
        refund_ref = f"refunds/{refund_id}"

    # --- the paperwork, last: statuses follow facts, never lead them -------
    object_records.update_collection_record(
        "shipments", shipment_id, {"status": DONE_STATUS},
        base_dir=base, actor=ACTOR)

    return_id = ""
    if rma:
        return_id = rma["id"]
        patch = {"status": "closed"}
        if refund_ref:
            patch["refund_ref"] = refund_ref
        object_records.update_collection_record(
            "return_authorizations", return_id, patch,
            base_dir=base, actor=ACTOR)

    result = {"ok": True, "shipment_id": shipment_id, "return_id": return_id,
              "status_of_shipment": DONE_STATUS,
              "status_of_return": "closed" if rma else "",
              "lines": len(composed), "dispositions": composed,
              "moved": moved, "refund": refund_mode,
              "refund_id": refund_id, "refund_ref": refund_ref,
              "refund_cents": str(refund_amount) if refund_id else "0",
              "date": when}
    if not stock_installed:
        result["note"] = ("stock is not installed, so nothing moved; the "
                          "return is closed and any refund stands")
    elif not rma:
        result["note"] = ("no return authorization points at this shipment, so "
                          "only the shipment was closed -- the goods and the "
                          "money are recorded either way")
    return result
