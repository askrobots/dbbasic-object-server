"""Pre-write hook for wallet_entries: you cannot spend what is not there,
and you cannot post the same event twice.

Two rules, and the second one arrived later than the first.

## Rule 2: a provenance marker is used once

Doctrine #7 says a replayed event posts nothing twice, and every money
writer on this box already honours it -- checkout stamps
`checkout/{order_id}`, the auto top-up stamps
`auto_topup/{wallet}/{period}/{amount}`, dispute resolution stamps the
dispute, the template runner stamps `template_run/{id}/{leg}`. But each
of them enforces it the same way: READ the ledger, look for the marker,
then write. That is check-then-write, and it is only as good as the
absence of a second writer between the two steps.

The second writer is not hypothetical. `system_wallet_replenish` runs on
a daily schedule AND is executable by hand; two overlapping passes both
find no marker and both credit the wallet. The Stripe charge itself is
safe -- that pass sends its marker as Stripe's `idempotency_key`, which
is exactly right and means the CARD is charged once -- but the wallet
credit that follows has no such protection, so the customer is credited
twice for one charge and the shop eats the difference.

Enforcing it here makes doctrine #7 a property of the collection rather
than a habit of six callers, and it covers writers that do not exist yet:
the image and video handlers coming to app-runner have precisely this
shape. The callers keep their own checks -- a caller that can skip the
work entirely should skip it rather than attempt a write and be refused
-- but the ledger no longer depends on them being right.

**A blank marker is not a collision.** Most entries have none: a manual
top-up, an adjustment, an ordinary debit. Uniqueness is a property of the
markers that exist, not a requirement that every entry carry one -- the
same view hook_wallets takes of a blank gift-card code.

**Both directions are checked**, credits included. That is the case that
motivated the rule, and it is why the marker check runs BEFORE the
"money arriving is never gated" shortcut below: a duplicated credit is
money leaving the business just as surely as a duplicated debit is money
leaving a customer.

## Rule 1: you cannot spend what is not there

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

    wallet_id = str(record.get("wallet_id") or "").strip()
    marker = str(record.get("generated_from") or "").strip()
    base = _base_dir()

    # One read serves both rules. The marker check needs every row (a
    # marker is unique across the ledger, not per wallet); the balance
    # needs this wallet's rows.
    try:
        entries = object_records.read_collection_records("wallet_entries",
                                                         base_dir=base)
    except Exception:
        # Fails CLOSED, in both directions and for both rules. An
        # unreadable ledger means an unknown balance AND unknown markers,
        # and posting into that is how a duplicate becomes permanent.
        return {"error": "Wallet ledger is unreadable; refusing to write against "
                         "an unknown balance and unknown history.", "status": 409}

    # Rule 2, before the credit shortcut: a duplicated credit is money
    # leaving the business exactly as a duplicated debit is money leaving
    # a customer.
    if marker:
        for row in entries:
            if str(row.get("generated_from") or "").strip() == marker:
                return {
                    "error": (f"This event has already been posted to the "
                              f"ledger ({marker}). A provenance marker is used "
                              f"once: whatever produced it has run before, and "
                              f"posting again would move money twice for one "
                              f"event."),
                    "status": 409,
                }

    if amount > 0:
        return None  # money arriving is never gated on balance

    if not wallet_id:
        return None  # required/relation validation owns this

    # Sum the entries themselves: the gate must not depend on a rollup it
    # could recompute (see module docstring).
    balance = 0
    for row in entries:
        if row.get("wallet_id") == wallet_id:
            balance += _int(row.get("amount_minor"))

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
