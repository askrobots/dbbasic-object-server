"""What the goods cost: the valuation COGS-on-sale was blocked on.

The journal shape was never the hard part. `DR cost of goods sold, CR
inventory` is two lines. The blocker was knowing what number to put on
them, and system_stock_books said so in its own docstring for as long as
it declined to compose sales: "COGS-on-sale needs a real valuation method
(FIFO/weighted average)".

**Weighted average**, and it is a decision. FIFO is more precise and is
right for a lot-tracked warehouse, but it needs cost LAYERS -- which
units, from which receipt, are still on the shelf -- and stock_moves has
no lot dimension at all. Adding one to satisfy a journal entry would be a
storage change driven by a report. LIFO is not permitted under IFRS.
Weighted average needs nothing the append log does not already carry.

MOVING average specifically, folded in move order. The distinction is not
academic and the first test below is the proof: a lifetime "total spent
over total ever bought" average silently reprices goods you no longer
own.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import object_stock
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_OBJECTS = REPO_ROOT / "packages" / "app-catalog" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def move(quantity, *, cost=None, reason="purchase", into="shelf",
         out_of="", when="2026-01-01", product="p1"):
    return {"product_id": product, "quantity": str(quantity),
            "unit_cost_cents": "" if cost is None else str(cost),
            "reason": reason, "to_location_id": into,
            "from_location_id": out_of, "occurred_at": when}


# --- the fold (pure) ------------------------------------------------------------

def test_the_average_blends_two_purchase_prices_by_quantity():
    moves = [move(10, cost=200, when="2026-01-01"),
             move(30, cost=300, when="2026-01-02")]
    assert object_stock.weighted_average_cost_cents(moves, "p1") == 275


def test_a_sale_does_not_change_the_average():
    """The defining property: depletion consumes stock at the average it
    was carried at, so the average survives it unchanged."""
    moves = [move(10, cost=200), move(30, cost=300, when="2026-01-02")]
    before = object_stock.weighted_average_cost_cents(moves, "p1")
    moves.append(move(5, reason="sale", into="", out_of="shelf",
                      when="2026-01-03"))
    assert object_stock.weighted_average_cost_cents(moves, "p1") == before


def test_moving_average_differs_from_a_lifetime_average():
    """THE test for why this is folded in order rather than summed.

    Buy 10 at 100, sell all 10, buy 1 at 300. The next sale costs 300 --
    that is the only unit on the shelf. A lifetime average would answer
    118 by averaging in ten units the shop no longer owns.
    """
    moves = [
        move(10, cost=100, when="2026-01-01"),
        move(10, reason="sale", into="", out_of="shelf", when="2026-01-02"),
        move(1, cost=300, when="2026-01-03"),
    ]
    assert object_stock.weighted_average_cost_cents(moves, "p1") == 300

    lifetime = (10 * 100 + 1 * 300) / 11
    assert round(lifetime) == 118        # what the wrong version reports


def test_a_transfer_between_shelves_does_not_re_average():
    """Both ends set means the goods never left; value is unchanged."""
    moves = [move(10, cost=200),
             move(4, reason="transfer", into="back-room", out_of="shelf",
                  when="2026-01-02")]
    assert object_stock.weighted_average_cost_cents(moves, "p1") == 200


def test_free_stock_dilutes_the_average_rather_than_being_ignored():
    """Uncosted stock still occupies the shelf. Skipping it entirely would
    report the average of the priced units as if the free ones were not
    there to be sold."""
    moves = [move(10, cost=300), move(10, cost=None, when="2026-01-02")]
    assert object_stock.weighted_average_cost_cents(moves, "p1") == 150


def test_nothing_on_hand_has_no_honest_average():
    assert object_stock.weighted_average_cost_cents([], "p1") is None
    sold_out = [move(10, cost=200),
                move(10, reason="sale", into="", out_of="shelf",
                     when="2026-01-02")]
    assert object_stock.weighted_average_cost_cents(sold_out, "p1") is None


def test_stock_that_never_carried_a_price_has_no_average():
    """None, not zero. A caller must skip rather than book a zero-cost
    sale that makes the margin look perfect."""
    assert object_stock.weighted_average_cost_cents(
        [move(10, cost=None)], "p1") is None


def test_products_do_not_contaminate_each_other():
    moves = [move(10, cost=200, product="p1"),
             move(10, cost=900, product="p2")]
    assert object_stock.weighted_average_cost_cents(moves, "p1") == 200
    assert object_stock.weighted_average_cost_cents(moves, "p2") == 900


def test_moves_fold_in_date_order_not_file_order():
    """stock_moves is an append log; a backdated receipt can land after a
    later one. Order is a property of the dates, not the rows."""
    late_first = [move(1, cost=300, when="2026-03-01"),
                  move(9, cost=100, when="2026-01-01")]
    assert object_stock.weighted_average_cost_cents(late_first, "p1") == 120


# --- the hook stamps it at write time -------------------------------------------

def setup_shop(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    for loc in ("loc-shelf", "loc-customer"):
        object_records.create_collection_record(
            "locations", {"id": loc, "name": loc, "owner_id": "shop"},
            base_dir=data_dir)
    object_records.create_collection_record(
        "products",
        {"id": "p1", "name": "Enamel Mug", "product_type": "physical",
         "cost_cents": "250", "owner_id": "shop"},
        base_dir=data_dir)
    return data_dir


def buy(data_dir, move_id, quantity, cost, when="2026-01-01"):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": move_id, "product_id": "p1", "to_location_id": "loc-shelf",
         "quantity": str(quantity), "unit_cost_cents": str(cost),
         "reason": "purchase", "occurred_at": when, "owner_id": "shop"},
        base_dir=data_dir)


def hook(record, action="create"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_stock_moves", method="BEFORE_WRITE",
            payload={"action": action, "collection": "stock_moves",
                     "record": record}),
        roots=[CATALOG_OBJECTS]).result


def sale_record(**overrides):
    record = {"id": "mv-sale", "product_id": "p1",
              "from_location_id": "loc-shelf", "to_location_id": "loc-customer",
              "quantity": "3", "reason": "sale", "occurred_at": "2026-02-01",
              "owner_id": "shop"}
    record.update(overrides)
    return record


def test_a_sale_is_stamped_with_the_average_of_what_is_on_the_shelf(
        tmp_path, monkeypatch):
    """Not products.cost_cents (250), which is a planning figure someone
    typed. What was actually paid: 10 at 200 and 30 at 300 blends to 275."""
    data_dir = setup_shop(tmp_path, monkeypatch)
    buy(data_dir, "mv-buy-1", 10, 200, when="2026-01-01")
    buy(data_dir, "mv-buy-2", 30, 300, when="2026-01-02")

    result = hook(sale_record())
    assert result["record"]["unit_cost_cents"] == "275"


def test_the_stamp_excludes_the_sale_being_written(tmp_path, monkeypatch):
    """BEFORE_WRITE: the average is the one at the moment of sale, and
    the sale itself is not in the log yet to distort it."""
    data_dir = setup_shop(tmp_path, monkeypatch)
    buy(data_dir, "mv-buy-1", 10, 200)
    assert hook(sale_record(quantity="10"))["record"]["unit_cost_cents"] == "200"


def test_a_sale_with_no_priced_stock_falls_back_to_the_standard_cost(
        tmp_path, monkeypatch):
    """A shop that never recorded a purchase still gets an honest-ish
    number rather than nothing -- and it is the same fallback losses have
    always used, not a new invention."""
    data_dir = setup_shop(tmp_path, monkeypatch)
    assert hook(sale_record())["record"]["unit_cost_cents"] == "250"


def test_a_client_supplied_cost_is_never_overwritten(tmp_path, monkeypatch):
    """Purchases carry their invoice cost; nothing may second-guess it."""
    data_dir = setup_shop(tmp_path, monkeypatch)
    buy(data_dir, "mv-buy-1", 10, 200)
    assert hook(sale_record(unit_cost_cents="999")) is None


def test_a_loss_still_uses_the_standard_cost_not_the_average(
        tmp_path, monkeypatch):
    """Deliberate asymmetry. Writing off a broken mug at standard cost is
    fine and was always the behaviour; reporting cost of SALES at a
    typed-in number is how gross margin quietly becomes fiction. Only the
    sale path changed."""
    data_dir = setup_shop(tmp_path, monkeypatch)
    buy(data_dir, "mv-buy-1", 10, 200)
    breakage = {"id": "mv-break", "product_id": "p1",
                "from_location_id": "loc-shelf", "quantity": "1",
                "reason": "breakage", "occurred_at": "2026-02-01",
                "owner_id": "shop"}
    assert hook(breakage)["record"]["unit_cost_cents"] == "250"
