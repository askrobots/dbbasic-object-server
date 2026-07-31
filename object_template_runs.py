"""object_template_runs -- the arithmetic of running a template.

Pure. No I/O, no clock, no data directory. The same posture as
object_rates, object_cart and object_promotions: the package objects in
packages/app-runner do the reading and writing, and everything here is
testable without either.

## What a run IS

A run is a COMMITMENT the moment it is queued: money is placed on hold
against it, a provider may be paid real cash to execute it, and the terms
it runs under are stamped onto its own row so that editing the template
tomorrow restates nothing that already happened
(docs/logic-decisions.md #1). A template is edited constantly -- that is
what makes it a template -- so the run must carry its own copy of the
handler, the body, the price and the model that were in force when the
user pressed the button. A user reading last month's run history is
reading what last month actually cost, not what this month's prices would
have made it cost.

## The money model: hold, release, charge

Three wallet movements, and the naming is the design:

* **hold** (negative) -- placed at queue time for the stamped price. Goes
  through the wallet gate, which is the point: the moment the gate is
  asked is the moment money is committed, so a user with 5 cents cannot
  queue four hundred runs and present the provider bill later.
* **release** (positive) -- the hold handed back at settlement, always in
  full, always paired to its hold by provenance marker.
* **charge** -- an ordinary `debit` for what the run actually cost the
  user. Deliberately NOT a new kind: every report that already reads
  debits keeps working, and a released hold never masquerades as a
  refund. A hold is not a debit that got undone; it is money that was
  never spent, and the ledger should read that way.

A successful run settles as release + debit (sum: the price). A failed
run settles as release alone (sum: zero). Nothing is ever mutated and the
balance stays derived, so every state -- reserved, spent, given back --
is reconstructible from the entries alone.

## Provenance markers

Each movement carries a `generated_from` marker naming the run and the
leg (`template_run/<id>/hold`, `/release`, `/charge`). Settlement checks
for the release marker before writing, which is what makes it idempotent:
the runner and the sweeper can both try to settle the same run and the
second one finds the marker and writes nothing (doctrine #7). The failure
this prevents is not theoretical -- it is a crash between "settled" and
"status updated", replayed at the next pass, giving the money back twice.
"""

import json
import re

# The one non-negotiable ordering rule, written down where both the
# runner and the sweeper import it: a stale run is never re-queued.
# Retrying a paid, non-idempotent provider call automatically is the sort
# of feature that looks like robustness and is actually an unbounded
# spend. A retry is a NEW run, with a new hold, started by a person.
TERMINAL_STATUSES = ("succeeded", "failed", "abandoned")

# `running` sits between claimed and terminal: the provider has the job,
# we have a provider_job_id, and no pass is executing anything. It is
# owned but idle, which is why it needs its own status rather than
# stretching `claimed` to cover it.
RUNNING_STATUS = "running"

# The one state a machine must not resolve. A submitted job whose outcome
# we never learned is money that MAY already be spent at the provider:
# releasing the hold gives away what we paid, and charging bills a
# customer for something they may never have received. Both are wrong and
# the box genuinely cannot tell which, so it stops and asks. Deliberately
# NOT in TERMINAL_STATUSES -- that tuple is what drives the sweeper's
# automatic hold release, and the whole point is that this one is not
# released automatically.
NEEDS_REVIEW_STATUS = "needs_review"

HOLD_MARKER = "template_run/{run_id}/hold"
RELEASE_MARKER = "template_run/{run_id}/release"
CHARGE_MARKER = "template_run/{run_id}/charge"

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


# --- the form ----------------------------------------------------------------

def form_fields(template):
    """The template's declared form, as a list of {name, required, ...}.

    templates.schema is a JSON Schema string (stored as text because the
    field-type contract has no json type). Only the parts a runner needs
    are read: property names and the required list. A template with no
    schema has no form and accepts anything, which is the right default
    for a body with no placeholders.
    """
    raw = _text(template.get("schema"))
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    properties = parsed.get("properties")
    if not isinstance(properties, dict):
        return []
    required = set(parsed.get("required") or [])
    return [{"name": name, "required": name in required,
             "title": _text((spec or {}).get("title")) or name}
            for name, spec in properties.items()]


