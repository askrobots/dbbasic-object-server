"""Bank-statement import primitives: parsing, hashing, and the self-checks.

Pure helpers over packages/app-banking's collections, same posture as
object_stock.py (derived stock levels) and object_finance.py (journal
folds): a plain importable library, not a registered object.

The design point (plan/bank-import-reconciliation-spec.md): **import is not
ETL, it is evidence handling.** A bank statement is the only record in the
system authored by an independent third party -- that independence IS the
anti-fraud control. So imported lines are append-only, keep their original
text verbatim in `raw`, and are never hand-edited; parsed fields are a VIEW
of raw, never a replacement.

"Are the imports good?" has a real answer because a statement carries its
own checksum. Three gates live here:

- **tie_out**: opening + sum(lines) == closing. A truncated or tampered
  file fails arithmetic.
- **continuity**: this statement's opening equals the account's previous
  closing. Gaps and edited files surface as a mismatch.
- **dedup**: external_id (OFX FITID) when the format has one, else
  line_hash over (date, amount, description, ordinal-within-day) so genuine
  same-day twins still both land.

CSV exports often lack balances and always lack transaction ids, so a
profile records what the format actually supports and an import records
which checks actually RAN. Honest weaker assurance beats pretended strong
assurance -- a reconciliation that silently skipped its checks is worse
than none.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import object_records
from object_versions import DEFAULT_DATA_DIR

IMPORTS_COLLECTION = "bank_statement_imports"
LINES_COLLECTION = "bank_lines"

STATUS_ACCEPTED = "accepted"
STATUS_FLAGGED = "flagged"


class ImportError_(Exception):
    """Unusable import input (bad profile, unparseable content)."""


def is_present(value: Any) -> bool:
    """True when a numeric field was actually supplied.

    Deliberately not `bool(value)`: 0 is a real balance (a new account
    opens at zero) and must not read as "not provided" -- that distinction
    decides whether a self-check runs at all.
    """
    return value is not None and str(value).strip() != ""


def file_hash(content: str) -> str:
    """Stable hash of the statement file itself -- re-importing the same
    file is a no-op, the same provenance posture generated_from gives
    composed journals."""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def line_hash(posted_on: str, amount_cents: int, description: str, ordinal: int = 0) -> str:
    """Identity of a statement line when the bank gives us no id.

    The ordinal distinguishes genuine same-day twins (two identical $4.50
    coffees) from a re-imported duplicate: within one statement the second
    twin gets ordinal 1, so both land; across statements the same twin
    hashes the same way and is rejected.
    """
    parts = f"{posted_on}|{int(amount_cents)}|{' '.join(str(description or '').split())}|{ordinal}"
    return hashlib.sha256(parts.encode("utf-8", "replace")).hexdigest()[:32]


def parse_cents(value: Any, *, decimal_sep: str = ".") -> int:
    """Parse a money string from a bank export into integer cents.

    Handles thousands separators, currency symbols, parenthesised negatives
    ("(45.00)" = -4500, the accounting convention many exports use), and
    European "1.234,56" when the profile says the decimal separator is a
    comma. Decimal throughout -- never a float (00-doctrine-and-contract).
    """
    text = str(value or "").strip()
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    cleaned = []
    for ch in text:
        if ch.isdigit() or ch == "-":
            cleaned.append(ch)
        elif ch == decimal_sep:
            cleaned.append(".")
        # every other character (currency symbol, thousands separator,
        # stray space) is noise between the digits
    text = "".join(cleaned)
    if not text or text in ("-", "."):
        return 0
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ImportError_(f"Unparseable amount: {value!r}") from exc
    if negative:
        amount = -amount
    return int((amount * 100).to_integral_value())


def parse_date(value: Any, date_format: str = "") -> str:
    """Normalise a statement date to ISO (YYYY-MM-DD).

    A profile may pin the bank's format explicitly (%m/%d/%Y); without one
    we try the common exports rather than guessing per row.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    formats = [date_format] if date_format else []
    formats += ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d.%m.%Y",
                "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"]
    for fmt in formats:
        if not fmt:
            continue
        try:
            return datetime.strptime(text[:len(text)], fmt).date().isoformat()
        except ValueError:
            continue
    raise ImportError_(f"Unparseable date: {value!r}")


