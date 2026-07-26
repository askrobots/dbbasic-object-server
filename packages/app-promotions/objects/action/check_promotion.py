"""action_check_promotion -- does this code work on this basket, and if
not, why not (all of the whys).

POST {code, subtotal_cents?, shipping_cents?, customer_email?, today?}

The basket page's "apply code" button. It QUOTES and it reserves nothing:
no redemption row is written, no counter moves, and two shoppers may both
be told a one-use code will work. That is deliberate and it is the same
posture action_checkout takes about stock and app-pickup takes about
collection slots -- the authoritative decision is made once, at checkout,
against the real basket, by the same fold this object calls. A quote that
wrote a redemption would burn a code every time somebody typed one to see
what it did.

**The numbers come from the CALLER and are therefore advisory.** This is
a public object serving guest checkouts, so it has no session and no
basket; a shopper who claims a $500 subtotal can talk this object into
saying a $100-minimum code applies. Nothing is lost by that: checkout
resolves the same code against the basket it can actually read, and
refuses with the same sentences. Stating it plainly here beats somebody
later "hardening" a quote into a gate it was never asked to be.

**Every reason at once**, from object_promotions.blockers -- expired,
not started, below minimum, exhausted, already used by this customer,
inactive, unstackable. A page that reveals one problem per attempt is a
page people stop typing into.

Public on the OBJECT, never on the collection. What comes back is bounded
to what a shop would print on the offer itself: whether it works, what it
takes off, and why not. There is no way to list codes from here, which is
what stops this becoming an enumeration oracle for every unpublished
campaign the shop is holding back.
"""

import os
from datetime import date

import object_promotions
import object_records

ACTOR = "action_check_promotion"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def POST(request):
    base = _base_dir()
    code = object_promotions.normalize_code(request.get("code"))
    if not code:
        return {"status": 400, "error": "A promotion code is required."}

    try:
        promotions = object_records.read_collection_records("promotions",
                                                            base_dir=base)
    except Exception:
        # No promotions app on this box. An honest "that code does not
        # work here" beats a 500, and beats pretending it did.
        return {"status": 404, "code": code,
                "error": f"There is no promotion with the code '{code}'.",
                "problems": [f"There is no promotion with the code '{code}'."]}

    try:
        redemptions = object_records.read_collection_records(
            "promotion_redemptions", base_dir=base)
    except Exception:
        # Unreadable redemption log means the limits cannot be counted, and
        # a limit that cannot be counted has not been met -- refusing is the
        # safe direction, because a code handed out once too often is money
        # and a code refused once is a support email.
        return {"status": 409, "code": code,
                "error": ("The redemption log cannot be read, so this code "
                          "cannot be checked right now."),
                "problems": ["The redemption log cannot be read, so this code "
                             "cannot be checked right now."]}

    today = _text(request.get("today")) or date.today().isoformat()
    subtotal = max(0, _int(request.get("subtotal_cents")))
    shipping = max(0, _int(request.get("shipping_cents")))
    email = _text(request.get("customer_email"))

    promo = object_promotions.resolve(code, promotions, on_date=today)
    problems = object_promotions.blockers(
        promo, subtotal, email, redemptions, on_date=today, code=code)
    if problems:
        return {"status": 409, "code": code,
                "error": " ".join(problems), "problems": problems,
                "applies": False}

    discount = object_promotions.discount_for(subtotal, shipping, promo)
    return {"ok": True, "applies": True, "code": code,
            "kind": _text(promo.get("kind")),
            "applies_to": object_promotions.applies_to(promo),
            "discount_cents": discount,
            "note": ("a quote, not a reservation -- the code is applied for "
                     "real at checkout, against the basket the server can "
                     "see")}
