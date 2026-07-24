"""action_resolve_bank_line -- book the lines that have no book counterpart.

POST {line_id, kind, ...} where kind is one of:

  fee       a bank charge      -> DR bank fees      / CR this bank's cash
  interest  interest earned    -> DR this bank's cash / CR interest income
  transfer  own-account move   -> DR/CR between two of your own cash accounts
                                  (payload counterpart_bank_account_id)
  nsf       a payment bounced  -> flips the payment (payload payment_id) and
                                  reverses its journal
  timing    in transit         -> no journal; carried as a reconciling item
  other     out of scope       -> no journal; explained in the memo

This is where the unmatched tail stops being a mystery. Fees and interest
are entries most small books simply never record, which is exactly why
their cash balance drifts from the bank's until someone gives up on
reconciling at all. Every journal goes through the shared composer
(object_finance.compose_posted_journal) stamped generated_from
"bank_lines/{id}", so resolving twice composes once.

Accounts come from where they belong: the CASH side is the bank account's
own fin_account_id (per-account, not a global setting), the expense/income
side from app_settings:
    reconcile.journal.fees_account
    reconcile.journal.interest_account

NSF note (and a repeat worth counting): event handlers dispatch on the HTTP
write path only, so a storage-level write from an object never reaches
system_books. This verb therefore composes the bounce reversal itself --
not by duplicating that logic but by calling the same shared composer with
the SAME provenance marker system_books uses ("payments/{id}:bounced"), so
whichever path runs first wins and the other is a no-op. action_apply_count
needed the same treatment; a third instance should buy a daemon pass that
dispatches handlers from the change log rather than a third workaround
(docs/logic-decisions.md #4).
"""

import os

import object_finance
import object_records

ACTOR = "action_resolve_bank_line"

JOURNAL_KINDS = ("fee", "interest", "transfer", "nsf")
NOTE_KINDS = ("timing", "other")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _get(base, collection, record_id):
    try:
        return object_records.get_collection_record(collection, record_id, base_dir=base)
    except Exception:
        return None


