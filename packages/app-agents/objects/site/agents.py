"""site_agents -- the board a human watches while the agents work.

    GET /agents        who is here, what they can do, what they are saying
    GET /agents.json   the same fold, for a monitor

## Why a board rather than a list

The generative list renderer already gives `agent_registry` a perfectly
good table, and it is the wrong shape for the one question this page
exists to answer: **is everything that should be working, working?** A
table sorts by whatever column you clicked. A board leads with the
answer, puts the dead things first, and shows the conversation next to
the participants.

The model is WOLD's own asset gallery, which earns its keep the same way:
score, method, verdict and provenance on every card, so an operator can
scan a hundred of them and see exactly which ones need attention without
opening anything.

## Liveness is stated with its evidence

Every agent shows its state AND the heartbeat it was computed from, for
the reason the ledger-integrity page states its method beside its
verdict: "lost" without "last seen 4 hours ago" is a claim, and the
operator's next question is always the timestamp. Three states rather
than a boolean, because the useful distinction is not up-or-down but
"should I be worried yet".

## The feed is the other half, and it lives somewhere else

`feed_posts` (app-collab) has been the coordination feed since before this
package existed -- with `claim` and `release` already in its `kind` enum.
This page renders it rather than duplicating it, and app-collab keeps
owning it. That is why this package declares no feed collection of its
own: the channel was already here, it just had no face.
"""

import html
import json
import os
from datetime import datetime, timezone

import object_agents
import object_state
import object_records

REGISTRY = "agent_registry"
SCHEDULER_RUNS = "scheduler_runs"
FEED = "feed_posts"
SETTINGS_COLLECTION = "app_settings"
STALE_KEY = "agents.stale_seconds"
LOST_KEY = "agents.lost_seconds"

FEED_LIMIT = 25

_STYLE = """
.ag { max-width: 54rem; }
.ag h2 { font-size: 1rem; margin: 1.6rem 0 .5rem; }
.ag .banner { border: 1px solid var(--line, #38384a); border-radius: 8px;
              padding: .85rem 1.05rem; margin: 1rem 0 1.4rem; }
.ag .banner.warn { border-color: var(--accent, #b5713a); }
.ag .cols { display: grid; gap: 1.2rem; grid-template-columns: 1fr 1fr;
             align-items: start; }
@media (max-width: 46rem) { .ag .cols { grid-template-columns: 1fr; } }
.ag .cards { display: grid; gap: .7rem; }
.ag .card { border: 1px solid var(--line, #38384a); border-radius: 8px;
            padding: .7rem .85rem; }
.ag .card.live { border-left: 3px solid var(--accent, #b5713a); }
.ag .card.lost, .ag .card.never { border-left: 3px solid #d05a5a; }
.ag .card.stale { border-left: 3px solid #c9a227; }
.ag .card h3 { font-size: .95rem; margin: 0 0 .15rem; }
.ag .state { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; }
.ag .state.live { color: var(--accent, #b5713a); }
.ag .state.lost, .ag .state.never { color: #d05a5a; }
.ag .state.stale { color: #c9a227; }
.ag .tag { display: inline-block; border: 1px solid var(--line, #55556a);
           border-radius: 10px; padding: 0 .45rem; font-size: .72rem;
           margin: .15rem .15rem 0 0; }
.ag .muted { opacity: .7; font-size: .82rem; }
.ag .feed { margin: .4rem 0 0; padding: 0; list-style: none; }
.ag .feed li { border-bottom: 1px solid var(--line, #38384a); padding: .4rem 0;
               font-size: .88rem; }
.ag .kind { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
            border: 1px solid var(--line, #55556a); border-radius: 10px;
            padding: 0 .4rem; margin-right: .4rem; }
.ag .note { border-left: 3px solid var(--line, #55556a); padding: .1rem 0 .1rem .7rem;
            margin: .7rem 0 1.1rem; font-size: .86rem; opacity: .85; }
.ag table.workers { width: 100%; border-collapse: collapse; font-size: .86rem; }
.ag table.workers th, .ag table.workers td {
    text-align: left; padding: .35rem .5rem;
    border-bottom: 1px solid var(--line, #38384a); vertical-align: top; }
.ag table.workers th { font-weight: 600; opacity: .8; }
.ag .pill { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
            border: 1px solid var(--line, #55556a); border-radius: 10px;
            padding: 0 .45rem; }
.ag .pill.live { color: var(--accent, #b5713a); border-color: var(--accent, #b5713a); }
.ag .pill.stale { color: #c9a227; border-color: #c9a227; }
.ag .pill.lost, .ag .pill.never { color: #d05a5a; border-color: #d05a5a; }
.ag .workererr { color: #d05a5a; font-size: .78rem; margin-top: .2rem; }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _rows(collection, base):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return []


def _setting(base, key, default):
    for row in _rows(SETTINGS_COLLECTION, base):
        if _text(row.get("key")) == key and _text(row.get("value")):
            try:
                return int(_text(row.get("value")))
            except ValueError:
                return default
    return default


def _scheduled_tasks(base):
    """The scheduler's declared tasks, read from its object state.

    Degrades to [] when the scheduler has never been written -- a box
    with no scheduled work is not a broken box, and the workers panel
    simply has nothing to show.
    """
    try:
        state = object_state.get_object_state("scheduler", base)
    except Exception:
        return []
    tasks = []
    for key, value in (state or {}).items():
        if not str(key).startswith("task_"):
            continue
        try:
            task = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            continue
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def report(*, base=None, now=None):
    """One fold, two surfaces."""
    base = _base_dir() if base is None else base
    now = now or _now()

    fold = object_agents.board(
        _rows(REGISTRY, base), _rows("wallet_entries", base), now=now,
        stale_seconds=_setting(base, STALE_KEY,
                               object_agents.DEFAULT_STALE_SECONDS),
        lost_seconds=_setting(base, LOST_KEY,
                              object_agents.DEFAULT_LOST_SECONDS))

    # The box's own scheduled work, folded from the run log rather than
    # from a heartbeat somebody would have to remember to send. Every
    # scheduled execution already writes a scheduler_runs row, so the
    # evidence exists; nothing was applying a liveness verdict to it.
    #
    # The declared tasks come from the scheduler's own state, and they
    # matter for exactly one case the run log cannot report: a task that
    # has NEVER run has no rows at all, and an absence is invisible to a
    # fold over presences. That is the pass silently doing nothing since
    # the day it was added -- the failure most worth catching here.
    fold["workers"] = object_agents.worker_liveness(
        _rows(SCHEDULER_RUNS, base), tasks=_scheduled_tasks(base), now=now,
        stale_seconds=_setting(base, STALE_KEY,
                               object_agents.DEFAULT_STALE_SECONDS),
        lost_seconds=_setting(base, LOST_KEY,
                              object_agents.DEFAULT_LOST_SECONDS))

    posts = sorted(_rows(FEED, base),
                   key=lambda row: _text(row.get("created_at")), reverse=True)
    fold["feed"] = [{
        "kind": _text(post.get("kind")) or "status",
        "body": _text(post.get("body")),
        "owner_id": _text(post.get("owner_id")),
        "created_at": _text(post.get("created_at")),
        # Blank for rows written before feed_posts gained created_at (v2).
        # Those render WITHOUT a time rather than as "never", because a
        # post that predates the column is not a post that never happened.
        "ago": (object_agents.relative_time(post.get("created_at"), now)
                if _text(post.get("created_at")) else ""),
    } for post in posts[:FEED_LIMIT]]
    fold["feed_installed"] = bool(posts) or _rows(FEED, base) != []
    return fold


# --- rendering ---------------------------------------------------------------

def _banner(fold):
    if not fold["agents"]:
        return """
