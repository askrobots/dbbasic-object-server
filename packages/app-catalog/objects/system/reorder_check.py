"""system_reorder_check -- which shelves are low, said once a night.

POST {today?, limit?} -- the scheduled pass declared in this package's
`schedules`. It folds the stock ledger, compares each product against its
own reorder_point, and writes a SUGGESTION for the ones at or below it.

**It suggests. It does not order.** That is the entire design and it is
worth being blunt about, because every inventory system eventually gets
asked to "just raise the PO automatically". The fold can see the shelf.
It cannot see the supplier's minimum, the lead time, the case size, the
promotion that is about to triple demand, the line somebody has already
decided to run down, or how much cash is in the bank this week. A machine
that orders on its own is how a business ends up with forty pallets of
the wrong thing, and the honest division of labour is that the server
NOTICES and the person DECIDES. Nothing here writes a purchase order,
touches a supplier, or moves stock.

**At or below, not merely below.** "Reorder at 5" means five is the
moment. A shop that had to fall to four before anyone was told has been
given a threshold that means something other than what it says.

**Zero means off.** A product with no reorder_point is not in this pass at
all -- not "reorder when it hits zero". Most catalogues have a handful of
lines anybody actually reorders, and a queue that lit up for every
made-to-order item and every service would be a queue nobody reads.

**One open suggestion per product.** A nightly pass that appended a row
every night would produce ninety copies of one fact within a quarter,
which is the classic way a good signal becomes noise. The open row is
REFRESHED instead -- today's on-hand, today's date -- so the number in
front of a human is current, and `ordered` or `dismissed` rows are left
exactly as the human left them. Re-running the pass twice in one day is
therefore a no-op, which is what makes it safe for the scheduler to
retry (docs/logic-decisions.md #7).

**Time-driven work belongs to a daemon pass** (#2). Nothing writes when a
shelf becomes low -- a sale writes a stock move, and noticing that the
move crossed a line is a different question asked at a different time --
so a reaction could not see this, and a page computing it live would ask
every visitor to pay for a fold over the whole ledger.

Degrades to a zero-work report when products or stock_moves cannot be
read, rather than raising: a pass should not log an error every night
about an app nobody installed.
"""

import os
from datetime import date

import object_ids
import object_records
import object_stock

ACTOR = "system_reorder_check"

DEFAULT_LIMIT = 500

OPEN = "open"


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _number(value):
    try:
        return float(_text(value) or 0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value, default=True):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "on")


def _rows(collection, base):
    try:
        return object_records.read_collection_records(collection, base_dir=base)
    except Exception:
        return None


def below_the_point(products, on_hand):
    """The pure half: which products are at or below their own point.

    Kept as a plain function over two dicts so the rule can be read and
    tested without a data directory -- the same posture object_cart and
    object_promotions take about the arithmetic they own.

    Returns a list of {product_id, on_hand, reorder_point,
    suggested_quantity}, in a stable order so two runs produce the same
    report.
    """
    low = []
    for product in products:
        point = _int(product.get("reorder_point"))
        if point <= 0:
            continue                      # 0 means off, not "reorder at zero"
        if not _truthy(product.get("is_active")):
            continue                      # nobody reorders what is not for sale
        level = _number(on_hand.get(_text(product.get("id")), 0))
        if level > point:
            continue                      # at or below, not merely below
        low.append({
            "product_id": _text(product.get("id")),
            "on_hand": level,
            "reorder_point": point,
            "suggested_quantity": _int(product.get("reorder_quantity")),
            "owner_id": _text(product.get("owner_id")),
        })
    low.sort(key=lambda row: (row["on_hand"] - row["reorder_point"],
                              row["product_id"]))
    return low


def _quantity_text(value):
    """A level as the row should read it: 4 rather than 4.0."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def POST(request):
    base = _base_dir()
    products = _rows("products", base)
    if products is None:
        return {"ok": True, "skipped": "no catalogue on this server",
                "suggested": 0}
    existing = _rows("reorder_suggestions", base)
    if existing is None:
        return {"ok": True,
                "skipped": "reorder_suggestions is not installed",
                "suggested": 0}

    today = _text(request.get("today")) or date.today().isoformat()
    limit = max(1, _int(request.get("limit"), DEFAULT_LIMIT))

    tracked = [row for row in products if _int(row.get("reorder_point")) > 0]
    on_hand = {}
    for product in tracked:
        product_id = _text(product.get("id"))
        try:
            on_hand[product_id] = object_stock.total_quantity(product_id,
                                                              base_dir=base)
        except Exception:
            # A product whose level cannot be folded is left alone rather
            # than reported as empty: "we cannot tell" is not "buy more".
            continue
    low = [row for row in below_the_point(tracked, on_hand)
           if row["product_id"] in on_hand][:limit]

    open_by_product = {}
    for row in existing:
        if _text(row.get("status")) != OPEN:
            continue
        open_by_product.setdefault(_text(row.get("product_id")), row)

    created, refreshed = [], []
    for row in low:
        patch = {
            "on_hand": _quantity_text(row["on_hand"]),
            "reorder_point": str(row["reorder_point"]),
            "suggested_quantity": str(row["suggested_quantity"]),
            "suggested_on": today,
        }
        current = open_by_product.get(row["product_id"])
        if current is not None:
            # Refresh rather than append: ninety copies of one fact is a
            # queue nobody reads, and a stale number is worse than none.
            try:
                object_records.update_collection_record(
                    "reorder_suggestions", current["id"], patch,
                    base_dir=base, actor=ACTOR)
                refreshed.append(current["id"])
            except Exception:
                continue
            continue
        suggestion_id = object_ids.new_uuid4()
        try:
            object_records.create_collection_record(
                "reorder_suggestions",
                {"id": suggestion_id, "product_id": row["product_id"],
                 "status": OPEN, "owner_id": row["owner_id"], **patch},
                base_dir=base, actor=ACTOR)
        except Exception:
            continue
        created.append(suggestion_id)

    return {
        "ok": True,
        "checked": len(tracked),
        "low": len(low),
        "suggested": len(created),
        "refreshed": len(refreshed),
        "ordered": 0,
        "note": ("suggestions only -- this pass never raises a purchase "
                 "order, because the shelf is the only thing it can see"),
    }
