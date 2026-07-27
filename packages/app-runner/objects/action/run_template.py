"""action_run_template -- press Run on a template: validate, hold the
money, queue the job.

POST {template_id, form_data?}

This object QUEUES and never executes. A verb that both queued and ran
would make the queue a fiction under load, and -- worse -- would put a
paid provider call inside a web request, where a timeout looks to the
user like failure while the provider bills us anyway. Execution belongs
to system_template_runner, on the daemon, where a crash has a sweeper.

## The gate is asked at the moment of commitment

The stamped price goes on the wallet as a HOLD, here, before anything is
queued -- through hook_wallet_entries, asked in process, exactly the way
action_checkout asks it for gift cards. The gate summing the real entries
at THIS moment is what makes "insufficient balance" mean something: a
user with five cents cannot queue four hundred runs and present the
provider bill later, because the four-hundredth hold fails the gate.

Free templates (price 0) touch no wallet at all: no hold, no wallet_id on
the run, nothing to settle. Money machinery that runs for free jobs is
machinery that fails for free jobs.

## Capability boundaries are refused HERE, not discovered at 3am

An ai handler needs a model (`runner.ai_model` in app_settings) and a
stored provider key for whoever is running it. Both are checked at queue
time and refused with a 409 naming exactly what to configure -- the house
posture: absent by default, never a stub that pretends to work. Refusing
here beats queueing a run that the daemon fails an hour later, because
the person who can fix the configuration is the one looking at the screen
right now. (The runner re-checks at execution -- a key can be deleted
between queue and run -- but that failure is the rare race, not the
normal path.)

## Ordering, and the crash between the two writes

The hold is written BEFORE the run row. If the crash lands between them,
the result is a hold with no run -- money reserved for nothing, visible,
annoying, releasable. The other order would leave a QUEUED RUN WITH NO
HOLD: a job the runner would happily execute for free, invisibly. When a
crash must leave one of two wrongs, leave the one that fails safe and
shows up. (The window is one create call wide; on a run-create failure
this object immediately writes the release itself, so the orphan hold
survives only a crash, not an error.)
"""

import json
import os
from datetime import datetime, timezone

import object_execution
import object_ids
import object_records
import object_service_keys
import object_template_runs
import python_object_runtime

ACTOR = "action_run_template"
RUNS = "template_runs"
MODEL_SETTING = "runner.ai_model"

AI_HANDLERS = ("ai_text",)
KNOWN_HANDLERS = ("echo", "ai_text")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _setting(base, key):
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if _text(row.get("key")) == key:
                return _text(row.get("value"))
    except Exception:
        pass
    return ""


def _call(object_id, payload, *, method="POST"):
    """Run another installed object in process -- the pattern
    action_checkout uses to ask hook_wallet_entries."""
    try:
        runtime = python_object_runtime.PythonObjectRuntime()
        outcome = object_execution.execute_object(
            runtime,
            object_execution.ObjectExecutionRequest(
                object_id, method=method, payload=payload))
    except Exception as exc:
        return None, str(exc)[:200]
    if not outcome.ok:
        message = getattr(outcome.error, "message", "") if outcome.error else ""
        return None, (_text(message)[:200] or f"{object_id} failed")
    return outcome.result, ""


def _wallet_for(base, owner_id):
    """The owner's spending wallet: active, kind balance (or legacy blank).

    Gift cards and store credit are deliberately not reached from here in
    slice 1 -- a template run is metered usage, and mixing tender kinds
    into the hold path is a decision for the checkout that already makes
    it, not a default.
    """
    try:
        rows = object_records.read_collection_records("wallets", base_dir=base)
    except Exception:
        return None
    candidates = [row for row in rows
                  if _text(row.get("owner_id")) == owner_id
                  and _text(row.get("kind")) in ("", "balance")
                  and _text(row.get("is_active")).lower() != "false"]
    candidates.sort(key=lambda row: _text(row.get("created_at")))
    return candidates[0] if candidates else None


