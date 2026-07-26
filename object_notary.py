"""object_notary -- store a digest, check it later.

Pure. No I/O, no clock, no data directory. The package in
packages/app-notary calls this; the tests exercise it without either.

## What this is for

Every tamper-evidence scheme on this box eventually runs into the same
wall: a hash chain that lives inside the file it protects, and is only
ever checked by the machine that wrote it, defends against accident and
casual editing but not against anybody who actually wanted to. Recomputing
a chain is a couple of seconds' work. What makes it expensive is recording
the head somewhere the attacker cannot reach.

This is that somewhere. A notarization is an assertion by an independent
party that a particular digest existed no later than a particular moment.
It is deliberately the dumbest possible thing -- a list of (digest, when),
never edited, held by someone other than the person whose records the
digest describes -- because the security property is INDEPENDENCE, not
cleverness. A dumb store run by a third party is a stronger guarantee than
elaborate cryptography run by the party with the motive.

## The three refusals, which are the design

**It never stores content.** Only digests. A notary that holds your
documents is a breach liability and a subpoena target; one that holds
thirty-two bytes is neither. It is also what lets a submitter prove
something existed without revealing what it was.

**Nothing is ever amended or removed.** There is no update and no delete,
not as an oversight but as the product: a notary with an edit path is a
notary whose operator can be leaned on. `notarizations` is append storage,
the permission rules grant neither, and no object here writes one twice.

**It attests EXISTENCE and nothing else.** Not authorship, not ownership,
not meaning, not that the digest is of what the submitter says it is.
`attestation()` below is the exact wording, and every surface renders it,
because the difference between "this digest existed on the 26th of July"
and "Dan owned this file on the 26th of July" is the difference between a
useful service and a misleading one.

## Idempotency runs BACKWARDS from the usual rule

Everywhere else in this building a repeated write is suppressed to avoid a
duplicate (docs/logic-decisions.md #7). Here the same suppression exists
for a different and stronger reason: the claim being made is "this existed
BY then", and a later submission cannot make a thing earlier. So
`first_seen` returns the EARLIEST row for a digest and the surfaces return
that row for every subsequent submission -- resubmitting is not an error
and is not a new record, it is a lookup that happens to be spelled as a
write. Nobody can walk a timestamp forward by submitting again, which is
the only direction an attacker would want to walk it.

## What is deliberately NOT here

**No signature over the receipt.** A signed receipt would let a submitter
prove what the notary said even if the notary later denied it, which is a
real strengthening and needs a key, a rotation story and a published
public key -- see plan/notary-spec.md. Today's answer is that the record
is public, so anybody can check it at any time, and a notary that removed
a row would be visibly missing it.

**No chain over the notary's own log.** It belongs there and it is not
here yet, because doing it correctly means computing each link under the
same lock that appends the row -- a storage-layer change, not a package
one. Writing it at the package layer would read the tail, then append
without the lock, and two submissions landing together would fork the
chain. A chain that forks under ordinary load is worse than no chain,
because it spends its credibility on false alarms.

**No Merkle batching.** One published root covering a million submissions
is the right answer at volume and pure overhead below it.
"""

import hashlib
import urllib.parse

# Hosts that are always this machine, whatever anybody configured.
LOOPBACK_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "ip6-localhost",
})


def is_self_endpoint(endpoint, own_base_url="", *, allow_loopback=False):
    """Is this "independent" notary actually the same server?

    The hole this closes is small to describe and would undo the whole
    honesty design: an operator can point `notary.endpoints` at their own
    box, every lodgement succeeds, and the integrity page swaps "anchored,
    but only here" for "verified, and held elsewhere" -- a reassuring
    sentence about a digest that is still held by exactly one party, the
    one with the motive. The banner would then be worse than no banner,
    because it would be a specific false claim rather than a missing one.

    Detection is BEST EFFORT and cannot be otherwise: a determined
    operator can put their own server behind another hostname, and no
    check from inside the process can see through that. What it does catch
    is the two cases that happen by accident and by well-meaning
    misunderstanding -- a loopback address, and the server's own
    configured public URL. Those are the ways somebody self-anchors while
    believing they have done the right thing, which is the population this
    guard exists for.

    `allow_loopback` exists for tests and local development, where a
    notary on 127.0.0.1 is the only notary there is. Callers gate it on an
    ENVIRONMENT VARIABLE rather than a setting, deliberately: an operator
    clicking through a settings page must not be able to switch off the
    check that stops them lying to themselves, while somebody running the
    process for a test obviously can. Note that a second process on the
    same machine is not independent in any sense that matters -- same
    disk, same root, same backup -- so this really is a development
    affordance and not a supported deployment.
    """
    host = _host_of(endpoint)
    if not host:
        return False
    if host in LOOPBACK_HOSTS or host.startswith("127."):
        return not allow_loopback
    own = _host_of(own_base_url)
    return bool(own) and host == own