<div class="banner warn">
<h2>No agents are registered</h2>
<p>Nothing has introduced itself. An agent registers by sending its first
heartbeat to <code>action_agent_heartbeat</code> — there is no separate
registration step, because there is nothing a registration knows that the
first heartbeat does not.</p>
</div>"""

    missing = [row for row in fold["agents"]
               if row["liveness"] in ("lost", "never")]
    if missing:
        names = ", ".join(_esc(row["label"]) for row in missing[:4])
        return f"""
<div class="banner warn">
<h2>{len(missing)} of {fold['total']} are not answering</h2>
<p>{names} last checked in longer ago than this server's patience allows.
An agent goes quiet for two reasons — it crashed, or it is doing something
long and is not beating while it works. The heartbeat beside each card is
what tells them apart.</p>
</div>"""

    over = fold["over_cap"]
    if over:
        return f"""
<div class="banner warn">
<h2>{len(over)} over the spend cap</h2>
<p>{_esc(', '.join(over))} has committed more than its cap allows. The cap is
a fold over holds and debits in the ledger, not a stored counter, so this
is the ledger's own arithmetic rather than a number somebody maintained.</p>
</div>"""

    return f"""
<div class="banner">
<h2>All {fold['total']} answering</h2>
<p>Every registered agent has beaten within the window.</p>
</div>"""


def _card(row):
    spend = ""
    if row["spend"]:
        spend = (f'<p class="muted">spend {row["spend"]["committed_minor"]} / '
                 f'{row["spend"]["cap_minor"]}'
                 f'{" — OVER" if row["spend"]["over"] else ""}</p>')
    tags = "".join(f'<span class="tag">{_esc(tag)}</span>'
                   for tag in row["capabilities"])
    if not tags:
        tags = '<span class="muted">no capabilities advertised</span>'
    # The phrase, not the stamp: an operator asked "is it still there"
    # should not have to subtract two datetimes in their head.
    beat = _esc(row["heartbeat_ago"]) if row["heartbeat_at"] else "never checked in"
    return f"""
