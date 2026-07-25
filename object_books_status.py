"""Readiness check for the books spine's silent-skip posture.

system_books (packages/app-payments/objects/system/books.py),
system_stock_books (packages/app-catalog/objects/system/stock_books.py), and
action_resolve_bank_line (packages/app-banking/objects/action/
resolve_bank_line.py) each compose real double-entry journals as a SOFT
dependency: if fin_journals/fin_journal_lines/fin_accounts aren't installed,
or the app_settings account mapping they need is blank, they return
{"ok": True, "skipped": "..."} and move on. That posture is deliberate --
payments, stock keeping, and reconciliation all work without a chart of
accounts, per docs/logic-decisions.md #6 (reactions never block the source
write). But the skip reason lives only in a dispatcher return value nobody
reads. On a demo box that's harmless: no books, no problem. The day real
money and real books show up, an unconfigured or half-configured mapping
means payments get recorded and simply never reach the ledger -- forever,
with no error, no log line a human would ever see, and no way to tell
"nothing to book yet" apart from "the books are broken."

This module does not change any of that behavior. It is a PURE, read-only
readiness check: given a data directory, would any installed composer
above silently skip right now, and specifically why? It mirrors each
composer's own soft-dependency logic (same app_settings keys, same "is the
collection even installed" gate) so its answer matches what the composer
would actually do, and adds one distinction the composers don't bother
making because it doesn't change their behavior but matters enormously to
a human auditing the setup:

  missing   -- the app_settings key is absent or blank. Obviously
              unconfigured; an operator glancing at settings would notice.
  dangling  -- the key has a value, but that value does not name a real
              fin_accounts row. This is WORSE than missing: app_settings
              *looks* configured, a casual read of the settings screen
              looks fine, and the composer still skips every single time
              (compose_posted_journal never gets there -- the composer's
              own "not cash or not counter" check only tests for blank,
              not for existence, per its docstring/comments).

Only areas that are actually installed are reported: a server with no
inventory package should never see an "inventory accounts unconfigured"
problem, because system_stock_books can never fire there in the first
place -- that would be noise, not signal. Nothing here writes, posts, or
mutates; it only reads app_settings, fin_accounts, and the presence of a
handful of collections, the same way object_permission_status.py reads
identity/policy state to answer "is enforcement safe to turn on" without
touching either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import object_records

# The books spine itself: without all three, every composer's own
# _books_ready()-style gate trips and nothing composes at all, regardless
# of app_settings. fin_accounts is included here (not just journals/lines)
# because "dangling" is meaningless without a chart of accounts to check
# against -- no fin_accounts collection means every configured id is
# unverifiable, which this module treats as "books not installed" rather
# than guessing.
_BOOKS_COLLECTIONS = ("fin_journals", "fin_journal_lines", "fin_accounts")

# One entry per composer above. "collection" is what gates whether the
# composer (and therefore this check) applies at all; "keys" list the
# checks in the order system_books/system_stock_books/resolve_bank_line
# read them (see each module's docstring for the exact app_settings names).
_PAYMENTS_COLLECTION = "payments"
_INVENTORY_COLLECTION = "stock_moves"
_BANKING_COLLECTION = "bank_lines"


def _collection_present(base_dir: Path | str, name: str) -> bool:
    """True if `name` is an installed, readable collection.

    Same posture as system_books._books_ready() / system_stock_books.
    _books_ready(): a package that was never installed (unknown schema)
    or a collection that can't be read for any other reason is treated
    as absent, never as an error -- this module must never raise just
    because a package is missing.
    """
    try:
        object_records.read_collection_records(name, base_dir=base_dir)
    except Exception:
        return False
    return True


def _load_settings(base_dir: Path | str) -> dict[str, str]:
    """Return {key: value} for every non-blank app_settings row.

    First match wins on a duplicate key, mirroring the composers' own
    _setting() helpers (they return on the first row that matches and has
    a value). An unreadable or absent app_settings collection folds to an
    empty map -- every setting then reads as "missing", which is exactly
    the state the composers would see too.
    """
    settings: dict[str, str] = {}
    try:
        rows = object_records.read_collection_records("app_settings", base_dir=base_dir)
    except Exception:
        return settings
    for row in rows:
        key = row.get("key")
        value = str(row.get("value") or "").strip()
        if key and value and key not in settings:
            settings[key] = value
    return settings


def _account_ids(base_dir: Path | str) -> set[str]:
    try:
        rows = object_records.read_collection_records("fin_accounts", base_dir=base_dir)
    except Exception:
        return set()
    return {row.get("id") for row in rows if row.get("id")}


def _check(
    *,
    check_id: str,
    area: str,
    setting_key: str,
    settings: dict[str, str],
    account_ids: set[str],
    impact: str,
) -> dict[str, Any]:
    """One setting -> one verdict, matching what its composer would do.

    status="ok" only when the setting has a value AND that value names a
    fin_accounts row that actually exists -- either half of that failing
    is exactly the condition under which the real composer returns
    {"skipped": "accounts unconfigured"} and posts nothing.
    """
    value = settings.get(setting_key, "")
    if not value:
        return {
            "id": check_id,
            "area": area,
            "setting": setting_key,
            "status": "missing",
            "detail": f"app_settings has no value for '{setting_key}'.",
            "impact": impact,
        }
    if value not in account_ids:
        return {
            "id": check_id,
            "area": area,
            "setting": setting_key,
            "status": "dangling",
            "detail": (
                f"'{setting_key}' is set to '{value}', but no fin_accounts "
                f"row with that id exists."
            ),
            "impact": impact,
        }
    return {
        "id": check_id,
        "area": area,
        "setting": setting_key,
        "status": "ok",
        "detail": f"'{setting_key}' -> '{value}' (fin_accounts row exists).",
        "impact": "",
    }


def _payments_checks(base_dir, settings, account_ids) -> list[dict[str, Any]]:
    """Mirrors system_books.py's account reads exactly.

    cash_account and revenue_account are read on EVERY basis (revenue is
    the cash-basis counter account and also one side of the accrual
    issue entry); receivable_account is only read -- and only required --
    on accrual basis, per system_books.py's `basis == "accrual"` branch.
    """
    basis = settings.get("payments.accounting_basis", "cash") or "cash"
    checks = [
        _check(
            check_id="payments.cash_account",
            area="payments",
            setting_key="payments.journal.cash_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "payments and refunds are recorded but never reach the "
                "ledger: system_books skips every payment, refund, and "
                "bounce-reversal entry silently."
            ),
        ),
        _check(
            check_id="payments.revenue_account",
            area="payments",
            setting_key="payments.journal.revenue_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "payments have no revenue side to post against, so "
                "system_books skips the entry instead of posting an "
                "unbalanced one"
                + ("; accrual invoice-issue entries skip too" if basis == "accrual" else "")
                + "."
            ),
        ),
    ]
    if basis == "accrual":
        checks.append(_check(
            check_id="payments.receivable_account",
            area="payments",
            setting_key="payments.journal.receivable_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "on accrual basis, invoice issuance, payment, and refund "
                "entries all need the receivable account: system_books "
                "skips all three silently until it exists."
            ),
        ))
    return checks


def _inventory_checks(settings, account_ids) -> list[dict[str, Any]]:
    """Mirrors system_stock_books.py's two required account reads.

    Per-reason overrides (inventory.journal.{reason}_account) are
    optional -- they fall back to shrinkage_account when unset, so an
    absent override is not a problem. A CONFIGURED override that points
    at a nonexistent account is exactly the dangling trap this module
    exists to catch, so those are checked too, but only when set.
    """
    checks = [
        _check(
            check_id="inventory.inventory_account",
            area="inventory",
            setting_key="inventory.journal.inventory_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "inventory losses and count variances are recorded in "
                "stock_moves but never reach the ledger: "
                "system_stock_books skips every loss and variance entry "
                "silently."
            ),
        ),
        _check(
            check_id="inventory.shrinkage_account",
            area="inventory",
            setting_key="inventory.journal.shrinkage_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "loss and count-variance entries have no expense/mirror "
                "side to post against, so system_stock_books skips them "
                "silently instead of posting an unbalanced entry."
            ),
        ),
    ]
    return checks


# LOSS_REASONS is duplicated from system_stock_books.py rather than
# imported: this module must stay importable (and correct) even on a
# server where packages/app-catalog was never installed, and importing a
# package object module for one constant would tie this pure status
# module's import-time behavior to package install state. If that list
# ever changes there, it should change here too.
_LOSS_REASONS = ("waste", "breakage", "theft", "expiry", "damage", "disaster")


def _inventory_reason_overrides(settings, account_ids) -> list[dict[str, Any]]:
    checks = []
    for reason in _LOSS_REASONS:
        setting_key = f"inventory.journal.{reason}_account"
        value = settings.get(setting_key, "")
        if not value:
            continue  # unset -> falls back to shrinkage_account; not a problem
        checks.append(_check(
            check_id=f"inventory.{reason}_account",
            area="inventory",
            setting_key=setting_key,
            settings=settings,
            account_ids=account_ids,
            impact=(
                f"'{reason}' losses are meant to route to their own "
                f"account (e.g. for an insurance claim) but the "
                f"configured account does not exist, so "
                f"system_stock_books skips those entries silently "
                f"instead of falling back."
            ),
        ))
    return checks


def _banking_checks(settings, account_ids) -> list[dict[str, Any]]:
    """Mirrors action_resolve_bank_line.py's two account_settings reads.

    Unlike the two event-handler composers, resolve_bank_line is a
    user-facing action: today it returns a 409 error at the moment
    someone tries to resolve a fee/interest line, rather than skipping
    silently. That is louder than the other two, but it is still a
    surprise sprung at the worst possible time (mid-reconciliation)
    instead of a known gap surfaced ahead of time -- which is the gap
    this readiness check closes.
    """
    return [
        _check(
            check_id="banking.fees_account",
            area="banking",
            setting_key="reconcile.journal.fees_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "resolving a bank line as a fee fails at the moment a "
                "user tries it (a 409 error), instead of the account "
                "being ready before anyone hits reconciliation."
            ),
        ),
        _check(
            check_id="banking.interest_account",
            area="banking",
            setting_key="reconcile.journal.interest_account",
            settings=settings,
            account_ids=account_ids,
            impact=(
                "resolving a bank line as interest earned fails at the "
                "moment a user tries it (a 409 error), instead of the "
                "account being ready before anyone hits reconciliation."
            ),
        ),
    ]


def _not_installed_status(base_dir: Path | str, missing: list[str]) -> dict[str, Any]:
    return {
        "ready": False,
        "collections": {
            "fin_journals": _collection_present(base_dir, "fin_journals"),
            "fin_journal_lines": _collection_present(base_dir, "fin_journal_lines"),
            "fin_accounts": _collection_present(base_dir, "fin_accounts"),
            "app_settings": _collection_present(base_dir, "app_settings"),
        },
        "checks": [],
        "problems": [],
        "summary": (
            "No accounting books are installed (missing: "
            + ", ".join(missing)
            + "); every composer that tries to post a journal "
              "(system_books, system_stock_books, action_resolve_bank_line) "
              "will skip silently and nothing will reach the ledger."
        ),
    }


def books_status(*, base_dir: Path | str) -> dict[str, Any]:
    """Would any installed composer silently skip its journal right now?

    Returns a dict shaped for both a human report and a machine gate:
      ready        -- True only when every check for every INSTALLED area
                       came back "ok". A server with no payments/
                       inventory/banking apps installed is vacuously
                       ready (nothing composes journals, so nothing can
                       silently skip).
      collections  -- presence of the four collections every check below
                       depends on.
      checks       -- every check that was evaluated (installed areas
                       only), "ok" included.
      problems     -- the subset of checks that is not "ok" -- the
                       actionable list.
      summary      -- one sentence a human can read without opening the
                       dict.

    Never raises: every read goes through _collection_present /
    _load_settings / _account_ids, which fold any failure (unknown
    collection, unreadable file, missing package) to an empty/absent
    result rather than an exception, the same defensive posture every
    composer this module inspects already uses.
    """
    collections = {
        name: _collection_present(base_dir, name) for name in _BOOKS_COLLECTIONS
    }
    collections["app_settings"] = _collection_present(base_dir, "app_settings")

    books_installed = all(collections[name] for name in _BOOKS_COLLECTIONS)
    if not books_installed:
        missing = [name for name in _BOOKS_COLLECTIONS if not collections[name]]
        return _not_installed_status(base_dir, missing)

    settings = _load_settings(base_dir)
    account_ids = _account_ids(base_dir)

    checks: list[dict[str, Any]] = []
    if _collection_present(base_dir, _PAYMENTS_COLLECTION):
        checks.extend(_payments_checks(base_dir, settings, account_ids))
    if _collection_present(base_dir, _INVENTORY_COLLECTION):
        checks.extend(_inventory_checks(settings, account_ids))
        checks.extend(_inventory_reason_overrides(settings, account_ids))
    if _collection_present(base_dir, _BANKING_COLLECTION):
        checks.extend(_banking_checks(settings, account_ids))

    problems = [c for c in checks if c["status"] != "ok"]
    ready = not problems

    if not checks:
        summary = (
            "Books are installed but no app that posts journals "
            "(payments, inventory, banking) is installed; nothing "
            "composes entries yet, so nothing can silently skip."
        )
    elif ready:
        summary = (
            f"Books are ready: all {len(checks)} checked ledger account "
            f"setting(s) across the installed areas are configured and "
            f"point at real accounts."
        )
    else:
        named = "; ".join(f"{p['id']} ({p['status']})" for p in problems)
        summary = (
            f"Books are NOT ready: {len(problems)} of {len(checks)} "
            f"checked ledger account setting(s) will cause silent gaps "
            f"({named})."
        )

    return {
        "ready": ready,
        "collections": collections,
        "checks": checks,
        "problems": problems,
        "summary": summary,
    }


def is_ready(*, base_dir: Path | str) -> bool:
    """Convenience: books_status(base_dir=base_dir)["ready"]."""
    return books_status(base_dir=base_dir)["ready"]
