"""system_wallet_books -- wallet money reaching the double-entry books.

The prepaid twin of app-payments' system_books. That composer books
payments, refunds and invoices; nothing booked the WALLET, so a server
that charged real money for template runs showed zero revenue on the
profit and loss and neither the cash nor the customer liability on the
balance sheet. Every movement was recorded operationally in
wallet_entries and none of it reached fin_journals.

Placement per docs/logic-decisions.md #6: a REACTION (post-commit,
best-effort, never blocks or fails the source write), so an event
handler rather than a hook. Billing keeps working with no books
installed; the books simply learn nothing.

The accounting policy is NOT here -- object_billing.wallet_posting owns
it, pure and exhaustively testable without a data directory. This module
resolves settings, refetches the record, and posts. The one thing worth
repeating from there: **a top-up is not revenue.** Money taken before
the service is rendered is money owed back, and it becomes revenue only
when the work is done.

Account mapping is configuration (app_settings):
  billing.journal.cash_account            fin_accounts id (asset)
  billing.journal.customer_funds_account  fin_accounts id (LIABILITY)
  billing.journal.revenue_account         fin_accounts id
  billing.journal.promo_expense_account   fin_accounts id (optional)
  billing.journal.adjustment_account      fin_accounts id (optional)

Idempotency by provenance, the house rule: every composed journal stamps
generated_from "wallet_entries/{id}", so a replayed event composes
nothing. Entries are balanced by construction (one amount, two lines).
"""

import os

import object_billing
import object_finance
import object_records

HANDLES = [
    "wallet_entries.record.created",
]

ACTOR = "system_wallet_books"

SETTING_PREFIX = "billing.journal."


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


def _accounts(base):
    return {
        name: _setting(base, f"{SETTING_PREFIX}{name}_account")
        for name in object_billing.WALLET_ACCOUNT_KEYS
    }


def EVENT(request):
    """request = {"event","collection","record_id","action"} (no body --
    refetch post-commit). Best-effort: every failure returns a reason,
    never raises into the dispatcher."""
    record_id = str(request.get("record_id") or "")
    action = str(request.get("action") or "")
    action = {"create": "created", "update": "updated",
              "delete": "deleted"}.get(action, action)
    if not record_id:
        return {"ok": True, "skipped": "no record id"}
    if action != "created":
        # Entries are an append-only ledger; an edit to one is not a
        # second economic event, and composing for it would double-book.
        return {"ok": True, "skipped": "only new entries compose"}

    base = _base_dir()
    if not _books_ready(base):
        return {"ok": True, "skipped": "books not installed (fin_journals absent)"}

    try:
        entry = object_records.get_collection_record(
            "wallet_entries", record_id, base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "entry gone"}
    if entry is None:
        return {"ok": True, "skipped": "entry gone"}

    try:
        posting = object_billing.wallet_posting(
            entry.get("kind"), entry.get("amount_minor"), _accounts(base))
        if "skip" in posting:
            return {"ok": True, "skipped": posting["skip"]}

        wallet = {}
        try:
            wallet = object_records.get_collection_record(
                "wallets", str(entry.get("wallet_id") or ""), base_dir=base) or {}
        except Exception:
            wallet = {}

        description = (str(entry.get("description") or "").strip()
                       or f"Wallet {entry.get('kind')}")
        return object_finance.compose_posted_journal(
            base,
            generated_from=f"wallet_entries/{record_id}",
            date=str(entry.get("created_at") or "")[:10],
            description=description,
            lines=[
                {"account_id": posting["debit"],
                 "debit_cents": posting["amount_minor"], "credit_cents": 0},
                {"account_id": posting["credit"],
                 "debit_cents": 0, "credit_cents": posting["amount_minor"]},
            ],
            owner_id=entry.get("owner_id") or wallet.get("owner_id"),
            entity_id=wallet.get("entity_id", ""),
            actor=ACTOR,
        )
    except Exception as exc:  # never break the dispatcher
        return {"ok": False, "error": str(exc)[:200]}
