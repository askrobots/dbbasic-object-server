"""system_invoice_aging -- the time-driven half of the late-payment story.

Time changes an invoice's meaning with no write happening, so this runs as a
daemon-scheduled pass (docs/logic-decisions.md #2), like
system_fin_recurring_runner: register a daily scheduler task with
object_id="system_invoice_aging", method="POST"; also manually runnable.

Per pass:
- sent/partial invoices past due_date + grace (payments.grace_days, default 0)
  with a positive balance flip to OVERDUE, dunning_level=1, last_dunned_on set,
  and a dunning email is queued to customer_email via the generic outbox
  (raw address -> outbox directly; notify's user-mapping is for user ids).
- overdue invoices re-dun every payments.dunning_repeat_days (default 7) up to
  payments.dunning_max_level (default 3): bump dunning_level + re-queue email.
  Each bump is an ordinary record update, so notify_rules (owner in_app) and
  realtime fire like any other change -- escalation steps ARE state changes
  (docs/business-logic-patterns.md, dunning walkthrough).
- overdue invoices whose balance cleared some other way fall back to
  paid/partial (belt to system_invoice_status's suspenders).
"""

import os
import secrets
from datetime import date, timedelta

import object_ids
import object_records

ACTOR = "system_invoice_aging"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _setting(base, key, default):
    try:
        for row in object_records.read_collection_records("app_settings", base_dir=base):
            if row.get("key") == key and row.get("value"):
                return row["value"].strip()
    except Exception:
        pass
    return default


def _int_setting(base, key, default):
    try:
        return int(_setting(base, key, str(default)))
    except ValueError:
        return default


def _parse(day):
    try:
        y, m, d = (int(x) for x in str(day).split("-"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        return None


def _portal_link(base, invoice):
    """The door this dunning email points at
    (plan/customer-payment-portal-spec.md): closes the loop that makes
    dunning ACTIONABLE -- an email that says "you owe X" with nowhere to
    go is the exact gap this whole feature exists to close.

    Mints invoices.portal_token lazily, right here, the first time an
    invoice needs a working link. action_regenerate_portal_link is the
    owner-facing way to mint or rotate one on demand, but wiring EAGER
    issuance into the invoice "send" flow (draft -> sent) lives in objects
    outside this file's edit boundary (site_invoices / a hook on the
    invoices collection -- see that action object's own docstring), so
    without this fallback an invoice could reach its first overdue dunning
    email with no token yet and no way to get one. Dunning is the moment a
    working link is most needed, so this is where the fallback lives.

    Returns "" -- never a broken relative URL -- when portal.base_url is
    unset, or when the mint fails to persist. A dunning email with no link
    at all is honest about what this deployment has configured; one with a
    guaranteed-404 link would be worse than the plain amount-owed text it
    replaces.
    """
    base_url = str(_setting(base, "portal.base_url", "") or "").rstrip("/")
    if not base_url:
        return ""
    token = str(invoice.get("portal_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        try:
            object_records.update_collection_record(
                "invoices", invoice["id"], {"portal_token": token},
                base_dir=base, actor=ACTOR, preserve_read_only=True,
            )
        except Exception:
            return ""  # could not persist a token: don't mail a dead link
    return f"{base_url}/pay/{token}"


def _queue_dunning_email(base, invoice, level):
    to = (invoice.get("customer_email") or "").strip()
    if not to:
        return False
    link = _portal_link(base, invoice)
    pay_line = f"\nView and pay this invoice online: {link}\n" if link else ""
    try:
        object_records.create_collection_record(
            "email_outbox",
            {
                "id": object_ids.new_uuid4(),
                "to": to,
                "from_addr": _setting(base, "payments.dunning_from", ""),
                "subject": f"Invoice {invoice.get('number') or invoice.get('id')} is overdue"
                           + (f" (reminder {level})" if level > 1 else ""),
                "text_body": (
                    f"Hello {invoice.get('customer_name') or ''},\n\n"
                    f"Invoice {invoice.get('number') or invoice.get('id')} for "
                    f"{invoice.get('total_cents') or '0'} cents was due on "
                    f"{invoice.get('due_date') or 'its due date'} and has an outstanding "
                    f"balance of {invoice.get('balance_due_cents') or '?'} cents.\n"
                    f"{pay_line}\n"
                    "Please arrange payment at your earliest convenience.\n"
                ),
                "source_object_id": ACTOR,
            },
            base_dir=base,
            actor=ACTOR,
        )
        return True
    except Exception:
        return False  # outbox not installed / bad row: dunning is best-effort


def POST(request):
    base = _base_dir()
    today = _parse(request.get("today")) or date.today()
    grace = _int_setting(base, "payments.grace_days", 0)
    repeat = max(1, _int_setting(base, "payments.dunning_repeat_days", 7))
    max_level = _int_setting(base, "payments.dunning_max_level", 3)

    flipped, dunned, results = 0, 0, []
    try:
        invoices = object_records.read_collection_records("invoices", base_dir=base)
    except Exception:
        return {"ok": True, "skipped": "invoices not installed"}

    for inv in invoices:
        status = inv.get("status") or ""
        try:
            balance = int(inv.get("balance_due_cents") or 0)
            paid = int(inv.get("amount_paid_cents") or 0)
            total = int(inv.get("total_cents") or 0)
        except ValueError:
            continue

        if status in ("sent", "partial"):
            due = _parse(inv.get("due_date"))
            if due is None or balance <= 0 or total <= 0:
                continue
            if today <= due + timedelta(days=grace):
                continue
            changes = {"status": "overdue", "dunning_level": "1",
                       "last_dunned_on": today.isoformat()}
            object_records.update_collection_record(
                "invoices", inv["id"], changes, base_dir=base, actor=ACTOR)
            emailed = _queue_dunning_email(base, {**inv, **changes}, 1)
            flipped += 1
            results.append({"invoice": inv["id"], "action": "overdue", "emailed": emailed})

        elif status == "overdue":
            if total > 0 and balance <= 0:
                new_status = "paid" if paid >= total else "partial"
                object_records.update_collection_record(
                    "invoices", inv["id"], {"status": new_status},
                    base_dir=base, actor=ACTOR)
                results.append({"invoice": inv["id"], "action": new_status})
                continue
            level = int(inv.get("dunning_level") or 1)
            last = _parse(inv.get("last_dunned_on")) or today
            if level >= max_level or today < last + timedelta(days=repeat):
                continue
            changes = {"dunning_level": str(level + 1),
                       "last_dunned_on": today.isoformat()}
            object_records.update_collection_record(
                "invoices", inv["id"], changes, base_dir=base, actor=ACTOR)
            emailed = _queue_dunning_email(base, {**inv, **changes}, level + 1)
            dunned += 1
            results.append({"invoice": inv["id"], "action": f"dun-level-{level + 1}",
                            "emailed": emailed})

    return {"ok": True, "flipped_overdue": flipped, "escalated": dunned,
            "today": today.isoformat(), "results": results}
