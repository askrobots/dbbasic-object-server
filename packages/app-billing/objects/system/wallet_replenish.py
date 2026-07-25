"""system_wallet_replenish -- top up a low wallet from the saved card.

POST {today?, dry_run?} -- a daemon pass (schedulable hourly): for each
wallet with auto top-up enabled and a balance under its threshold, charge
the saved payment method off-session and record the credit.

Three safeguards, and the middle one is the whole reason this object is
written carefully rather than quickly:

1. **A monthly cap, enforced by summing this month's auto top-ups.**
   The predecessor tracked replenished-this-month for the same reason:
   auto-charging plus one runaway metering bug is how a customer wakes up
   to a four-figure surprise. The cap turns that into a bounded annoyance
   somebody notices. A cap of zero means auto top-up is off -- the safe
   default, since a blank number must never read as "unlimited".
2. **Idempotency by provenance.** Each charge stamps
   generated_from = "auto_topup/{wallet}/{period}", and a wallet that
   already has an entry for this period+attempt is skipped. A daemon that
   runs twice, or restarts mid-pass, must not charge twice.
3. **Failure is reported, never retried blindly.** A declined card -- and
   especially one needing authentication (SCA), which off-session charges
   cannot satisfy -- disables nothing and loops nothing: it records the
   attempt so a human (or a dunning email with a pay link) can act. A
   retry loop against a declining card is how a business gets its
   processor account flagged.

Stripe is optional: unconfigured, the pass reports what it WOULD charge
and writes nothing, so the rule can be tuned before any money moves.
"""

import os
from datetime import date

import object_records
import object_stripe

ACTOR = "system_wallet_replenish"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _int(value, default=0):
    try:
        return int(str(value or "0").strip() or default)
    except (TypeError, ValueError):
        return default


def _truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _entries(base):
    try:
        return object_records.read_collection_records("wallet_entries", base_dir=base)
    except Exception:
        return []


def POST(request):
    base = _base_dir()
    today = str(request.get("today") or date.today().isoformat())
    period = today[:7]                      # calendar month, the cap's window
    dry_run = _truthy(request.get("dry_run"))
    config = object_stripe.stripe_config_from_env()

    try:
        wallets = object_records.read_collection_records("wallets", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "billing not installed (wallets absent)"}

    entries = _entries(base)
    by_wallet = {}
    for row in entries:
        by_wallet.setdefault(row.get("wallet_id"), []).append(row)

    charged = skipped = failed = 0
    results = []
    for wallet in wallets:
        wallet_id = wallet.get("id")
        if not _truthy(wallet.get("auto_replenish_enabled")) or \
                not _truthy(wallet.get("is_active") or "true"):
            continue

        rows = by_wallet.get(wallet_id, [])
        balance = sum(_int(r.get("amount_minor")) for r in rows)
        threshold = _int(wallet.get("auto_replenish_threshold_minor"))
        amount = _int(wallet.get("auto_replenish_amount_minor"))
        cap = _int(wallet.get("auto_replenish_monthly_cap_minor"))

        if balance >= threshold:
            continue
        if amount <= 0:
            results.append({"wallet": wallet_id, "skipped": "no top-up amount set"})
            skipped += 1
            continue

        marker = f"auto_topup/{wallet_id}/{period}"
        # Already topped up this month at this attempt count? The marker
        # carries the running total so repeated top-ups in one month each
        # get their own, while a replayed pass gets none.
        this_month = sum(_int(r.get("amount_minor")) for r in rows
                         if r.get("kind") == "auto_topup"
                         and str(r.get("created_at") or "")[:7] == period)
        if cap <= 0:
            results.append({"wallet": wallet_id,
                            "skipped": "monthly cap is zero -- auto top-up is off"})
            skipped += 1
            continue
        if this_month + amount > cap:
            results.append({
                "wallet": wallet_id, "skipped": "monthly cap reached",
                "capped_at_minor": cap, "already_this_month_minor": this_month,
                "note": ("the cap did its job: a customer is not being charged "
                         "beyond what they agreed to this month"),
            })
            skipped += 1
            continue

        attempt_marker = f"{marker}/{this_month + amount}"
        if any(r.get("generated_from") == attempt_marker for r in rows):
            skipped += 1
            continue

        if dry_run or not config.configured:
            results.append({
                "wallet": wallet_id, "would_charge_minor": amount,
                "balance_minor": balance,
                "reason": "dry run" if dry_run else "Stripe not configured",
            })
            skipped += 1
            continue

        customer = str(wallet.get("stripe_customer_id") or "").strip()
        method = str(wallet.get("payment_method_ref") or "").strip()
        if not customer or not method:
            results.append({"wallet": wallet_id,
                            "skipped": "no saved card; top-up needs a payment method on file"})
            skipped += 1
            continue

        try:
            intent = object_stripe.api_request(
                config, "POST", "/v1/payment_intents",
                {
                    "amount": amount,
                    "currency": "usd",
                    "customer": customer,
                    "payment_method": method,
                    "off_session": True,
                    "confirm": True,
                    "metadata": {"wallet_id": wallet_id, "period": period},
                },
                idempotency_key=attempt_marker,
            )
        except object_stripe.StripeError as exc:
            # Declines -- especially authentication_required, which an
            # off-session charge cannot satisfy -- are reported, not
            # retried. The customer needs a pay link, not a loop.
            failed += 1
            results.append({"wallet": wallet_id, "failed": str(exc)[:160],
                            "next": "ask the customer to top up interactively"})
            continue

        object_records.create_collection_record(
            "wallet_entries",
            {
                "wallet_id": wallet_id,
                "amount_minor": str(amount),
                "kind": "auto_topup",
                "description": f"Automatic top-up ({period})",
                "reference": str(intent.get("id") or ""),
                "generated_from": attempt_marker,
                "owner_id": wallet.get("owner_id", ""),
            },
            base_dir=base, actor=ACTOR)
        charged += 1
        results.append({"wallet": wallet_id, "charged_minor": amount,
                        "intent": intent.get("id"), "balance_was_minor": balance})

    return {"ok": True, "charged": charged, "skipped": skipped, "failed": failed,
            "period": period, "results": results}
