"""system_mail_sync_attention -- how many mail accounts keep failing to sync.

COUNT only (docs/package-authoring.md, the attention contract). An account
counts when it is enabled and its last object_mail.ATTENTION_AFTER passes in
a row all failed: one failure is a network blip, three is a changed
password or a server that has gone away, and nobody will find out from the
inbox itself, because a mailbox that stopped syncing looks exactly like a
quiet one. That look-alike is the failure the predecessor's silent
``except: pass`` produced, and this count is what ends it.

The count is deployment-wide, as every attention count is, so the detail
names no person and no address. The owner reads which account and why on
/mail/accounts, where sync_error is shown in full.

A box without app-mail-sync installed reads zero rather than raising.
"""

import os

import object_mail
import object_records


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value):
    try:
        return int(_text(value) or 0)
    except ValueError:
        return 0


def COUNT(request):
    base = os.environ.get("DBBASIC_DATA_DIR", "data")
    try:
        accounts = object_records.read_collection_records("mail_accounts", base_dir=base)
    except (LookupError, OSError, ValueError):
        return {"count": 0}
    failing = [row for row in accounts
               if _text(row.get("enabled")).lower() not in {"false", "0", "no", "off"}
               and _int(row.get("sync_attempts")) >= object_mail.ATTENTION_AFTER]
    if not failing:
        return {"count": 0}
    return {"count": len(failing),
            "detail": f"failed {object_mail.ATTENTION_AFTER} or more syncs in a row"}
