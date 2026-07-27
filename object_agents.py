"""object_agents -- who is here, what they can do, and what they have
committed. Pure: no I/O, no clock, no data directory.

Same posture as object_cart, object_rates, object_ledger_head: the
package objects in packages/app-agents do the reading and writing and call
these.

## Liveness is the one thing the change log cannot tell you

Everything else about an agent is already recorded. Every write on this
box carries an actor; app-activity renders per-actor history; identity
owns who the agent is. The single question none of that answers is
**"is it still there?"** -- because an agent reasoning for ten minutes
writes nothing at all and is perfectly alive, while a crashed one looks
alive for as long as its last write stays recent.

So liveness is a heartbeat compared against a supplied `now`, and both
are parameters here rather than read from a clock, which is what makes it
testable without sleeping.

## Three states, not two

`live` / `stale` / `lost` rather than a boolean, because the useful thing
an operator wants is not "up or down" but "should I be worried yet". A
missed beat is ordinary; several missed beats is a question; a long
silence is an answer. The thresholds are the caller's -- 25-agents-spec
deliberately declined to name a number ("Open Questions #5"), and this
module declines too, defaulting only so a caller that has no opinion
still gets something sensible.

## Spend is a fold over the ledger, never a stored counter

`committed_minor` sums an agent's holds and debits out of wallet_entries.
It is NOT a column on agent_registry, for the reason the whole codebase
repeats: a counter beside a log of movements is two truths that drift,
and when they drift the gate that reads the counter authorises a spend
that should have been refused. The identical argument
hook_wallet_entries makes about wallets.balance_minor, and
object_promotions makes about redemptions_used.
"""

DEFAULT_STALE_SECONDS = 900       # 15 minutes: a missed beat or two
DEFAULT_LOST_SECONDS = 3600       # an hour of silence is an answer

# Wallet entry kinds that represent money an agent has COMMITTED. A hold
# is money reserved and not yet spent; a debit is money spent. Both count
# against a cap, because a cap that ignored holds would let an agent queue
# a hundred runs and only notice at settlement -- which is exactly the
# failure the hold model exists to prevent (plan/template-runner-spec.md).
COMMITTING_KINDS = ("hold", "debit", "auto_topup")

# Board ordering, worst first. `never` outranks `lost` because an agent
# that registered and never spoke is almost always a misconfiguration
# somebody can fix right now, where one that went quiet after working may
# simply be busy. Paused and retired sort BELOW live: they are settings,
# not problems, and an operator's own decision should never sit at the top
# of a page whose job is to show what needs attention.
_LIVENESS_RANK = {
    "never": 0, "lost": 1, "stale": 2, "live": 3,
    "suspended": 4, "paused": 5, "retired": 6,
}


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def capabilities(agent):
    """The tags this agent accepts work for, normalised.

    Lower-cased and de-duplicated, order preserved. Empty is the default
    and means "accepts nothing": a registration that advertises nothing
    must never be routed work, because opting in is the whole posture
    (nobody's laptop joins a compute pool by accident).
    """
    seen, out = set(), []
    for part in _text((agent or {}).get("capabilities")).split(","):
        tag = part.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def can_serve(agent, requirement):
    """Whether this agent advertises what a piece of work requires.

    A blank requirement is servable by ANYONE (ordinary work needs no
    special capability). A blank capability list serves NOTHING, including
    blank requirements -- an agent that advertised nothing is not
    volunteering for general duty either, it simply has not opted in.
    """
    wanted = _text(requirement).lower()
    have = capabilities(agent)
    if not have:
        return False
    if not wanted:
        return True
    return all(tag in have for tag in
               [part.strip() for part in wanted.split(",") if part.strip()])


def liveness(agent, *, now, stale_seconds=DEFAULT_STALE_SECONDS,
             lost_seconds=DEFAULT_LOST_SECONDS):
    """`live` | `stale` | `lost` | `never` for one agent.

    `never` is its own answer rather than being folded into `lost`,
    because "registered and has not yet said anything" is a different
    problem from "was here and stopped" -- the first is usually a
    misconfiguration, the second an incident.

    An agent that is not `active` reports its own status instead: a paused
    or retired agent is not silent, it is off, and rendering it as `lost`
    would manufacture an alarm out of an operator's own decision.
    """
    status = _text((agent or {}).get("status")) or "active"
    if status != "active":
        return status

    beat = _text((agent or {}).get("heartbeat_at"))
    if not beat:
        return "never"

    if beat >= _shift_iso(_text(now), -abs(int(stale_seconds))):
        return "live"
    if beat >= _shift_iso(_text(now), -abs(int(lost_seconds))):
        return "stale"
    return "lost"


def committed_minor(agent_id, wallet_entries):
    """What this agent has committed: holds plus debits, as a positive
    number. A FOLD over the ledger -- never a stored counter.

    Reads `owner_id` on the entry, which is the agent's user_id, the same
    value every write it makes already carries as its actor.
    """
    target = _text(agent_id)
    if not target:
        return 0
    total = 0
    for row in wallet_entries or ():
        if _text(row.get("owner_id")) != target:
            continue
        if _text(row.get("kind")) not in COMMITTING_KINDS:
            continue
        amount = _int(row.get("amount_minor"))
        if amount < 0:
            total += -amount
    return total


