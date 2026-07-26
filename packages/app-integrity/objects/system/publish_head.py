"""system_publish_head -- take a digest of each ledger and lodge it where
this server cannot reach.

Runs daily from the package's own schedule, and by hand whenever an
operator wants a fresh anchor before something important.

## Why this is a system object rather than an action

plan/tamper-evidence-spec.md called it `action_publish_head`. It is a
system object because it is TIME-DRIVEN, and time-driven work belongs to
the daemon (docs/logic-decisions.md #2). An anchoring pass that only
happens when somebody remembers to press a button is an anchoring pass
with a gap exactly where an incident is. It can still be executed by hand
-- the daily schedule and the button are the same object, so they cannot
drift apart.

## What one pass does

For each configured ledger: fold `object_ledger_head.head()` over the
rows, compare it with the newest anchor already recorded, and if anything
has changed, write a new anchor and submit the digest to every notary in
`notary.endpoints`.

**Nothing is submitted anywhere except a digest.** No collection name a
stranger could use, no row count -- wait, the label DOES carry the
collection and the count, because they are useless to an outsider and
essential to the operator reading their own anchor back. What never
travels is a single row of content, which is the entire reason lodging a
digest with a stranger is a reasonable thing to do.

## The idempotency, and why it is not the usual one

A ledger that has not changed since yesterday produces the same digest
over the same row count, and this pass writes nothing. That is not to
avoid a duplicate row for tidiness -- it is because an anchor's whole
content is `(row_count, digest)`, so a second identical anchor carries no
information a reader could act on, while a hundred of them make the
ladder `locate()` walks longer with no extra resolution.

## What it will not pretend

**Anchoring to nowhere is recorded honestly.** With no notaries
configured, the anchor is still written and its status is `recorded` with
`notary_count` 0 -- which site_ledger_integrity renders as "these anchors
prove nothing against anyone with access to this server", in those words.
The alternative, a green tick for a digest nobody outside this box holds,
would be the single most misleading thing this package could do.

**A notary that refuses does not lose the anchor.** The row is written
either way, with status `failed` and the reason in `note`. A digest only
this server holds is worth less than one a stranger holds and is worth
considerably more than no digest at all -- and tomorrow's pass will lodge
the same history under a longer prefix.

**It does not verify.** Checking anchors against the ledger is
site_ledger_integrity's job, deliberately kept out of the writer: a pass
that both took anchors and blessed them would be marking its own homework.
"""

import json
import os
import socket
import urllib.error
import urllib.request

import object_ledger_head
import object_notary
import object_records
import object_schemas

ACTOR = "system_publish_head"
ANCHORS = "anchors"
SETTINGS_COLLECTION = "app_settings"

COLLECTIONS_KEY = "ledger.anchored_collections"
ENDPOINTS_KEY = "notary.endpoints"
BASE_URL_KEY = "portal.base_url"

# Tests and local development only -- see
# object_notary.is_self_endpoint. An ENV VAR rather than a
# setting so that nobody can switch off the self-anchoring check
# from a settings page, which is exactly where somebody about to
# fool themselves would be looking.
ALLOW_LOOPBACK_ENV = "DBBASIC_NOTARY_ALLOW_LOOPBACK"

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024

# The ledgers worth anchoring when nobody has said otherwise. The rule
# behind the list, from plan/tamper-evidence-spec.md: ANCHOR WHAT SOMEBODY
# WOULD PROFIT FROM CHANGING. page_views is absent and stays absent --
# nobody profits from forging traffic and it is the highest-volume writer
# on the box, so anchoring it would cost a full read of the largest file
# here every day to protect the least valuable thing in it.
DEFAULT_COLLECTIONS = (
    "wallet_entries",        # customer money; the highest-value target
    "fin_journal_lines",     # the books an auditor arrives to check
    "payments",
    "refunds",
    "stock_moves",           # shrinkage is theft, and a backdated
                             # adjustment is how it gets hidden
    "notarizations",         # the notary's own log, which is otherwise
                             # the one thing here nobody is watching
)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _settings(base):
    try:
        rows = object_records.read_collection_records(SETTINGS_COLLECTION,
                                                      base_dir=base)
    except Exception:
        return {}
    return {_text(row.get("key")): _text(row.get("value"))
            for row in rows if _text(row.get("key"))}


def _csv_setting(settings, key):
    return [part.strip() for part in settings.get(key, "").split(",")
            if part.strip()]


def _schema_fields(base, collection):
    """The field list, in schema order, minus the bookkeeping columns.

    `_op` is the append-storage marker and belongs to the file rather than
    to the record, so hashing it would tie the digest to a storage detail
    that compaction rewrites -- exactly what hashing logical rows is meant
    to avoid.
    """
    try:
        schema = object_schemas.get_schema(collection, base_dir=base)
    except Exception:
        return []
    fields = []
    for field in (schema or {}).get("fields") or []:
        name = _text(field.get("name"))
        if name and name != "_op":
            fields.append(name)
    return fields


def _newest_anchor(anchors, collection):
    newest = None
    for row in anchors:
        if _text(row.get("collection")) != collection:
            continue
        if newest is None or _text(row.get("created_at")) > _text(
                newest.get("created_at")):
            newest = row
    return newest


