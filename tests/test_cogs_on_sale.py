"""COGS on sale: the entry the books do not get.

A sale takes goods off a shelf. Those goods cost money, and the moment
they leave, that cost stops being an asset and becomes an expense: DR
cost of goods sold, CR inventory. Without it the balance sheet carries
inventory the shop no longer owns and the profit and loss shows revenue
with nothing set against it -- every sale looks like pure margin, which
is the single most flattering way a small business can be wrong about
itself.

**system_stock_books does not do this today, and says so.** Its module
docstring is explicit: "Purchases/sales composition is explicitly NOT
here: COGS-on-sale needs a real valuation method (FIFO/weighted
average); stamped-cost losses do not. That boundary holds until a
valuation spec exists."

That is a defensible boundary and this file does not argue with it. It
does refuse to leave the gap undescribed. The xfail below is the
SPECIFICATION: it stages a real shop sale, fires the real handler with
the real dispatcher payload, and asserts the journal that ought to
exist. It fails at the first assertion because the handler returns
{"skipped": "not a loss event"} for reason="sale". The day somebody
implements COGS, strict xfail turns this file into the acceptance test
they already have -- and it fails loudly if the feature lands without
matching what was specified here.

What is missing, exactly:

1. `system_stock_books.compose_for_move` has no `sale` direction. Only
   the six loss reasons and count adjustments compose anything.
2. There is no cost-of-goods-sold account setting. The account map has
   `inventory.journal.inventory_account` and `shrinkage_account` plus a
   per-reason override pattern; a COGS account is neither configured
   nor read.
3. There is no valuation method. This test stages `unit_cost_cents` on
   the move so the arithmetic is unambiguous -- but nothing stamps that
   cost on a sale move today (hook_stock_moves stamps cost for losses),
   and where a sale move carries no cost, no honest journal can be
   composed at all. A valuation decision (FIFO vs weighted average) is
   the real prerequisite, and picking one by accident inside a handler
   would be worse than the current silence.
"""

import pathlib

import pytest
from conftest import stage_collection

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
CATALOG_OBJECTS = PACKAGES / "app-catalog" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

INV, COGS, SHRINK = "acct-inventory", "acct-cogs", "acct-shrinkage"

# Both spellings of the account setting are staged: the existing map uses
# `inventory.journal.{reason}_account` as a per-reason override, so
# `sale_account` is the shape that already fits, while `cogs_account` is
# what somebody would reach for by name. The assertions below are on the
# ACCOUNT the journal hits, not on which key was read, so whichever
# convention an implementer picks, this specification still holds.
SETTINGS = (("inventory.journal.inventory_account", INV),
            ("inventory.journal.shrinkage_account", SHRINK),
            ("inventory.journal.sale_account", COGS),
            ("inventory.journal.cogs_account", COGS))


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-finance", "fin_journals"),
                      ("app-finance", "fin_journal_lines"),
                      ("app-finance", "fin_accounts")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(SETTINGS))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    for acct in (INV, COGS, SHRINK):
        object_records.create_collection_record(
            "fin_accounts",
            {"id": acct, "name": acct, "account_type": "asset",
             "owner_id": "shop"},
            base_dir=data_dir)
    for loc in ("loc-shelf", "loc-customer"):
        object_records.create_collection_record(
            "locations", {"id": loc, "name": loc, "owner_id": "shop"},
            base_dir=data_dir)
    object_records.create_collection_record(
        "products",
        {"id": "p1", "name": "Enamel Mug", "product_type": "physical",
         "cost_cents": "250", "price_cents": "1200", "owner_id": "shop"},
        base_dir=data_dir)
    return data_dir


def sale_move(data_dir, move_id="mv-sale", *, quantity="3", cost="250"):
    """The move system_shop_fulfillment composes when money arrives:
    stock leaves the shelf and goes to the customer."""
    return object_records.create_collection_record(
        "stock_moves",
        {"id": move_id, "product_id": "p1", "from_location_id": "loc-shelf",
         "to_location_id": "loc-customer", "quantity": quantity,
         "unit_cost_cents": cost, "reason": "sale",
         "occurred_at": "2026-07-24", "reference": "orders/o1:fulfil",
         "owner_id": "shop"},
        base_dir=data_dir)


