"""Run due fin_recurring templates: the dormant half of adjusting entries.

fin_recurring shipped schema-only (template_lines, frequency, next_run,
auto_post) -- the daemon never processed it. This object is the engine:
POST runs one pass over due templates (is_active, next_run <= today),
composes a journal (kind=adjusting) from template_lines, posts it when
auto_post says so AND it balances (an unbalanced template stays draft with a
note -- a broken template must never crash the pass), then advances
next_run by frequency and stamps last_run.

Time-driven work belongs to the daemon (docs/logic-decisions.md #2): wire it
via the existing scheduler contract -- a scheduler task with
object_id="system_fin_recurring_runner", method="POST" on a daily cron. Also
callable manually (an admin "run now"). Idempotent per period:
generated_from="fin_recurring/{id}:{next_run}".
"""

import json
import os
from datetime import date, timedelta

import object_finance
import object_records

ACTOR = "fin_recurring_runner"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _advance(day_iso, frequency):
    try:
        y, m, d = (int(x) for x in day_iso.split("-"))
        current = date(y, m, d)
    except (ValueError, AttributeError):
        return ""
    freq = (frequency or "monthly").strip().lower()
    if freq == "weekly":
        return (current + timedelta(days=7)).isoformat()
    if freq == "yearly":
        return current.replace(year=current.year + 1).isoformat()
    # monthly (default): same day next month, clamped
    month = current.month + 1
    year = current.year + (1 if month > 12 else 0)
    month = 1 if month > 12 else month
    for day in (current.day, 30, 29, 28):
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def POST(request):
    base = _base_dir()
    today = str(request.get("today") or date.today().isoformat())
    try:
        templates = object_records.read_collection_records("fin_recurring", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "fin_recurring not installed"}

    ran, results = 0, []
    for tpl in templates:
        if (tpl.get("is_active") or "").lower() not in ("true", "1", "yes"):
            continue
        next_run = (tpl.get("next_run") or "").strip()
        if not next_run or next_run > today:
            continue
        marker = f"fin_recurring/{tpl['id']}:{next_run}"
        already = object_finance.find_journal_by_provenance(base, marker) is not None
        if not already:
            try:
                lines_spec = json.loads(tpl.get("template_lines") or "[]")
            except (ValueError, TypeError):
                results.append({"template": tpl["id"], "error": "bad template_lines JSON"})
                continue
            auto_post = (tpl.get("auto_post") or "").lower() in ("true", "1", "yes")
            composed = object_finance.compose_posted_journal(
                base,
                generated_from=marker,
                date=next_run,
                description=f"{tpl.get('name') or 'Recurring entry'} ({next_run})",
                lines=lines_spec,
                owner_id=tpl.get("owner_id", ""),
                entity_id=tpl.get("entity_id", ""),
                kind="adjusting",
                actor=ACTOR,
                post=auto_post,
            )
            if not composed.get("ok"):
                results.append({"template": tpl["id"], "error": composed.get("error", "compose failed")})
                continue
            result = {"template": tpl["id"],
                      "journal": composed.get("journal_id", ""),
                      "posted": bool(composed.get("posted"))}
            if "did not balance" in (composed.get("note") or ""):
                result["note"] = "unbalanced: left draft"
            results.append(result)
            ran += 1
        # Advance the schedule even if this period was already composed
        # (a crashed earlier pass may have composed but not advanced).
        object_records.update_collection_record(
            "fin_recurring", tpl["id"],
            {"next_run": _advance(next_run, tpl.get("frequency")), "last_run": next_run},
            base_dir=base, actor=ACTOR,
        )
    return {"ok": True, "ran": ran, "results": results, "today": today}
