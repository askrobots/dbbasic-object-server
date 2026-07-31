"""action_resolve_run -- a person decides a run the machine refused to.

POST {run_id, decision: "charge" | "refund", note?}

`needs_review` is the one state this system creates deliberately and
cannot leave on its own. A run submitted to a provider whose outcome was
never learned has money genuinely undecided: releasing the hold gives
away what we may already have paid, charging bills a customer for work
that may never have arrived. The sweeper stops there on purpose.

Stopping without a way to continue is not a design, it is an abandoned
queue -- so this is the other half. It is the ONLY hand that moves a run
out of needs_review, and it is a person's.

## Why the two decisions are not symmetric

**refund** is the ordinary failure settlement, unchanged: release the
hold, charge nothing. The operator has decided the customer should not
pay, whatever it cost us. That cost is already recorded on the run
(provider_cost_cents), so eating it is a visible number rather than a
silent one.

**charge** is the release-plus-debit that a successful run gets. The
operator has confirmed the work arrived. Note what this does NOT do: it
never re-runs anything and never contacts the provider. This verb settles
money against a job whose fate somebody has established by other means --
looking in the provider's dashboard, or at the file that landed.

## What it refuses

Only a run actually in `needs_review`. A terminal run is already settled
and re-settling it is the double-charge the provenance markers exist to
prevent (they would no-op it, but a verb that silently does nothing is
worse than one that says why). And a run still `running` is not yours to
decide -- the provider may still answer, and polling is cheaper and more
truthful than a guess.

Settlement stays idempotent by the same release/charge markers everything
else uses, so a double-submitted decision writes money once.
"""

import os

import object_records
import object_template_runs

ACTOR = "action_resolve_run"
RUNS = "template_runs"
DECISIONS = ("charge", "refund")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def POST(request):
    request = request or {}
    base = _base_dir()

    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    if not user_id:
        return {"status": 401,
                "error": "Resolving a run decides who pays for it, so it "
                         "requires an account."}

    run_id = _text(request.get("run_id"))
    if not run_id:
        return {"status": 400, "error": "run_id is required."}

    decision = _text(request.get("decision")).lower()
    if decision not in DECISIONS:
        return {"status": 400,
                "error": (f"decision must be one of {', '.join(DECISIONS)}. "
                          f"'charge' settles the run as delivered (release "
                          f"the hold, then debit the stamped price); "
                          f"'refund' settles it as not delivered (release "
                          f"the hold, charge nothing).")}

    try:
        run = object_records.get_collection_record(RUNS, run_id, base_dir=base)
    except Exception:
        return {"status": 404, "error": f"There is no run {run_id!r}."}
    if run is None:
        return {"status": 404, "error": f"There is no run {run_id!r}."}

    # Owner or admin. A run's money is its owner's money.
    is_admin = bool(identity.get("is_admin")) or "admin" in (identity.get("roles") or [])
    if not is_admin and _text(run.get("owner_id")) != user_id:
        return {"status": 403,
                "error": "Only the run's owner or an admin can resolve it."}

    status = _text(run.get("status"))
    if status != object_template_runs.NEEDS_REVIEW_STATUS:
        return {"status": 409,
                "error": (f"Run {run_id} is {status!r}, not "
                          f"{object_template_runs.NEEDS_REVIEW_STATUS!r}. Only "
                          f"a run the machine refused to decide is yours to "
                          f"decide: a terminal run is already settled, and a "
                          f"running one may still be answered by the provider "
                          f"-- polling is cheaper and more truthful than a "
                          f"guess.")}

    succeeded = decision == "charge"

    # Money first, then status -- the same ordering the runner uses, and
    # for the same reason: a crash between them leaves money correct and
    # a status the next pass can see is behind, rather than the reverse.
    try:
        entries = object_records.read_collection_records("wallet_entries",
                                                         base_dir=base)
    except Exception:
        entries = []
    if object_template_runs.already_settled(run_id, entries):
        settled = "already settled"
    else:
        written = object_template_runs.settlement(run, succeeded=succeeded)
        for entry in written:
            object_records.create_collection_record(
                "wallet_entries", entry, base_dir=base, actor=user_id)
        settled = "settled" if written else "nothing to settle (free run)"

    note = _text(request.get("note"))
    resolution = (f"Resolved by {user_id} as {decision} on {_now()}."
                  + (f" {note}" if note else ""))
    object_records.update_collection_record(
        RUNS, run_id,
        {"status": "succeeded" if succeeded else "failed",
         "error": (_text(run.get("error")) + "\n\n" + resolution)[:2000],
         "finished_at": _now()},
        base_dir=base, actor=user_id)

    return {"status": 200, "ok": True, "run_id": run_id,
            "decision": decision, "settlement": settled,
            "resolved_by": user_id}
