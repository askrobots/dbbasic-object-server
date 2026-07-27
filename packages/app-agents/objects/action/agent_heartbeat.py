"""action_agent_heartbeat -- "still here", and register on the way in.

POST {label?, purpose?, capabilities?, endpoint?}

One verb, not two. An agent's first beat registers it and every later
beat updates the timestamp, because a separate `register` call is a step
somebody forgets and then debugs -- and there is nothing a registration
knows that the first heartbeat does not.

## It registers the CALLER, never a named agent

`agent_id` comes from `_identity`, never from the request body. An agent
cannot beat on behalf of another, which matters more than it looks: a
heartbeat is the evidence something is alive, and evidence one party can
forge about another is not evidence. Same reason action_run_template sets
`owner_id` from identity rather than accepting it.

## Capabilities are opt-in and only the caller may set them

An agent advertises what it will accept work for. Absent, it advertises
nothing and is routed nothing -- because nobody's laptop should join a
compute pool by accident (plan/federated-workers-spec.md). A beat that
omits `capabilities` LEAVES THEM ALONE rather than clearing them, so a
minimal keep-alive is safe to send on a timer; passing an empty string
explicitly is how you withdraw.

## What it deliberately does not do

**It does not report what the agent has done.** That is record_changes,
which already carries an actor on every write, and app-activity already
renders it. A heartbeat that also submitted a work summary would be a
second, forgeable account of the same facts.

**It does not gate on the spend cap.** The cap is evaluated where money
moves -- the wallet gate -- not at the door of a keep-alive. Refusing a
heartbeat because an agent is over budget would make it look dead, which
is the opposite of what an operator needs to see.
"""

import os
from datetime import datetime, timezone

import object_agents
import object_ids
import object_records

ACTOR = "action_agent_heartbeat"
COLLECTION = "agent_registry"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _existing(base, agent_id):
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        return None
    for row in rows:
        if _text(row.get("agent_id")) == agent_id:
            return row
    return None


def POST(request):
    request = request or {}
    base = _base_dir()

    identity = request.get("_identity") or {}
    agent_id = _text(identity.get("user_id"))
    if not agent_id:
        return {"status": 401,
                "error": ("A heartbeat identifies the caller, so it needs an "
                          "identity. Sign in or present an API key; an agent "
                          "cannot beat anonymously, and it cannot beat on "
                          "behalf of another.")}

    now = _now()
    beat = {"heartbeat_at": now}
    # Present-but-empty withdraws; absent leaves alone. That distinction is
    # what makes a bare keep-alive safe to send on a timer.
    for field in ("label", "purpose", "capabilities", "endpoint"):
        if field in request:
            beat[field] = _text(request.get(field))

    existing = _existing(base, agent_id)
    if existing is not None:
        try:
            stored = object_records.update_collection_record(
                COLLECTION, existing["id"], beat, base_dir=base, actor=agent_id)
        except Exception as exc:
            return {"status": 500,
                    "error": f"The heartbeat could not be recorded: {str(exc)[:160]}"}
        registered = False
    else:
        record = {
            "id": object_ids.new_uuid4(),
            "agent_id": agent_id,
            "status": "active",
            "registered_at": now,
            "owner_id": agent_id,
            **beat,
        }
        try:
            stored = object_records.create_collection_record(
                COLLECTION, record, base_dir=base, actor=agent_id)
        except Exception as exc:
            return {"status": 500,
                    "error": f"The agent could not be registered: {str(exc)[:160]}"}
        registered = True

    return {
        "ok": True,
        "agent_id": agent_id,
        "registered": registered,
        "heartbeat_at": now,
        "capabilities": object_agents.capabilities(stored),
        "status": _text(stored.get("status")) or "active",
        "note": ("Registered. Capabilities are opt-in: an agent advertising "
                 "nothing is routed nothing." if registered else ""),
    }