def POST(request):
    request = request or {}
    base = _base_dir()

    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))
    if not user_id:
        return {"status": 401,
                "error": "Running a template requires an account: runs are "
                         "held against a wallet, and an anonymous run would "
                         "be an open tap."}

    template_id = _text(request.get("template_id"))
    if not template_id:
        return {"status": 400, "error": "template_id is required."}
    try:
        template = object_records.get_collection_record(
            "templates", template_id, base_dir=base)
    except Exception:
        return {"status": 404,
                "error": f"There is no template {template_id!r} on this server."}

    form_data = request.get("form_data")
    if isinstance(form_data, str):
        try:
            form_data = json.loads(form_data or "{}")
        except ValueError:
            return {"status": 400, "error": "form_data is not valid JSON."}
    form_data = form_data if isinstance(form_data, dict) else {}

    found = object_template_runs.problems(template, form_data)
    handler = _text(template.get("handler"))
    if handler and handler not in KNOWN_HANDLERS:
        found.append(f"'{handler}' is not a handler this server knows. "
                     f"Available: {', '.join(KNOWN_HANDLERS)}.")
    if found:
        return {"status": 400, "error": " ".join(found), "problems": found}

    # --- capability boundaries, refused while somebody is looking ------------
    model = ""
    if handler in AI_HANDLERS:
        model = _setting(base, MODEL_SETTING)
        missing = []
        if not model or ":" not in model:
            missing.append(f"set {MODEL_SETTING} in app_settings to "
                           f"'<service>:<model>' (e.g. 'anthropic:claude-sonnet-4-5')")
        else:
            service = model.split(":", 1)[0]
            if not object_service_keys.get_service_key(user_id, service,
                                                       base_dir=base):
                missing.append(f"store a {service} API key for {user_id} "
                               f"(Settings -> Service Keys)")
        if missing:
            return {"status": 409,
                    "error": ("This template runs on an AI provider, and this "
                              "server is not configured for one: "
                              + "; ".join(missing) + ". Nothing was queued "
                              "and nothing was charged."),
                    "problems": missing}

    stamped = object_template_runs.stamp(template, form_data, model=model)
    price = int(stamped["price_cents"])
    run_id = object_ids.new_uuid4()

    # --- the hold, through the gate, before the run --------------------------
    wallet_id = ""
    if price > 0:
        wallet = _wallet_for(base, user_id)
        if wallet is None:
            return {"status": 409,
                    "error": (f"This template costs {price} cents to run and "
                              f"{user_id} has no active wallet to hold it "
                              f"against. Create a wallet and top it up first.")}
        wallet_id = _text(wallet.get("id"))
        hold = object_template_runs.hold_entry(
            run_id, wallet_id, price, user_id,
            template_name=stamped["template_name"])

        verdict, error = _call("hook_wallet_entries",
                               {"action": "create",
                                "collection": "wallet_entries",
                                "record": hold},
                               method="BEFORE_WRITE")
        if error:
            return {"status": 409,
                    "error": (f"The wallet gate could not be consulted "
                              f"({error}), so nothing was held and nothing "
                              f"was queued.")}
        if isinstance(verdict, dict) and verdict.get("error"):
            return {"status": int(verdict.get("status") or 402),
                    "error": _text(verdict.get("error"))}

        try:
            object_records.create_collection_record(
                "wallet_entries", hold, base_dir=base, actor=user_id)
        except Exception as exc:
            return {"status": 500,
                    "error": f"The hold could not be placed: {str(exc)[:160]}"}

    # --- the run --------------------------------------------------------------
    record = {
        "id": run_id,
        **stamped,
        "status": "queued",
        "idempotency_key": object_ids.new_uuid4(),
        "wallet_id": wallet_id,
        "owner_id": user_id,
    }
    try:
        stored = object_records.create_collection_record(
            RUNS, record, base_dir=base, actor=user_id)
    except Exception as exc:
        # The one wrong this ordering can leave is a hold with no run; give
        # the money back now rather than leaving it to be noticed.
        if price > 0:
            for entry in object_template_runs.settlement(
                    {"id": run_id, "price_cents": str(price),
                     "wallet_id": wallet_id, "owner_id": user_id},
                    succeeded=False):
                try:
                    object_records.create_collection_record(
                        "wallet_entries", entry, base_dir=base, actor=ACTOR)
                except Exception:
                    pass
        return {"status": 500,
                "error": f"The run could not be queued: {str(exc)[:160]}"}

    return {"ok": True, "run_id": run_id, "status": "queued",
            "price_cents": price, "handler": handler,
            "held": price > 0,
            "note": ("Queued. The runner executes it on its next pass; the "
                     "hold settles when it finishes -- released in full plus "
                     "an ordinary debit on success, released alone on "
                     "failure." if price > 0 else
                     "Queued. Free to run, so no money was held.")}
