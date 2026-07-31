"""invoice_totals -- HANDLES invoice_lines writes; recomputes invoice totals.

Registered as a Phase-5a event handler (object_handlers.py,
docs/event-hooks-decisions.md, docs/upgrade-and-customization.md Rule 4).
The platform's post-commit dispatcher (object_server.py's
_dispatch_event_handlers) executes this object's EVENT method whenever a
client-facing write to invoice_lines commits, but only when the operator
has DBBASIC_ENABLE_EVENT_HANDLERS set. With the flag unset (the default),
invoice_lines/invoices writes still succeed -- they just stop getting
fresher totals until a line is touched again after the flag is turned on.
Same posture plan/vocabulary/20-invoice-spec.md documents for itself.

Integer-cents arithmetic only, per 00-doctrine-and-contract.md /
20-invoice-spec.md: money is always a whole number of cents. quantity is
the one deliberate exception to "never a float" in this package, because
it is a count/measure (e.g. 2.5 hours), not currency. Line/tax math uses
Decimal (never a bare Python float) so a fractional quantity can never
introduce binary-float rounding error, then floors to an integer number
of cents -- never rounds up ("exact arithmetic, floor not round").

Why these fields are NOT schema `read_only` despite the task brief asking
for it: verified against object_records.py before writing this file.
`update_collection_record` has no `preserve_read_only` escape hatch at
all (only `create_collection_record` does, and even there defaults are
never stamped onto a read_only field -- see `_apply_schema_defaults`), so
a genuinely `read_only` field can never be written again after creation,
by ANY caller, including this handler's own follow-up write. Marking
subtotal_cents/tax_cents/total_cents/balance_due_cents/line_total_cents/
line_tax_cents read_only would therefore let this handler compute the
correct value once and then permanently fail to ever write it again.
Instead this package follows plan/vocabulary/20-invoice-spec.md's own
already-reasoned fallback posture for exactly this situation ("Permissions
Posture"): these stay ordinary owner-writable fields, simply omitted from
forms.default so the generated form never renders them as editable
inputs (confirmed against packages/app-theme/objects/site/form.py's own
`skip()`, which also independently omits any read_only field --
belt-and-suspenders, not the primary protection); any actual tamper is
fully visible in record_changes (before/after, actor, timestamp)
regardless. Detection, not prevention -- the same backstop the platform
already relies on elsewhere.

Idempotence: every write below is compared against the already-stored
value first and skipped if unchanged. This platform's dispatch lives in
object_server.py's HTTP request handlers, not inside object_records.py
itself (checked: object_records.py has zero references to event
dispatch), so a handler calling object_records directly -- as this one
does -- does not currently re-enter the dispatcher and cannot recurse.
The guard is kept anyway: it is cheap, it avoids a pointless no-op write
and record_change entry on every re-run, and it is the correct discipline
regardless of whether a future refactor moves dispatch closer to the
write path (plan/vocabulary/20-invoice-spec.md calls this out as an open
question worth a shared helper one day).
"""
from __future__ import annotations

import os
from decimal import ROUND_FLOOR, Decimal

import object_record_changes
import object_finance
import object_lines
import object_records

HANDLES = [
    "invoice_lines.record.created",
    "invoice_lines.record.updated",
    "invoice_lines.record.deleted",
]

ACTOR = "invoice_totals"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def _data_dir() -> str:
    # Mirrors object_handlers.handlers_enabled()'s own note: standalone,
    # reads os.environ directly rather than depending on object_server.
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _to_int(value):
    """The shared defensive ledger parser (object_finance.to_cents).

    This was a byte-identical private copy in three modules. One
    shared floor-parser is one place to be right about rounding, and
    a *_cents field left blank by a half-filled draft still reads 0
    rather than raising.
    """
    return object_finance.to_cents(value)


def _line_amounts(line: dict) -> tuple[int, int]:
    """Return (line_total_cents, line_tax_cents) for one invoice_lines row.

    The arithmetic lives in object_lines, shared with app-orders: the two
    copies were near enough identical that one was the other plus
    modifier_cents, and both now subtract a line discount. See that
    module for the order of operations and why tax is charged on the
    discounted amount.
    """
    amounts = object_lines.line_amounts(line)
    return amounts["line_total_cents"], amounts["line_tax_cents"]


def _line_discount(line: dict) -> int:
    return object_lines.line_amounts(line)["discount_cents"]


def _resolve_invoice_id(record_id: str, action: str, base_dir: str) -> str | None:
    """Return the invoice_id an invoice_lines write belongs to, or None.

    create/update: the line record still exists -- read it directly.
    delete: the line is already gone by the time this fires (post-commit
    dispatch runs after the write), so recover its last known invoice_id
    from record_changes' own "before" snapshot -- the same "read the
    change log directly" shape object_server.py's own
    _record_change_for_publish already uses to recover a just-committed
    write for its own downstream publish step.
    """
    if action == "deleted":
        try:
            payload = object_record_changes.list_record_changes(
                "invoice_lines", record_id=record_id, limit=1, base_dir=base_dir
            )
        except (OSError, ValueError):
            return None
        changes = payload.get("changes") or []
        if not changes:
            return None
        before = changes[0].get("before") or {}
        invoice_id = before.get("invoice_id")
        return invoice_id or None

    try:
        line = object_records.get_collection_record("invoice_lines", record_id, base_dir=base_dir)
    except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
        return None
    return line.get("invoice_id") or None


