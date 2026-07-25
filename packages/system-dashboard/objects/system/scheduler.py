"""system_scheduler -- the Flower-style board for daemon-scheduled tasks.

Dan's bar: "celery flower would do better than that" -- the raw /daemon/*
JSON (admin-token-gated, unreadable) is not an operator surface. This page
is: the task board (schedule, next run, last outcome, run count) joined
with the run history (scheduler_runs records the daemon appends per
execution -- result JSON, error + type, duration), plus the two controls
an operator actually reaches for: Run now and Pause/Resume.

Identity posture: public execute like every page object; everything is
gated on an ADMIN identity inside (anonymous/non-admin get a prompt, no
data). POST actions mutate the scheduler trigger's task state directly --
the same state the daemon reads -- so Run now simply sets next_run to now
and the next daemon poll executes it; results then appear here because the
daemon records them as scheduler_runs rows. Live: subscribes to
scheduler_runs over /ws (admin read permission gates the push), with a
60s poll fallback.
"""

import html
import json
import os
import time
from datetime import datetime, timezone

import object_records
import object_state

_logger = None

_TASK_PREFIX = "task_"
_RUNS_SHOWN = 50


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _tasks():
    rows, invalid = [], 0
    for key, value in object_state.get_object_state("scheduler", _base_dir()).items():
        if not key.startswith(_TASK_PREFIX):
            continue
        try:
            task = json.loads(value)
        except (ValueError, TypeError):
            invalid += 1
            continue
        if not isinstance(task, dict):
            invalid += 1
            continue
        task["_key"] = key
        rows.append(task)
    rows.sort(key=lambda t: str(t.get("id") or ""))
    return rows, invalid


def _runs():
    try:
        rows = object_records.read_collection_records("scheduler_runs", base_dir=_base_dir())
    except Exception:
        return None  # collection not installed
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return rows


def _when(epoch):
    if not epoch:
        return "&mdash;"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError):
        return "&mdash;"


def _esc(value):
    return html.escape(str(value or ""))


_DAYS = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
         "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
         "sun": "Sunday", "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
         "thu": "Thursday", "fri": "Friday", "sat": "Saturday"}


def describe_schedule(schedule, task_type=""):
    """Render a schedule in plain English.

    "10 6 * * *" is precise and completely opaque to anyone who does not
    write cron daily -- and an operator who cannot read WHEN a task runs
    cannot tell whether it ran when it should have. So the board leads
    with the English and keeps the cron string as the small print.
    """
    text = str(schedule or "").strip()
    if not text:
        return ""
    if str(task_type).strip() == "onetime":
        return f"Once at {text[:16].replace('T', ' ')}"

    parts = text.split()
    if len(parts) != 5:
        return ""
    minute, hour, dom, month, dow = parts

    def _every(field):
        return field.startswith("*/") and field[2:].isdigit()

    if _every(minute) and hour == dom == month == dow == "*":
        return f"Every {minute[2:]} minutes"
    if minute.isdigit() and hour == "*" and dom == month == dow == "*":
        return f"Hourly at :{int(minute):02d}"
    if _every(hour) and minute.isdigit() and dom == month == dow == "*":
        return f"Every {hour[2:]} hours at :{int(minute):02d}"
    if not (minute.isdigit() and hour.isdigit()):
        return ""

    at = f"{int(hour):02d}:{int(minute):02d} UTC"
    if dom == month == dow == "*":
        return f"Daily at {at}"
    if dow != "*" and dom == "*":
        # Only a single named day is describable: "1-5" is weekdays and
        # "1,4" is twice a week -- calling either "Weekly" would be a
        # confident wrong answer, so those fall back to the raw cron.
        day = _DAYS.get(dow.lower())
        return f"Weekly on {day} at {at}" if day else ""
    if dom.isdigit() and month == "*" and dow == "*":
        return f"Monthly on day {int(dom)} at {at}"
    if dom.isdigit() and month.isdigit() and dow == "*":
        return f"Yearly on {int(month)}/{int(dom)} at {at}"
    return ""


