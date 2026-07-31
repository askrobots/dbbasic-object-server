"""Pre-write hook for app_settings: a key is configured once.

app_settings is read by forty-odd objects, each through its own small
`_setting(base, key)` helper, and every one of them scans the collection
and returns the FIRST row whose key matches. That is a perfectly
reasonable reader. It is only dangerous because nothing stopped a second
row with the same key from existing -- and when one does, it does not
conflict, it does not warn, it simply loses. The newer value is invisible
and the operator has no way to see why their change did nothing.

This is not hypothetical. It shipped a wrong journal on the live box:
`inventory.journal.inventory_account` had an old verification value, a
correct one was added beside it, and the very next sale credited the
stale account. The books balanced. The entry was wrong.

So the same posture as the provenance rule in hook_wallet_entries
(docs/logic-decisions.md #7): stop honouring an invariant in every reader
and start ENFORCING it at the one write path they all share. A duplicate
key is refused with the id of the row that already owns it, because the
operator's next question is always "then where IS it set?".

Updates are allowed to keep their own key -- that is just editing a
setting -- and only a COLLISION with a different row is refused.
"""

import os

import object_records

COLLECTION = "app_settings"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def BEFORE_WRITE(request):
    action = request.get("action")
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}
    key = str(record.get("key") or "").strip()
    if not key:
        return None  # schema validation owns the required-field error

    record_id = str(record.get("id") or "")
    try:
        rows = object_records.read_collection_records(
            COLLECTION, base_dir=_base_dir())
    except Exception:
        # An unreadable settings collection is not this hook's problem to
        # report; the write path itself will fail properly if it matters.
        return None

    for row in rows:
        if str(row.get("key") or "").strip() != key:
            continue
        if str(row.get("id") or "") == record_id:
            continue    # editing the row that already owns this key
        return {
            "error": (
                f"Setting {key!r} is already configured (record "
                f"{row.get('id')}, value {row.get('value')!r}). A key is "
                f"configured once: readers take the first row that matches, "
                f"so a second row would not override it -- it would be "
                f"silently ignored. Edit that record instead."
            ),
            "status": 409,
        }
    return None