def problems(template, form_data):
    """Every reason this submission cannot run, at once.

    A list rather than a first failure -- a form that reveals one problem
    per attempt is a form people abandon, which is the same argument
    object_promotions.blockers makes about checkouts.
    """
    found = []
    handler = _text(template.get("handler"))
    if not handler:
        found.append(
            f"The template '{_text(template.get('name')) or '?'}' declares no "
            f"handler, so there is nothing to run it with. Set `handler` on "
            f"the template (e.g. 'echo' or 'ai_text').")

    form_data = form_data if isinstance(form_data, dict) else {}
    for field in form_fields(template):
        if field["required"] and not _text(form_data.get(field["name"])):
            found.append(f"'{field['title']}' is required and was not provided.")

    price = _int(template.get("run_cost_cents"))
    if price < 0:
        found.append("run_cost_cents is negative; a template cannot pay "
                     "people to run it.")
    return found


def render(body, form_data):
    """The template body with {name} placeholders filled from the form.

    Unknown placeholders stay as written rather than erroring or blanking:
    a brace in prose ("{TBD}") must not kill a run, and a silently emptied
    placeholder is a prompt that quietly lost its subject -- the worst
    outcome, because the provider answers SOMETHING and charges for it.
    """
    form_data = form_data if isinstance(form_data, dict) else {}

    def fill(match):
        key = match.group(1)
        if key in form_data:
            return str(form_data[key])
        return match.group(0)

    return _PLACEHOLDER.sub(fill, str(body or ""))


# --- stamping ------------------------------------------------------------------

def stamp(template, form_data, *, model=""):
    """The terms of this run, copied at queue time.

    Everything execution or a later reader needs, taken from the template
    NOW, so the run is immune to every future edit of the template. The
    rendered body is stamped rather than re-rendered later for the same
    reason: rendering reads the template, and by execution time the
    template may say something else.
    """
    return {
        "template_id": _text(template.get("id")),
        "template_name": _text(template.get("name")),
        "handler": _text(template.get("handler")),
        "model": _text(model),
        "form_data": json.dumps(form_data if isinstance(form_data, dict) else {},
                                sort_keys=True),
        "rendered_body": render(template.get("body"), form_data),
        "instructions": _text(template.get("instructions")),
        "price_cents": str(max(0, _int(template.get("run_cost_cents")))),
    }


# --- settlement ----------------------------------------------------------------

def settlement(run, *, succeeded):
    """The wallet movements that close this run. Pure: returns a list of
    entries for the caller to write, after checking the release marker.

    Success: release the hold in full, then an ordinary debit for the
    stamped price. Failure: release alone -- the user pays nothing for a
    run that did not do the thing, whatever it cost US at the provider
    (our cost is recorded on the run for margin, not billed to them).

    A free run (price 0) had no hold and settles to no entries at all.
    """
    run_id = _text(run.get("id"))
    price = _int(run.get("price_cents"))
    wallet_id = _text(run.get("wallet_id"))
    if price <= 0 or not wallet_id:
        return []

    entries = [{
        "wallet_id": wallet_id,
        "amount_minor": str(price),
        "kind": "release",
        "description": f"Hold released: run {run_id}",
        "reference": f"template_runs/{run_id}",
        "generated_from": RELEASE_MARKER.format(run_id=run_id),
        "owner_id": _text(run.get("owner_id")),
    }]
    if succeeded:
        entries.append({
            "wallet_id": wallet_id,
            "amount_minor": str(-price),
            "kind": "debit",
            "description": (f"Template run: "
                            f"{_text(run.get('template_name')) or run_id}"),
            "reference": f"template_runs/{run_id}",
            "generated_from": CHARGE_MARKER.format(run_id=run_id),
            "owner_id": _text(run.get("owner_id")),
        })
    return entries


def hold_entry(run_id, wallet_id, price_cents, owner_id, template_name=""):
    """The hold placed at queue time. Negative, so the wallet gate fires
    on it -- which is the entire reason a hold exists: the gate is asked
    at the moment of commitment, not at the moment of spending."""
    price = max(0, _int(price_cents))
    if price == 0:
        return None
    return {
        "wallet_id": _text(wallet_id),
        "amount_minor": str(-price),
        "kind": "hold",
        "description": f"Hold: template run {_text(template_name) or run_id}",
        "reference": f"template_runs/{run_id}",
        "generated_from": HOLD_MARKER.format(run_id=run_id),
        "owner_id": _text(owner_id),
    }


