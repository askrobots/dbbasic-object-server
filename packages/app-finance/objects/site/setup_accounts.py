"""setup_accounts -- 65 multi-entity, slice 4: seed an entity's chart of
accounts from its mode.

POST /finance/setup-accounts {entity_id}. A faithful port of the predecessor's
Entity.create_default_accounts / the setup_finance_accounts MCP tool: given one
of the caller's entities, create its fin_accounts based on the entity's `mode`
(simple = income + expenses; standard = a 15-account chart; double_entry = a
full 29-account chart). Idempotent -- an account already present for the entity
(matched by name, the predecessor's get_or_create key) is skipped, so running
it twice, or after adding a few accounts by hand, never duplicates.

Owner-gated in the handler (like app-timers' timer_actions): the entity must
belong to the caller, and every account is created owned by the caller and
scoped to that entity_id. No public/cross-owner path -- a caller can only set
up accounts for their own set of books.
"""

import json
import os

import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
ACCOUNTS_COLLECTION = "fin_accounts"
ENTITIES_COLLECTION = "entities"

# (account_type, name, code) per mode -- carried verbatim from the predecessor's
# Entity.STANDARD_ACCOUNTS / DOUBLE_ENTRY_ACCOUNTS (private source audit).
_SIMPLE = [
    ("income", "Income", "4000"),
    ("expense", "Expenses", "5000"),
]
_STANDARD = [
    ("asset", "Cash", "1010"), ("asset", "Checking Account", "1020"),
    ("asset", "Savings Account", "1030"), ("asset", "Accounts Receivable", "1100"),
    ("liability", "Accounts Payable", "2100"), ("income", "Income", "4000"),
    ("expense", "Cost of Goods Sold", "5000"), ("expense", "Rent", "5100"),
    ("expense", "Utilities", "5200"), ("expense", "Software & Subscriptions", "5300"),
    ("expense", "Marketing", "5400"), ("expense", "Travel", "5500"),
    ("expense", "Meals & Entertainment", "5600"), ("expense", "Payroll", "5700"),
    ("expense", "Taxes", "5800"),
]
_DOUBLE_ENTRY = [
    ("asset", "Cash", "1010"), ("asset", "Checking Account", "1020"),
    ("asset", "Savings Account", "1030"), ("asset", "Accounts Receivable", "1100"),
    ("asset", "Inventory", "1200"), ("asset", "Equipment", "1500"),
    ("asset", "Prepaid Expenses", "1510"), ("liability", "Accounts Payable", "2100"),
    ("liability", "Credit Card", "2200"), ("liability", "Sales Tax Payable", "2300"),
    ("liability", "Loans Payable", "2400"), ("equity", "Owner's Equity", "3100"),
    ("equity", "Retained Earnings", "3200"), ("equity", "Owner's Draw", "3300"),
    ("income", "Sales Revenue", "4100"), ("income", "Service Revenue", "4200"),
    ("income", "Other Income", "4900"), ("expense", "Cost of Goods Sold", "5000"),
    ("expense", "Rent", "5100"), ("expense", "Utilities", "5200"),
    ("expense", "Software & Subscriptions", "5300"), ("expense", "Marketing", "5400"),
    ("expense", "Travel", "5500"), ("expense", "Meals & Entertainment", "5600"),
    ("expense", "Payroll", "5700"), ("expense", "Insurance", "5800"),
    ("expense", "Depreciation", "5810"), ("expense", "Interest Expense", "5820"),
    ("expense", "Taxes", "5830"),
]
CHART_BY_MODE = {"simple": _SIMPLE, "standard": _STANDARD, "double_entry": _DOUBLE_ENTRY}


def _data_dir():
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _error(status, message):
    return {"content_type": "application/json", "status": status,
            "body": json.dumps({"status": "error", "error": message})}


def POST(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    if not user_id:
        return _error(401, "Sign in to set up accounts.")

    entity_id = str(request.get("entity_id") or "").strip()
    if not entity_id:
        return _error(400, "entity_id is required.")

    base_dir = _data_dir()
    try:
        entity = object_records.get_collection_record(ENTITIES_COLLECTION, entity_id, base_dir=base_dir)
    except (object_records.RecordNotFoundError, object_records.InvalidRecordIdError):
        return _error(404, f"Entity not found: {entity_id}")
    if entity.get("owner_id") != user_id:
        return _error(403, "That entity belongs to another owner.")

    mode = (entity.get("mode") or "simple").strip()
    chart = CHART_BY_MODE.get(mode)
    if chart is None:
        return _error(400, f"Unknown entity mode: {mode!r}")

    # Idempotent: match the predecessor's get_or_create-by-name. Only this
    # entity's existing accounts count -- two entities may share account names.
    try:
        all_accounts = object_records.read_collection_records(ACCOUNTS_COLLECTION, base_dir=base_dir)
    except (LookupError, OSError, ValueError):
        all_accounts = []
    have = {a.get("name") for a in all_accounts if a.get("entity_id") == entity_id}

    created, skipped = 0, 0
    for account_type, name, code in chart:
        if name in have:
            skipped += 1
            continue
        object_records.create_collection_record(
            ACCOUNTS_COLLECTION,
            {"name": name, "code": code, "account_type": account_type,
             "entity_id": entity_id, "owner_id": user_id, "is_active": "true"},
            base_dir=base_dir, actor="setup_accounts",
        )
        created += 1

    _logger.info("setup_accounts", entity_id=entity_id, mode=mode, created=created, skipped=skipped)
    return {"content_type": "application/json",
            "body": json.dumps({"status": "ok", "mode": mode,
                                "created": created, "skipped": skipped})}