def _host_of(url):
    text = str(url if url is not None else "").strip().lower()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        return (urllib.parse.urlsplit(text).hostname or "").strip()
    except ValueError:
        return ""


# Digest algorithms this notary will accept, and the hex length each one
# produces. Restricted rather than open: accepting an arbitrary algorithm
# name means storing a claim nobody can check, and accepting a short one
# (md5, sha1) means storing a claim somebody can forge a collision against
# -- which is precisely the attack a notary exists to prevent.
ALGORITHMS = {
    "sha256": 64,
    "sha512": 128,
}

DEFAULT_ALGORITHM = "sha256"

# Labels are the submitter's own note about what a digest was -- "payroll
# ledger head, 26 July", "contract with Acme, signed copy". Free text,
# capped, and NEVER required: a submitter who does not want to say what a
# digest is has an excellent reason not to, and the whole point of storing
# only a hash is that they do not have to.
MAX_LABEL = 200

_HEX = set("0123456789abcdef")


def normalize_algorithm(value):
    """The algorithm name, lower-cased and trimmed, or "" if unsupported."""
    name = str(value if value is not None else "").strip().lower()
    return name if name in ALGORITHMS else ""


def normalize_digest(value, algorithm=DEFAULT_ALGORITHM):
    """A digest in canonical form, or "" if it is not one.

    Lower-cased, trimmed, and checked against the exact hex length the
    named algorithm produces. Case is normalised rather than rejected
    because a hex digest is the same value in either case and losing a
    lookup to a capital letter is not a policy anybody chose -- the same
    reasoning object_promotions applies to a code typed off a postcard.

    A leading "0x" or a "sha256:" prefix is stripped, because both are
    ordinary ways for a tool to hand one over and neither changes the
    value.
    """
    text = str(value if value is not None else "").strip().lower()
    for prefix in ("0x", "sha256:", "sha512:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.strip()
    expected = ALGORITHMS.get(normalize_algorithm(algorithm) or algorithm)
    if expected is None:
        return ""
    if len(text) != expected:
        return ""
    if not text or not set(text) <= _HEX:
        return ""
    return text


def normalize_label(value):
    """The submitter's note, trimmed to MAX_LABEL, tabs and newlines gone.

    Tabs would break the TSV row this becomes and newlines would break the
    line, so they are collapsed to spaces rather than rejected: a label is
    a courtesy field and refusing a submission over whitespace would lose
    a notarization to a formatting detail.
    """
    text = str(value if value is not None else "")
    text = text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:MAX_LABEL]


def digest_of(data, algorithm=DEFAULT_ALGORITHM):
    """The digest of some bytes, for callers that hold the content.

    Present so a caller anchoring its own ledger head does not reach for
    hashlib and pick a different algorithm by accident. Note that a
    submitter with anything confidential should hash it themselves and
    send only the result -- this function existing does not mean content
    should travel to the notary, and it never does from any surface here.
    """
    name = normalize_algorithm(algorithm)
    if not name:
        raise ValueError(f"Unsupported digest algorithm: {algorithm!r}")
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.new(name, data).hexdigest()


