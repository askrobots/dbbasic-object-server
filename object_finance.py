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

compose_posted_journal() joined this module later, at the doctrine-#4
extraction threshold (docs/logic-decisions.md): once system_books
(payments/refunds/bounces/issues), fin_recurring_runner (adjusting
entries), and action_reverse_journal (mirrors) had each hand-rolled the
same create-journal -> create-lines -> verify -> post sequence, the third
composer (system_stock_books, inventory losses) triggered the extraction.
Callers own POLICY (which accounts, what amount, when to compose); this
function owns MECHANICS (idempotency by provenance, draft -> lines ->
re-read -> verify balance -> post). Composed entries are storage-level
writes that bypass the HTTP-only balance hook, so the re-verify here is
the gate for every generated journal; the hook remains the gate for
human entries.
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Iterable

import object_ids
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


_UNREADABLE = object()  # sentinel: could not scan fin_journals for a marker


def _scan_provenance(base_dir, generated_from):
    try:
        for row in object_records.read_collection_records("fin_journals", base_dir=base_dir):
            if row.get("generated_from") == generated_from:
                return row
    except Exception:
        return _UNREADABLE
    return None


def find_journal_by_provenance(base_dir, generated_from):
    """The fin_journals row stamped with this generated_from, else None.

    Unreadable books also return None -- callers that need the
    do-not-risk-a-duplicate posture get it inside compose_posted_journal,
    which distinguishes 'absent' from 'could not tell'.
    """
    row = _scan_provenance(base_dir, generated_from)
    return None if row is _UNREADABLE else row


def compose_posted_journal(base_dir, *, generated_from, date, description,
                           lines, owner_id, entity_id="", kind="standard",
                           actor="object_finance", post=True):
    """Compose one journal from line specs and post it when balanced.

    lines: iterable of {"account_id", "debit_cents", "credit_cents",
    optional "memo", optional "entity_id" (defaults to the journal's)}.
    Returns {"ok": True, ...} shapes:
      already composed -> {"ok", "skipped", "journal_id", "posted"}
      nothing to book  -> {"ok", "skipped"}
      composed         -> {"ok", "journal_id", "posted", maybe "note"}
    and {"ok": False, "error"} only for unusable line amounts. post=False
    composes but always leaves the draft (recurring templates without
    auto_post).
    """
    normalized, debits, credits = [], 0, 0
    for spec in lines or []:
        if not isinstance(spec, dict):
            continue
        try:
            dr = int(spec.get("debit_cents") or 0)
            cr = int(spec.get("credit_cents") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "line amounts must be integer cents"}
        if dr < 0 or cr < 0:
            return {"ok": False, "error": "line amounts must not be negative"}
        debits += dr
        credits += cr
        normalized.append({
            "account_id": str(spec.get("account_id") or ""),
            "debit_cents": str(dr),
            "credit_cents": str(cr),
            "memo": str(spec.get("memo") or ""),
            "entity_id": str(spec.get("entity_id") or entity_id or ""),
        })

    if generated_from:
        existing = _scan_provenance(base_dir, generated_from)
        if existing is _UNREADABLE:
            return {"ok": True, "skipped": "books unreadable; not risking a duplicate"}
        if existing is not None:
            return {"ok": True, "skipped": f"already composed: {generated_from}",
                    "journal_id": existing.get("id"),
                    "posted": existing.get("status") == "posted"}

    if not normalized or (debits == 0 and credits == 0):
        return {"ok": True, "skipped": "zero amount"}

    journal_id = object_ids.new_uuid4()
    journal = {
        "id": journal_id,
        "date": date or "",
        "description": description or "",
        "status": "draft",
        "generated_from": generated_from or "",
        "kind": kind,
        "owner_id": owner_id or "",
    }
    if entity_id:
        journal["entity_id"] = entity_id
    object_records.create_collection_record(
        "fin_journals", journal, base_dir=base_dir, actor=actor,
        allow_computed_submission=False,
    )
    for spec in normalized:
        line = {
            "id": object_ids.new_uuid4(),
            "journal_id": journal_id,
            "account_id": spec["account_id"],
            "debit_cents": spec["debit_cents"],
            "credit_cents": spec["credit_cents"],
            "owner_id": owner_id or "",
        }
        if spec["memo"]:
            line["memo"] = spec["memo"]
        if spec["entity_id"]:
            line["entity_id"] = spec["entity_id"]
        object_records.create_collection_record(
            "fin_journal_lines", line, base_dir=base_dir, actor=actor
        )

    # Balanced by construction; verify by RE-READING what actually landed
    # before posting (a failed line write must never post a lopsided entry).
    landed = journal_totals(journal_id, base_dir=base_dir)
    if (post and landed["is_balanced"] and landed["total_debits_cents"] > 0):
        object_records.update_collection_record(
            "fin_journals", journal_id, {"status": "posted"},
            base_dir=base_dir, actor=actor,
        )
        return {"ok": True, "journal_id": journal_id, "posted": True}
    note = ("left draft: did not balance" if not landed["is_balanced"]
            else "left draft: post not requested")
    return {"ok": True, "journal_id": journal_id, "posted": False, "note": note}
