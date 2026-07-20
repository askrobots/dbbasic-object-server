"""Pure computed helpers over packages/app-finance's collections.

Mirrors the predecessor system's own design for its finance app (reconciled
against a private predecessor-system audit, not part of this repo): journal
totals and the trial balance were COMPUTED PROPERTIES, folded from journal
lines on read, never stored or enforced. This module keeps that exact
posture:

- journal_totals() folds fin_journal_lines for one journal into
  (total_debits_cents, total_credits_cents, is_balanced). Nothing writes
  these numbers back onto the journal record -- there is no totals-
  stamping HANDLES handler in this package, unlike app-invoices'
  invoice_totals or app-orders' order_totals. That absence is
  deliberate: the source's own posting flow never enforced or cached a
  balance either (posting is a bare draft->posted status flip -- see
  packages/app-finance/schemas/fin_journals.json's status field help and
  dbbasic-package.json's Deferred list). A future slice could add a
  HANDLES handler that stamps a balance-check warning, but that would be
  new behavior the source never had, so it stays out of this migration.

- trial_balance() is the one report this v1 slice ships (the predecessor
  system's own reports "filter posted lines", per the same reconciled
  source audit). Profit & loss, balance sheet, and cash flow are the same
  fold shape
  over the same posted-lines data and are DEFERRED -- not built here
  (see dbbasic-package.json's description).

Both functions are read-only folds over object_records.read_collection_
records(); base_dir is the caller's responsibility, same convention as
packages/app-invoices/objects/system/invoice_totals.py's own _data_dir()
helper (this module is a plain library, not a DBBASIC object, so it has
no request payload to read an identity or base_dir override from -- see
this module's own callers for how each resolves base_dir).

Integer-cents arithmetic only, per 00-doctrine-and-contract.md: money is
always a whole number of cents, parsed defensively (blank/None -> 0)
since a hand-edited TSV row or a partially-filled form draft can leave a
*_cents field blank.
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Iterable

import object_records
from object_versions import DEFAULT_DATA_DIR

# Chart-of-accounts display order for trial_balance rows: the conventional
# accounting statement order (assets, liabilities, equity, then income,
# expense), matching fin_accounts.json's account_type enum order. Any
# account_type not in this map (should not happen -- the schema enum is
# closed) sorts last rather than raising.
_ACCOUNT_TYPE_ORDER = {
    "asset": 0,
    "liability": 1,
    "equity": 2,
    "income": 3,
    "expense": 4,
}

_STATUS_POSTED = "posted"


def _to_cents(value: Any) -> int:
    """Parse a stored numeric string as an integer number of cents.

    Decimal (never a bare float) so a stray fractional value in a
    hand-edited row can't introduce binary-float rounding error before
    the floor -- same discipline invoice_totals.py's own _to_int uses.
    Blank/None -> 0.
    """
    text = str(value or "").strip()
    if not text:
        return 0
    return int(Decimal(text).to_integral_value(rounding=ROUND_FLOOR))


def journal_totals(
    journal_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Fold fin_journal_lines for one journal into computed totals.

    Returns {"total_debits_cents", "total_credits_cents", "is_balanced"}.
    is_balanced is total_debits_cents == total_credits_cents -- true for
    a journal with zero lines (0 == 0), same as any other computed-
    property equality check. Nothing here checks whether journal_id
    actually refers to an existing fin_journals record: a dangling or
    unknown id simply folds zero matching lines and returns a balanced
    zero total, the same graceful-empty behavior a fresh draft journal
    (no lines yet) gets.
    """
    lines = object_records.read_collection_records("fin_journal_lines", base_dir=base_dir, roots=roots)

    total_debits_cents = 0
    total_credits_cents = 0
    for line in lines:
        if line.get("journal_id") != journal_id:
            continue
        total_debits_cents += _to_cents(line.get("debit_cents"))
        total_credits_cents += _to_cents(line.get("credit_cents"))

    return {
        "total_debits_cents": total_debits_cents,
        "total_credits_cents": total_credits_cents,
        "is_balanced": total_debits_cents == total_credits_cents,
    }


def trial_balance(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    roots: Iterable[Path] | None = None,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Per-account debit/credit totals over POSTED journals only.

    Matches the source's own report semantics (reconciled against a
    private predecessor-system audit, not part of this repo: reports
    filter posted lines) -- a draft journal's lines never contribute,
    with no separate "is this journal balanced" check
    at all (that would be enforcement; the source never added it here
    either, see this module's docstring).

    owner, when given, restricts the fold to journals owned by that
    user (fin_journals.owner_id == owner) before any line is summed.
    This is NOT a convenience filter -- callers that read a specific
    user's financial data (e.g. packages/app-finance/objects/site/
    trial_balance.py) MUST pass owner=<that user's id>, because this
    function reads collections directly via object_records and is not
    itself subject to the row_filter owner_id=$user_id permission rule
    that packages/app-finance/permissions/rules.json enforces on the
    HTTP /collections/* API. Passing owner=None folds every owner's
    posted journals together and is only appropriate for an operator/
    admin-level caller (nothing in this package calls it that way yet).

    Returns a list of {"account_id", "account_name", "account_code",
    "account_type", "debit_total_cents", "credit_total_cents"} rows, one
    per account that has at least one posted line -- accounts with zero
    posted activity are omitted rather than listed at zero, since this
    is a fold over lines, not an enumeration of the chart of accounts.
    Sorted in conventional statement order (asset, liability, equity,
    income, expense), then by account code/name within each type.
    """
    journals = object_records.read_collection_records("fin_journals", base_dir=base_dir, roots=roots)
    posted_journal_ids = {
        journal.get("id")
        for journal in journals
        if journal.get("status") == _STATUS_POSTED
        and (owner is None or journal.get("owner_id") == owner)
    }
    if not posted_journal_ids:
        return []

    lines = object_records.read_collection_records("fin_journal_lines", base_dir=base_dir, roots=roots)
    totals_by_account: dict[str, list[int]] = {}
    for line in lines:
        if line.get("journal_id") not in posted_journal_ids:
            continue
        account_id = line.get("account_id")
        if not account_id:
            continue
        bucket = totals_by_account.setdefault(account_id, [0, 0])
        bucket[0] += _to_cents(line.get("debit_cents"))
        bucket[1] += _to_cents(line.get("credit_cents"))

    if not totals_by_account:
        return []

    accounts_by_id = {
        account.get("id"): account
        for account in object_records.read_collection_records("fin_accounts", base_dir=base_dir, roots=roots)
    }

    rows = []
    for account_id, (debit_total_cents, credit_total_cents) in totals_by_account.items():
        account = accounts_by_id.get(account_id, {})
        rows.append({
            "account_id": account_id,
            "account_name": account.get("name", ""),
            "account_code": account.get("code", ""),
            "account_type": account.get("account_type", ""),
            "debit_total_cents": debit_total_cents,
            "credit_total_cents": credit_total_cents,
        })

    rows.sort(key=lambda row: (
        _ACCOUNT_TYPE_ORDER.get(row["account_type"], 99),
        row["account_code"],
        row["account_name"],
    ))
    return rows