def _sync_triggering_line(record_id: str, action: str, base_dir: str) -> None:
    """Recompute + write back the one line that triggered this dispatch.

    No-op for delete (nothing left to write) and for a line already
    carrying the correct stamped values (the idempotence guard).
    """
    if action == "deleted":
        return
    try:
        line = object_records.get_collection_record("invoice_lines", record_id, base_dir=base_dir)
    except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
        return

    line_total_cents, line_tax_cents = _line_amounts(line)
    line_discount_cents = _line_discount(line)
    # The discount is STORED beside the total it produced, not left to be
    # re-derived: an invoice has to be able to print "10% off  -2.00"
    # next to the line, and a reader who only has the net total cannot
    # say what was taken off or whether anything was.
    if line_total_cents == _to_int(line.get("line_total_cents")) and \
            line_tax_cents == _to_int(line.get("line_tax_cents")) and \
            line_discount_cents == _to_int(line.get("line_discount_cents")):
        return

    try:
        object_records.update_collection_record(
            "invoice_lines",
            record_id,
            {"line_total_cents": str(line_total_cents),
             "line_tax_cents": str(line_tax_cents),
             "line_discount_cents": str(line_discount_cents)},
            base_dir=base_dir,
            actor=ACTOR,
        )
    except (object_records.RecordNotFoundError, object_records.InvalidRecordPayloadError,
            object_records.InvalidRecordIdError):
        pass


def _recompute_invoice(invoice_id: str, base_dir: str) -> bool:
    """Re-sum every line on invoice_id and stamp the parent invoice.

    Returns True if the invoice row actually changed. A full scan of
    invoice_lines is the right cost/complexity trade at this collection's
    expected scale (a handful of lines per invoice, per
    plan/vocabulary/20-invoice-spec.md) -- no index needed.
    """
    try:
        invoice = object_records.get_collection_record("invoices", invoice_id, base_dir=base_dir)
    except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
        return False

    lines = object_records.read_collection_records("invoice_lines", base_dir=base_dir)

    subtotal_cents = 0
    line_tax_cents_total = 0
    any_line_states_a_rate = False
    for line in lines:
        if line.get("invoice_id") != invoice_id:
            continue
        line_total_cents, line_tax_cents = _line_amounts(line)
        subtotal_cents += line_total_cents
        line_tax_cents_total += line_tax_cents
        if _to_int(line.get("tax_rate_bps")):
            any_line_states_a_rate = True

    # A fold must not replace a value it cannot reproduce.
    #
    # Tax can legitimately live in either of two places: on the LINES (a
    # rate per line, summed here) or on the DOCUMENT (computed once over
    # the whole taxable sale, which is how action_checkout does it, so that
    # four fifty-cent lines at 5% owe 10c rather than the 12c four
    # separately-rounded lines come to). This handler can only see the
    # first. When no line states a rate, a recomputed tax of zero is not a
    # finding -- it is this object looking in the wrong place -- and
    # writing it destroys real money.
    #
    # It did. On the live shop a customer was quoted 2116 at checkout
    # (1400 goods + 600 postage + 116 tax), and by the time the invoice
    # lines had been written this pass had restated the invoice to 2000
    # with tax 0. The tests did not catch it because they never ran with
    # event handlers enabled; the shop did, and the invoice disagreed with
    # the price the customer had already agreed to.
    if any_line_states_a_rate:
        tax_cents = line_tax_cents_total
    else:
        tax_cents = _to_int(invoice.get("tax_cents"))

    total_cents = subtotal_cents + tax_cents
    # balance_due_cents / amount_paid_cents are formula fields now (invoices
    # v2, app-payments): the storage layer derives them from total and the
    # payments/refunds rollups on every write. Writing them here would be
    # rejected as a computed-field submission -- this object owns only the
    # line-derived totals until those, too, graduate to rollups.

    changes = {}
    if _to_int(invoice.get("subtotal_cents")) != subtotal_cents:
        changes["subtotal_cents"] = str(subtotal_cents)
    if _to_int(invoice.get("tax_cents")) != tax_cents:
        changes["tax_cents"] = str(tax_cents)
    if _to_int(invoice.get("total_cents")) != total_cents:
        changes["total_cents"] = str(total_cents)

    if not changes:
        return False

    try:
        object_records.update_collection_record(
            "invoices", invoice_id, changes, base_dir=base_dir, actor=ACTOR
        )
    except (object_records.RecordNotFoundError, object_records.InvalidRecordPayloadError,
            object_records.InvalidRecordIdError):
        return False
    return True


def EVENT(request):
    """Dispatch entry point. request = {"event","collection","record_id","action"}.

    Post-commit, best-effort: every exception is caught here so a bad row
    or a race with a concurrent delete never surfaces as a failed write.
    object_server.py's own dispatcher already wraps this call in a
    try/except for the same reason -- this is defense in depth, not
    redundant.
    """
    collection = str(request.get("collection") or "")
    record_id = str(request.get("record_id") or "")
    action = str(request.get("action") or "")
    if collection != "invoice_lines" or not record_id:
        return {"ok": True, "skipped": "not an invoice_lines event"}

    base_dir = _data_dir()
    try:
        invoice_id = _resolve_invoice_id(record_id, action, base_dir)
        if not invoice_id:
            _logger.info("invoice_totals: no invoice_id resolved", record_id=record_id, action=action)
            return {"ok": True, "skipped": "no invoice_id"}
        _sync_triggering_line(record_id, action, base_dir)
        changed = _recompute_invoice(invoice_id, base_dir)
    except Exception as exc:  # never break the triggering write
        _logger.warning("invoice_totals recompute failed", error=str(exc))
        return {"ok": False, "error": str(exc)}

    _logger.info("invoice_totals recomputed", invoice_id=invoice_id, changed=changed)
    return {"ok": True, "invoice_id": invoice_id, "changed": changed}