def _column(row: dict, header: list[str], key: Any) -> Any:
    """Resolve a column_map entry: a header name or a 0-based index."""
    if key is None or key == "":
        return ""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        index = int(key)
        return row.get(header[index], "") if 0 <= index < len(header) else ""
    return row.get(key, "")


def parse_statement_csv(content: str, column_map: dict) -> list[dict]:
    """Parse a CSV export into canonical line dicts using a saved profile.

    column_map keys: date, description, amount (one signed column) OR
    debit/credit (a pair), external_id (optional), plus date_format,
    decimal_sep, and sign ("normal" | "flip" for banks that export
    withdrawals as positive numbers).

    Returns [{posted_on, amount_cents, description, external_id, raw}] in
    file order. Blank rows are skipped; a row whose date or amount cannot
    be parsed raises -- a half-read statement is worse than a refused one.
    """
    if not isinstance(column_map, dict) or not column_map:
        raise ImportError_("Profile has no column_map")
    text = content.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    header = list(reader.fieldnames or [])
    if not header:
        raise ImportError_("CSV has no header row")

    date_format = str(column_map.get("date_format") or "")
    decimal_sep = str(column_map.get("decimal_sep") or ".")
    flip = str(column_map.get("sign") or "normal").lower() == "flip"
    has_pair = bool(column_map.get("debit") or column_map.get("credit"))

    lines = []
    for row in reader:
        if not any(str(v or "").strip() for v in row.values()):
            continue
        raw = "\t".join(str(row.get(col, "") or "") for col in header)
        if has_pair:
            debit = parse_cents(_column(row, header, column_map.get("debit")), decimal_sep=decimal_sep)
            credit = parse_cents(_column(row, header, column_map.get("credit")), decimal_sep=decimal_sep)
            # Money out of the account is negative in the canonical shape.
            amount_cents = credit - abs(debit) if credit else -abs(debit)
        else:
            amount_cents = parse_cents(
                _column(row, header, column_map.get("amount")), decimal_sep=decimal_sep)
        if flip:
            amount_cents = -amount_cents
        lines.append({
            "posted_on": parse_date(_column(row, header, column_map.get("date")), date_format),
            "amount_cents": amount_cents,
            "description": " ".join(str(_column(row, header, column_map.get("description")) or "").split()),
            "external_id": str(_column(row, header, column_map.get("external_id")) or "").strip(),
            "raw": raw,
        })
    return lines


def assign_line_hashes(lines: Iterable[dict]) -> list[dict]:
    """Stamp line_hash on parsed lines, ordinal-disambiguating same-day
    twins so two identical transactions on one statement both survive."""
    seen: dict[tuple, int] = {}
    out = []
    for line in lines:
        key = (line.get("posted_on"), int(line.get("amount_cents") or 0),
               line.get("description"))
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        stamped = dict(line)
        stamped["line_hash"] = line_hash(
            line.get("posted_on", ""), int(line.get("amount_cents") or 0),
            line.get("description", ""), ordinal)
        out.append(stamped)
    return out