<div class="card {_esc(row['liveness'])}">
<h3>{_esc(row['label'])}</h3>
<p class="state {_esc(row['liveness'])}">{_esc(row['liveness'])}</p>
<p class="muted">{_esc(row['agent_id'])}</p>
{f'<p class="muted">{_esc(row["purpose"])}</p>' if row['purpose'] else ''}
<p>{tags}</p>
<p class="muted" title="{_esc(row['heartbeat_at'])}">last beat {beat}</p>
{spend}
</div>"""


def _feed_html(fold):
    if not fold["feed"]:
        return ('<p class="muted">Nothing on the feed yet. It is the '
                '<code>feed_posts</code> collection (app-collab), which has '
                'carried <code>claim</code> and <code>release</code> in its '
                'kinds since before this page existed.</p>')
    # Relative time on the line, the exact stamp in the tooltip. A feed is
    # read for "how long ago", never for "at what instant" -- and the one
    # time somebody does want the instant, hovering gives it without
    # spending a line on it.
    items = "".join(
        f'<li><span class="kind">{_esc(post["kind"])}</span>'
        f'{_esc(post["body"])}'
        f'<br><span class="muted" title="{_esc(post["created_at"])}">'
        f'{_esc(post["owner_id"])}'
        f'{" &middot; " + _esc(post["ago"]) if post["ago"] else ""}</span></li>'
        for post in fold["feed"])
    return f'<ul class="feed">{items}</ul>'


def _worker_rows(fold):
    workers = fold.get("workers", {}).get("workers", [])
    if not workers:
        return ('<p class="muted">Nothing is scheduled on this server yet.</p>')
    out = []
    for row in workers:
        state = _esc(row["liveness"])
        when = _esc(row["last_run_ago"]) or "never"
        schedule = _esc(row["schedule"]) or "&mdash;"
        # "live, and failing" is a worse state than "lost" and the one a
        # liveness badge alone would render as healthy, so the error gets
        # its own line rather than a colour.
        error = (f'<div class="workererr">last run failed: '
                 f'{_esc(row["last_error"][:160])}</div>'
                 if row["failing"] else "")
        out.append(
            f'<tr class="w-{state}">'
            f'<td><code>{_esc(row["object_id"])}</code>{error}</td>'
            f'<td><code>{schedule}</code></td>'
            f'<td><span class="pill {state}">{state}</span></td>'
            f'<td>{when}</td>'
            f'</tr>')
    return ('<table class="workers"><thead><tr><th>Scheduled object</th>'
            '<th>Schedule</th><th>State</th><th>Last run</th></tr></thead>'
            f'<tbody>{"".join(out)}</tbody></table>')


def _page(fold):
    cards = "".join(_card(row) for row in fold["agents"])
    caps = (", ".join(_esc(tag) for tag in fold["capabilities"])
            or "none advertised")
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Agents</div>
<h1>Agents</h1>
<p>Who is operating this server, what they will accept work for, and
whether they are still answering.</p>
{_banner(fold)}
<div class="cols">
<div>
<h2>Agents</h2>
<div class="cards">{cards}</div>
</div>
<div>
<h2>Coordination feed</h2>
{_feed_html(fold)}
</div>
</div>
<p class="note">Liveness is a <strong>heartbeat</strong>, not a last write.
An agent reasoning for ten minutes writes nothing and is perfectly alive,
so "when did it last change a record" cannot answer "is it still there" —
which is the only reason this collection exists. Everything else about an
agent is already recorded: what it did is the
<a href="/activity">change log</a>, who it is, is its user.</p>

<h2>This server's own work</h2>
<p>The scheduled passes this box runs for itself, and whether they are
still running. This is <strong>folded from the run log, not from a
heartbeat</strong>: every scheduled execution already writes a
<code>scheduler_runs</code> row, and a row saying the work ran is better
evidence than a process saying it is alive.</p>
{_worker_rows(fold)}
<p class="muted">A task that has <em>never</em> run is shown from the
scheduler's declared tasks rather than the log, because an object with no
rows is invisible to a fold over rows — and a pass that has quietly done
nothing since the day it was added is exactly the failure worth
catching.</p>

<h2>Capabilities on this server</h2>
<p>{caps}</p>
<p class="muted">Capabilities are opt-in and empty by default. An agent
advertising nothing is routed nothing — nobody's machine joins a compute
pool by accident.</p>

<p class="note"><a href="/agents.json">This page as JSON</a> ·
<a href="/agent-registry">the registry as a table</a></p>
"""


def GET(request):
    request = request or {}
    path = _text((request or {}).get("_path") or "/agents").lower()
    fold = report()

    if path.endswith(".json"):
        return {"status": 200,
                "content_type": "application/json; charset=utf-8",
                "body": json.dumps(fold, indent=2, sort_keys=True, default=str)}

    return {
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agents</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap ag">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{_page(fold)}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }


def POST(request):
    return GET(request)
