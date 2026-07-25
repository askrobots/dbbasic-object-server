"""OFX/QFX statement parsing (plan/bank-import-reconciliation-spec.md section 3,
"v1.5: action_import_ofx").

OFX is the near-universal US bank/card download format, and it comes in two
dialects that share a name and nothing else structurally:

- **OFX 1.x** is SGML-ish, not XML: a `KEY:VALUE` header block, a blank
  line, then tags that are routinely left UNCLOSED
  (`<DTPOSTED>20260702120000[-5:EST]` with no `</DTPOSTED>` -- the closing
  tag is implied by the next tag starting). Feeding this to an XML parser
  fails outright, so the SGML branch below is a small regex-based reader,
  not a workaround for a parser bug: aggregates (STMTTRN, LEDGERBAL, ...)
  ARE always closed in practice, it is only leaf value tags that are not,
  so a leaf's value is simply "everything up to the next '<' or newline".
- **OFX 2.x** is real XML (`<?xml ...?>`, closed tags), so stdlib's
  xml.etree can read it properly -- entity-unescaping included -- but
  "can parse a file a user uploaded" means refusing DOCTYPE/ENTITY
  declarations first. A statement upload is exactly the untrusted-input
  shape XXE and billion-laughs attacks target, and there is no legitimate
  reason an OFX statement needs a custom entity, so both dialects are
  refused outright if either declaration appears -- before either parser
  ever sees the text.

Both dialects land in the SAME canonical shape object_banking.py already
uses for CSV imports (posted_on/amount_cents/description/external_id/raw),
because the importer that consumes parsed lines must not care which dialect
or which bank produced them -- one canonical landing zone, thin parsers
(the doctrine plan/bank-import-reconciliation-spec.md section 3 names
directly: "don't build four parsers first").

FITID (the bank's own transaction id) becomes external_id, which is the
strong half of the dedup story in object_banking.py: OFX statements don't
need the line_hash ordinal fallback CSV imports rely on, because the bank
already gave every line a stable identity.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


class OfxError(ValueError):
    """Unparseable OFX/QFX input -- refuse rather than half-read a statement.

    A statement is evidence (object_banking.py's module docstring); a
    parser that silently dropped the transactions it could not understand
    would corrupt that evidence without telling anyone.
    """


# --- sniffing ---------------------------------------------------------------

_SNIFF_RE = re.compile(r"OFXHEADER|<OFX[ >]", re.IGNORECASE)


def is_ofx(content: str) -> bool:
    """Cheap sniff: does this look like an OFX/QFX file at all.

    Deliberately loose (a header token or an <OFX> tag anywhere in the
    first slice) -- this gates which parser a caller tries, not whether the
    file is well-formed. parse_ofx() does the real, strict validation and
    raises OfxError on anything that does not actually check out.
    """
    if not content:
        return False
    head = content[:2048]
    return bool(_SNIFF_RE.search(head))


# --- shared value parsing ----------------------------------------------------

def parse_ofx_amount(value: Any) -> int:
    """OFX TRNAMT/BALAMT to signed integer cents.

    These are already signed in the bank's own convention (money into the
    account positive, out negative) -- unlike object_banking.parse_cents()
    there is no sign-flip or parenthesised-negative convention to apply,
    only whitespace and an occasional leading "+". Decimal throughout,
    never float (00-doctrine-and-contract).
    """
    text = str(value or "").strip()
    if not text:
        raise OfxError("Missing amount")
    if text.startswith("+"):
        text = text[1:]
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise OfxError(f"Unparseable OFX amount: {value!r}") from exc
    return int((amount * 100).to_integral_value())


_DATE_RE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})")


def parse_ofx_date(value: Any) -> str:
    """OFX date (DTPOSTED/DTSTART/DTEND/DTASOF) to ISO calendar date.

    Format is YYYYMMDD[HHMMSS[.XXX]][[+-]TZ[:NAME]] -- e.g.
    "20260702120000[-5:EST]" or a bare "20260702". We take the calendar
    date the bank stated and stop there: the timezone bracket describes
    what clock the bank used to stamp the transaction, not a correction to
    apply. Shifting the date across a TZ boundary would move a transaction
    to a different calendar day than the one printed on the statement,
    which breaks reconciliation against that same statement's own totals
    -- the posting date is the fact, not the timestamp.
    """
    text = str(value or "").strip()
    match = _DATE_RE.match(text)
    if not match:
        raise OfxError(f"Unparseable OFX date: {value!r}")
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise OfxError(f"Unparseable OFX date: {value!r}") from exc


def _combine_name_memo(name: str, memo: str) -> str:
    """NAME and MEMO joined per the import shape: both when present and
    different, otherwise whichever one exists, collapsed to single-spaced
    text. Neither present is tolerated -- an empty description, not an
    error, since some FITID-only lines genuinely carry no text."""
    name = " ".join(str(name or "").split())
    memo = " ".join(str(memo or "").split())
    if name and memo:
        return name if name == memo else f"{name} — {memo}"
    return name or memo


# --- input hygiene ------------------------------------------------------------

def _strip_bom(content: str) -> str:
    return content.lstrip("﻿")


def _normalise_newlines(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _reject_dangerous_declarations(content: str) -> None:
    """Refuse DOCTYPE/ENTITY declarations before either parser sees them.

    This is the XXE/billion-laughs gate: stdlib's xml.etree does not
    resolve *external* entities by default, but a DTD can still define
    internal entities that expand recursively, and a DOCTYPE has no
    legitimate role in a bank statement -- so it is rejected on sight
    rather than trying to parse a "safe subset" of DTD syntax. Applied
    uniformly to both dialects: the SGML branch never touches an XML
    parser at all, but a hostile DOCTYPE has no business in a bank
    statement of either dialect, so it is refused just as hard.
    """
    if re.search(r"<!DOCTYPE", content, re.IGNORECASE) or \
            re.search(r"<!ENTITY", content, re.IGNORECASE):
        raise OfxError("OFX file declares a DOCTYPE/ENTITY -- refused")


def _looks_like_xml(content: str) -> bool:
    return content.lstrip().lower().startswith("<?xml")


# --- OFX 1.x (SGML) -----------------------------------------------------------

def _aggregate(text: str, tag: str) -> str | None:
    """Inner text of the first <TAG>...</TAG> aggregate, or None.

    Aggregates (as opposed to leaf value tags) are always explicitly
    closed in OFX 1.x -- this is what makes a regex-based reader viable
    without a real SGML parser.
    """
    m = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text,
                  re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else None


def _iter_aggregate(text: str, tag: str):
    """Yield (full_text, inner_text) for every <TAG>...</TAG> aggregate,
    in document order -- full_text is the verbatim slice for `raw`."""
    for m in re.finditer(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text,
                         re.IGNORECASE | re.DOTALL):
        yield m.group(0), m.group(1)


def _leaf(text: str, tag: str) -> str:
    """Value of a leaf tag that may or may not be closed: everything from
    just after <TAG> up to the next '<' (a following tag, closing or not)
    or end of line. This one regex is what lets the same reader handle
    both `<DTPOSTED>20260702120000[-5:EST]` (unclosed) and
    `<DTPOSTED>20260702</DTPOSTED>` (closed) identically -- the closing
    tag also starts with '<', so it stops the match on its own."""
    m = re.search(rf"<{re.escape(tag)}>\s*([^<\r\n]*)", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _detect_statement_kind(text: str) -> tuple[str, str, str]:
    """(acct_tag, default_account_type, is_cc) worth of routing between a
    bank statement (STMTRS/BANKACCTFROM) and a credit-card statement
    (CCSTMTRS/CCACCTFROM) -- same transaction/balance shape, different
    wrapper and account-identification tags."""
    if re.search(r"<CCSTMTRS>", text, re.IGNORECASE):
        return "CCACCTFROM", "CREDITCARD"
    if re.search(r"<STMTRS>", text, re.IGNORECASE):
        return "BANKACCTFROM", ""
    raise OfxError("No STMTRS/CCSTMTRS statement block found")


def _transaction_from_leaves(full_text: str, inner: str) -> dict:
    dtposted = _leaf(inner, "DTPOSTED")
    if not dtposted:
        raise OfxError("STMTTRN missing DTPOSTED")
    return {
        "posted_on": parse_ofx_date(dtposted),
        "amount_cents": parse_ofx_amount(_leaf(inner, "TRNAMT")),
        "description": _combine_name_memo(_leaf(inner, "NAME"), _leaf(inner, "MEMO")),
        "external_id": _leaf(inner, "FITID"),
        "raw": full_text.strip(),
    }


def _parse_sgml(text: str) -> dict:
    if not re.search(r"<OFX>", text, re.IGNORECASE):
        raise OfxError("Not a valid OFX 1.x file: no <OFX> aggregate found")
    acct_tag, default_type = _detect_statement_kind(text)

    acct_inner = _aggregate(text, acct_tag) or ""
    tranlist_inner = _aggregate(text, "BANKTRANLIST") or ""
    ledger_inner = _aggregate(text, "LEDGERBAL")

    period_start = _leaf(tranlist_inner, "DTSTART")
    period_end = _leaf(tranlist_inner, "DTEND")

    closing_balance_cents = None
    as_of = ""
    if ledger_inner is not None:
        balamt = _leaf(ledger_inner, "BALAMT")
        if balamt:
            closing_balance_cents = parse_ofx_amount(balamt)
        dtasof = _leaf(ledger_inner, "DTASOF")
        if dtasof:
            as_of = parse_ofx_date(dtasof)

    lines = [_transaction_from_leaves(full, inner)
             for full, inner in _iter_aggregate(text, "STMTTRN")]

    return {
        "bank_id": _leaf(acct_inner, "BANKID"),
        "account_id": _leaf(acct_inner, "ACCTID"),
        "account_type": _leaf(acct_inner, "ACCTTYPE") or default_type,
        "currency": _leaf(text, "CURDEF") or "USD",
        "period_start": parse_ofx_date(period_start) if period_start else "",
        "period_end": parse_ofx_date(period_end) if period_end else "",
        "closing_balance_cents": closing_balance_cents,
        "opening_balance_cents": None,
        "as_of": as_of,
        "lines": lines,
    }


# --- OFX 2.x (XML) --------------------------------------------------------

def _findall_ci(elem: ET.Element | None, tag: str) -> list[ET.Element]:
    """Elements (elem itself included) matching `tag` case-insensitively --
    real OFX 2.x is almost always upper-cased per the standard, but some
    exporters emit it lower-cased throughout, and XML tag matching is
    otherwise case-sensitive."""
    if elem is None:
        return []
    tag = tag.lower()
    return [e for e in elem.iter() if e.tag.lower() == tag]


def _find_ci(elem: ET.Element | None, tag: str) -> ET.Element | None:
    matches = _findall_ci(elem, tag)
    return matches[0] if matches else None


def _text_ci(elem: ET.Element | None, tag: str) -> str:
    found = _find_ci(elem, tag)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _transaction_from_xml(elem: ET.Element, raw: str) -> dict:
    dtposted = _text_ci(elem, "DTPOSTED")
    if not dtposted:
        raise OfxError("STMTTRN missing DTPOSTED")
    return {
        "posted_on": parse_ofx_date(dtposted),
        "amount_cents": parse_ofx_amount(_text_ci(elem, "TRNAMT")),
        "description": _combine_name_memo(_text_ci(elem, "NAME"), _text_ci(elem, "MEMO")),
        "external_id": _text_ci(elem, "FITID"),
        "raw": raw.strip(),
    }


def _parse_xml(text: str) -> dict:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise OfxError(f"Malformed OFX XML: {exc}") from exc
    if root.tag.lower() != "ofx":
        raise OfxError("Not a valid OFX 2.x file: root element is not <OFX>")

    ccstmtrs = _find_ci(root, "CCSTMTRS")
    stmtrs = ccstmtrs if ccstmtrs is not None else _find_ci(root, "STMTRS")
    if stmtrs is None:
        raise OfxError("No STMTRS/CCSTMTRS statement block found")
    is_cc = ccstmtrs is not None
    acct_elem = _find_ci(stmtrs, "CCACCTFROM" if is_cc else "BANKACCTFROM")

    tranlist = _find_ci(stmtrs, "BANKTRANLIST")
    period_start = _text_ci(tranlist, "DTSTART") if tranlist is not None else ""
    period_end = _text_ci(tranlist, "DTEND") if tranlist is not None else ""

    ledger = _find_ci(stmtrs, "LEDGERBAL")
    closing_balance_cents = None
    as_of = ""
    if ledger is not None:
        balamt = _text_ci(ledger, "BALAMT")
        if balamt:
            closing_balance_cents = parse_ofx_amount(balamt)
        dtasof = _text_ci(ledger, "DTASOF")
        if dtasof:
            as_of = parse_ofx_date(dtasof)

    txn_elems = _findall_ci(tranlist if tranlist is not None else stmtrs, "STMTTRN")
    # Pull the verbatim source slice for `raw` from the original text (not
    # ET.tostring, which re-serialises and would not be byte-identical to
    # what the bank sent) -- the same evidence posture object_banking.py's
    # CSV parser uses. Falls back to a re-serialisation only if the counts
    # somehow disagree, which should not happen for well-formed input.
    raw_texts = [m.group(0) for m in
                 re.finditer(r"<STMTTRN>.*?</STMTTRN>", text, re.IGNORECASE | re.DOTALL)]
    if len(raw_texts) != len(txn_elems):
        raw_texts = [ET.tostring(e, encoding="unicode") for e in txn_elems]

    lines = [_transaction_from_xml(elem, raw) for elem, raw in zip(txn_elems, raw_texts)]

    return {
        "bank_id": _text_ci(acct_elem, "BANKID") if acct_elem is not None else "",
        "account_id": _text_ci(acct_elem, "ACCTID") if acct_elem is not None else "",
        "account_type": (_text_ci(acct_elem, "ACCTTYPE") if acct_elem is not None else "")
                        or ("CREDITCARD" if is_cc else ""),
        "currency": _text_ci(stmtrs, "CURDEF") or _text_ci(root, "CURDEF") or "USD",
        "period_start": parse_ofx_date(period_start) if period_start else "",
        "period_end": parse_ofx_date(period_end) if period_end else "",
        "closing_balance_cents": closing_balance_cents,
        "opening_balance_cents": None,
        "as_of": as_of,
        "lines": lines,
    }


# --- entry point ------------------------------------------------------------

def _build_result(fields: dict) -> dict:
    """Common shape-assembly for both dialects, including the period
    fallback: DTSTART/DTEND from BANKTRANLIST when the bank supplied them,
    else the min/max of the transaction dates actually present -- a
    statement with a transaction list but no explicit period markers still
    gets an honest period rather than an empty one."""
    lines = fields["lines"]
    period_start = fields["period_start"]
    period_end = fields["period_end"]
    if not period_start or not period_end:
        dates = sorted(l["posted_on"] for l in lines if l.get("posted_on"))
        if dates:
            period_start = period_start or dates[0]
            period_end = period_end or dates[-1]
    return {
        "lines": lines,
        "account": {
            "account_id": fields["account_id"],
            "bank_id": fields["bank_id"],
            "account_type": fields["account_type"],
        },
        "balances": {
            "opening_balance_cents": fields["opening_balance_cents"],
            "closing_balance_cents": fields["closing_balance_cents"],
            "as_of": fields["as_of"],
        },
        "period": {"start": period_start, "end": period_end},
        "currency": fields["currency"],
    }


def parse_ofx(content: str) -> dict:
    """Parse an OFX/QFX statement (either dialect) into the canonical
    import shape object_banking.py's importer path consumes.

    Returns {"lines": [...], "account": {...}, "balances": {...},
    "period": {...}, "currency": str}. Raises OfxError for anything that
    does not check out -- a statement is evidence (object_banking.py's
    module docstring), and evidence half-read is worse than evidence
    refused.

    Dialect is decided by a light prefix check (`<?xml` means OFX 2.x,
    real XML; anything else is tried as OFX 1.x SGML) -- not by a
    declared version number, because both header styles exist in the
    wild independent of the OFX version a file claims.
    """
    if not isinstance(content, str) or not content.strip():
        raise OfxError("Empty OFX content")
    text = _normalise_newlines(_strip_bom(content))
    _reject_dangerous_declarations(text)
    if _looks_like_xml(text):
        fields = _parse_xml(text)
    else:
        fields = _parse_sgml(text)
    return _build_result(fields)
