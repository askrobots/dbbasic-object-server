"""site_kitchen -- what has to be made, and by when. GET /kitchen.

A READ, never a table -- the same doctrine as site_pick_list, and this
page is its sibling. There is no kitchen_queue collection and there must
never be one: the queue is orders-being-prepared folded live, exactly the
way the pick list is ordered-minus-shipped and stock levels are folded
from stock_moves. Storing it would mean two answers to "what is cooking"
that can disagree, and the stored one is always the one somebody trusts
at the wrong moment -- here, at the moment a customer is standing at the
counter.

**Sorted by PROMISED TIME, not grouped by product.** That is the whole
difference from the pick list, and it is the difference between a
warehouse and a kitchen. A picker walks the room once, so a pick list
groups by product and saves the walk. A cook is racing a clock somebody
else already promised, so the only order that matters is which promise
comes due next. Grouping a kitchen by product would batch six burgers
across four orders into one row and lose the one fact the cook is being
measured on.

**Late is the loudest thing on the page.** An order past its promised
time is not "due in -4 minutes", it is LATE, in the biggest type here,
because the customer for that one is already standing there. Sorting
alone would not do it: the late order sorts first anyway, and a queue
where the top row looks like every other row is a queue that gets read
top-down at the same speed whether or not somebody is waiting.

Big type, and print CSS, for the same reason the packing slip has both: a
cook reads this from a metre away with their hands full, and plenty of
kitchens will print the queue at the start of a rush rather than keep a
screen in the splash zone.

**Auto-refresh is a plain <meta refresh>, deliberately.** There is no
realtime-subscribe precedent to follow on any page in this box --
site_pick_list and site_stock, the two nearest siblings, both re-read on
load and nothing else -- so a live subscription here would be this
package inventing a pattern for itself, on the one screen where a
half-working one is most expensive. Thirty seconds is well inside the
resolution of the fact being shown (a promised time is a minute-grained
promise), it costs one small fold per tick, and it keeps working on a
tablet whose JavaScript has been asleep in a pocket. The day a page in
this repo does subscribe, this is one line to change.

Requires a signed-in identity, the same gate site_pick_list and
site_stock use, and the same SHAPE: a sign-in prompt, not a 403. Orders
are included when they belong to the signed-in user OR carry no owner at
all -- guest web checkout leaves owner_id blank by design, so filtering
strictly by owner would show an empty kitchen to the one person with food
to make.

Every pickup field is read with .get. `fulfillment_method` and
`promised_at` belong to the pickup slice, and this page must be
installable beside an app-orders that has not gained them yet: on such a
box no order claims to be a pickup, the queue is honestly empty, and the
page says which field is missing rather than looking broken.
"""

import html
import os
import urllib.parse
from datetime import datetime

import object_records

ACTOR = "site_kitchen"

# What a cook is asked to work on: an order that has been committed and is
# not yet ready to hand over. `draft` is not a commitment, `ready` is
# already made and waiting on the shelf, `collected` has left, and
# `cancelled` must never be cooked.
PREPARING_STATUSES = {"confirmed", "preparing"}

# The one fulfilment method this page is about. `shipping` and `delivery`
# have a warehouse and a van between them and the customer; `counter` is
# made and handed over in one motion with nobody waiting on a promise.
PICKUP_METHOD = "pickup"

# How often the page re-reads itself. See the module docstring.
REFRESH_SECONDS = 30


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _esc(value):
    return html.escape(_text(value))


def _now():
    return datetime.now()


def _parse_when(value):
    """A promised time as NAIVE local wall-clock, or None when it will not
    parse.

    Naive local, not UTC, because that is the convention the package that
    OWNS this field already set: app-pickup's slot generator writes
    pickup_slots.starts_at as the shop's own clock, checkout stamps it
    onto promised_at unchanged, and system_pickup_attention reads it back
    the same way. A kitchen that decided the same string meant UTC would
    show every order in a shop an hour off the meridian as late by the
    offset, on the one screen whose entire job is the clock.

    An offset-carrying stamp (hand-typed, or imported) is converted to
    local and flattened, rather than left aware: comparing an aware
    datetime to a naive one raises, and one badly-typed order must not
    take the whole queue down at the start of a rush.

    None is not zero and not "now": an order whose promised time cannot be
    read is not late and is not on time, it is unscheduled, and it sorts
    to the BOTTOM rather than screaming at the top of the queue. A blank
    or hand-typed value must never invent an emergency.
    """
    raw = _text(value)
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


