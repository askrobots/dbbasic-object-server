"""Pre-write hook for wallet_entries: you cannot spend what is not there.

The gate sums the ENTRIES rather than reading wallets.balance_minor, even
though that rollup exists and is usually right. A gate must be
authoritative, and a derived caption is not: if the rollup were ever
stale or blank, trusting it would authorize a debit against money that
does not exist (docs/business-logic-patterns.md -- never let a gate read
only a derived value it could recompute).

Overdraft is a setting rather than a hard zero, because the honest limit
is not always zero: billing.wallet.overdraft_minor lets a high-frequency
API workload tolerate a few cents of race rather than fail a customer's
request mid-flight. Which brings up the thing worth stating plainly
instead of hiding:

**The known race.** Check-then-append is two steps. Two debits arriving
at the same instant can both pass this gate and both land, taking the
balance slightly below the floor. The write lock serialises the writes
but not the decision. This is tolerated deliberately in v1 -- the
exposure is bounded by the overdraft floor and by how small individual
debits are -- and the real fix (reserve/capture, an auth hold that makes
the check and the claim one atomic act) waits for a workload that
actually shows the race, per doctrine #4. Building a hold system before
a single customer has raced would be inventing a primitive from
imagination.

Credits are never gated: money arriving is always allowed.
"""

import os

import object_records

DEFAULT_OVERDRAFT_MINOR = 0


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


def _int(value, default=0):
    try:
        return int(str(value or "0").strip() or default)
    except (TypeError, ValueError):
        return default


def BEFORE_WRITE(request):
    if request.get("action") != "create":
        return None
    record = request.get("record") or {}

    amount = _int(record.get("amount_minor"))
    if amount == 0:
        return {"error": "A wallet entry of zero moves nothing; record nothing.",
                "status": 400}
    if amount > 0:
        return None  # money arriving is never gated

    wallet_id = str(record.get("wallet_id") or "").strip()
    if not wallet_id:
        return None  # required/relation validation owns this

    base = _base_dir()
    # Sum the entries themselves: the gate must not depend on a rollup it
    # could recompute (see module docstring).
    balance = 0
    try:
        for row in object_records.read_collection_records("wallet_entries", base_dir=base):
            if row.get("wallet_id") == wallet_id:
                balance += _int(row.get("amount_minor"))
    except Exception:
        # Unreadable ledger means unknown balance; refusing a debit is the
        # safe direction -- a declined charge is recoverable, an unfunded
        # one is a collection problem.
        return {"error": "Wallet ledger is unreadable; refusing to debit against an "
                         "unknown balance.", "status": 409}

    floor = -abs(_int(_setting(base, "billing.wallet.overdraft_minor",
                               DEFAULT_OVERDRAFT_MINOR)))
    remaining = balance + amount
    if remaining < floor:
        return {
            "error": (f"Insufficient wallet balance: this would leave {remaining} "
                      f"(minor units) against a floor of {floor}. Top up, or raise "
                      "app_settings billing.wallet.overdraft_minor if small overdrafts "
                      "are acceptable for this workload."),
            "status": 402,
        }
    return None