def _task_rows_html(tasks, latest_by_task):
    if not tasks:
        return '<tr><td colspan="8" class="muted">No task_* entries in the scheduler trigger state.</td></tr>'
    out = []
    for task in tasks:
        tid = str(task.get("id") or task.get("_key"))
        last = latest_by_task.get(tid)
        if last is None:
            last_cell = '<span class="muted">never</span>'
        else:
            badge = ('<span class="ok">&#10003; ok</span>' if last.get("ok") == "true"
                     else '<span class="bad">&#10007; failed</span>')
            last_cell = (f"{badge} <span class=\"muted\">{_esc(last.get('started_at', ''))[:16]}"
                         f" &middot; {_esc(last.get('duration_ms'))}ms</span>")
        status = str(task.get("status") or "")
        toggle = ("pause" if status == "active" else "resume")
        english = describe_schedule(task.get("schedule"), task.get("type"))
        schedule_cell = (
            (f"<strong>{_esc(english)}</strong><br>" if english else "")
            + f"<code class=\"muted\" title=\"minute hour day-of-month month day-of-week\">"
              f"{_esc(task.get('schedule'))}</code>"
            + (f" <span class=\"muted\">{_esc(task.get('type'))}</span>" if not english else "")
        )
        # A package that declares a schedule says WHY it exists; showing it
        # here is the difference between a board of cron strings and a board
        # an operator can reason about at 3am.
        description = str(task.get("description") or "")
        out.append(
            "<tr>"
            f"<td><code>{_esc(tid)}</code>"
            + (f"<br><span class=\"muted\">{_esc(description)}</span>" if description else "")
            + "</td>"
            f"<td><code>{_esc(task.get('object_id'))}</code>.{_esc(task.get('method', 'POST'))}</td>"
            f"<td>{schedule_cell}</td>"
            f"<td class=\"{'ok' if status == 'active' else 'warn'}\">{_esc(status)}</td>"
            f"<td>{_when(task.get('next_run'))}</td>"
            f"<td>{last_cell}</td>"
            f"<td>{_esc(task.get('run_count') or 0)}</td>"
            "<td>"
            f"<button data-act=\"run_now\" data-key=\"{_esc(task['_key'])}\">Run now</button> "
            f"<button data-act=\"{toggle}\" data-key=\"{_esc(task['_key'])}\">{toggle.capitalize()}</button>"
            "</td>"
            "</tr>"
        )
    return "".join(out)


def _run_rows_html(runs):
    if runs is None:
        return ('<tr><td colspan="6" class="warn">scheduler_runs collection not installed '
                "&mdash; reinstall the system-dashboard package to record run history.</td></tr>")
    if not runs:
        return '<tr><td colspan="6" class="muted">No runs recorded yet.</td></tr>'
    out = []
    for run in runs[:_RUNS_SHOWN]:
        ok = run.get("ok") == "true"
        detail = run.get("result") if ok else run.get("error")
        detail_html = ""
        if detail:
            label = "result" if ok else _esc(run.get("error_type") or "error")
            detail_html = (f"<details><summary>{label}</summary>"
                           f"<pre>{_esc(detail)}</pre></details>")
        out.append(
            "<tr>"
            f"<td>{_esc(run.get('started_at', ''))[:19]}</td>"
            f"<td><code>{_esc(run.get('task_id'))}</code></td>"
            f"<td><code>{_esc(run.get('object_id'))}</code></td>"
            f"<td>{'<span class=ok>&#10003; ok</span>' if ok else '<span class=bad>&#10007; failed</span>'}</td>"
            f"<td>{_esc(run.get('duration_ms'))}ms</td>"
            f"<td>{detail_html}</td>"
            "</tr>"
        )
    return "".join(out)


_SCRIPT = """
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  btn.disabled = true;
  const resp = await fetch('/scheduler', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: btn.dataset.act, key: btn.dataset.key}),
  });
  if (!resp.ok) { alert('Action failed: ' + resp.status); btn.disabled = false; return; }
  location.reload();
});
let reloadTimer = null;
function queueReload() {
  if (reloadTimer) return;
  reloadTimer = setTimeout(() => location.reload(), 400);
}
function trySubscribe() {
  if (window.dbbasicSubscribe) { window.dbbasicSubscribe('scheduler_runs', queueReload); }
  else { setTimeout(trySubscribe, 1500); }
}
trySubscribe();
setTimeout(() => location.reload(), 60000);
"""


