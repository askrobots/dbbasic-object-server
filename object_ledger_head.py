"""object_ledger_head -- one digest that stands for a whole ledger, and
the verdict on whether that ledger still reproduces it.

Pure. No I/O, no clock, no data directory.

## The simplification this module is built on

plan/tamper-evidence-spec.md described a hash chain written into the
storage layer: every row carrying the hash of the one before it, computed
under the append lock. That is real work and it is not what shipped here,
because of an observation that makes most of it unnecessary:

**For an append-only log, a PREFIX DIGEST is a chain head.** A hash chain
is simply an incremental way of computing prefix digests cheaply as rows
arrive. If the digest is only wanted once a day, over a file that is being
read anyway, it can be computed directly in one pass -- no per-row column,
no storage-layer change, and no risk of two concurrent appends forking the
chain, which was the specific hazard that kept the package-layer version
off the table.

`head()` below is literally the chain fold, run over the rows in order. So
the value it produces is the value an incremental in-storage chain would
produce, and moving the computation into the append path later would NOT
invalidate a single anchor already published. That is the property that
makes shipping the cheap version now safe rather than a detour.

## What is hashed, and the two decisions that matter

**Logical rows, never file bytes.** Hashing the file would be simpler and
would break on the first compaction: compaction rewrites an append log --
folding updates, dropping deleted rows, rewriting the header -- so a
byte-level digest would report tampering every time routine maintenance
ran, and an integrity check that cries wolf on schedule is one nobody
reads by the third week. What is hashed is the folded rows the rest of
this system sees.

**The field list is part of the anchor, not read from today's schema.**
Anchoring records which fields were covered, and verification reads THOSE
fields out of today's rows. Without this, adding a field to a schema would
change every historical digest at once and look exactly like a break --
turning an ordinary migration into an integrity alarm, which again teaches
an operator to ignore the alarm. With it, a new field simply is not
covered by old anchors, which is true and harmless.

## What an anchor proves, and the honest limit

An anchor is `(row_count, digest)` at a moment. Verifying it says: the
first `row_count` rows of this ledger are still, byte for byte in their
canonical form, what they were when the anchor was taken.

That is a strong statement and it has an exact boundary: **it is only
worth what the anchor's storage is worth.** An anchor row sitting in the
same data directory as the ledger is defeated by anybody who can edit
both, which is everybody who could edit the ledger in the first place.
The value appears when the digest is lodged somewhere else -- with an
independent notary (app-notary), on another machine, in a mailbox at a
different provider. `system_publish_head` does the lodging; this module
does the arithmetic, and the arithmetic is the easy half.

## Locating a break

A single anchor says "something in the first N rows changed" and cannot
say which row -- naming the row needs a per-row hash, which is the
in-storage chain. But anchors accumulate: a daily pass leaves a ladder of
them at increasing row counts, and `locate()` walks that ladder to bracket
a break between the newest anchor that still verifies and the oldest that
does not. With daily anchoring that is a day-sized window on which rows
were touched, from a fold that stores one line a day.
"""

import hashlib

ALGORITHM = "sha256"

# Versioned and mixed into the genesis, so a future change to the
# canonical form is a DIFFERENT digest rather than a silently
# incompatible one. An anchor that verifies under a scheme it was not
# computed with would be the worst possible bug in this file.
SCHEME = "dbbasic-ledger-head-v1"


def canonical_row(row, fields):
    """One row as bytes, injectively.

    Length-prefixed rather than delimiter-joined, on purpose. A naive
    "\\t".join(values) is only injective while no value can contain a tab
    -- true of TSV storage today, and a coupling between the integrity
    digest and the storage format that nobody would remember when the
    coupling stopped holding. Two rows that differ must produce different
    bytes under every possible content, or an attacker gets to move
    characters across a field boundary without changing the digest.

    A field the row does not have reads as empty, which is what makes an
    anchor survive a schema gaining a column.
    """
    parts = []
    for field in fields:
        value = row.get(field)
        text = "" if value is None else str(value)
        encoded = text.encode("utf-8")
        parts.append(str(len(encoded)).encode("ascii"))
        parts.append(b":")
        parts.append(encoded)
        parts.append(b"|")
    return b"".join(parts)


def genesis(collection, fields):
    """The starting value of the fold.

    Binds the digest to the collection name and the exact field list, so
    an anchor taken over `wallet_entries` cannot be checked against
    `payments` and come out clean, and so a field list that differs by one
    column is a different chain rather than a coincidence.
    """
    seed = "\n".join([SCHEME, str(collection), ",".join(fields)])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def link(previous, row, fields):
    """One step of the chain: the new head after appending `row`.

    Exposed separately from `head()` because it is the function an
    in-storage chain would call per append. Whatever computes it, the
    value is the same -- which is what lets the expensive version replace
    the cheap one later without orphaning anchors already published.
    """
    digest = hashlib.sha256()
    digest.update(str(previous).encode("ascii"))
    digest.update(b"\n")
    digest.update(canonical_row(row, fields))
    return digest.hexdigest()