def already_settled(run_id, wallet_entries):
    """Has this run's hold already been released?

    THE idempotency check. The runner settling a run it just executed and
    the sweeper settling a run whose worker died can both reach here; the
    marker means at most one of them writes. Checked against the entries
    themselves, never against run.status, because the crash this guards
    is precisely the one between writing money and updating status.
    """
    marker = RELEASE_MARKER.format(run_id=_text(run_id))
    return any(_text(row.get("generated_from")) == marker
               for row in wallet_entries or ())


def outstanding_holds(wallet_entries):
    """Sum of holds not yet released, by run id -- what is reserved right
    now. A fold over the entries, like every other balance here."""
    held = {}
    for row in wallet_entries or ():
        marker = _text(row.get("generated_from"))
        if marker.startswith("template_run/"):
            parts = marker.split("/")
            if len(parts) == 3 and parts[2] in ("hold", "release"):
                run_id = parts[1]
                held[run_id] = held.get(run_id, 0) + _int(row.get("amount_minor"))
    return {run_id: -total for run_id, total in held.items() if total != 0}


def poll_disposition(run, *, now, stale_seconds, max_run_seconds):
    """What should be done with this run right now? Pure; no clock.

    The money rule for asynchronous jobs lives here, in one place, so the
    sweeper and the runner cannot disagree about it:

    - **claimed, heartbeat stopped, no provider_job_id** -> "abandon".
      Nothing was submitted, so nothing was spent, and the hold is
      released in full. This is the original sweeper behaviour and it
      stays exactly right for the case it was written for.

    - **running (or claimed WITH a provider_job_id), heartbeat stopped**
      -> "poll". NOT abandoned. The provider is holding the job and can
      simply be asked, so a dead worker is not a lost job -- and crucially
      the hold is NOT released, because a submitted job may already have
      cost everything. This is the case the original sweeper got actively
      wrong for media.

    - **running past max_run_seconds** -> "review". Polling has not
      resolved it for longer than any job should take, so it stops being
      a machine's decision. The hold stays put.

    - anything else -> None, leave it alone.

    A claimed run that carries a provider_job_id is treated as running
    even if the status write never landed: the id is the evidence that a
    call was made, and evidence outranks a status field that a crash may
    have prevented.
    """
    status = _text(run.get("status"))
    if status in TERMINAL_STATUSES or status == NEEDS_REVIEW_STATUS:
        return None

    submitted = bool(_text(run.get("provider_job_id")))
    beat = _text(run.get("heartbeat_at")) or _text(run.get("claimed_at"))

    if status == RUNNING_STATUS or (status == "claimed" and submitted):
        started = _text(run.get("claimed_at")) or beat
        if started:
            deadline = _shift_iso(started, abs(int(max_run_seconds)))
            if _text(now) > deadline:
                return "review"
        return "poll"

    if status == "claimed":
        if not beat:
            return "abandon"
        if beat < _shift_iso(_text(now), -abs(int(stale_seconds))):
            return "abandon"
    return None


def is_stale(run, *, now, stale_seconds):
    """Has this claimed run's heartbeat stopped?

    String comparison on ISO-8601 timestamps, which sort lexicographically
    -- the same convention the rest of the codebase leans on. `now` is a
    parameter, never read from a clock here, so the sweeper's tests do not
    need to sleep.
    """
    if _text(run.get("status")) not in ("claimed",):
        return False
    beat = _text(run.get("heartbeat_at")) or _text(run.get("claimed_at"))
    if not beat:
        return True
    cutoff = _shift_iso(_text(now), -abs(int(stale_seconds)))
    return beat < cutoff


def _shift_iso(stamp, seconds):
    """An ISO timestamp moved by N seconds, staying pure (no clock)."""
    from datetime import datetime, timedelta

    text = stamp.rstrip("Z")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return stamp
    shifted = parsed + timedelta(seconds=seconds)
    out = shifted.isoformat()
    return out + "Z" if stamp.endswith("Z") else out
