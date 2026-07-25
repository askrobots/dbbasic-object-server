"""Reading a receipt: text in, a SUGGESTION out.

The pure half of document intake, testable without an OCR engine, a
vision model, or a data directory.

The load-bearing idea in this file is that everything it produces is a
suggestion and nothing it produces is a fact. A scan is evidence and is
never edited (docs/logic-decisions.md #8); the expense is the record, and
it is created by a human confirming, with their name on it. That single
boundary is what makes the whole feature safe to ship with a bad
extractor: a weak reading costs somebody thirty seconds of typing, while
a confident wrong reading that posted straight to the books would cost
them an audit. Every design choice below follows from refusing the second
failure.

It also makes the free path real. `guess_from_text` is regex over
whatever text an OCR engine produced -- no model, no key, no per-page
cost -- and it degrades to "extracted nothing, here is your image, type
the total" rather than to wrong numbers. An AI extractor plugs into the
same shape and simply guesses better; `normalize_extraction` takes its
loose JSON and clamps it into the same suggestion the regex path
produces, so the confirm step cannot tell them apart and does not care.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import object_banking

KINDS = ("receipt", "bill", "invoice", "statement", "other")

# Ordered by how strongly the label implies "this is the number that was
# actually charged". A receipt often shows subtotal, tax and total within
# a few lines of each other, and picking the largest number on the page
# is how intake systems bill a client for the tip line.
_TOTAL_LABELS = (
    (r"grand\s*total", 100),
    (r"total\s*due", 95),
    (r"amount\s*due", 95),
    (r"balance\s*due", 90),
    (r"\btotal\b", 80),
    (r"amount\s*paid", 75),
    (r"\bpaid\b", 60),
)

_MONEY = r"[-(]?\s*[$€£]?\s*\d[\d,. ]*\d|\d"

_DATE_PATTERNS = (
    # ISO first: unambiguous, so it never has to be guessed at.
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), ("m", "d", "y")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b"), ("m", "d", "yy")),
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"), ("d", "m", "y")),
)

_NOISE = re.compile(r"^[\W_\d]+$")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1:                    # a model that answered in percent
        number = number / 100
    return max(0.0, min(1.0, round(number, 3)))


def _iso_date(parts: tuple[str, ...], order: tuple[str, ...]) -> str:
    values = dict(zip(order, parts))
    year = values.get("y") or values.get("yy") or ""
    if "yy" in values:
        year = f"20{values['yy']}"
    try:
        return date(int(year), int(values["m"]), int(values["d"])).isoformat()
    except (ValueError, KeyError, TypeError):
        return ""


def find_date(text: str) -> str:
    """The first date the document states, in ISO form.

    Ambiguous formats are read US-first (M/D/Y) because that is what the
    receipts this was built for print, and the confirm step shows the
    parsed date next to the image precisely so a human catches the day a
    machine cannot know.
    """
    for pattern, order in _DATE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            parsed = _iso_date(match.groups(), order)
            if parsed:
                return parsed
    return ""


def find_total_cents(text: str) -> int:
    """The amount most likely to be what was charged.

    Scored by the LABEL beside a number rather than by size. "Take the
    biggest figure on the receipt" is the shortcut that bills a client
    for the suggested-tip line, and a wrong total is worse than no total
    because a plausible one gets confirmed without looking.
    """
    best_score = 0
    best_cents = 0
    for line in (text or "").splitlines():
        lowered = line.lower()
        for pattern, score in _TOTAL_LABELS:
            if not re.search(pattern, lowered):
                continue
            amounts = re.findall(_MONEY, line)
            if not amounts:
                continue
            cents = abs(object_banking.parse_cents(amounts[-1]))
            if cents <= 0:
                continue
            if score > best_score:
                best_score, best_cents = score, cents
            break
    return best_cents


def find_vendor(text: str) -> str:
    """Whoever the money went to, guessed from the top of the page.

    Receipts put the trading name first and the useful detail later, so
    the first line that is not punctuation, a number or a bare date is a
    better guess than anything cleverer -- and being wrong here is
    harmless, because the vendor is a description a human is reading
    anyway.
    """
    for line in (text or "").splitlines():
        candidate = line.strip()
        if len(candidate) < 3 or len(candidate) > 60:
            continue
        if _NOISE.match(candidate):
            continue
        if find_date(candidate):
            continue
        return candidate
    return ""


def guess_from_text(text: str, *, kind_hint: str = "") -> dict:
    """The free extractor: no model, no key, no per-page cost.

    Confidence here is deliberately modest and is computed from how much
    was actually found, not asserted. It exists so a UI can sort "probably
    fine" above "look at this one", never so anything can be posted
    unattended.
    """
    body = text or ""
    total = find_total_cents(body)
    when = find_date(body)
    vendor = find_vendor(body)
    found = sum(1 for value in (total, when, vendor) if value)
    return {
        "kind": kind_hint if kind_hint in KINDS else "receipt",
        "vendor": vendor,
        "date": when,
        "total_cents": total,
        "tax_cents": 0,
        "currency": "",
        "line_items": [],
        "confidence": _clamp_confidence(found / 3 * 0.6),
        "engine": "text_rules",
    }


def _minor_units(payload: dict, minor_key: str, money_key: str) -> int:
    """Read an amount from whichever key the extractor used.

    `minor_key` is already in minor units and is only coerced to an int;
    `money_key` is what a human would type and goes through the money
    parser. Never both, since re-parsing 899 as money yields 89900.
    """
    raw = payload.get(minor_key)
    if raw not in (None, ""):
        try:
            return abs(int(str(raw).strip()))
        except (TypeError, ValueError):
            pass
    if payload.get(money_key) not in (None, ""):
        return abs(object_banking.parse_cents(payload.get(money_key)))
    return 0


def normalize_extraction(payload: Any, *, engine: str = "") -> dict:
    """Clamp a model's loose JSON into the same suggestion shape.

    Anything unparseable becomes an EMPTY suggestion rather than an
    exception: an extractor having a bad day must leave a scan waiting for
    a human, never break the pass that is reading a hundred other
    documents.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    if not isinstance(payload, dict):
        return {"kind": "other", "vendor": "", "date": "", "total_cents": 0,
                "tax_cents": 0, "currency": "", "line_items": [],
                "confidence": 0.0, "engine": engine or "unknown",
                "note": "extractor returned nothing usable"}

    kind = _text(payload.get("kind")).lower()
    # A model may answer in minor units ("total_cents": 899) or in the
    # money a human would type ("total": "$8.99"). Each is parsed by its
    # own rule and never by both -- running a cents figure back through
    # the money parser is a hundredfold overcharge that looks plausible.
    total = _minor_units(payload, "total_cents", "total")
    tax = _minor_units(payload, "tax_cents", "tax")

    items = payload.get("line_items")
    clean_items = []
    if isinstance(items, list):
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            clean_items.append({
                "description": _text(item.get("description"))[:200],
                "amount_cents": _minor_units(item, "amount_cents", "amount"),
            })

    return {
        "kind": kind if kind in KINDS else "other",
        "vendor": _text(payload.get("vendor"))[:120],
        "date": _text(payload.get("date"))[:10],
        "total_cents": total,
        "tax_cents": tax,
        "currency": _text(payload.get("currency"))[:8].upper(),
        "line_items": clean_items,
        "confidence": _clamp_confidence(payload.get("confidence")),
        "engine": engine or _text(payload.get("engine")) or "ai_vision",
    }


def expense_draft_from(scan: dict, extraction: dict) -> dict:
    """The DRAFT an operator is about to confirm -- fields, not a write.

    Returns what the confirm step would create, so the same mapping can
    be shown on screen before anyone commits to it. A draft with no
    amount is still returned: the point of confirmation is that a person
    can fill in what the machine could not read, with the image in front
    of them.
    """
    vendor = _text(extraction.get("vendor"))
    when = _text(extraction.get("date")) or _text(scan.get("created_at"))[:10]
    return {
        "description": vendor or _text(scan.get("filename")) or "Scanned receipt",
        "incurred_on": when[:10],
        "amount_cents": int(extraction.get("total_cents") or 0),
        "currency": _text(extraction.get("currency")) or "USD",
        "receipt_ref": f"scans/{_text(scan.get('id'))}",
        "status": "draft",
        "notes": _text(extraction.get("note")),
    }
