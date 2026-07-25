"""system_scan_processor -- read what arrived, guess, and stop there.

POST {limit?, engine?, today?} -- the scheduled pass that turns pending
scans into suggestions. It never posts anything; producing a confirmable
draft is the whole of its job (docs/logic-decisions.md #6 -- extraction
is a suggestion, the write that matters is human-confirmed).

Engine selection is configuration, not code (app_settings
intake.ocr_engine):

  text_rules  the default and the FREE path -- regex over whatever text is
              already on the scan. No model, no key, no per-page cost, and
              it degrades to "found nothing, here is your image" rather
              than to wrong numbers.
  tesseract   local OCR when pytesseract and PIL are installed on the host.
              Absent, the pass says so and leaves the scan pending rather
              than failing it: a missing system package is an operator's
              problem to fix, not a document to give up on.
  ai_vision   object_ai with the owner's own key. Better guesses, and the
              only path with a real per-page cost -- which is why it is
              opt-in and why the key is theirs.

Whatever the engine, the output lands in the same shape
(object_intake.normalize_extraction), so the confirm step cannot tell
which one read the document and does not care. That is what lets the
cheap path be the default without the expensive path being a rewrite.

Failure posture: attempts are counted and a document that keeps failing
stops being retried and starts being VISIBLE. Churning a paid extractor
forever against a corrupt PDF is how an intake queue silently spends
money.
"""

import json
import os
from datetime import date

import object_intake
import object_records
import object_user_files

ACTOR = "system_scan_processor"

DEFAULT_LIMIT = 25
MAX_ATTEMPTS = 3


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default=""):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _int(value, default=0):
    try:
        return int(str(value or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _text(value):
    return str(value if value is not None else "").strip()


def _read_text(base, scan, engine):
    """Whatever text we can get, and how we got it.

    Returns (text, engine_used, error, recoverable). `recoverable` is
    carried explicitly rather than sniffed out of the error message
    later, because the two failures need opposite treatment and telling
    them apart by substring is the kind of thing that silently stops
    working when somebody rewords a message:

      recoverable -- the HOST is not set up (a package missing, an engine
        not built in). The document is fine; leave it pending so fixing
        the host drains the backlog.
      not recoverable -- THIS document could not be read. Count the
        attempt against it, so a corrupt file surfaces instead of
        churning an extractor forever.

    Text already on the scan wins outright: a forwarded email receipt
    needs no OCR, and re-reading an image we already read is pure cost.
    """
    existing = _text(scan.get("ocr_text"))
    if existing:
        return existing, _text(scan.get("ocr_engine")) or "supplied", "", False

    if engine == "tesseract":
        try:
            import pytesseract                      # noqa: F401
            from PIL import Image                   # noqa: F401
        except ImportError:
            return "", "", ("tesseract engine selected but pytesseract/PIL are "
                            "not installed on this host"), True
        try:
            import io

            content = object_user_files.read_file(
                _text(scan.get("owner_id")), _text(scan.get("file_id")), base_dir=base)
            image = Image.open(io.BytesIO(content))
            return pytesseract.image_to_string(image), "tesseract", "", False
        except Exception as exc:
            return "", "", f"tesseract failed: {str(exc)[:120]}", False

    if engine == "ai_vision":
        try:
            import object_ai
        except ImportError:
            return "", "", "ai_vision engine selected but object_ai is unavailable", True
        reader = getattr(object_ai, "read_document", None)
        if reader is None:
            return "", "", ("ai_vision engine selected but object_ai has no "
                            "document reader on this build"), True
        try:
            content = object_user_files.read_file(
                _text(scan.get("owner_id")), _text(scan.get("file_id")), base_dir=base)
        except Exception as exc:
            return "", "", f"could not read the stored document: {str(exc)[:120]}", False
        try:
            return (_text(reader(content, kind=_text(scan.get("category_hint")))),
                    "ai_vision", "", False)
        except Exception as exc:
            return "", "", f"ai_vision failed: {str(exc)[:120]}", False

    # text_rules and anything unrecognised: we read what is there, which
    # for an image with no OCR engine is nothing -- and nothing is an
    # honest answer that still leaves a confirmable draft.
    return "", "text_rules", "", False


def POST(request):
    base = _base_dir()
    limit = _int(request.get("limit"), DEFAULT_LIMIT) or DEFAULT_LIMIT
    engine = (_text(request.get("engine"))
              or _setting(base, "intake.ocr_engine", "text_rules"))

    try:
        scans = object_records.read_collection_records("scans", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "intake not installed (scans absent)"}

    pending = [row for row in scans
               if _text(row.get("status")) in ("pending", "processing")
               and _int(row.get("attempts")) < MAX_ATTEMPTS]
    pending.sort(key=lambda row: _text(row.get("created_at")))

    read = extracted = failed = 0
    results = []
    for scan in pending[:limit]:
        attempts = _int(scan.get("attempts")) + 1
        text, engine_used, error, recoverable = _read_text(base, scan, engine)
        if error:
            # A host misconfiguration leaves the document PENDING (fix the
            # host, run again); a genuine read failure counts against it.
            object_records.update_collection_record(
                "scans", scan["id"],
                {"status": "pending" if recoverable else "error",
                 "attempts": str(attempts), "error": error},
                base_dir=base, actor=ACTOR)
            failed += 1
            results.append({"scan": scan["id"], "error": error})
            continue

        suggestion = object_intake.guess_from_text(
            text, kind_hint=_text(scan.get("category_hint")))
        if engine_used and engine_used not in ("text_rules", "supplied"):
            suggestion["engine"] = engine_used
        object_records.update_collection_record(
            "scans", scan["id"],
            {
                "status": "extracted",
                "ocr_text": text,
                "ocr_engine": engine_used or "text_rules",
                "extracted": json.dumps(suggestion),
                "confidence": str(suggestion["confidence"]),
                "attempts": str(attempts),
                "error": "",
            },
            base_dir=base, actor=ACTOR)
        read += 1 if text else 0
        extracted += 1
        results.append({"scan": scan["id"], "engine": engine_used,
                        "total_cents": suggestion["total_cents"],
                        "confidence": suggestion["confidence"]})

    stuck = [row["id"] for row in scans
             if _int(row.get("attempts")) >= MAX_ATTEMPTS
             and _text(row.get("status")) == "error"]
    return {"ok": True, "today": _text(request.get("today")) or date.today().isoformat(),
            "engine": engine, "considered": len(pending), "extracted": extracted,
            "text_read": read, "failed": failed,
            "needing_a_human": stuck, "results": results}