def _page(body, *, title="Scheduler"):
    return {
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/style">
<style>
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 1.25rem; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line, #333); vertical-align: top; }}
th {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }}
.muted {{ color: var(--muted, #999); }}
.ok {{ color: var(--positive, #52d273); }}
.bad {{ color: var(--danger, #ff6b6b); }}
.warn {{ color: var(--warning, #f1b747); }}
.tiles {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; }}
.tile {{ background: var(--panel, #1a1a22); border: 1px solid var(--line, #333); border-radius: 8px; padding: 0.6rem 1rem; min-width: 130px; }}
.tile .n {{ font-size: 1.3rem; font-weight: 600; }}
.tile .l {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); }}
h2 {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted, #999); margin: 1.5rem 0 0.5rem; }}
pre {{ white-space: pre-wrap; max-width: 480px; font-size: 0.75rem; margin: 0.3rem 0 0; }}
details summary {{ cursor: pointer; }}
button {{ cursor: pointer; }}
</style>
</head>
<body>
<script src="/nav"></script>
<div class="wrap">
<h1>Scheduler</h1>
{body}
</div>
</body>
</html>""",
    }


def GET(request):
    identity = request.get("_identity") or {}
    user_id = identity.get("user_id")
    if "admin" not in (identity.get("roles") or []):
        prompt = ('<a href="/login?next=/scheduler">Sign in</a> with an admin account to see '
                  "scheduled tasks and their run history."
                  if not user_id else
                  "You are signed in, but the scheduler board needs an admin role.")
        return _page(f'<p class="muted">{prompt}</p>')

    tasks, invalid = _tasks()
    runs = _runs()
    latest_by_task = {}
    failures_24h = runs_24h = 0
    if runs:
        cutoff = datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc).isoformat()
        for run in runs:
            tid = str(run.get("task_id") or "")
            if tid and tid not in latest_by_task:
                latest_by_task[tid] = run  # runs sorted newest-first
            if (run.get("started_at") or "") >= cutoff:
                runs_24h += 1
                if run.get("ok") != "true":
                    failures_24h += 1

    now = int(time.time())
    due = sum(1 for t in tasks
              if t.get("status") == "active" and (t.get("next_run") or 0) and int(t.get("next_run") or 0) <= now)
    tiles = (
        f'<div class="tiles">'
        f'<div class="tile"><div class="n">{sum(1 for t in tasks if t.get("status") == "active")}</div><div class="l">Active tasks</div></div>'
        f'<div class="tile"><div class="n">{due}</div><div class="l">Due now</div></div>'
        f'<div class="tile"><div class="n">{runs_24h}</div><div class="l">Runs (24h)</div></div>'
        f'<div class="tile"><div class="n {"bad" if failures_24h else ""}">{failures_24h}</div><div class="l">Failures (24h)</div></div>'
        + (f'<div class="tile"><div class="n warn">{invalid}</div><div class="l">Invalid entries</div></div>' if invalid else "")
        + "</div>"
    )
    body = f"""{tiles}
<h2>Tasks</h2>
<table>
<thead><tr><th>Task</th><th>Target</th><th>Schedule</th><th>Status</th><th>Next Run</th><th>Last Run</th><th>Runs</th><th>Actions</th></tr></thead>
<tbody>{_task_rows_html(tasks, latest_by_task)}</tbody>
</table>
<p class="muted" style="font-size:0.75rem;margin:0.5rem 0 0">
Cron fields are <code>minute hour day-of-month month day-of-week</code>
(all times UTC). <code>*</code> means every, <code>*/5</code> means every 5th.
</p>
<h2>Recent Runs</h2>
<table>
<thead><tr><th>Started</th><th>Task</th><th>Target</th><th>Outcome</th><th>Duration</th><th>Detail</th></tr></thead>
<tbody>{_run_rows_html(runs)}</tbody>
</table>
<script>{_SCRIPT}</script>"""
    return _page(body)


def POST(request):
    identity = request.get("_identity") or {}
    if "admin" not in (identity.get("roles") or []):
        return {"status": 403, "error": "Scheduler actions require an admin session."}

    action = str(request.get("action") or "").strip()
    key = str(request.get("key") or "").strip()
    if action not in ("run_now", "pause", "resume") or not key.startswith(_TASK_PREFIX):
        return {"status": 400, "error": "action must be run_now|pause|resume with a task_* key"}

    manager = object_state.ObjectStateManager("scheduler", base_dir=_base_dir())
    raw = manager.get(key)
    if not raw:
        return {"status": 404, "error": f"No such task entry: {key}"}
    try:
        task = json.loads(raw)
    except (ValueError, TypeError):
        return {"status": 409, "error": f"Task entry is not valid JSON: {key}"}

    if action == "run_now":
        task["next_run"] = int(time.time())
        task["status"] = "active"
    elif action == "pause":
        task["status"] = "paused"
    else:  # resume
        task["status"] = "active"
        task["next_run"] = None  # daemon recomputes from the schedule
    manager.set(key, json.dumps(task))
    if _logger:
        _logger.info("scheduler task action", action=action, key=key,
                     actor=identity.get("user_id"))
    return {"status": 200, "ok": True, "action": action, "key": key,
            "next_run": task.get("next_run"), "task_status": task.get("status")}
