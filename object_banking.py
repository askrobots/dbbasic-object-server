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
