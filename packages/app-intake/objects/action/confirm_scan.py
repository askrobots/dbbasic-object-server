"""action_confirm_scan -- a person says yes, and the record is theirs.

POST {scan_id, project_id?, billable?, paid_by?, markup_bps?,
      description?, incurred_on?, amount_cents?, currency?, preview?}

The one write in this whole pipeline that matters. Everything upstream --
OCR, extraction, confidence -- produced a suggestion; this creates the
real record, as a DRAFT, stamped with where it came from and with a
human's name on it.

Overrides are first-class rather than an escape hatch. Any field passed in
beats what the machine read, because the point of confirmation is that
somebody is looking at the image while they do it. A total the extractor
missed is typed in here, and that is a success of the design rather than
a failure of it: thirty seconds of typing is the cost of never posting a
confident wrong number.

`preview: true` returns the draft without writing, so the same mapping
that would be committed can be shown on screen first.

Idempotency: one scan, one record, however many times anyone clicks. The
scan carries confirmed_record once it is done, and that stamp -- not a
status flag alone -- is what a second call checks.

The expense it creates then rides every gate that already exists: the
approval gate (somebody OTHER than the person who spent it), the markup
stamp, the books composer, the T&M invoice generator. Intake is a new
front door onto machinery that was already there, which is the reason it
is a small object.
"""

import json
import os

import object_ids
import object_intake
import object_records

ACTOR = "action_confirm_scan"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _truthy(value):
    return _text(value).lower() in ("true", "1", "yes", "on")


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def POST(request):
    base = _base_dir()
    scan_id = _text(request.get("scan_id"))
    if not scan_id:
        return {"status": 400, "error": "scan_id is required"}

    try:
        scan = object_records.get_collection_record("scans", scan_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"No such scan: {scan_id}"}
    if not scan:
        return {"status": 404, "error": f"No such scan: {scan_id}"}

    already = _text(scan.get("confirmed_record"))
    if already:
        return {"ok": True, "scan_id": scan_id, "record": already,
                "duplicate": True,
                "note": "this scan was already confirmed; one scan, one record"}

    try:
        extraction = json.loads(_text(scan.get("extracted")) or "{}")
    except (TypeError, ValueError):
        extraction = {}
    extraction = object_intake.normalize_extraction(
        extraction, engine=_text(scan.get("ocr_engine")))

    draft = object_intake.expense_draft_from(scan, extraction)
    # Anything the person typed beats anything the machine read. They have
    # the image in front of them; the extractor had a guess.
    for field in ("description", "incurred_on", "currency", "notes"):
        if _text(request.get(field)):
            draft[field] = _text(request.get(field))
    if _text(request.get("amount_cents")):
        draft["amount_cents"] = _int(request.get("amount_cents"))

    identity = request.get("_identity") or {}
    owner = (_text(request.get("owner_id"))
             or _text(scan.get("owner_id"))
             or _text(identity.get("user_id")))

    expense = {
        "id": object_ids.new_uuid4(),
        "description": draft["description"],
        "incurred_on": draft["incurred_on"],
        "amount_cents": str(draft["amount_cents"]),
        "currency": draft["currency"],
        "project_id": _text(request.get("project_id")),
        "paid_by": _text(request.get("paid_by")) or "company",
        "billable": "true" if _truthy(request.get("billable", "true")) else "false",
        "receipt_ref": draft["receipt_ref"],
        "status": "draft",
        "notes": draft.get("notes", ""),
        "owner_id": owner,
        "entity_id": _text(scan.get("entity_id")),
    }
    if _text(request.get("markup_bps")):
        expense["markup_bps"] = str(_int(request.get("markup_bps")))

    if _truthy(request.get("preview")):
        return {"ok": True, "preview": True, "scan_id": scan_id,
                "would_create": {"collection": "expenses", **expense},
                "extraction": extraction}

    if expense["amount_cents"] in ("", "0"):
        return {"status": 400,
                "error": ("This document has no amount yet. The extractor did "
                          "not read one, so type it in from the image -- an "
                          "expense with no amount would post nothing and hide "
                          "a real cost.")}

    try:
        object_records.create_collection_record(
            "expenses", expense, base_dir=base, actor=ACTOR)
    except Exception as exc:
        return {"status": 400,
                "error": f"Could not create the expense: {str(exc)[:200]}"}

    reference = f"expenses/{expense['id']}"
    object_records.update_collection_record(
        "scans", scan_id,
        {"status": "confirmed", "confirmed_record": reference},
        base_dir=base, actor=ACTOR)

    return {"ok": True, "scan_id": scan_id, "record": reference,
            "expense_id": expense["id"], "status_of_expense": "draft",
            "note": "created as a draft; it still needs submitting and "
                    "approving by somebody else"}
