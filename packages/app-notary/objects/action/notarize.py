"""action_notarize -- lodge a digest, get back the moment it was first seen.

POST {digest, algorithm?, label?, source?}

The one write in this package, and the only object anywhere that may
append to `notarizations`.

## Resubmitting is a lookup, not an error

If the digest is already on the log, this returns THE EXISTING ROW with
its original timestamp and writes nothing. Not a 409, not a duplicate,
not a fresh stamp -- the same receipt the first submitter got.

That is the opposite of the usual reflex, and it is the load-bearing rule
of the whole service. A notarization claims "this existed BY then". A
later submission cannot make a thing earlier, so accepting one and
recording a new time would let anybody discard the only fact the log
carries by simply submitting again. Everywhere else in this building
idempotency exists to avoid a duplicate row (docs/logic-decisions.md #7);
here it exists to stop a timestamp being walked forward, which is the
only direction an attacker would ever want to walk it.

## What it refuses, and why each refusal is the product

**Content.** There is no field for it and no code path that would accept
it. A submitter hashes their own data and sends the digest. This is not a
limitation to be relaxed later: it is why notarizing something
confidential is safe, and why this collection is not worth stealing.

**Weak algorithms.** sha256 and sha512 only. A digest somebody can find a
collision for lets them notarize one document today and produce a
different one with the same hash tomorrow, which is precisely the attack
a notary exists to make hard. md5 and sha1 are refused rather than
unsupported, and the refusal says which.

**Anonymous submission, unless the operator switched it on.** Default
closed. `notary.public_submission = true` in app_settings opens it, which
is what a notary run as a service for other people needs and what a notary
run for one business's own ledger heads does not. The refusal names the
setting, in the shape every capability boundary here uses: absent by
default, a 409 saying exactly what to configure, never a stub that
pretends to have worked.

## What this object does NOT do

**It does not sign the receipt.** A signed receipt would let a submitter
prove what this server said even if this server later denied it. That is
a real strengthening; it needs a key, a rotation story and a published
public key, and it is in plan/notary-spec.md rather than here. Today the
answer is weaker but not nothing: the record is PUBLIC, so anyone can
check it at any moment, and a row that vanished would be visibly gone.

**It does not verify anything about the data.** It cannot. Every surface
renders object_notary.attestation() next to the answer so nobody reads
"notarized" as "checked", "owned" or "approved" -- overstating what this
means is the only way a service this simple can do harm.
"""

import os

import object_notary
import object_records

ACTOR = "action_notarize"
COLLECTION = "notarizations"
SETTINGS_COLLECTION = "app_settings"
PUBLIC_SUBMISSION_KEY = "notary.public_submission"

TRUE_VALUES = {"1", "true", "yes", "on"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _public_submission_allowed(base):
    """Whether a caller with no identity may lodge a digest.

    Read fresh from app_settings rather than cached, like every other
    setting read on this box (docs/logic-decisions.md #4). A missing or
    unreadable settings collection means NOT allowed, which is the safe
    direction: the failure mode of guessing wrong the other way is an
    open write endpoint on a public server.
    """
    try:
        rows = object_records.read_collection_records(
            SETTINGS_COLLECTION, base_dir=base)
    except Exception:
        return False
    for row in rows:
        if _text(row.get("key")) == PUBLIC_SUBMISSION_KEY:
            return _text(row.get("value")).lower() in TRUE_VALUES
    return False


def POST(request):
    request = request or {}
    base = _base_dir()

    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))

    if not user_id and not _public_submission_allowed(base):
        return {
            "status": 409,
            "error": (
                "This notary does not accept anonymous submissions. Sign in, "
                f"use a service key, or set {PUBLIC_SUBMISSION_KEY} to true "
                "in app_settings to run it as a service for other people."),
            "setting": PUBLIC_SUBMISSION_KEY,
        }

    algorithm = (object_notary.normalize_algorithm(request.get("algorithm"))
                 or _text(request.get("algorithm"))
                 or object_notary.DEFAULT_ALGORITHM)
    found = object_notary.problems(request.get("digest"), algorithm)
    if found:
        return {"status": 400, "error": " ".join(found), "problems": found}

    digest = object_notary.normalize_digest(request.get("digest"), algorithm)
    algorithm = object_notary.normalize_algorithm(algorithm)

    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        # The log cannot be read, so a first-seen row cannot be found, so a
        # new row would be a SECOND stamp on a digest that may already have
        # an earlier one -- exactly the thing this object exists to prevent.
        # Refusing is the only safe direction: a submission refused is
        # retried, a timestamp silently reset is gone.
        return {
            "status": 409,
            "error": ("The notarization log cannot be read, so this digest "
                      "cannot be lodged without risking a second, later "
                      "stamp on something already recorded."),
        }

    existing = object_notary.first_seen(digest, rows, algorithm)
    if existing is not None:
        receipt = object_notary.receipt(existing)
        receipt["already_recorded"] = True
        receipt["note"] = (
            "Already on the log. This is the ORIGINAL record and its original "
            "time -- resubmitting cannot move a timestamp forward, because "
            "the claim being made is that the data existed by then.")
        return receipt

    label = object_notary.normalize_label(request.get("label"))
    source = object_notary.normalize_label(request.get("source"))

    try:
        stored = object_records.create_collection_record(
            COLLECTION,
            {
                "digest": digest,
                "algorithm": algorithm,
                "label": label,
                "submitter": user_id or "public",
                "source": source or ACTOR,
            },
            base_dir=base,
            actor=user_id or ACTOR,
        )
    except Exception as error:
        return {"status": 500,
                "error": f"The digest could not be recorded: {error}"}

    receipt = object_notary.receipt(stored)
    receipt["already_recorded"] = False
    receipt["check_url"] = f"/notary/{digest}"
    return receipt
