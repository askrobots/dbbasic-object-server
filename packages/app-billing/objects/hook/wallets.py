"""Pre-write hook for wallets: one code, one card.

A gift card is a BEARER instrument. Whoever holds the code can spend the
balance, and nothing else identifies it -- there is no account, no email
and usually no name. That makes the code the only key, and a key that is
not unique is not a key: two cards sharing one code means one customer
spending another's money, with no evidence afterwards about which balance
was meant. So this gate exists for exactly one reason, and refuses a
create or an update that would give a second wallet a code somebody else
already has.

Schemas in this repo carry no uniqueness constraint, which is why the
rule lives here rather than in wallets.json. That is the same division
hook_wallet_entries draws: the schema describes shape, a hook decides
what may be written.

**Compared upper-cased and trimmed**, because the code is compared that
way at redemption (object_promotions.normalize_code takes the identical
view of promotion codes, for the identical reason). A gate that matched
case-sensitively while the redeemer matched case-insensitively would let
`gc-abc` and `GC-ABC` both be created and then let either be spent as the
other -- the worst kind of near-miss, since both look fine in a list.

**A blank code is not a collision.** Ordinary wallets have no code and
there are many of them; uniqueness is a property of the codes that exist,
not a requirement that every wallet have one.

**Fails CLOSED when the collection cannot be read.** An unreadable wallet
list means unknown codes, and issuing a card that might duplicate a live
one is worse than refusing to issue it: a refused card is reissued in ten
seconds, a duplicated one is discovered when somebody's balance is gone.
Same direction hook_wallet_entries takes about an unreadable ledger.

Nothing else is gated here. The balance is the entries' business
(hook_wallet_entries), the kind is a label, and this hook deliberately
does not decide who may open a wallet -- that is permissions'.
"""

import os

import object_records


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _code(value):
    return _text(value).upper()


def BEFORE_WRITE(request):
    action = _text(request.get("action"))
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}

    code = _code(record.get("code"))
    if not code:
        return None                  # most wallets have none; that is fine

    mine = _text(record.get("id"))
    base = _base_dir()
    try:
        rows = object_records.read_collection_records("wallets", base_dir=base)
    except Exception:
        return {"error": ("The wallet list is unreadable, so this code cannot "
                          "be checked for a duplicate. Refusing rather than "
                          "issuing a card that might already exist."),
                "status": 409}

    for row in rows:
        if _text(row.get("id")) == mine:
            continue
        if _code(row.get("code")) == code:
            return {
                "error": (f"Another wallet already carries the code {code}. A "
                          f"gift card is spent by whoever holds its code, so "
                          f"two cards with one code is one customer spending "
                          f"another's money -- issue a different code."),
                "status": 409,
            }
    return None
