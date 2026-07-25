"""action_scan_ingest -- get the paper in, fast, and get out of the way.

POST {content_base64 | text, filename?, content_type?, source?,
      category_hint?, notes?}

Ingest is deliberately dumb and deliberately quick. It stores the bytes,
records that a document arrived, and returns -- no OCR, no model call, no
waiting. The reason is behavioural rather than technical: capture has to
happen at the moment the paper exists, standing at a till with a phone,
and anything that makes that moment slower is a receipt that ends up in a
coat pocket instead. Reading it is the processor's problem, later.

Deduplication is by content hash and lives in the hook, so the second
photo of the same crumpled receipt lands on the scan that already exists
rather than becoming a second expense. That is the normal case, not an
edge case.

`text` is accepted alongside bytes for documents that arrive as text
already -- a forwarded email receipt, an MCP agent pasting what it read.
Same pipeline, no image, and the free extractor works on it directly.
"""

import base64
import hashlib
import os

import object_ids
import object_records
import object_user_files

ACTOR = "action_scan_ingest"

MAX_BYTES = 50 * 1024 * 1024        # the predecessor's cap, and a sane one

SOURCES = ("web", "phone", "email", "mcp", "api")
HINTS = ("receipt", "bill", "invoice", "statement", "document", "photo")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def POST(request):
    identity = request.get("_identity") or {}
    owner = (_text(request.get("owner_id"))
             or _text(identity.get("user_id"))
             or _text(identity.get("id")))
    if not owner:
        return {"status": 401,
                "error": "Ingest needs an owner: a scan belongs to somebody."}

    raw = request.get("content_base64")
    body = None
    if raw:
        try:
            body = base64.b64decode(_text(raw), validate=True)
        except Exception:
            return {"status": 400, "error": "content_base64 is not valid base64."}
    elif _text(request.get("text")):
        body = _text(request.get("text")).encode("utf-8")

    if not body:
        return {"status": 400,
                "error": "Nothing to ingest: send content_base64 or text."}
    if len(body) > MAX_BYTES:
        return {"status": 413,
                "error": f"Document exceeds the {MAX_BYTES // (1024 * 1024)}MB cap."}

    digest = hashlib.sha256(body).hexdigest()
    source = _text(request.get("source")) or "web"
    hint = _text(request.get("category_hint")) or "receipt"
    if source not in SOURCES:
        return {"status": 400, "error": f"source must be one of {', '.join(SOURCES)}"}
    if hint not in HINTS:
        return {"status": 400, "error": f"category_hint must be one of {', '.join(HINTS)}"}

    base = _base_dir()
    # The hook is the authority on duplicates, but checking here too means
    # the bytes of a re-snapped receipt are never written a second time.
    try:
        for row in object_records.read_collection_records("scans", base_dir=base):
            if (row.get("content_sha256") == digest
                    and _text(row.get("owner_id")) == owner):
                return {"ok": True, "scan_id": row["id"], "duplicate": True,
                        "status_of_scan": row.get("status"),
                        "note": "this document was already received"}
    except Exception:
        return {"status": 503,
                "error": "Scan history unreadable; refusing to ingest against "
                         "an unknown history rather than risk a duplicate."}

    scan_id = object_ids.new_uuid4()
    file_id = f"scan-{scan_id}"
    try:
        size = object_user_files.save_file(owner, file_id, body, base_dir=base)
    except Exception as exc:
        return {"status": 500, "error": f"Could not store the document: {str(exc)[:120]}"}

    is_text = _text(request.get("text")) and not raw
    object_records.create_collection_record(
        "scans",
        {
            "id": scan_id,
            "filename": _text(request.get("filename")) or f"{hint}-{scan_id[:8]}",
            "content_type": (_text(request.get("content_type"))
                             or ("text/plain" if is_text else "application/octet-stream")),
            "size": str(size),
            "content_sha256": digest,
            "file_id": file_id,
            "source": source,
            "category_hint": hint,
            "status": "pending",
            # Text that arrived as text needs no OCR pass to have text.
            "ocr_text": body.decode("utf-8", "replace") if is_text else "",
            "notes": _text(request.get("notes")),
            "owner_id": owner,
            "entity_id": _text(request.get("entity_id")),
        },
        base_dir=base, actor=ACTOR)

    return {"ok": True, "scan_id": scan_id, "size": size, "status_of_scan": "pending",
            "note": "stored; reading happens on the next intake pass"}
