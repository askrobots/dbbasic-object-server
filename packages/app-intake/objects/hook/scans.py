"""Pre-write hook for scans: one document, one scan; evidence stays put.

Two gates, both about the same thing -- a scan is a record of what
ARRIVED, and neither a person nor a retry may quietly change that.

**The same bytes are the same scan.** People photograph a receipt twice
because the first one was blurry, and forward the email version of
something a colleague already snapped. Treating those as separate
documents is how one dinner becomes two expenses. The content hash is the
identity, per owner -- two businesses receiving the same invoice PDF are
two genuinely separate facts.

**Confirmed evidence is closed.** Once a human has turned a scan into an
expense, the thing to correct is the expense; editing what the machine
read afterwards would leave the ledger explained by evidence that no
longer says what it said when somebody agreed to it (docs/logic-decisions.md
#8).
"""

import os

import object_records

# What may still move after a scan is confirmed. Nothing that changes what
# the document said or what it became.
_ALLOWED_AFTER_CONFIRM = {"notes", "status"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def BEFORE_WRITE(request):
    action = request.get("action")
    record = dict(request.get("record") or {})
    existing = request.get("existing") or {}
    changes = request.get("changes") or {}

    if action == "create":
        digest = _text(record.get("content_sha256"))
        if not digest:
            return None                    # text-only intake without bytes
        owner = _text(record.get("owner_id"))
        try:
            rows = object_records.read_collection_records("scans", base_dir=_base_dir())
        except Exception:
            return {"error": ("Scan history unreadable; refusing to record "
                              "against an unknown history."), "status": 503}
        for row in rows:
            if (_text(row.get("content_sha256")) == digest
                    and _text(row.get("owner_id")) == owner):
                return {"error": (f"This document is already here as scan "
                                  f"{row['id']}. Photographing the same receipt "
                                  f"twice must not become two expenses."),
                        "status": 409}
        return None

    if action == "update" and _text(existing.get("status")) == "confirmed":
        touched = [field for field in changes if field not in _ALLOWED_AFTER_CONFIRM]
        if touched:
            return {"error": (
                f"This scan has been confirmed as "
                f"{_text(existing.get('confirmed_record')) or 'a record'}. "
                f"Correct that record, not the evidence it came from "
                f"({', '.join(sorted(touched))})."), "status": 409}
        if _text(record.get("status", "confirmed")) != "confirmed":
            return {"error": ("A confirmed scan stays confirmed. Reverse the "
                              "record it produced instead."), "status": 409}

    return None