def existing_line_keys(bank_account_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> tuple[set, set]:
    """(external_ids, line_hashes) already stored for one bank account."""
    external_ids, hashes = set(), set()
    try:
        rows = object_records.read_collection_records(LINES_COLLECTION, base_dir=base_dir)
    except Exception:
        return external_ids, hashes
    for row in rows:
        if row.get("bank_account_id") != bank_account_id:
            continue
        if row.get("external_id"):
            external_ids.add(row["external_id"])
        if row.get("line_hash"):
            hashes.add(row["line_hash"])
    return external_ids, hashes


def is_duplicate_line(record: dict, *, base_dir: Path | str = DEFAULT_DATA_DIR) -> bool:
    """True when this line is already on file for its bank account."""
    external_ids, hashes = existing_line_keys(
        str(record.get("bank_account_id") or ""), base_dir=base_dir)
    if record.get("external_id") and record["external_id"] in external_ids:
        return True
    return bool(record.get("line_hash")) and record["line_hash"] in hashes


def find_import_by_file_hash(bank_account_id: str, digest: str, *,
                             base_dir: Path | str = DEFAULT_DATA_DIR) -> dict | None:
    """The prior import of this exact file, if any (file-level idempotency)."""
    try:
        rows = object_records.read_collection_records(IMPORTS_COLLECTION, base_dir=base_dir)
    except Exception:
        return None
    for row in rows:
        if row.get("bank_account_id") == bank_account_id and row.get("file_hash") == digest:
            return row
    return None


def previous_import(bank_account_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR,
                    before: str = "") -> dict | None:
    """The account's latest accepted-or-flagged import before `before`
    (by period_end) -- the continuity check's comparison point."""
    try:
        rows = object_records.read_collection_records(IMPORTS_COLLECTION, base_dir=base_dir)
    except Exception:
        return None
    candidates = [r for r in rows
                  if r.get("bank_account_id") == bank_account_id
                  and (r.get("period_end") or "")
                  and (not before or (r.get("period_end") or "") <= before)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.get("period_end") or "", r.get("created_at") or ""))


def run_gates(lines: list[dict], *, bank_account_id: str,
              opening_balance_cents: Any, closing_balance_cents: Any,
              period_start: str = "", base_dir: Path | str = DEFAULT_DATA_DIR) -> dict:
    """Run tie-out and continuity, returning the import's flags block.

    Shape: {"checks": {...ran/passed...}, "status": accepted|flagged}. The
    flags record which checks RAN, not just which passed -- a balance-less
    CSV that "passed" nothing must not look as trustworthy as a tied-out
    OFX file.
    """
    checks: dict[str, Any] = {}
    total = sum(int(l.get("amount_cents") or 0) for l in lines)
    checks["line_total_cents"] = total

    # `or ""` would be wrong here: a zero opening balance is a real balance
    # (a new account starts at zero), and treating it as "no balance given"
    # would silently skip the tie-out check on exactly the statement whose
    # arithmetic is easiest to verify.
    has_balances = is_present(opening_balance_cents) and is_present(closing_balance_cents)
    if has_balances:
        opening = int(opening_balance_cents or 0)
        closing = int(closing_balance_cents or 0)
        expected = opening + total
        checks["tie_out"] = {
            "ran": True,
            "passed": expected == closing,
            "expected_closing_cents": expected,
            "stated_closing_cents": closing,
            "delta_cents": closing - expected,
        }
        prior = previous_import(bank_account_id, base_dir=base_dir, before=period_start)
        if prior is not None and str(prior.get("closing_balance_cents") or "").strip():
            prior_closing = int(prior["closing_balance_cents"])
            checks["continuity"] = {
                "ran": True,
                "passed": prior_closing == opening,
                "previous_closing_cents": prior_closing,
                "this_opening_cents": opening,
                "gap_cents": opening - prior_closing,
                "previous_import_id": prior.get("id", ""),
            }
        else:
            checks["continuity"] = {"ran": False,
                                    "reason": "no prior statement for this account"}
    else:
        checks["tie_out"] = {"ran": False,
                             "reason": "statement carried no opening/closing balance"}
        checks["continuity"] = {"ran": False, "reason": "no balances to chain"}

    failed = [name for name, c in checks.items()
              if isinstance(c, dict) and c.get("ran") and not c.get("passed")]
    return {
        "checks": checks,
        "failed": failed,
        "status": STATUS_FLAGGED if failed else STATUS_ACCEPTED,
    }


def flags_json(gates: dict) -> str:
    return json.dumps(gates.get("checks", {}), sort_keys=True)