def over_cap(agent, wallet_entries):
    """Has this agent spent past its cap? (cap, committed, over) -- or
    None when it has no cap.

    A cap of 0 means NO CAP, matching billing.wallet.overdraft_minor's
    convention. Stating that here because the opposite reading -- zero as
    "may spend nothing" -- would silently suspend every agent the moment
    the field shipped, which is the worst possible default for a field
    added to existing rows.
    """
    cap = _int((agent or {}).get("spend_cap_minor"))
    if cap <= 0:
        return None
    committed = committed_minor((agent or {}).get("agent_id"), wallet_entries)
    return {"cap_minor": cap, "committed_minor": committed,
            "over": committed > cap,
            "remaining_minor": max(0, cap - committed)}


def board(agents, wallet_entries=None, *, now,
          stale_seconds=DEFAULT_STALE_SECONDS,
          lost_seconds=DEFAULT_LOST_SECONDS):
    """Everything the agent board renders, as one fold.

    One shape for the page and the JSON, so the two cannot disagree --
    the posture site_privacy and site_ledger_integrity both take.
    """
    rows = []
    for agent in agents or ():
        state = liveness(agent, now=now, stale_seconds=stale_seconds,
                         lost_seconds=lost_seconds)
        rows.append({
            "agent_id": _text(agent.get("agent_id")),
            "label": _text(agent.get("label")) or _text(agent.get("agent_id")),
            "purpose": _text(agent.get("purpose")),
            "capabilities": capabilities(agent),
            "endpoint": _text(agent.get("endpoint")),
            "status": _text(agent.get("status")) or "active",
            "heartbeat_at": _text(agent.get("heartbeat_at")),
            # Both forms: the stamp for anything computing, the phrase for
            # the human being asked "is it still there".
            "heartbeat_ago": relative_time(agent.get("heartbeat_at"), now),
            "liveness": state,
            "spend": over_cap(agent, wallet_entries or []),
        })
    # WORST FIRST. A board exists to surface what needs attention, so the
    # silent agents lead and the working ones sink -- the opposite of the
    # obvious sort, and the reason to write the order down rather than
    # leave it to a boolean. An operator's own decisions (paused, retired)
    # rank below live: they are not problems, they are settings.
    rows.sort(key=lambda row: (_LIVENESS_RANK.get(row["liveness"], 50),
                               row["label"].lower()))

    counts = {}
    for row in rows:
        counts[row["liveness"]] = counts.get(row["liveness"], 0) + 1
    return {
        "agents": rows,
        "counts": counts,
        "live": counts.get("live", 0),
        "total": len(rows),
        "over_cap": [row["agent_id"] for row in rows
                     if row["spend"] and row["spend"]["over"]],
        "capabilities": sorted({tag for row in rows for tag in row["capabilities"]}),
    }


def relative_time(stamp, now):
    """"4 hours ago" rather than "2026-07-27T03:12:44Z".

    A liveness verdict is supposed to be stated WITH its evidence, and an
    ISO timestamp is not evidence a human can act on -- an operator asked
    "is it still there" does not want to subtract two datetimes in their
    head. "last beat 4 hours ago" answers the question the page exists to
    answer; the raw stamp is kept in the JSON for anything that needs to
    compute rather than read.

    Pure, `now` passed in, same as everything else here. Deliberately
    coarse: nobody needs "3 hours, 14 minutes, 9 seconds", they need to
    know whether it is minutes or days.
    """
    from datetime import datetime

    text, current = _text(stamp), _text(now)
    if not text:
        return "never"
    try:
        then = datetime.fromisoformat(text.rstrip("Z"))
        right_now = datetime.fromisoformat(current.rstrip("Z"))
    except ValueError:
        return text

    seconds = (right_now - then).total_seconds()
    if seconds < 0:
        return "just now"          # clock skew reads as now, never "in 3 hours"
    for limit, divisor, unit in ((60, 1, "second"), (3600, 60, "minute"),
                                 (86400, 3600, "hour"), (604800, 86400, "day"),
                                 (2629800, 604800, "week")):
        if seconds < limit:
            value = int(seconds // divisor)
            if value <= 0:
                return "just now"
            return f"{value} {unit}{'' if value == 1 else 's'} ago"
    months = int(seconds // 2629800)
    return f"{months} month{'' if months == 1 else 's'} ago"


def _shift_iso(stamp, seconds):
    """An ISO timestamp moved by N seconds, staying pure (no clock).

    Same helper object_template_runs uses for staleness, and for the same
    reason: ISO-8601 strings sort lexicographically, so a comparison
    against a shifted stamp is the whole of the arithmetic.
    """
    from datetime import datetime, timedelta

    text = stamp.rstrip("Z")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return stamp
    shifted = parsed + timedelta(seconds=seconds)
    out = shifted.isoformat()
    return out + "Z" if stamp.endswith("Z") else out