def fire_stock_books(record_id):
    """The REAL dispatcher payload: event name participle, action raw."""
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_stock_books", method="EVENT",
            payload={"event": "stock_moves.record.created",
                     "collection": "stock_moves", "record_id": record_id,
                     "action": "create"}),
        roots=[CATALOG_OBJECTS]).result


def journals(data_dir):
    return object_records.read_collection_records("fin_journals",
                                                  base_dir=data_dir)


def lines_for(data_dir, journal_id):
    return [line for line in object_records.read_collection_records(
                "fin_journal_lines", base_dir=data_dir)
            if line["journal_id"] == journal_id]


# --- the gap, pinned from the side that passes -------------------------------

def test_today_a_sale_reaches_the_shelf_and_never_the_books(
        tmp_path, monkeypatch):
    """Documented current behaviour, asserted so the xfail below cannot be
    mistaken for a flaky test: the handler sees the sale and declines it."""
    data_dir = setup_env(tmp_path, monkeypatch)
    sale_move(data_dir)
    assert fire_stock_books("mv-sale")["skipped"] == "not a loss event"
    assert journals(data_dir) == []


# --- the gap, written as the feature it is not -------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "COGS-on-sale is not implemented. system_stock_books composes losses "
    "and count variances only; sales are skipped as 'not a loss event' "
    "pending a valuation method (FIFO/weighted average) and a "
    "cost-of-goods-sold account setting. See this module's docstring."))
def test_a_sale_books_cost_of_goods_sold_against_inventory(
        tmp_path, monkeypatch):
    """SPECIFICATION, not a passing test.

    Three mugs at 250c of cost leave the shelf: DR cost of goods sold
    750, CR inventory 750, on the date the goods moved. Same shape as
    the loss composer, same shared composer, same generated_from
    provenance -- the only new thing is the direction and the account.
    """
    data_dir = setup_env(tmp_path, monkeypatch)
    sale_move(data_dir, quantity="3", cost="250")

    result = fire_stock_books("mv-sale")
    assert result["posted"] is True

    journal = journals(data_dir)[0]
    assert journal["status"] == "posted"
    assert journal["generated_from"] == "stock_moves/mv-sale"
    assert journal["date"] == "2026-07-24"

    by_account = {line["account_id"]: line
                  for line in lines_for(data_dir, result["journal_id"])}
    assert by_account[COGS]["debit_cents"] == "750"
    assert by_account[COGS]["credit_cents"] == "0"
    assert by_account[INV]["credit_cents"] == "750"
    assert by_account[INV]["debit_cents"] == "0"


@pytest.mark.xfail(strict=True, reason=(
    "COGS-on-sale is not implemented -- see the module docstring. This "
    "asserts the idempotency the feature must have on the day it lands: a "
    "replayed event composes nothing twice."))
def test_a_replayed_sale_event_books_one_journal_not_two(
        tmp_path, monkeypatch):
    """A dispatcher retry, a daemon re-run and an operator poking the
    handler by hand all arrive as the same event. Provenance
    ("stock_moves/{id}") is what makes the second one a no-op -- exactly
    the guard the loss path already proves, which is why the sale path
    must not invent its own."""
    data_dir = setup_env(tmp_path, monkeypatch)
    sale_move(data_dir, quantity="3", cost="250")

    first = fire_stock_books("mv-sale")
    assert first["posted"] is True
    replay = fire_stock_books("mv-sale")
    assert "already composed" in replay["skipped"]
    assert len(journals(data_dir)) == 1
    assert len(lines_for(data_dir, first["journal_id"])) == 2


@pytest.mark.xfail(strict=True, reason=(
    "COGS-on-sale is not implemented -- see the module docstring. Staged "
    "because the valuation gap is the real blocker: nothing stamps a cost "
    "on a sale move today, and a costless sale must skip rather than "
    "guess."))
def test_a_sale_with_no_stamped_cost_books_nothing_rather_than_guessing(
        tmp_path, monkeypatch):
    """The honest failure mode. Where a sale move carries no cost there is
    no journal that can be composed truthfully, and inventing one from the
    product's current cost_cents would be a valuation method chosen by
    accident inside a handler."""
    data_dir = setup_env(tmp_path, monkeypatch)
    sale_move(data_dir, move_id="mv-costless", quantity="3", cost="0")
    result = fire_stock_books("mv-costless")
    assert result["skipped"] == "no stamped cost; nothing to book"
    assert journals(data_dir) == []