def head(rows, fields, *, collection="", count=None):
    """The head over the first `count` rows (all of them by default).

    Returns the count actually covered alongside the digest, because a
    digest without its row count is unverifiable: the whole verification
    is "recompute over the same prefix", and the prefix has to be named.
    """
    fields = list(fields)
    rows = list(rows)
    if count is None:
        count = len(rows)
    count = max(0, min(int(count), len(rows)))

    value = genesis(collection, fields)
    for row in rows[:count]:
        value = link(value, row, fields)
    return {
        "collection": str(collection),
        "algorithm": ALGORITHM,
        "scheme": SCHEME,
        "fields": list(fields),
        "row_count": count,
        "digest": value,
    }


def verify(rows, anchor):
    """Does this ledger still reproduce that anchor?

    Reads the field list and the row count OUT OF THE ANCHOR rather than
    from today's schema, which is what stops an ordinary migration
    presenting as tampering.

    Three outcomes, and the middle one is the interesting one:

    * `verified` -- the anchored prefix is unchanged.
    * `truncated` -- the ledger now has FEWER rows than the anchor
      covered. Rows have been removed, which in an append-only ledger is
      not something ordinary operation does. Reported separately from a
      content mismatch because the remedy is different: a mismatch means
      find what changed, a truncation means find what is missing.
    * `mismatch` -- the prefix is the right length and hashes differently.
      Something inside it was edited.

    Retention deliberately produces `truncated`, and that is correct
    rather than a false alarm: pruning a ledger really does destroy the
    evidence an anchor was taken over. A collection that is both anchored
    and pruned is a configuration mistake, and this is where it surfaces.
    """
    fields = list(anchor.get("fields") or [])
    collection = str(anchor.get("collection") or "")
    expected = str(anchor.get("digest") or "")
    try:
        anchored_rows = int(anchor.get("row_count") or 0)
    except (TypeError, ValueError):
        anchored_rows = 0

    rows = list(rows)
    if not expected or not fields:
        return {"status": "unusable", "verified": False,
                "detail": "The anchor carries no digest or no field list, so "
                          "there is nothing to check it against."}

    if len(rows) < anchored_rows:
        return {
            "status": "truncated",
            "verified": False,
            "anchored_rows": anchored_rows,
            "present_rows": len(rows),
            "detail": (f"The anchor covered {anchored_rows:,} rows and the "
                       f"ledger now holds {len(rows):,}. Rows an anchor was "
                       f"taken over have been removed."),
        }

    recomputed = head(rows, fields, collection=collection, count=anchored_rows)
    if recomputed["digest"] == expected:
        return {
            "status": "verified",
            "verified": True,
            "anchored_rows": anchored_rows,
            "present_rows": len(rows),
            "detail": (f"The first {anchored_rows:,} rows are unchanged since "
                       f"this anchor was taken."),
        }
    return {
        "status": "mismatch",
        "verified": False,
        "anchored_rows": anchored_rows,
        "present_rows": len(rows),
        "expected": expected,
        "actual": recomputed["digest"],
        "detail": (f"The first {anchored_rows:,} rows no longer hash to what "
                   f"was anchored. Something inside them was changed after "
                   f"the fact."),
    }


def locate(rows, anchors):
    """Bracket a break between the anchors that pass and the ones that fail.

    One anchor can only say "something in the first N rows changed". A
    LADDER of anchors -- which is what a daily pass leaves behind -- says
    more: the newest anchor that still verifies puts a floor under the
    break, and the oldest that fails puts a ceiling on it. With daily
    anchoring that brackets an edit to the rows added on one particular
    day, which is usually enough to find it by hand.

    Naming the exact row needs a per-row hash, and that is the in-storage
    chain this module deliberately did not build. Reporting the window is
    the honest thing a prefix digest can do, and it costs one line a day.
    """
    checked = []
    for anchor in anchors or ():
        result = verify(rows, anchor)
        checked.append({
            "anchor": anchor,
            "row_count": result.get("anchored_rows", 0),
            "status": result["status"],
            "verified": result["verified"],
        })
    checked.sort(key=lambda entry: entry["row_count"])

    good = [entry for entry in checked if entry["verified"]]
    bad = [entry for entry in checked if not entry["verified"]]
    if not bad:
        return {"broken": False, "checked": len(checked),
                "detail": ("Every anchor verifies." if checked else
                           "No anchors have been taken, so there is nothing "
                           "to verify against.")}

    floor = max((entry["row_count"] for entry in good), default=0)
    ceiling = min(entry["row_count"] for entry in bad)
    return {
        "broken": True,
        "checked": len(checked),
        "failed": len(bad),
        "first_bad_row_count": ceiling,
        "last_good_row_count": floor,
        "detail": (f"A change lies between row {floor + 1:,} and row "
                   f"{ceiling:,}. The anchor at {floor:,} rows still "
                   f"verifies; the one at {ceiling:,} does not."
                   if good else
                   f"Every anchor fails, including the earliest at "
                   f"{ceiling:,} rows. Either the change is very old or the "
                   f"field list has moved under them."),
    }