def _whole_minutes(seconds):
    """Minutes elapsed, never minutes started.

    Floor of a positive magnitude, so a promise 90 seconds away is "1
    min" rather than the 2 that rounding up would claim, and one 90
    seconds overdue is "LATE by 1". Both directions are computed on the
    magnitude and the SIGN is carried separately: floor-dividing a
    negative number rounds away from zero, which would report an order
    seven minutes and one second late as eight -- a page that overstates
    lateness gets argued with, and a page that gets argued with stops
    being believed about the ones that matter.
    """
    return int(abs(seconds) // 60)


def _due_html(when, now):
    """The clock, in the states that matter.

    LATE is a different sentence rather than a negative number, because a
    reader scanning a column of "due in" values will read "-4" as "4" at
    speed, and the one row that must not be misread is that one.
    """
    if when is None:
        return ('<span class="due unscheduled">no promised time</span>',
                "unscheduled")
    seconds = (when - now).total_seconds()
    minutes = _whole_minutes(seconds)
    if seconds < 0 and minutes:
        return (f'<span class="due late">LATE by {minutes} min</span>',
                "late")
    if minutes == 0:
        # Inside the minute either way: a promise 40 seconds overdue is
        # not a crisis and 40 seconds early is not a wait, and both are
        # the same instruction to the cook.
        return ('<span class="due now">due now</span>', "now")
    return (f'<span class="due">in {minutes} min</span>', "soon")


def _clock(when):
    """The promised time itself, HH:MM, beside the countdown.

    Both, not one: the countdown is what a cook acts on and the wall-clock
    time is what they say out loud to the customer who rings up asking.
    """
    return when.strftime("%H:%M") if when is not None else ""


def _orders_for(base, user_id):
    """Every pickup order still being made, soonest promise first.

    A full scan of two collections, the same cost site_pick_list accepts
    and for the same reason: a shop with a kitchen this size has tens of
    open orders, not millions.
    """
    try:
        rows = object_records.read_collection_records("orders", base_dir=base)
    except Exception:
        return [], {}, False

    saw_method = False
    open_orders = {}
    for row in rows:
        if _text(row.get("fulfillment_method")):
            saw_method = True
        if _text(row.get("doc_type") or "sale") != "sale":
            continue
        if _text(row.get("status")) not in PREPARING_STATUSES:
            continue
        if _text(row.get("fulfillment_method")) != PICKUP_METHOD:
            continue
        if _text(row.get("owner_id")) not in ("", user_id):
            continue
        open_orders[row["id"]] = row

    try:
        all_lines = object_records.read_collection_records("order_lines",
                                                           base_dir=base)
    except Exception:
        all_lines = []
    lines = {}
    for line in all_lines:
        order_id = _text(line.get("order_id"))
        if order_id in open_orders:
            lines.setdefault(order_id, []).append(line)
    for rows_for_order in lines.values():
        rows_for_order.sort(key=lambda row: _text(row.get("description")))

    ordered = list(open_orders.values())
    # Soonest promise first; an order with no readable promised time sorts
    # last rather than first -- "we do not know when this was promised" is
    # not a claim to urgency, the same rule the pick list applies to a
    # missing order date. Sorted on the PARSED instant, not on the string:
    # two shops an ocean apart write the same moment with different
    # offsets, and a lexicographic sort would cook them in the wrong order
    # while looking entirely reasonable.
    ordered.sort(key=_sort_key)
    return ordered, lines, saw_method


_FOREVER = datetime.max


def _sort_key(order):
    when = _parse_when(order.get("promised_at"))
    return (when is None, when or _FOREVER,
            _text(order.get("number")) or order["id"])


_STYLE = """
.kitchen { font-size: 1.15rem; }
.ticket { border: 2px solid var(--line, #38384a); border-radius: 10px; padding: 0.9rem 1.1rem; margin: 0 0 1.1rem; }
.ticket.late { border-color: #c33; }
.ticket header { display: flex; flex-wrap: wrap; gap: 0.6rem 1.2rem; align-items: baseline; margin-bottom: 0.5rem; }
.ticket .clock { font-size: 2rem; font-weight: 700; line-height: 1; }
.ticket .who { font-size: 1.2rem; font-weight: 600; }
.ticket .ref { opacity: 0.6; font-size: 0.9rem; }
.due { font-size: 1.3rem; font-weight: 600; }
.due.late { color: #c33; font-size: 1.9rem; text-transform: uppercase; }
.due.now { font-weight: 700; }
.due.unscheduled { opacity: 0.6; font-weight: 400; font-size: 1rem; }
.klines { list-style: none; margin: 0.4rem 0 0; padding: 0; }
.klines li { padding: 0.25rem 0; border-top: 1px solid var(--line, #38384a); }
.klines .qty { font-weight: 700; display: inline-block; min-width: 2.2rem; }
.klines .line-note { display: block; margin-left: 2.2rem; font-style: italic; font-weight: 600; }
.hint { color: var(--muted, #999); font-size: 0.9rem; }
@media print {
  nav, header.app, .noprint, .btn { display: none !important; }
  body { background: #fff; color: #000; }
  .ticket { break-inside: avoid; border-color: #666; }
  .due.late { color: #000; }
}
"""


def _lines_html(lines):
    if not lines:
        return '<p class="hint">This order has no lines.</p>'
    items = []
    for line in lines:
        quantity = _text(line.get("quantity")) or "1"
        description = (_text(line.get("description"))
                       or _text(line.get("product_id")) or "Item")
        # The note is the reason this page renders lines at all rather than
        # a count -- read with .get so an order_lines that predates the
        # column still cooks.
        note = _text(line.get("line_note"))
        note_html = (f'<span class="line-note">{_esc(note)}</span>'
                     if note else "")
        items.append(f'<li><span class="qty">{_esc(quantity)}&times;</span>'
                     f'{_esc(description)}{note_html}</li>')
    return f'<ul class="klines">{"".join(items)}</ul>'


def _ticket_html(order, lines, now):
    when = _parse_when(order.get("promised_at"))
    due, state = _due_html(when, now)
    number = _text(order.get("number")) or order["id"]
    who = _text(order.get("customer_name")) or "No name on this order"
    clock = _clock(when)
    clock_html = f'<span class="clock">{_esc(clock)}</span>' if clock else ""
    return f"""
<article class="ticket {state}">
<header>
  {clock_html}
  {due}
  <span class="who">{_esc(who)}</span>
  <span class="ref">{_esc(number)}</span>
  <span class="ref noprint"><a href="/kitchen/{_esc(urllib.parse.quote(order['id'], safe=''))}/ticket">Ticket</a></span>
</header>
{_lines_html(lines)}
</article>"""


def GET(request):
    identity = request.get("_identity") or {}
    user_id = _text(identity.get("user_id"))

    if not user_id:
        body = ('<div class="pagehead"><h1>Kitchen</h1></div>'
                '<p class="hint"><a href="/login?next=/kitchen">Sign in</a> '
                'to see what is being made.</p>')
        refresh = ""
    else:
        base = _base_dir()
        now = _now()
        orders, lines, saw_method = _orders_for(base, user_id)
        if orders:
            queue = "".join(_ticket_html(order, lines.get(order["id"], []), now)
                            for order in orders)
        elif not saw_method:
            # The honest version of an empty page. A kitchen queue that is
            # blank because nothing is cooking and one that is blank
            # because no order on this box has ever said how it is being
            # fulfilled look identical, and only one of them is somebody's
            # bug.
            queue = ('<p class="hint">Nothing is being prepared. No order on '
                     'this server states a <code>fulfillment_method</code> '
                     'yet, so nothing can be a pickup: the queue is empty '
                     'because the shop has not taken a pickup order, not '
                     'because it is missing one.</p>')
        else:
            queue = '<p class="hint">Nothing is being prepared right now.</p>'

        body = f"""
<div class="breadcrumb noprint"><a href="/">Home</a> / Kitchen</div>
<div class="pagehead"><h1>Kitchen</h1></div>
<p class="hint noprint">Pickup orders being prepared, soonest promise
first. This is a read of the order records, never a stored queue, and it
re-reads itself every {REFRESH_SECONDS} seconds.</p>
<div class="kitchen">{queue}</div>
<p class="hint noprint">An order leaves this queue when it is marked
ready, which is also what tells the customer to come and get it.</p>
"""
        refresh = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'

    who = (f"signed in as <strong>{_esc(user_id)}</strong>" if user_id
           else '<a href="/login?next=/kitchen">sign in</a>')
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>Kitchen</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": page}