# --- matching ---------------------------------------------------------------
#
# Matching is DERIVED and non-authoritative: the matcher proposes, a human
# (or an explicitly configured auto-match tier) disposes. That split is the
# control -- reconciliation performed invisibly by whoever moves the money
# is the classic fraud hole, so the act of confirming is an ordinary
# attributed update and shows up in the change log with a name on it.

# Fields of a bank line that are EVIDENCE: what the bank said, verbatim.
# An update may never touch them -- corrections come from re-importing a
# corrected statement, not from editing the record to agree with the books.
EVIDENCE_FIELDS = ("import_id", "bank_account_id", "posted_on", "amount_cents",
                   "description", "external_id", "line_hash", "raw")


def _day_gap(left: str, right: str) -> int | None:
    try:
        a = datetime.strptime(str(left)[:10], "%Y-%m-%d").date()
        b = datetime.strptime(str(right)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return abs((a - b).days)


def _reference_hit(description: str, reference: str) -> bool:
    """Does the statement text carry this record's reference?

    Banks mangle memos (case, padding, truncation), so compare
    case-insensitively on collapsed alphanumerics. A reference shorter than
    four characters is too weak to assert identity on -- "12" appears in
    half of all descriptions.
    """
    ref = "".join(ch for ch in str(reference or "") if ch.isalnum()).upper()
    if len(ref) < 4:
        return False
    haystack = "".join(ch for ch in str(description or "") if ch.isalnum()).upper()
    return ref in haystack


def candidate_matches(line: dict, candidates: Iterable[dict], *,
                      window_days: int = 5, max_combination: int = 3) -> list[dict]:
    """Propose book-side records for one bank line, best first.

    `candidates` are dicts of {ref, amount_cents, date, reference} where
    amount_cents is already expressed in the LINE's sign convention (money
    into the account positive) and `ref` is "collection/id".

    Tiers, strongest to weakest:
      1. the record's reference appears in the statement text AND the
         amount agrees -- two independent signals, safe to auto-confirm
      2. exact amount within the date window -- one signal; plausible but a
         same-priced invoice from another customer looks identical
      3. a same-window combination of 2..max_combination records summing to
         the line -- the batched deposit case; always suggestion-only
    """
    amount = int(line.get("amount_cents") or 0)
    posted_on = str(line.get("posted_on") or "")
    description = str(line.get("description") or "")

    near = []
    for cand in candidates:
        gap = _day_gap(posted_on, cand.get("date"))
        if gap is None or gap > window_days:
            continue
        near.append((gap, cand))

    proposals = []
    for gap, cand in near:
        same_amount = int(cand.get("amount_cents") or 0) == amount
        if same_amount and _reference_hit(description, cand.get("reference")):
            proposals.append({
                "tier": 1, "refs": [cand["ref"]], "day_gap": gap,
                "why": f"reference {cand.get('reference')} appears in the statement text "
                       f"and the amount matches exactly",
            })
        elif same_amount:
            proposals.append({
                "tier": 2, "refs": [cand["ref"]], "day_gap": gap,
                "why": f"exact amount within {gap} day(s)",
            })

    if not any(p["tier"] == 1 for p in proposals) and max_combination >= 2:
        proposals.extend(_combination_matches(amount, near, max_combination))

    proposals.sort(key=lambda p: (p["tier"], p.get("day_gap", 99), len(p["refs"])))
    return proposals


def _combination_matches(amount: int, near: list, max_combination: int) -> list[dict]:
    """Batched-deposit case: several book records banked as one line.

    Bounded on purpose (few candidates, small k): an unbounded subset search
    over a busy account would both hang and produce coincidences that are
    worse than no suggestion at all.
    """
    from itertools import combinations

    pool = [c for _, c in near][:12]
    found = []
    for size in range(2, min(max_combination, len(pool)) + 1):
        for combo in combinations(pool, size):
            if sum(int(c.get("amount_cents") or 0) for c in combo) != amount:
                continue
            found.append({
                "tier": 3,
                "refs": [c["ref"] for c in combo],
                "day_gap": 0,
                "why": f"{size} records sum to this line (batched deposit)",
            })
            if len(found) >= 3:      # a handful of options, not a catalogue
                return found
    return found


def evidence_changes(existing: dict, changes: dict) -> list[str]:
    """Which evidence fields an update is trying to alter (should be none)."""
    touched = []
    for field in EVIDENCE_FIELDS:
        if field not in changes:
            continue
        if str(changes.get(field) or "") != str(existing.get(field) or ""):
            touched.append(field)
    return touched


# --- the reconciliation statement --------------------------------------------
#
# plan/bank-import-reconciliation-spec.md section 6: "bank closing balance
# (last accepted import) vs. book cash balance (fin account), reconciled by:
# matched total, outstanding timing items, unresolved tail." Nothing here
# writes anything -- it is a fold over already-stored state, the same
# posture as object_finance.trial_balance().

ACCOUNTS_COLLECTION = "value_accounts"
JOURNALS_COLLECTION = "fin_journals"
JOURNAL_LINES_COLLECTION = "fin_journal_lines"

_STATUS_POSTED = "posted"

ASSURANCE_VERIFIED = "verified"
ASSURANCE_UNVERIFIED = "unverified"
ASSURANCE_FLAGGED = "flagged"


def _safe_read(collection: str, base_dir: Path | str) -> list[dict]:
    """read_collection_records, folding a missing/uninstalled collection to
    an empty list instead of raising -- this report must degrade to "not
    enough data" rather than a 500 when a dependent package (app-finance)
    or a not-yet-imported account has nothing on file yet."""
    try:
        return object_records.read_collection_records(collection, base_dir=base_dir)
    except Exception:
        return []


def _latest_by_period_end(rows: list[dict]) -> dict | None:
    return max(rows, key=lambda r: (r.get("period_end") or "", r.get("created_at") or ""),
               default=None)


def _assurance_from_flags(flags_raw: Any) -> str:
    """verified/unverified/flagged from one import's flags JSON (the
    `checks` block run_gates() produces).

    "flagged" is deliberately keyed on ANY failed check (tie_out or
    continuity), not just tie_out, because a broken chain of statements is
    exactly as much of an anti-fraud gap as a broken statement -- both mean
    the evidence trail cannot be trusted yet. "unverified" is the honest
    middle ground for a balance-less CSV: nothing failed because nothing
    that could fail was even run.
    """
    try:
        checks = json.loads(flags_raw or "{}")
    except (ValueError, TypeError):
        checks = {}
    if not isinstance(checks, dict):
        checks = {}
    failed = any(isinstance(c, dict) and c.get("ran") and not c.get("passed")
                 for c in checks.values())
    if failed:
        return ASSURANCE_FLAGGED
    tie_out = checks.get("tie_out")
    if isinstance(tie_out, dict) and tie_out.get("ran") and tie_out.get("passed"):
        return ASSURANCE_VERIFIED
    return ASSURANCE_UNVERIFIED


def reconciliation(bank_account_id: str, *, base_dir: Path | str = DEFAULT_DATA_DIR,
                   as_of: str = "") -> dict:
    """The classic bank reconciliation statement for one account.

    Two independent truths are folded and compared, never merged: the
    bank's (bank_statement_imports/bank_lines, imported evidence) and the
    books' (fin_journals/fin_journal_lines through the account's own
    fin_account_id). bank_closing_cents deliberately comes from the latest
    ACCEPTED import only -- an import whose own tie-out either passed or
    was never claimed to run -- because a statement that failed its own
    arithmetic check is not something to build a "you're reconciled" claim
    on top of. `assurance`, by contrast, is read off the single most
    recent import regardless of its status: a fresh statement that just
    failed tie-out is exactly the thing this view exists to surface, even
    while the number it displays quietly falls back to the last
    trustworthy one.

    The tie: once every bank line is either matched to a book record or
    resolved (composing its own journal via action_resolve_bank_line),
    bank_closing_cents - book_balance_cents should equal exactly the sum
    of lines resolved as "timing" (on the statement, deliberately never
    booked -- see object_banking module docstring / the resolution-verbs
    tests). Any remaining unmatched or merely-suggested line is slack in
    that equation, which is what `reconciled` actually tests: it is not
    "do the two numbers match", it is "do they match FOR AN EXPLAINED
    REASON".

    as_of, when given (ISO date), scopes the statement to that date:
    imports with a period_end after it and posted journals dated after it
    are excluded, so a caller can ask "were we reconciled as of last
    month-end" without waiting for a fresh import.

    Never raises -- a bank account that does not exist, one with no
    fin_account_id set, or collections that are not installed all fold to
    a dict of mostly-None fields. This is a report a caller may render for
    a not-yet-configured account, not a gate.
    """
    accounts = _safe_read(ACCOUNTS_COLLECTION, base_dir)
    account = next((a for a in accounts if a.get("id") == bank_account_id), None)
    fin_account_id = str((account or {}).get("fin_account_id") or "")

    imports = [r for r in _safe_read(IMPORTS_COLLECTION, base_dir)
               if r.get("bank_account_id") == bank_account_id
               and (r.get("period_end") or "")
               and (not as_of or (r.get("period_end") or "") <= as_of)]
    latest_import = _latest_by_period_end(imports)
    latest_accepted = _latest_by_period_end(
        [r for r in imports if r.get("status") == STATUS_ACCEPTED])

    bank_closing_cents = None
    bank_statement_date = None
    if latest_accepted is not None and is_present(latest_accepted.get("closing_balance_cents")):
        bank_closing_cents = int(latest_accepted["closing_balance_cents"])
        bank_statement_date = latest_accepted.get("period_end") or ""

    assurance = _assurance_from_flags(latest_import.get("flags")) if latest_import else None

    book_balance_cents = None
    if fin_account_id:
        posted_ids = {
            j.get("id") for j in _safe_read(JOURNALS_COLLECTION, base_dir)
            if j.get("status") == _STATUS_POSTED
            and (not as_of or (j.get("date") or "") <= as_of)
        }
        debit_total = credit_total = 0
        for line in _safe_read(JOURNAL_LINES_COLLECTION, base_dir):
            if line.get("journal_id") not in posted_ids or line.get("account_id") != fin_account_id:
                continue
            debit_total += int(line.get("debit_cents") or 0)
            credit_total += int(line.get("credit_cents") or 0)
        # An asset account: debits increase the balance, credits decrease
        # it -- the same convention object_finance.py uses throughout.
        book_balance_cents = debit_total - credit_total

    matched_cents = timing_cents = unmatched_cents = 0
    unmatched_count = timing_count = suggested_count = 0
    for row in _safe_read(LINES_COLLECTION, base_dir):
        if row.get("bank_account_id") != bank_account_id:
            continue
        amount = int(row.get("amount_cents") or 0)
        status = row.get("match_status") or "unmatched"
        if status == "matched":
            matched_cents += amount
        elif status == "resolved":
            if (row.get("resolved_as") or "") == "timing":
                timing_cents += amount
                timing_count += 1
            else:
                matched_cents += amount
        else:
            # unmatched and suggested are both still an open tail -- a
            # suggestion is the matcher's opinion, not a human decision
            # (object_banking module docstring), so it has not yet earned
            # its way into matched_cents.
            unmatched_cents += amount
            if status == "suggested":
                suggested_count += 1
            else:
                unmatched_count += 1

    difference_cents = None
    if bank_closing_cents is not None and book_balance_cents is not None:
        difference_cents = bank_closing_cents - book_balance_cents
    reconciled = difference_cents is not None and difference_cents == timing_cents

    return {
        "bank_closing_cents": bank_closing_cents,
        "bank_statement_date": bank_statement_date,
        "book_balance_cents": book_balance_cents,
        "matched_cents": matched_cents,
        "unmatched_cents": unmatched_cents,
        "timing_cents": timing_cents,
        "unmatched_count": unmatched_count,
        "timing_count": timing_count,
        "suggested_count": suggested_count,
        "difference_cents": difference_cents,
        "reconciled": reconciled,
        "assurance": assurance,
    }