def _lodge(endpoint, digest, label, *, own_base_url=""):
    """Submit one digest to one notary. Returns (ok, detail).

    Refuses an endpoint that is this server before making any request.
    Lodging with yourself always succeeds and proves nothing, and the cost
    of letting it through is not a wasted request -- it is
    site_ledger_integrity swapping "anchored, but only here" for
    "verified, and held elsewhere" on the strength of it, which turns an
    honest missing claim into a specific false one.

    Deliberately narrow: a POST to a known object with a known body, no
    redirects followed, a timeout, and a bounded read. Every failure mode
    -- refused, timed out, 4xx, unparseable -- comes back as (False,
    reason) rather than raising, because one unreachable notary must not
    stop the pass from lodging with the others or from recording the
    anchor locally.
    """
    allow_loopback = os.environ.get(ALLOW_LOOPBACK_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")
    if object_notary.is_self_endpoint(endpoint, own_base_url,
                                      allow_loopback=allow_loopback):
        return False, (f"{endpoint} is this server. An anchor a server holds "
                       f"about itself is not independent, so it is not "
                       f"counted -- point this at a notary somebody else "
                       f"runs.")

    url = endpoint.rstrip("/") + "/objects/action_notarize"
    body = json.dumps({"digest": digest, "algorithm": object_ledger_head.ALGORITHM,
                       "label": label}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "dbbasic-object-server/publish-head"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        return False, f"{endpoint} answered {error.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        return False, f"{endpoint} unreachable ({error})"

    try:
        answer = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, f"{endpoint} answered something that was not JSON"

    if answer.get("found") is True:
        # `already_recorded` is a success, and an interesting one: it means
        # this exact digest was lodged there before, so the history it
        # covers has an even older independent timestamp than this pass.
        return True, ("already held" if answer.get("already_recorded")
                      else "accepted")
    return False, f"{endpoint} refused: {_text(answer.get('error'))[:120]}"


def _anchor_one(base, collection, endpoints, anchors, *, own_base_url=""):
    fields = _schema_fields(base, collection)
    if not fields:
        return {"collection": collection, "status": "skipped",
                "reason": "no schema on this box"}

    try:
        rows = object_records.read_collection_records(collection, base_dir=base)
    except Exception as error:
        return {"collection": collection, "status": "skipped",
                "reason": f"unreadable ({error})"}

    folded = object_ledger_head.head(rows, fields, collection=collection)

    previous = _newest_anchor(anchors, collection)
    if previous is not None and _text(previous.get("digest")) == folded["digest"]:
        return {"collection": collection, "status": "unchanged",
                "row_count": folded["row_count"],
                "reason": "nothing appended since the last anchor"}

    label = f"{collection} head @ {folded['row_count']} rows"
    lodged, notes = [], []
    for endpoint in endpoints:
        ok, detail = _lodge(endpoint, folded["digest"], label,
                            own_base_url=own_base_url)
        if ok:
            lodged.append(endpoint)
        notes.append(f"{endpoint}: {detail}")

    if not endpoints:
        status = "recorded"
    elif len(lodged) == len(endpoints):
        status = "published"
    elif lodged:
        status = "partial"
    else:
        status = "failed"

    record = {
        "collection": collection,
        "row_count": str(folded["row_count"]),
        "digest": folded["digest"],
        "algorithm": folded["algorithm"],
        "scheme": folded["scheme"],
        "covered_fields": ",".join(folded["fields"]),
        "notaries": ",".join(lodged),
        "notary_count": str(len(lodged)),
        "status": status,
        "note": "; ".join(notes)[:500],
    }
    try:
        stored = object_records.create_collection_record(
            ANCHORS, record, base_dir=base, actor=ACTOR)
    except Exception as error:
        return {"collection": collection, "status": "error",
                "reason": f"the anchor could not be written: {error}"}

    return {"collection": collection, "status": status,
            "row_count": folded["row_count"], "digest": folded["digest"],
            "notary_count": len(lodged), "anchor_id": stored.get("id")}


def EVENT(request):
    """The daemon entry point.

    Named EVENT with POST aliased below because the change dispatcher and
    the scheduler call EVENT -- declaring only POST is a pass that silently
    never runs, which has been a real production bug here twice.
    """
    request = request or {}
    base = _base_dir()
    settings = _settings(base)

    requested = _csv_setting(settings, COLLECTIONS_KEY) or list(DEFAULT_COLLECTIONS)
    endpoints = _csv_setting(settings, ENDPOINTS_KEY)

    try:
        anchors = object_records.read_collection_records(ANCHORS, base_dir=base)
    except Exception:
        anchors = []

    own_base_url = settings.get(BASE_URL_KEY, "")
    results = [_anchor_one(base, collection, endpoints, anchors,
                           own_base_url=own_base_url)
               for collection in requested]

    written = [r for r in results
               if r["status"] in ("recorded", "published", "partial", "failed")]
    independent = sum(r.get("notary_count", 0) for r in written)

    return {
        "ok": True,
        "anchored": len(written),
        "considered": len(results),
        "endpoints": endpoints,
        "independent_lodgements": independent,
        "results": results,
        "warning": (
            f"No notary endpoints are configured ({ENDPOINTS_KEY}), so these "
            f"anchors are held only by the server that took them. Anybody "
            f"able to rewrite a ledger here can rewrite its anchor beside it. "
            f"They become evidence when the digest is also held somewhere "
            f"else." if not endpoints else ""),
    }


POST = EVENT
