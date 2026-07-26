"""action_export_subject_data -- everything this server holds against one
email address, folded into one JSON document.

POST {email} -> {ok, email, collections: {...}, counts: {...},
                 unavailable: [...], not_searchable: [...]}

The access/portability half of a subject request, and the half this system
can do honestly. Almost everything a customer gives this server is keyed
by `customer_email` -- orders, invoices, carts, disputes -- and the rest
hangs off those by id: payments belong to an invoice, shipments to an
order. So the export is a fold, not a search, and it can be complete in a
way a keyword search over a database never is.

## The property it is judged on: nothing keyed to anybody else

An export that leaks a second customer's order into the first customer's
subject request is not a partial success, it is a data breach performed by
the compliance feature. So the joins run only in the safe direction: the
subject's invoice ids select payments, the subject's order ids select
shipments, and a payment whose invoice is not the subject's is never
looked at again. tests/test_privacy.py stages two customers and asserts
one export contains nothing of the other's -- that is the test that
matters here, and it is worth more than every other assertion in the file.

## Admin-executed, always

There is no self-service door. A page that returned somebody's entire
order history to whoever typed their email address would be a lookup
oracle for anyone who knows a customer's address, and the identity check a
real subject request needs -- "prove you are this person" -- is a human
judgement rather than an HTTP parameter. So this refuses anyone who is not
an operator, and the operator runs it after satisfying themselves who they
are talking to.

## What it deliberately does NOT return, and says so

`page_views` and untethered `conversions` rows are keyed by IP address and
by an opaque visitor token with no link to a person. They cannot be
searched for a subject in either direction, and guessing -- "these hits
came from an address that once loaded her invoice" -- would invent a link
this system spent real effort not creating. They are reported in
`not_searchable` with that reason rather than silently omitted, because a
silent omission reads as "we hold nothing".

Conversions ARE returned where they are genuinely identifiable: a row
whose `user_id` is this subject's account, or whose provenance names one
of their own orders or invoices.

## There is no erasure here, and that is a decision rather than a gap

Do not add one to this object. Erasure and Doctrine #8 (evidence is never
edited) genuinely conflict: orders, invoices and payments are business
records with statutory retention, and deleting a row an invoice
references does not anonymise a customer, it breaks the ledger. The
resolution the plan argues for is redaction-with-a-tombstone -- the LEDGER
keeps the fact, the CONTACT record loses the identifier, and the tombstone
records that a redaction happened so a later reader knows the gap is
deliberate rather than corruption. That needs a doctrine decision about
which fields are contact and which are evidence, per collection, before
any code is written. A `delete_subject_data` written before that decision
would make the choice silently, one collection at a time, in the one place
it can never be undone.
"""

import json
import os

import object_identity
import object_records

ACTOR = "action_export_subject_data"
DATA_DIR_ENV = "DBBASIC_DATA_DIR"

# Collections that carry the subject's email directly, and the field that
# carries it. This is the whole primary index: everything else is reached
# through an id on one of these.
EMAIL_KEYED = (
    ("orders", "customer_email"),
    ("invoices", "customer_email"),
    ("carts", "customer_email"),
    ("disputes", "customer_email"),
    ("contacts", "email"),
)

# Collections reached only through a record already proven to be the
# subject's. (collection, field, which primary collection's ids fill it)
LINKED = (
    ("payments", "invoice_id", "invoices"),
    ("shipments", "order_id", "orders"),
)

CONVERSIONS = "conversions"

NOT_SEARCHABLE = (
    {
        "collection": "page_views",
        "reason": "Traffic rows are keyed by network address and by an opaque "
                  "visitor token that is never joined to an account, so they "
                  "cannot be matched to a person in either direction. They are "
                  "deleted on the retention period stated at /privacy.",
    },
    {
        "collection": "conversions",
        "reason": "Conversion rows recorded by a back-office transition carry "
                  "no identity at all. Rows that DO name this subject -- by "
                  "account id, or by naming one of their own orders or "
                  "invoices -- are included in the export above.",
    },
)


def _base_dir():
    return os.environ.get(DATA_DIR_ENV, "data")


