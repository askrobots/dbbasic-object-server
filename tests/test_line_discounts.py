"""Line discounts, and the order of operations that IS the specification.

A negotiated reduction on one row -- ex-display stock, a trade rate, ten
per cent off this item and nothing else. Recorded on the line, printed
beside what it reduced.

Deliberately NOT a promotion. object_promotions owns those: a code
somebody types, applied to the whole basket and rounded ONCE, precisely
so four separately-rounded lines cannot accumulate an error nobody can
reconcile against the percentage on the receipt. Keeping the two apart is
what stops "10% off" meaning two different numbers depending on which
layer applied it.

The arithmetic now lives in one place. app-orders and app-invoices each
carried their own `_line_amounts`, near enough identical that one was the
other plus modifier_cents -- survivable while the answer was "quantity
times price", not survivable once both have to subtract a discount.
"""

import pathlib

import pytest

import object_lines


def line(**fields):
    base = {"quantity": "1", "unit_price_cents": "1000"}
    base.update(fields)
    return base


# --- the order of operations ----------------------------------------------------

def test_a_discount_comes_off_the_line_before_tax():
    """THE rule. A discount on goods reduces the taxable base -- the same
    thing object_promotions.applies_to says for promotions, and the two
    must not disagree. Taxing the gross would charge a customer tax on
    money nobody ever asked them for."""
    amounts = object_lines.line_amounts(
        line(quantity="2", unit_price_cents="1000",
             discount_bps="1000", tax_rate_bps="2000"))
    assert amounts["gross_cents"] == 2000
    assert amounts["discount_cents"] == 200
    assert amounts["line_total_cents"] == 1800
    assert amounts["line_tax_cents"] == 360        # 20% of 1800, not of 2000


def test_line_total_is_the_net_a_customer_pays():
    """Every existing caller already stores that number under that name."""
    amounts = object_lines.line_amounts(line(discount_bps="2500"))
    assert amounts["line_total_cents"] == 750


def test_a_modifier_is_discounted_too():
    """The modifier is part of the unit price -- two oat lattes are two
    lots of oat milk -- so a discount on the line covers it."""
    amounts = object_lines.line_amounts(
        line(quantity="2", unit_price_cents="500", modifier_cents="60",
             discount_bps="1000"))
    assert amounts["gross_cents"] == 1120
    assert amounts["discount_cents"] == 112


def test_a_negative_modifier_is_how_a_FLAT_reduction_is_expressed():
    """Which is why discount_bps is a percentage and nothing else: the
    flat case already had a home."""
    amounts = object_lines.line_amounts(
        line(quantity="2", unit_price_cents="500", modifier_cents="-50"))
    assert amounts["line_total_cents"] == 900


# --- the invisibility that makes this safe to ship ------------------------------

def test_no_discount_is_arithmetically_invisible():
    """The only acceptable behaviour for a change touching every
    historical document: a line without the column folds bit-identically
    to how it always did."""
    without = object_lines.line_amounts(
        {"quantity": "3", "unit_price_cents": "333", "tax_rate_bps": "2000"})
    assert without["gross_cents"] == 999
    assert without["discount_cents"] == 0
    assert without["line_total_cents"] == 999
    assert without["line_tax_cents"] == 199        # unchanged floor division


def test_a_row_written_before_any_of_these_columns_existed_still_folds():
    assert object_lines.line_amounts({"quantity": "2",
                                      "unit_price_cents": "50"})[
        "line_total_cents"] == 100


def test_the_gross_floors_before_the_discount_is_taken():
    """Off the already-floored gross, not off a fractional intermediate --
    which is what keeps the zero case exact."""
    amounts = object_lines.line_amounts(
        line(quantity="0.5", unit_price_cents="333", discount_bps="1000"))
    assert amounts["gross_cents"] == 166           # floor(166.5)
    assert amounts["discount_cents"] == 16         # floor(16.6)
    assert amounts["line_total_cents"] == 150


# --- the refusals ---------------------------------------------------------------

def test_a_discount_can_never_make_a_line_negative():
    """12000 basis points is a typo, not a refund. A line that pays the
    customer is not something to infer from a mistyped rate."""
    amounts = object_lines.line_amounts(line(discount_bps="12000"))
    assert amounts["discount_cents"] == 1000
    assert amounts["line_total_cents"] == 0


def test_a_full_discount_is_free_not_broken():
    amounts = object_lines.line_amounts(line(discount_bps="10000",
                                             tax_rate_bps="2000"))
    assert amounts["line_total_cents"] == 0
    assert amounts["line_tax_cents"] == 0          # nothing to tax


def test_a_negative_rate_is_not_a_surcharge():
    """A discount field is for discounts. Someone wanting to add money
    has modifier_cents, which is signed and says so."""
    assert object_lines.line_amounts(line(discount_bps="-2000"))[
        "discount_cents"] == 0


def test_a_credit_line_is_not_discountable():
    """Taking a percentage off a negative line makes the credit SMALLER,
    which is the opposite of what anyone typing '10% off' means."""
    amounts = object_lines.line_amounts(
        line(quantity="1", unit_price_cents="-500", discount_bps="1000"))
    assert amounts["discount_cents"] == 0
    assert amounts["line_total_cents"] == -500


def test_unparseable_values_fold_to_zero_rather_than_raising():
    amounts = object_lines.line_amounts(
        {"quantity": "banana", "unit_price_cents": "", "discount_bps": "x"})
    assert amounts["line_total_cents"] == 0


# --- the document fold ----------------------------------------------------------

def test_totals_sum_gross_discount_and_net_separately():
    """An invoice has to be able to print what was taken off, not only
    what is left."""
    result = object_lines.totals([
        line(quantity="2", unit_price_cents="1000", discount_bps="1000",
             tax_rate_bps="2000"),
        line(quantity="1", unit_price_cents="500", tax_rate_bps="2000"),
    ])
    assert result["gross_cents"] == 2500
    assert result["discount_cents"] == 200
    assert result["subtotal_cents"] == 2300
    assert result["tax_cents"] == 460
    assert result["total_cents"] == 2760


def test_an_empty_document_totals_to_zero():
    assert object_lines.totals([])["total_cents"] == 0


# --- one implementation, not two ------------------------------------------------

@pytest.mark.parametrize("path", [
    "packages/app-orders/objects/system/order_totals.py",
    "packages/app-invoices/objects/system/invoice_totals.py",
])
def test_both_totals_handlers_delegate_rather_than_restate(path):
    """The duplication is the thing this file exists to end. A handler
    that re-implements the arithmetic is one that will disagree with the
    other the next time either changes -- and its own docstring already
    warns that a fold which cannot reproduce the number it restates
    quietly replaces a correct total with a smaller one."""
    source = (pathlib.Path(__file__).resolve().parents[1] / path).read_text()
    assert "object_lines.line_amounts" in source
    assert "ROUND_FLOOR" not in source.split("def _line_amounts")[1][:600]