def problems(digest, algorithm=DEFAULT_ALGORITHM):
    """Every reason this submission cannot be accepted, at once.

    A list rather than a first failure, and each entry names the actual
    constraint -- "a sha256 digest is 64 hex characters; that one is 40"
    rather than "invalid digest", because the commonest cause of the
    second sentence is somebody having hashed with sha1 and having no way
    to find that out from the refusal.
    """
    found = []
    name = normalize_algorithm(algorithm)
    if not name:
        supported = ", ".join(sorted(ALGORITHMS))
        found.append(
            f"'{str(algorithm).strip()}' is not an algorithm this notary "
            f"accepts. Supported: {supported}. Shorter digests (md5, sha1) "
            f"are refused rather than unsupported: a notary whose digests "
            f"can be collided proves nothing.")
        return found

    raw = str(digest if digest is not None else "").strip()
    if not raw:
        found.append("A digest is required. Hash the thing you want to "
                     "notarize and send the hex digest -- never the thing "
                     "itself.")
        return found

    if not normalize_digest(raw, name):
        expected = ALGORITHMS[name]
        cleaned = raw.lower()
        for prefix in ("0x", "sha256:", "sha512:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        if set(cleaned) - _HEX:
            found.append(f"That is not hex. A {name} digest is {expected} "
                         f"characters of 0-9 and a-f.")
        else:
            found.append(f"A {name} digest is {expected} hex characters; "
                         f"that one is {len(cleaned)}.")
    return found


def first_seen(digest, rows, algorithm=DEFAULT_ALGORITHM):
    """The EARLIEST notarization of this digest, or None.

    Earliest, not latest, and this is the whole idempotency rule: the
    claim a notarization makes is "this existed by then", so a second
    submission cannot improve on the first and must not be allowed to
    replace it. Anybody able to walk their own timestamp forward by
    resubmitting could quietly discard the only fact the record carries.

    Ties on created_at resolve to the row that appears first in the log,
    which in an append collection is the row that was written first.
    """
    target = normalize_digest(digest, algorithm)
    if not target:
        return None
    name = normalize_algorithm(algorithm) or DEFAULT_ALGORITHM
    best = None
    best_stamp = None
    for row in rows or ():
        if str(row.get("digest") or "").strip().lower() != target:
            continue
        row_algorithm = (str(row.get("algorithm") or "").strip().lower()
                         or DEFAULT_ALGORITHM)
        if row_algorithm != name:
            continue
        stamp = str(row.get("created_at") or "")
        if best is None or (stamp and best_stamp and stamp < best_stamp):
            best, best_stamp = row, stamp
    return best


def attestation(row=None):
    """Exactly what a notarization claims, and exactly what it does not.

    One definition, rendered by every surface -- the submission response,
    the check page and the JSON -- so they cannot drift into three
    different strengths of the same promise. Overstating this is the only
    way a service this simple can do harm: a reader who takes "notarized"
    to mean "verified", "owned" or "approved" has been misled by the word
    rather than by anything the record says.
    """
    when = str((row or {}).get("created_at") or "")
    subject = f"was recorded here at {when}" if when else "is recorded here"
    return {
        "proves": [
            f"This exact digest {subject}, and therefore the data it was "
            f"computed from existed no later than that moment.",
            "The record is public and append-only: it can be checked by "
            "anyone, at any time, without an account.",
        ],
        "does_not_prove": [
            "WHO created the data. Anybody can notarize anybody's digest.",
            "That the submitter owned it, wrote it, or had any right to it.",
            "That the data did not also exist much earlier. This is an "
            "upper bound on its age and nothing more.",
            "Anything about the CONTENT. This notary never saw it and "
            "cannot say what the digest is of.",
        ],
        "rests_on": [
            "This notary's clock being right and its operator being honest. "
            "Trusting one notary is trusting one party -- which is a large "
            "improvement on trusting yourself, and is not the end of the "
            "argument. Anchor a digest with several independent notaries "
            "and forging it means compromising all of them.",
        ],
    }


def receipt(row, *, found=True):
    """The submission and lookup answer, in one shape.

    Both surfaces return this so a submitter's receipt and a checker's
    answer are the same object -- if they could differ, the interesting
    case is the one where they do, and nobody would be watching it.
    """
    if not found or not row:
        return {
            "found": False,
            "attestation": {
                "proves": [],
                "does_not_prove": [
                    "That the data did not exist. A digest that was never "
                    "submitted is simply unknown here; absence from this "
                    "log is not evidence of anything.",
                ],
                "rests_on": [],
            },
        }
    return {
        "found": True,
        "digest": str(row.get("digest") or ""),
        "algorithm": str(row.get("algorithm") or DEFAULT_ALGORITHM),
        "first_seen_at": str(row.get("created_at") or ""),
        "label": str(row.get("label") or ""),
        "attestation": attestation(row),
    }
