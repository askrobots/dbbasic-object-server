"""COGS on sale: the entry the books used to miss.

A sale takes goods off a shelf. Those goods cost money, and the moment
they leave, that cost stops being an asset and becomes an expense: DR
cost of goods sold, CR inventory. Without it the balance sheet carries
inventory the shop no longer owns and the profit and loss shows revenue
with nothing set against it -- every sale looks like pure margin, which
is the single most flattering way a small business can be wrong about
itself.

This file was written first as a SPECIFICATION, three strict xfails
describing the feature that did not exist. It is now the acceptance test
they promised to become, and the assertions are unchanged from the day
they were staged -- which is the point of writing them that way.

What unblocked it was the valuation method, which was always the real
prerequisite rather than the journal shape:

**Weighted average**, moving, folded in move order
(object_stock.weighted_average_cost_cents). FIFO is more precise and is
what a lot-tracked warehouse should use, but it needs cost LAYERS and
stock_moves has no lot dimension -- adding one to satisfy a journal entry
would be a storage change driven by a report. Weighted average needs
nothing the append log does not already carry.

The split that keeps it honest: hook_stock_moves VALUES the sale at write
time (doctrine #1, a point-in-time fact gets stamped, so tomorrow's price
change cannot reprice yesterday's sale), and system_stock_books only ever
READS the stamped cost. A costless sale still skips rather than reaching
for products.cost_cents, because a valuation method picked by accident
inside a handler is exactly what the original boundary existed to
prevent.
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
    for loc in ("loc-shelf", "loc-customer", "loc-back-room"):
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


# --- the boundaries that did not move -------------------------------

def test_a_transfer_between_shelves_still_books_nothing(tmp_path, monkeypatch):
    """The boundary that survives COGS landing: moving goods from one
    shelf to another changes no value, so it must compose nothing. A sale
    is distinguished by leaving the system, not by having a from-location.
    """
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "stock_moves",
        {"id": "mv-move", "product_id": "p1", "from_location_id": "loc-shelf",
         "to_location_id": "loc-back-room", "quantity": "3",
         "unit_cost_cents": "250", "reason": "transfer",
         "occurred_at": "2026-07-24", "owner_id": "shop"},
        base_dir=data_dir)
    assert fire_stock_books("mv-move")["skipped"] == "not a loss event"
    assert journals(data_dir) == []


# --- the feature, asserted exactly as it was specified -------------------------------

def test_a_sale_books_cost_of_goods_sold_against_inventory(
        tmp_path, monkeypatch):
    """Three mugs at 250c of cost leave the shelf: DR cost of goods sold
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
