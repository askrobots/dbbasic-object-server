"""action_record_count -- reconcile the accounts nobody sends a statement for.

POST {value_account_id, counted_minor, counted_on?, witnessed_by?, notes?}

A till, a petty-cash box, a safe: real money, and no third party will ever
send you a statement about it. The only evidence available is that someone
counted it. That makes this the weakest reconciliation in the system and
the one most worth doing honestly, because a drawer is where a slow leak
hides best.

Three things follow from that, and they are the whole object:

1. **The count is evidence, so it is a record** -- who counted, who
   witnessed, what the books said at that moment (stamped, #1), and the
   variance. Append-only: a count is an observation, not a figure to
   revise (#8).
2. **A witness can be required.** value_accounts.requires_second_attestor
   refuses a self-certified count outright, because the person counting the
   till is usually the person who could take from it. This is the oldest
   control in retail and it is one field.
3. **A variance composes a real journal.** Cash over/short is an expense a
   business genuinely incurs; booking it keeps the ledger equal to reality
   and makes the leak visible as a number that accumulates. Pretending the
   drawer always balances is how years go by.

Structurally this is the same verb as action_apply_count in app-catalog:
count the thing, compare to the derived book figure, write ONE compensating
record for the difference. Inventory shrinkage and cash shortage are the
same event in different denominations, and they book the same way.
"""

import os
from datetime import date

import object_finance
import object_ids
import object_money
import object_records

ACTOR = "action_record_count"

COUNTABLE_KINDS = ("cash_box", "metal", "gift_card", "other")


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


def _book_balance_minor(base, fin_account_id, as_of):
    """What the ledger says this account holds, over POSTED journals only.

    Same fold and same posted-only rule the trial balance and the bank
    reconciliation use -- an asset account, so debits increase it.
    """
    if not fin_account_id:
        return None
    posted = {j.get("id") for j in
              object_records.read_collection_records("fin_journals", base_dir=base)
              if j.get("status") == "posted"
              and (not as_of or (j.get("date") or "") <= as_of)}
    debits = credits = 0
    for line in object_records.read_collection_records("fin_journal_lines", base_dir=base):
        if line.get("journal_id") not in posted or line.get("account_id") != fin_account_id:
            continue
        debits += int(line.get("debit_cents") or 0)
        credits += int(line.get("credit_cents") or 0)
    return debits - credits


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id") or ""
    is_admin = "admin" in (identity.get("roles") or [])
    if not user_id:
        return {"status": 403, "error": "Sign in to record a count."}

    account_id = str(request.get("value_account_id") or "").strip()
    if not account_id:
        return {"status": 400, "error": "value_account_id is required."}
    try:
        counted = int(request.get("counted_minor"))
    except (TypeError, ValueError):
        return {"status": 400,
                "error": "counted_minor is required, as a whole number of the "
                         "denomination's smallest unit."}
    if counted < 0:
        return {"status": 400, "error": "A count cannot be negative."}

    base = _base_dir()
    try:
        account = object_records.get_collection_record(
            "value_accounts", account_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"Value account not found: {account_id}"}
    if account.get("owner_id") and account["owner_id"] != user_id and not is_admin:
        return {"status": 403, "error": "That account belongs to someone else."}

    if (account.get("verification") or "") != "physical_count":
        return {"status": 409,
                "error": (f"This account is verified by "
                          f"{account.get('verification') or 'nothing'}, not by counting. "
                          "Import its statement instead, or change how the account is "
                          "verified if it really is counted by hand.")}

    witnessed_by = str(request.get("witnessed_by") or "").strip()
    if str(account.get("requires_second_attestor") or "").lower() in ("true", "1", "yes") \
            and not witnessed_by:
        return {"status": 409,
                "error": ("This account requires a witness: a count of it is only "
                          "evidence if a second person attests. Pass witnessed_by.")}
    if witnessed_by and witnessed_by == user_id:
        return {"status": 409,
                "error": "The witness must be someone other than the person counting."}

    counted_on = str(request.get("counted_on") or "").strip() or date.today().isoformat()
    fin_account_id = account.get("fin_account_id") or ""
    book_balance = _book_balance_minor(base, fin_account_id, counted_on)
    variance = None if book_balance is None else counted - book_balance

    count_id = object_ids.new_uuid4()
    journal = None
    journal_id = ""
    if variance:
        over_short = _setting(base, "reconcile.journal.cash_over_short_account")
        if not over_short:
            return {"status": 409,
                    "error": ("A variance needs somewhere to go: set app_settings "
                              "reconcile.journal.cash_over_short_account first. "
                              "Cash over/short is a real expense and must not be "
                              "silently dropped.")}
        amount = abs(variance)
        shortage = variance < 0
        journal = object_finance.compose_posted_journal(
            base,
            generated_from=f"value_account_counts/{count_id}",
            date=counted_on,
            description=(f"Cash {'shortage' if shortage else 'overage'} on "
                         f"{account.get('name') or account_id} ({counted_on})"),
            lines=[
                {"account_id": over_short if shortage else fin_account_id,
                 "debit_cents": amount, "credit_cents": 0},
                {"account_id": fin_account_id if shortage else over_short,
                 "debit_cents": 0, "credit_cents": amount},
            ],
            owner_id=account.get("owner_id") or user_id,
            entity_id=account.get("entity_id", ""),
            actor=ACTOR,
        )
        journal_id = journal.get("journal_id", "") if journal.get("ok") else ""

    record = {
        "id": count_id,
        "value_account_id": account_id,
        "counted_on": counted_on,
        "counted_minor": str(counted),
        "book_balance_minor": "" if book_balance is None else str(book_balance),
        "variance_minor": "" if variance is None else str(variance),
        "counted_by": user_id,
        "witnessed_by": witnessed_by,
        "journal_id": journal_id,
        "notes": str(request.get("notes") or "").strip(),
        "owner_id": account.get("owner_id") or user_id,
    }
    if account.get("entity_id"):
        record["entity_id"] = account["entity_id"]
    object_records.create_collection_record(
        "value_account_counts", record, base_dir=base, actor=ACTOR)

    return {
        "status": 200,
        "count_id": count_id,
        "counted_minor": counted,
        "book_balance_minor": book_balance,
        "variance_minor": variance,
        "journal": journal,
        "assurance": object_money.assurance_for("physical_count",
                                                witnessed=bool(witnessed_by)),
        "note": ("counted balance agrees with the books" if variance == 0 else
                 "no book account set, so no variance could be computed"
                 if variance is None else None),
    }
