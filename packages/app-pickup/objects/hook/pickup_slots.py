"""Pre-write hook for pickup_slots: a window cannot hold minus one order,
and it cannot already hold more than it can.

Two gates, both of them things the schema has no way to say. `capacity`
and `orders_taken` are ordinary integers as far as validation is
concerned, and integers are perfectly happy to be -3 or to be four in a
window that holds two.

**Capacity may not be negative.** Zero is legal and means something
useful -- "this window exists and is full", which is a different sentence
from is_open=false's "we are not open then". Minus one is not a smaller
version of that; it is a number no surface can act on, and the picker
that reads it would quietly decide the window has places left.

**orders_taken may not exceed capacity, and the refusal carries both
numbers.** A gate that only says "no" leaves whoever is fixing the slot
guessing which of the two figures is wrong -- exactly the objection
hook_shipment_lines' three-number refusal answers on the shipping side.

**The race this does NOT stop, and must not be read as stopping.** Two
shoppers can pass action_checkout's capacity gate for the last place in
one window; the second increment then pushes orders_taken past capacity
for real. Checkout writes through object_records directly, which is a
trusted server-side path and bypasses hooks by design
(docs/validation-and-logic.md), so that write lands. THAT IS DELIBERATE
AND IT IS THE POINT: the oversell is a fact about what happened, and the
shop needs to see it on the row it happened to so somebody can ring the
customer. What this hook refuses is a NUMBER SOMEBODY TYPED -- an
operator editing a slot in a form, or an API client posting one -- and a
typo is not a fact. The same split hook_shipment_lines and
action_create_shipment already carry, named in both files so neither is
removed on the assumption the other covers it.
"""


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=None):
    """The integer a field holds, or `default` when it holds nothing and
    None when it holds something that is not one.

    None means "schema validation owns this error": a hook that invented
    its own message for a non-integer would produce two different
    refusals for one mistake depending on which gate saw it first.
    """
    text = _text(value)
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return None


def BEFORE_WRITE(request):
    action = _text(request.get("action"))
    if action not in ("create", "update"):
        return None
    record = request.get("record") or {}

    capacity = _int(record.get("capacity"), 0)
    if capacity is None:
        return None
    if capacity < 0:
        return {"error": (f"A slot's capacity cannot be negative (got "
                          f"{capacity}). Zero is the way to say this window "
                          f"is full, and is_open=false is the way to say the "
                          f"shop is shut then -- a negative capacity is "
                          f"neither, and every surface that reads it would "
                          f"have to guess which one was meant."),
                "status": 400}

    taken = _int(record.get("orders_taken"), 0)
    if taken is None:
        return None
    if taken < 0:
        return {"error": (f"A slot cannot have taken a negative number of "
                          f"orders (got {taken}). Nothing booked is what zero "
                          f"means; a correction is a new number, never a "
                          f"negative one."),
                "status": 400}

    if taken > capacity:
        starts_at = _text(record.get("starts_at"))
        when = f" starting {starts_at}" if starts_at else ""
        return {"error": (f"That slot{when} would hold more orders than it "
                          f"can: capacity {capacity}, orders taken {taken}. "
                          f"Raise the capacity if the shop really can serve "
                          f"{taken} in that window -- do not raise the count, "
                          f"which is a record of orders that exist."),
                "status": 409}

    return None
