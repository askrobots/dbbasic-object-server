"""action_import_bank_csv -- read a bank's CSV export through a saved profile.

POST {bank_account_id, profile_id, content, opening_balance_cents?,
closing_balance_cents?, period_start?, period_end?}

The evidence-handling posture (plan/bank-import-reconciliation-spec.md):

1. **File-level idempotency** -- the same file re-imported is a no-op,
   reported as already_imported with the original import's id. Operators
   re-upload by accident constantly; that must be boring, not destructive.
2. **Deterministic parsing** -- the profile's column_map decides how to
   read the file. AI helps AUTHOR a profile once; it never re-reads
   statements, because a fraud control cannot be nondeterministic.
3. **The statement checks itself** -- tie-out (opening + lines == closing)
   and continuity (this opening == the previous statement's closing) run
   here, and their verdict SETS the import's status. Flagged imports still
   store their lines: evidence is evidence, and hiding a truncated
   statement is exactly the failure this control exists to catch.
4. **Line-level dedup** -- lines already on file for this account (by the
   bank's transaction id, else a content hash) are skipped and counted,
   so overlapping date ranges between statements are safe. This action
   writes at the storage level (never through the HTTP hook), so it
   performs the same check hook_bank_lines enforces for direct writers.

Nothing here posts to the books. Imported lines are the OTHER party's
record; journals only ever come from resolving a line deliberately.
"""

import json
import os

import object_banking
import object_ids
import object_records

ACTOR = "action_import_bank_csv"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    if not user_id:
        return {"status": 403, "error": "Sign in to import a statement."}

    bank_account_id = str(request.get("bank_account_id") or "").strip()
    profile_id = str(request.get("profile_id") or "").strip()
    content = request.get("content")
    if not bank_account_id or not isinstance(content, str) or not content.strip():
        return {"status": 400, "error": "bank_account_id and content are required."}

    base = _base_dir()
    try:
        account = object_records.get_collection_record(
            "value_accounts", bank_account_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Bank account not found: {bank_account_id}"}
    if account.get("owner_id") and account["owner_id"] != user_id and "admin" not in (identity.get("roles") or []):
        return {"status": 403, "error": "That bank account belongs to someone else."}

    column_map = {}
    profile = None
    if profile_id:
        try:
            profile = object_records.get_collection_record(
                "bank_import_profiles", profile_id, base_dir=base)
        except Exception:
            return {"status": 404, "error": f"Import profile not found: {profile_id}"}
        try:
            column_map = json.loads(profile.get("column_map") or "{}")
        except (ValueError, TypeError):
            return {"status": 409,
                    "error": f"Profile {profile_id} has an unparseable column_map."}
    if not column_map:
        column_map = request.get("column_map") or {}
    if not isinstance(column_map, dict) or not column_map:
        return {"status": 400,
                "error": "No column_map: pass profile_id (preferred) or an inline column_map."}

    digest = object_banking.file_hash(content)
    prior = object_banking.find_import_by_file_hash(bank_account_id, digest, base_dir=base)
    if prior is not None:
        return {"status": 200, "already_imported": True, "import_id": prior.get("id"),
                "imported": 0, "duplicates": 0,
                "note": "This exact file was already imported; nothing changed."}

    try:
        parsed = object_banking.parse_statement_csv(content, column_map)
    except object_banking.ImportError_ as exc:
        return {"status": 422, "error": f"Could not read the statement: {exc}"}
    if not parsed:
        return {"status": 422, "error": "The statement had no data rows."}
    lines = object_banking.assign_line_hashes(parsed)

    dates = sorted(l["posted_on"] for l in lines if l.get("posted_on"))
    period_start = str(request.get("period_start") or "").strip() or (dates[0] if dates else "")
    period_end = str(request.get("period_end") or "").strip() or (dates[-1] if dates else "")

    gates = object_banking.run_gates(
        lines, bank_account_id=bank_account_id,
        opening_balance_cents=request.get("opening_balance_cents"),
        closing_balance_cents=request.get("closing_balance_cents"),
        period_start=period_start, base_dir=base)

    import_id = object_ids.new_uuid4()
    import_row = {
        "id": import_id,
        "bank_account_id": bank_account_id,
        "profile_id": profile_id,
        "source_format": (profile or {}).get("source_format", "csv") or "csv",
        "file_hash": digest,
        "period_start": period_start,
        "period_end": period_end,
        # is_present, not `or ""`: a zero balance is a real balance.
        "opening_balance_cents": (str(request.get("opening_balance_cents")).strip()
                                  if object_banking.is_present(request.get("opening_balance_cents")) else ""),
        "closing_balance_cents": (str(request.get("closing_balance_cents")).strip()
                                  if object_banking.is_present(request.get("closing_balance_cents")) else ""),
        "line_count": str(len(lines)),
        "status": gates["status"],
        "flags": object_banking.flags_json(gates),
        "owner_id": user_id,
    }
    if account.get("entity_id"):
        import_row["entity_id"] = account["entity_id"]
    object_records.create_collection_record(
        "bank_statement_imports", import_row, base_dir=base, actor=ACTOR)

    known_ids, known_hashes = object_banking.existing_line_keys(bank_account_id, base_dir=base)
    imported = duplicates = 0
    for line in lines:
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
        "checks": gates["checks"],
        "period": {"start": period_start, "end": period_end},
    }