def _rows(collection, base):
    """A collection's rows, or None when this box does not have it.

    None and [] are different answers and the export reports them
    differently: "this server does not record shipments" is not "this
    person has no shipments"."""
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def _matches(row, field, email):
    return str(row.get(field) or "").strip().lower() == email


def _subject_user_id(base, email):
    """The account id for this email, if this server has one.

    Read from identity rather than guessed, and used only to select
    conversion rows that already name it.
    """
    try:
        users = object_identity.list_users(base_dir=base)
    except Exception:
        return ""
    for user in users:
        if str(user.get("email") or "").strip().lower() == email:
            return str(user.get("user_id") or "")
    return ""


def _identifiable_conversions(base, email, order_ids, invoice_ids):
    """Conversion rows that genuinely name this subject.

    Two ways a row can: it carries their account id, or its provenance
    blob names a record already proven to be theirs
    (`{"source": "orders/ord-1"}`). Nothing is matched by visitor token --
    that token is anonymous by construction and joining it to a person
    here would perform exactly the de-anonymisation the analytics rules
    forbid, inside the feature meant to protect them.
    """
    rows = _rows(CONVERSIONS, base)
    if rows is None:
        return None

    user_id = _subject_user_id(base, email)
    sources = {f"orders/{oid}" for oid in order_ids}
    sources |= {f"invoices/{iid}" for iid in invoice_ids}

    matched = []
    for row in rows:
        row_user = str(row.get("user_id") or "").strip()
        if user_id and row_user == user_id:
            matched.append(row)
            continue
        try:
            metadata = json.loads(str(row.get("metadata") or "") or "{}")
        except (TypeError, ValueError):
            continue
        source = str((metadata or {}).get("source") or "").strip()
        if source and source in sources:
            matched.append(row)
    return matched


def POST(request):
    identity = (request or {}).get("_identity") or {}
    if "admin" not in (identity.get("roles") or []):
        # No self-service door on purpose -- see the module docstring.
        return {"status": 403,
                "error": "Only an operator may export a subject's data. "
                         "Identifying the person asking is a human judgement, "
                         "not an HTTP parameter."}

    email = str((request or {}).get("email") or "").strip().lower()
    if not email:
        return {"status": 400, "error": "email is required"}

    base = _base_dir()
    collections = {}
    counts = {}
    unavailable = []

    for collection, field in EMAIL_KEYED:
        rows = _rows(collection, base)
        if rows is None:
            unavailable.append(collection)
            continue
        matched = [row for row in rows if _matches(row, field, email)]
        collections[collection] = matched
        counts[collection] = len(matched)

    # Ids proven to belong to this subject. Everything below selects from
    # these and never from the email again, so a linked row can only be
    # reached through a record already established as theirs.
    owned = {name: {str(row.get("id") or "") for row in collections.get(name, [])}
             for name in ("orders", "invoices")}

    for collection, field, via in LINKED:
        rows = _rows(collection, base)
        if rows is None:
            unavailable.append(collection)
            continue
        ids = {value for value in owned.get(via, set()) if value}
        matched = [row for row in rows
                   if str(row.get(field) or "").strip() in ids]
        collections[collection] = matched
        counts[collection] = len(matched)

    conversions = _identifiable_conversions(
        base, email, owned.get("orders", set()), owned.get("invoices", set()))
    if conversions is None:
        unavailable.append(CONVERSIONS)
    else:
        collections[CONVERSIONS] = conversions
        counts[CONVERSIONS] = len(conversions)

    return {
        "status": 200,
        "ok": True,
        "email": email,
        "collections": collections,
        "counts": counts,
        "total": sum(counts.values()),
        # Which collections this box does not have at all, kept apart from
        # the ones it has and found nothing in: "we do not ship things" and
        # "you have no shipments" are different answers to a subject
        # request and only one of them is about them.
        "unavailable": sorted(set(unavailable)),
        "not_searchable": [dict(entry) for entry in NOT_SEARCHABLE],
        # Erasure is deliberately not offered here. See the docstring.
        "erasure": "Not available through this action. Business records have "
                   "a statutory retention period and the redaction design "
                   "that would honour an erasure request without breaking the "
                   "ledger has not been decided yet; write to the contact "
                   "address on /privacy.",
    }
