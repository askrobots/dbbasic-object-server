"""system_stock_books -- inventory losses reach the books.

Event handler (HANDLES stock_moves.record.created): when a LOSS move lands
(waste/breakage/theft/expiry/damage/disaster -- plan/inventory-adjustments-
spec.md), compose and post DR loss-expense / CR inventory-asset for
round(quantity * unit_cost_cents), through the shared composer
(object_finance.compose_posted_journal -- this module is the third
composer, the one that triggered the doctrine-#4 extraction).

Count variances (reason=adjustment, written by action_apply_count) compose
too: a shortage books like a loss; an overage books the mirror (DR
inventory / CR shrinkage). Transfer-shaped adjustments (both locations set)
carry no financial meaning and are skipped.

Account mapping is configuration (app_settings):
  inventory.journal.inventory_account   the CR side (asset)
  inventory.journal.shrinkage_account   the default DR side (expense)
  inventory.journal.{reason}_account    optional per-reason DR override --
                                        e.g. route theft to its own account
                                        for the insurance claim.
Soft dependency, same posture as system_books: books absent or accounts
unmapped -> skip with a reason; stock keeping works without books.

Purchases/sales composition is explicitly NOT here: COGS-on-sale needs a
real valuation method (FIFO/weighted average); stamped-cost losses do not.
That boundary holds until a valuation spec exists.
"""

import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import object_finance
import object_records

HANDLES = ["stock_moves.record.created"]

ACTOR = "system_stock_books"

LOSS_REASONS = {"waste", "breakage", "theft", "expiry", "damage", "disaster"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and row.get("value"):
                return row["value"].strip()
    except Exception:
        pass
    return default


def _books_ready(base):
    try:
        object_records.read_collection_records("fin_journals", base_dir=base)
        object_records.read_collection_records("fin_journal_lines", base_dir=base)
    except Exception:
        return False
    return True


def _loss_amount_cents(move):
    """round(quantity * unit_cost_cents) -- Decimal, never float."""
    try:
        quantity = Decimal(str(move.get("quantity") or "0"))
        unit_cost = int(move.get("unit_cost_cents") or 0)
    except (InvalidOperation, ValueError):
        return 0
    if quantity <= 0 or unit_cost <= 0:
        return 0
    return int((quantity * unit_cost).to_integral_value(rounding=ROUND_HALF_UP))


def compose_for_move(base, move):
    """Compose the loss/variance journal for one stock move.

    action_apply_count writes at the storage level (never reaches the
    dispatcher) and composes its own variance journal with the SAME
    generated_from marker ("stock_moves/{id}"), so the two paths stay
    idempotent against each other whichever runs first."""
    reason = (move.get("reason") or "").strip()
    from_loc = (move.get("from_location_id") or "").strip()
    to_loc = (move.get("to_location_id") or "").strip()

    if reason in LOSS_REASONS:
        direction = "loss"
    elif reason == "adjustment" and from_loc and not to_loc:
        direction = "loss"       # count shortage
    elif reason == "adjustment" and to_loc and not from_loc:
        direction = "overage"    # count overage: the mirror
    else:
        return {"ok": True, "skipped": "not a loss event"}

    if not _books_ready(base):
        return {"ok": True, "skipped": "books not installed (fin_journals absent)"}

    inventory_acct = _setting(base, "inventory.journal.inventory_account")
    shrinkage_acct = _setting(base, "inventory.journal.shrinkage_account")
    loss_acct = _setting(base, f"inventory.journal.{reason}_account", shrinkage_acct)
    if not inventory_acct or not loss_acct:
        return {"ok": True, "skipped": "accounts unconfigured"}

    amount = _loss_amount_cents(move)
    if amount <= 0:
        return {"ok": True, "skipped": "no stamped cost; nothing to book"}

    if direction == "loss":
        debit_acct, credit_acct = loss_acct, inventory_acct
        what = f"Inventory {reason}" if reason in LOSS_REASONS else "Count shortage"
    else:
        debit_acct, credit_acct = inventory_acct, shrinkage_acct
        what = "Count overage"
    reference = (move.get("reference") or "").strip()
    description = f"{what}: product {move.get('product_id')} x {move.get('quantity')}"
    if reference:
        description += f" ({reference})"

    return object_finance.compose_posted_journal(
        base,
        generated_from=f"stock_moves/{move.get('id')}",
        date=str(move.get("occurred_at") or move.get("created_at") or "")[:10],
        description=description,
        lines=[
            {"account_id": debit_acct, "debit_cents": amount, "credit_cents": 0},
            {"account_id": credit_acct, "debit_cents": 0, "credit_cents": amount},
        ],
        owner_id=move.get("owner_id", ""),
        entity_id=move.get("entity_id", ""),
        actor=ACTOR,
    )


def EVENT(request):
    """Best-effort reaction: every failure returns a reason, never raises
    into the dispatcher."""
    # The dispatcher's payload carries the RAW action ("create"); the event
    # NAME uses the participle. Accept both (the lesson system_books learned
    # in production).
    action = str(request.get("action") or "")
    action = {"create": "created", "update": "updated", "delete": "deleted"}.get(action, action)
    if action != "created" or str(request.get("collection") or "") != "stock_moves":
        return {"ok": True, "skipped": "not a stock_moves create"}
    record_id = str(request.get("record_id") or "")
    if not record_id:
        return {"ok": True, "skipped": "no record id"}

    base = _base_dir()
    try:
        move = object_records.get_collection_record("stock_moves", record_id, base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "move gone"}
    try:
        return compose_for_move(base, move)
    except Exception as exc:  # never break the dispatcher
        return {"ok": False, "error": str(exc)[:200]}
