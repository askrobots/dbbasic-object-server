"""action_import_ofx -- import an OFX/QFX download.

POST {bank_account_id, content, opening_balance_cents?, profile_id?}

OFX is the format worth supporting after CSV, because it carries the two
things a CSV usually cannot: a stable per-transaction id (FITID), which
makes deduplication exact instead of heuristic, and a stated closing
balance (LEDGERBAL), which lets the statement check its own arithmetic.
Parsing lives in object_ofx.py; this action is the same landing path
action_import_bank_csv uses, so everything downstream -- gates, dedup,
matching, resolution, reconciliation -- is unchanged. One canonical shape,
thin importers (plan/bank-import-reconciliation-spec.md section 3).

**Chained openings.** OFX states a closing balance but has no standard
opening-balance tag at all, so a lone file cannot tie out. It can once
there is a prior statement: last month's closing IS this month's opening.
When the caller does not supply one, this action derives the opening from
the account's previous import and records in the import's flags that the
figure was DERIVED rather than stated. That distinction matters -- a
derived opening makes continuity true by construction, so the check that
actually earns its keep is tie-out: do this statement's transactions
explain the movement from the last statement's end to this one's? Claiming
both checks passed when one was tautological would be exactly the
comfortable lie the flags exist to prevent.
"""

import json
import os

import object_banking
import object_ids
import object_ofx
import object_records

ACTOR = "action_import_ofx"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    if not user_id:
        return {"status": 403, "error": "Sign in to import a statement."}

    bank_account_id = str(request.get("bank_account_id") or "").strip()
    content = request.get("content")
    if not bank_account_id or not isinstance(content, str) or not content.strip():
        return {"status": 400, "error": "bank_account_id and content are required."}

    base = _base_dir()
    try:
        account = object_records.get_collection_record(
            "bank_accounts", bank_account_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Bank account not found: {bank_account_id}"}
    if account.get("owner_id") and account["owner_id"] != user_id \
            and "admin" not in (identity.get("roles") or []):
        return {"status": 403, "error": "That bank account belongs to someone else."}

    digest = object_banking.file_hash(content)
    prior = object_banking.find_import_by_file_hash(bank_account_id, digest, base_dir=base)
    if prior is not None:
        return {"status": 200, "already_imported": True, "import_id": prior.get("id"),
                "imported": 0, "duplicates": 0,
                "note": "This exact file was already imported; nothing changed."}

    try:
        parsed = object_ofx.parse_ofx(content)
    except object_ofx.OfxError as exc:
        return {"status": 422, "error": f"Could not read the statement: {exc}"}
    lines = object_banking.assign_line_hashes(parsed["lines"])
    if not lines:
        return {"status": 422, "error": "The statement had no transactions."}

    period_start = parsed["period"].get("start") or ""
    period_end = parsed["period"].get("end") or ""
    closing = parsed["balances"].get("closing_balance_cents")

    opening = request.get("opening_balance_cents")
    opening_derived = False
    if not object_banking.is_present(opening):
        previous = object_banking.previous_import(
            bank_account_id, base_dir=base, before=period_start)
        if previous is not None and str(previous.get("closing_balance_cents") or "").strip():
            opening = int(previous["closing_balance_cents"])
            opening_derived = True

    gates = object_banking.run_gates(
        lines, bank_account_id=bank_account_id,
        opening_balance_cents=opening, closing_balance_cents=closing,
        period_start=period_start, base_dir=base)
    checks = dict(gates["checks"])
    if opening_derived:
        # Say plainly that continuity could not fail here: it compared the
        # previous closing balance against itself.
        checks["opening_balance"] = {
            "ran": True, "derived": True,
            "detail": ("OFX states no opening balance; used the previous statement's "
                       "closing balance, so continuity is true by construction and "
                       "tie-out is the check that carries weight here"),
        }
        if isinstance(checks.get("continuity"), dict):
            checks["continuity"]["tautological"] = True
    gates = {**gates, "checks": checks}

    import_id = object_ids.new_uuid4()
    import_row = {
        "id": import_id,
        "bank_account_id": bank_account_id,
        "profile_id": str(request.get("profile_id") or "").strip(),
        "source_format": "ofx",
        "file_hash": digest,
        "period_start": period_start,
        "period_end": period_end,
        "opening_balance_cents": str(opening) if object_banking.is_present(opening) else "",
        "closing_balance_cents": str(closing) if closing is not None else "",
        "line_count": str(len(lines)),
        "status": gates["status"],
        "flags": json.dumps(checks, sort_keys=True),
        "owner_id": user_id,
    }
    if account.get("entity_id"):
        import_row["entity_id"] = account["entity_id"]
    object_records.create_collection_record(
        "bank_statement_imports", import_row, base_dir=base, actor=ACTOR)

    known_ids, known_hashes = object_banking.existing_line_keys(bank_account_id, base_dir=base)
    imported = duplicates = 0
    for line in lines:
        # FITID first: the bank's own id survives the bank re-wording a memo,
        # which a content hash does not.
        if (line.get("external_id") and line["external_id"] in known_ids) or \
                line["line_hash"] in known_hashes:
            duplicates += 1
            continue
        row = {
            "id": object_ids.new_uuid4(),
            "import_id": import_id,
            "bank_account_id": bank_account_id,
            "posted_on": line["posted_on"],
            "amount_cents": str(line["amount_cents"]),
            "description": line["description"],
            "external_id": line["external_id"],
            "line_hash": line["line_hash"],
            "raw": line["raw"],
            "match_status": "unmatched",
            "owner_id": user_id,
        }
        if account.get("entity_id"):
            row["entity_id"] = account["entity_id"]
        object_records.create_collection_record("bank_lines", row, base_dir=base, actor=ACTOR)
        if line.get("external_id"):
            known_ids.add(line["external_id"])
        known_hashes.add(line["line_hash"])
        imported += 1

    return {
        "status": 200,
        "import_id": import_id,
        "imported": imported,
        "duplicates": duplicates,
        "import_status": gates["status"],
        "failed_checks": gates["failed"],
        "checks": checks,
        "opening_derived": opening_derived,
        "period": {"start": period_start, "end": period_end},
        "account": parsed.get("account", {}),
    }
