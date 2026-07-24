"""system_bank_matcher -- propose book-side records for unmatched bank lines.

POST {bank_account_id?, today?} -- runs over unmatched lines and writes what
it found onto each: match_status=suggested plus a `suggestions` JSON block
saying which records, at which tier, and WHY. Schedulable daily like any
other runner (the scheduler board shows its results).

The important restraint: **the matcher suggests, a person confirms.** Only
tier 1 (the record's reference appears in the statement text AND the amount
agrees -- two independent signals) is auto-confirmable, and even that is
configurable down to nothing:

    reconcile.auto_match_tier   0 = never auto-match, 1 = tier 1 (default)
    reconcile.date_window_days  how far apart a line and a record may be (5)

Confirming a match is deliberately NOT an action object -- it is an
ordinary attributed record update, so the change log names who reconciled
what and when. Reconciliation done invisibly by the same hands that move
the money is the classic fraud hole; making confirmation a normal write
closes it with machinery that already exists.

Candidates are payments (money in) and refunds (money out), each converted
to the statement's sign convention before comparison. Journals and bills
join the candidate pool when those flows exist.
"""

import json
import os

import object_banking
import object_records

ACTOR = "system_bank_matcher"

DEFAULT_WINDOW_DAYS = 5
DEFAULT_AUTO_TIER = 1


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and str(row.get("value") or "").strip():
                return row["value"].strip()
    except Exception:
        pass
    return default


def _int_setting(base, key, default):
    try:
        return int(_setting(base, key, default))
    except (TypeError, ValueError):
        return default


def _candidates(base, owner_id):
    """Book-side records this line could be, in the statement's signs."""
    out = []
    try:
        for payment in object_records.read_collection_records("payments", base_dir=base):
            if owner_id and payment.get("owner_id") and payment["owner_id"] != owner_id:
                continue
            if (payment.get("status") or "received") != "received":
                continue
            out.append({
                "ref": f"payments/{payment['id']}",
                "amount_cents": int(payment.get("amount_cents") or 0),   # money in: positive
                "date": payment.get("received_on") or payment.get("created_at", "")[:10],
                "reference": payment.get("reference") or "",
            })
    except Exception:
        pass
    try:
        for refund in object_records.read_collection_records("refunds", base_dir=base):
            if owner_id and refund.get("owner_id") and refund["owner_id"] != owner_id:
                continue
            out.append({
                "ref": f"refunds/{refund['id']}",
                "amount_cents": -abs(int(refund.get("amount_cents") or 0)),  # money out
                "date": refund.get("refunded_on") or refund.get("created_at", "")[:10],
                "reference": refund.get("payment_id") or "",
            })
    except Exception:
        pass
    return out


def _already_matched(base):
    """Refs already claimed by another line -- one book record, one line."""
    claimed = set()
    try:
        for row in object_records.read_collection_records("bank_lines", base_dir=base):
            if row.get("matched_to"):
                claimed.add(row["matched_to"])
    except Exception:
        pass
    return claimed


def POST(request):
    base = _base_dir()
    identity = request.get("_identity") or {}
    owner_id = identity.get("user_id") or ""
    only_account = str(request.get("bank_account_id") or "").strip()

    window = _int_setting(base, "reconcile.date_window_days", DEFAULT_WINDOW_DAYS)
    auto_tier = _int_setting(base, "reconcile.auto_match_tier", DEFAULT_AUTO_TIER)

    try:
        lines = object_records.read_collection_records("bank_lines", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "banking not installed (bank_lines absent)"}

    open_lines = [l for l in lines
                  if (l.get("match_status") or "unmatched") in ("unmatched", "suggested")
                  and not (only_account and l.get("bank_account_id") != only_account)
                  and not (owner_id and l.get("owner_id") and l["owner_id"] != owner_id)]
    if not open_lines:
        return {"ok": True, "scanned": 0, "suggested": 0, "auto_matched": 0, "results": []}

    pool = _candidates(base, owner_id)
    claimed = _already_matched(base)
    available = [c for c in pool if c["ref"] not in claimed]

    suggested = auto_matched = 0
    results = []
    for line in open_lines:
        proposals = object_banking.candidate_matches(
            line, [c for c in available if c["ref"] not in claimed], window_days=window)
        if not proposals:
            continue
        best = proposals[0]
        changes = {"suggestions": json.dumps(proposals[:5])}
        outcome = "suggested"
        if best["tier"] <= auto_tier and len(best["refs"]) == 1:
            changes["match_status"] = "matched"
            changes["matched_to"] = best["refs"][0]
            claimed.add(best["refs"][0])
            auto_matched += 1
            outcome = "auto_matched"
        else:
            changes["match_status"] = "suggested"
            suggested += 1
        try:
            object_records.update_collection_record(
                "bank_lines", line["id"], changes, base_dir=base, actor=ACTOR)
        except Exception as exc:
            results.append({"line": line["id"], "error": str(exc)[:160]})
            continue
        results.append({"line": line["id"], "outcome": outcome, "tier": best["tier"],
                        "refs": best["refs"], "why": best["why"]})

    return {"ok": True, "scanned": len(open_lines), "suggested": suggested,
            "auto_matched": auto_matched, "auto_match_tier": auto_tier,
            "window_days": window, "results": results}