def _compose(base, *, marker, date, description, debit, credit, amount, owner_id,
             entity_id="", kind="standard"):
    return object_finance.compose_posted_journal(
        base, generated_from=marker, date=date, description=description,
        lines=[{"account_id": debit, "debit_cents": amount, "credit_cents": 0},
               {"account_id": credit, "debit_cents": 0, "credit_cents": amount}],
        owner_id=owner_id, entity_id=entity_id, kind=kind, actor=ACTOR)


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    is_admin = "admin" in (identity.get("roles") or [])
    if not user_id:
        return {"status": 403, "error": "Sign in to resolve a bank line."}

    line_id = str(request.get("line_id") or "").strip()
    kind = str(request.get("kind") or "").strip().lower()
    if not line_id or kind not in JOURNAL_KINDS + NOTE_KINDS:
        return {"status": 400,
                "error": f"line_id and kind are required; kind must be one of "
                         f"{', '.join(JOURNAL_KINDS + NOTE_KINDS)}."}

    base = _base_dir()
    line = _get(base, "bank_lines", line_id)
    if line is None:
        return {"status": 404, "error": f"Bank line not found: {line_id}"}
    if line.get("owner_id") and line["owner_id"] != user_id and not is_admin:
        return {"status": 403, "error": "That bank line belongs to someone else."}
    if line.get("match_status") == "resolved":
        return {"status": 409,
                "error": f"This line is already resolved as {line.get('resolved_as') or 'other'}."}

    amount = abs(int(line.get("amount_cents") or 0))
    date = str(line.get("posted_on") or "")[:10]
    memo = str(request.get("memo") or "").strip()
    changes = {"match_status": "resolved", "resolved_as": kind}
    journal = None

    if kind in JOURNAL_KINDS:
        account = _get(base, "bank_accounts", line.get("bank_account_id", "")) or {}
        cash = account.get("fin_account_id") or ""
        entity_id = line.get("entity_id") or account.get("entity_id") or ""
        if not cash:
            return {"status": 409,
                    "error": ("This bank account has no book account set (fin_account_id), "
                              "so there is nothing to post against. Set it on the bank "
                              "account first.")}
        if amount <= 0:
            return {"status": 409, "error": "This line has no amount to book."}

        if kind == "fee":
            fees = _setting(base, "reconcile.journal.fees_account")
            if not fees:
                return {"status": 409,
                        "error": "Set app_settings reconcile.journal.fees_account first."}
            journal = _compose(base, marker=f"bank_lines/{line_id}", date=date,
                               description=f"Bank fee: {line.get('description') or line_id}"
                                           + (f" ({memo})" if memo else ""),
                               debit=fees, credit=cash, amount=amount,
                               owner_id=line.get("owner_id"), entity_id=entity_id)

        elif kind == "interest":
            income = _setting(base, "reconcile.journal.interest_account")
            if not income:
                return {"status": 409,
                        "error": "Set app_settings reconcile.journal.interest_account first."}
            journal = _compose(base, marker=f"bank_lines/{line_id}", date=date,
                               description=f"Interest earned: {line.get('description') or line_id}",
                               debit=cash, credit=income, amount=amount,
                               owner_id=line.get("owner_id"), entity_id=entity_id)

        elif kind == "transfer":
            other_id = str(request.get("counterpart_bank_account_id") or "").strip()
            counterpart = _get(base, "bank_accounts", other_id) if other_id else None
            if counterpart is None or not counterpart.get("fin_account_id"):
                return {"status": 400,
                        "error": ("transfer needs counterpart_bank_account_id naming another "
                                  "of your bank accounts that has a book account set.")}
            if counterpart.get("owner_id") and counterpart["owner_id"] != user_id and not is_admin:
                return {"status": 403, "error": "That counterpart account belongs to someone else."}
            other_cash = counterpart["fin_account_id"]
            incoming = int(line.get("amount_cents") or 0) > 0
            journal = _compose(
                base, marker=f"bank_lines/{line_id}", date=date,
                description=f"Transfer between own accounts: {line.get('description') or line_id}",
                debit=cash if incoming else other_cash,
                credit=other_cash if incoming else cash,
                amount=amount, owner_id=line.get("owner_id"), entity_id=entity_id)

        else:  # nsf
            payment_id = str(request.get("payment_id")
                             or (line.get("matched_to") or "").split("/")[-1] or "").strip()
            payment = _get(base, "payments", payment_id) if payment_id else None
            if payment is None:
                return {"status": 400,
                        "error": ("nsf needs payment_id (or a line already matched to a "
                                  "payment) so the right payment can be bounced.")}
            if payment.get("status") != "bounced":
                try:
                    object_records.update_collection_record(
                        "payments", payment_id, {"status": "bounced"},
                        base_dir=base, actor=ACTOR)
                except Exception as exc:
                    return {"status": 409,
                            "error": f"Could not bounce payment {payment_id}: {str(exc)[:160]}"}
            basis = _setting(base, "payments.accounting_basis", "cash")
            counter = _setting(base, "payments.journal.receivable_account") if basis == "accrual" \
                else _setting(base, "payments.journal.revenue_account")
            book_cash = _setting(base, "payments.journal.cash_account") or cash
            if not counter:
                journal = {"ok": True, "skipped": "payment accounts unconfigured"}
            else:
                # Same marker system_books stamps, so whichever path composes
                # the reversal first, the other is a no-op.
                journal = _compose(
                    base, marker=f"payments/{payment_id}:bounced", date=date,
                    description=f"Bounce reversal of payment {payment_id} (bank NSF)",
                    debit=counter, credit=book_cash, amount=abs(int(payment.get("amount_cents") or 0)),
                    owner_id=payment.get("owner_id"), entity_id=entity_id, kind="reversing")
            changes["matched_to"] = f"payments/{payment_id}"

    # memo rides into the journal description (above); the line itself keeps
    # the matcher's suggestions block untouched -- what was proposed and why
    # stays readable after a human decided otherwise.
    try:
        object_records.update_collection_record(
            "bank_lines", line_id, changes, base_dir=base, actor=ACTOR)
    except Exception as exc:
        return {"status": 409, "error": f"Could not update the line: {str(exc)[:160]}"}

    return {"status": 200, "line_id": line_id, "resolved_as": kind,
            "journal": journal,
            "note": ("recorded as a reconciling item; no journal" if kind in NOTE_KINDS else None)}
