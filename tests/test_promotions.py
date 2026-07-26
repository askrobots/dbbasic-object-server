"""Promotions, gift cards and store credit: the money that comes OFF a
sale, and the money that PAYS for one.

Two claims carry this file, and they are different claims about
different kinds of thing.

A DISCOUNT IS A PRICE. It changes what the goods cost, so it comes off
the taxable base before tax is computed -- what every jurisdiction that
levies a sales or value-added tax actually requires -- it lands on the
invoice as a LINE so the total stays the sum of its own lines, and its
terms are STAMPED on the redemption at use so that editing the promotion
next month cannot restate what somebody already got.

A GIFT CARD IS TENDER. It is money the customer already gave this shop,
so it changes no price and no taxable base; it goes through the wallet
gate that already exists (hook_wallet_entries, which sums the entries
rather than trusting the rollup), it cannot overdraw, and -- the property
most easily broken by a refactor -- a PREVIEW SPENDS NOTHING.
"""

import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_cart
import object_execution
import object_ids
import object_promotions
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHOP_OBJECTS = PACKAGES / "app-shop" / "objects"
BILLING_OBJECTS = PACKAGES / "app-billing" / "objects"
PROMO_OBJECTS = PACKAGES / "app-promotions" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TOKEN = "sess-promo"
TODAY = "2026-06-15"


# --- the pure fold, with no data directory in sight ---------------------------

def promo(**fields):
    row = {"id": "pr1", "code": "SPRING20", "kind": "percent", "value": "2000",
           "is_active": "true", "stacking": "never"}
    row.update({k: str(v) for k, v in fields.items()})
    return row


def test_a_percentage_comes_off_before_the_tax_is_computed():
    """Not a preference -- tax is owed on the consideration, and a
    seller's own price reduction reduces it. Charging tax on the
    pre-discount price collects money the shop then remits on revenue it
    never earned, which is the kind of systematic error an audit finds by
    multiplying one rate by one column."""
    items = [{"id": "a", "quantity": "1", "unit_price_cents": "10000"}]
    folded = object_cart.checkout_totals(items, tax_rate_bps=1000,
                                         discount_cents=2000)
    assert folded["subtotal_cents"] == 10000
    assert folded["discount_cents"] == 2000
    assert folded["taxable_cents"] == 8000
    assert folded["tax_cents"] == 800              # 10% of 8000, not of 10000
    assert folded["total_cents"] == 8800

    undiscounted = object_cart.checkout_totals(items, tax_rate_bps=1000)
    assert undiscounted["tax_cents"] == 1000       # and the shop would keep 200


def test_the_discount_is_rounded_once_over_the_whole_basket():
    """Rounded per line, the cents of error accumulate into a number no
    customer can reconcile against the percentage on their receipt."""
    # 15% of 333 is 49.95 -- three separately rounded lines come to 150,
    # the single rounding to 150 as well, but the base matters: 15% of 999
    # is 149.85 -> 150, and per-line 50+50+50 = 150 only by luck. The
    # honest check is that the fold rounds the WHOLE base once.
    assert object_promotions.discount_for(999, 0, promo(value=1500)) == 150
    assert object_promotions.discount_for(1099, 0, promo(value=825)) == 91


def test_a_fixed_discount_bigger_than_the_basket_cannot_make_a_negative_total():
    """A shop does not owe money to somebody for shopping, and a negative
    total would propagate into an invoice, a payment and eventually a
    refund of money nobody paid."""
    big = promo(kind="fixed", value=5000)
    assert object_promotions.discount_for(3000, 500, big) == 3000    # clamped

    folded = object_cart.checkout_totals(
        [{"id": "a", "quantity": "1", "unit_price_cents": "3000"}],
        tax_rate_bps=1000, shipping_flat_cents=500, discount_cents=5000)
    assert folded["discount_cents"] == 3000
    assert folded["taxable_cents"] == 0
    assert folded["tax_cents"] == 0
    assert folded["total_cents"] == 500            # the postage, and no less
    assert folded["total_cents"] >= 0


def test_free_shipping_comes_off_the_postage_and_its_tax_with_it():
    """Where a jurisdiction taxes delivery, free postage must remove that
    tax too -- otherwise the shop charges tax on a charge it waived."""
    items = [{"id": "a", "quantity": "1", "unit_price_cents": "5000"}]
    post = promo(kind="free_shipping", value=0)
    assert object_promotions.applies_to(post) == "shipping"
    assert object_promotions.discount_for(5000, 700, post) == 700

    folded = object_cart.checkout_totals(
        items, tax_rate_bps=1000, tax_shipping=True, shipping_flat_cents=700,
        discount_cents=700, discount_on="shipping")
    assert folded["discount_cents"] == 700
    assert folded["taxable_cents"] == 5000         # goods only; postage waived
    assert folded["tax_cents"] == 500
    assert folded["total_cents"] == 5500


def test_every_blocker_is_reported_together_and_never_just_the_first():
    """A shopper told their code expired, who finds another and is then
    told the basket is too small, is a shopper who has left."""
    exhausted = promo(id="pr9", code="DEAD", is_active="false",
                      starts_on="2026-07-01", ends_on="2026-05-01",
                      minimum_spend_cents=10000, max_redemptions=1,
                      per_customer_limit=1)
    redemptions = [{"promotion_id": "pr9", "customer_email": "Ada@Example.com"}]
    reasons = object_promotions.blockers(
        exhausted, 500, "ada@example.com", redemptions, on_date=TODAY,
        code="DEAD")
    joined = " ".join(reasons)
    assert len(reasons) == 6, reasons
    assert "not active" in joined
    assert "does not start until 2026-07-01" in joined
    assert "ended on 2026-05-01" in joined
    assert "at least 100.00" in joined and "this one is 5.00" in joined
    assert "used 1 times and its limit is 1" in joined
    assert "already been used 1 times by ada@example.com" in joined


def test_an_expired_or_unstarted_code_is_refused_with_its_date():
    """'That code has expired' starts a support ticket. 'That code ended
    on the 30th of June' ends the conversation."""
    ended = object_promotions.blockers(
        promo(ends_on="2026-06-30"), 5000, "", [], on_date="2026-07-01",
        code="SPRING20")
    assert ended == ["The code SPRING20 ended on 2026-06-30."]

    early = object_promotions.blockers(
        promo(starts_on="2026-07-01"), 5000, "", [], on_date=TODAY,
        code="SPRING20")
    assert early == ["The code SPRING20 does not start until 2026-07-01."]


def test_a_code_at_its_limit_is_refused_with_both_numbers():
    used = [{"promotion_id": "pr1", "customer_email": f"{n}@x.com"}
            for n in range(3)]
    assert object_promotions.blockers(
        promo(max_redemptions=3), 5000, "new@x.com", used, on_date=TODAY) == [
        "The code SPRING20 has been used 3 times and its limit is 3."]
    # One under, and it works.
    assert object_promotions.blockers(
        promo(max_redemptions=4), 5000, "new@x.com", used, on_date=TODAY) == []


def test_the_per_customer_limit_counts_one_person_not_everybody():
    used = [{"promotion_id": "pr1", "customer_email": "ada@example.com"},
            {"promotion_id": "pr1", "customer_email": "grace@example.com"}]
    limited = promo(per_customer_limit=1)
    assert object_promotions.blockers(limited, 5000, "ada@example.com", used,
                                      on_date=TODAY)
    assert object_promotions.blockers(limited, 5000, "alan@example.com", used,
                                      on_date=TODAY) == []


def test_the_per_customer_limit_survives_a_capital_letter():
    """The same address with a shift key is the same person, and a limit
    that a capital letter walks around is not a limit."""
    used = [{"promotion_id": "pr1", "customer_email": "Ada@Example.COM"}]
    assert object_promotions.blockers(promo(per_customer_limit=1), 5000,
                                      "ada@example.com", used, on_date=TODAY)


def test_the_count_of_redemptions_is_a_fold_and_not_the_stored_counter():
    """promotions.redemptions_used is a rollup. A gate that read it would
    authorise the 1001st use of a 1000-use code the moment it went stale
    -- the identical argument hook_wallet_entries makes about summing the
    ledger instead of trusting wallets.balance_minor."""
    lying = promo(max_redemptions=2, redemptions_used=0)
    really_used = [{"promotion_id": "pr1", "customer_email": "a@x.com"},
                   {"promotion_id": "pr1", "customer_email": "b@x.com"}]
    assert object_promotions.blockers(lying, 5000, "c@x.com", really_used,
                                      on_date=TODAY)


def test_resolution_never_looks_forward():
    """Two rows share a code -- a campaign re-run on new terms. The answer
    is the newest one that had ALREADY STARTED, exactly as object_rates
    picks a rate card."""
    rows = [promo(id="old", value=1000, starts_on="2026-01-01"),
            promo(id="new", value=3000, starts_on="2026-09-01")]
    assert object_promotions.resolve("spring20", rows, on_date=TODAY)["id"] == "old"
    assert object_promotions.resolve("SPRING20", rows,
                                     on_date="2026-09-02")["id"] == "new"


def test_a_code_nobody_has_started_yet_still_resolves_so_it_can_be_dated():
    """Returning None would leave the refusal only able to say 'no such
    code', which sends somebody to support instead of to a calendar."""
    rows = [promo(id="future", starts_on="2026-09-01")]
    found = object_promotions.resolve("SPRING20", rows, on_date=TODAY)
    assert found["id"] == "future"
    assert object_promotions.blockers(found, 5000, "", [], on_date=TODAY,
                                      code="SPRING20") == [
        "The code SPRING20 does not start until 2026-09-01."]


def test_an_unknown_code_is_one_sentence_and_names_what_was_typed():
    assert object_promotions.resolve("NOPE", [promo()], on_date=TODAY) is None
    assert object_promotions.blockers(None, 5000, "", [], on_date=TODAY,
                                      code="nope") == [
        "There is no promotion with the code 'NOPE'."]


def test_a_code_that_never_stacks_says_so_with_the_company_it_refused():
    assert object_promotions.blockers(promo(stacking="never"), 5000, "", [],
                                      on_date=TODAY, others=["FREESHIP"]) == [
        "The code SPRING20 cannot be combined with FREESHIP."]
    assert object_promotions.blockers(promo(stacking="with_others"), 5000, "",
                                      [], on_date=TODAY,
                                      others=["FREESHIP"]) == []


def test_the_terms_that_get_stamped_are_the_ones_that_applied():
    stamped = object_promotions.terms(promo(minimum_spend_cents=1000))
    assert stamped == {"promotion_id": "pr1", "code_used": "SPRING20",
                       "kind": "percent", "value": "2000",
                       "minimum_spend_cents": "1000", "stacking": "never"}


def test_a_gift_card_is_tender_and_changes_no_taxable_base():
    """Treating a card as a discount would let a shop sell $100 of goods
    and remit tax on $60."""
    items = [{"id": "a", "quantity": "1", "unit_price_cents": "10000"}]
    folded = object_cart.checkout_totals(items, tax_rate_bps=1000,
                                         credit_cents=4000)
    assert folded["taxable_cents"] == 10000
    assert folded["tax_cents"] == 1000
    assert folded["total_cents"] == 11000          # the sale is still the sale
    assert folded["credit_applied_cents"] == 4000
    assert folded["amount_due_cents"] == 7000


def test_a_card_worth_more_than_the_bill_pays_the_bill_and_no_more():
    folded = object_cart.checkout_totals(
        [{"id": "a", "quantity": "1", "unit_price_cents": "1000"}],
        credit_cents=999999)
    assert folded["credit_applied_cents"] == 1000
    assert folded["amount_due_cents"] == 0


def test_the_fold_is_unchanged_for_every_caller_that_passes_no_code():
    """The regression that matters most: a shop that has never heard of
    promotions or gift cards gets byte-for-byte the arithmetic it had."""
    items = [{"id": "a", "quantity": "2", "unit_price_cents": "1000"}]
    folded = object_cart.checkout_totals(items, tax_rate_bps=1000,
                                         shipping_flat_cents=500)
    assert folded["discount_cents"] == 0
    assert folded["tax_cents"] == 200
    assert folded["total_cents"] == 2700
    assert folded["credit_applied_cents"] == 0
    assert folded["amount_due_cents"] == folded["total_cents"]


# --- the shop, with a data directory -----------------------------------------

def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A shop that can take a code and a card: app-shop's objects and
    app-billing's, on one root, the way a real box has them -- checkout
    asks hook_wallet_entries in process, and a trusted server-side write
    bypasses hooks by design, so the gate has to be REACHABLE or it is
    not a gate."""
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shop", "carts"), ("app-shop", "cart_items"),
                      ("app-catalog", "products"), ("app-catalog", "locations"),
                      ("app-catalog", "stock_moves"),
                      ("app-catalog", "backorders"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-invoices", "invoices"),
                      ("app-invoices", "invoice_lines"),
                      ("app-payments", "payments"),
                      ("app-billing", "wallets"),
                      ("app-billing", "wallet_entries"),
                      ("app-promotions", "promotions"),
                      ("app-promotions", "promotion_redemptions")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for source in (SHOP_OBJECTS, BILLING_OBJECTS, PROMO_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    object_records.create_collection_record(
        "locations",
        {"id": "loc-shelf", "name": "Shelf", "location_type": "warehouse",
         "owner_id": "shop"}, base_dir=data_dir)
    return data_dir, objects_root


def product(data_dir, product_id, name, cents, **fields):
    record = {"id": product_id, "name": name, "sku": product_id.upper(),
              "product_type": "physical", "price_cents": str(cents),
              "currency": "USD", "is_active": "true", "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("products", record,
                                                   base_dir=data_dir)


def stock_in(data_dir, product_id, quantity):
    return object_records.create_collection_record(
        "stock_moves",
        {"id": object_ids.new_uuid4(), "product_id": product_id,
         "to_location_id": "loc-shelf", "quantity": str(quantity),
         "reason": "purchase", "occurred_at": "2026-06-01", "owner_id": "shop"},
        base_dir=data_dir)


def promotion(data_dir, **fields):
    record = {"id": "pr1", "code": "SPRING20", "kind": "percent",
              "value": "2000", "is_active": "true", "stacking": "never",
              "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("promotions", record,
                                                    base_dir=data_dir)


def wallet(data_dir, wallet_id="gc1", code="GC-TEST", kind="gift_card",
           **fields):
    record = {"id": wallet_id, "owner_id": "shop", "kind": kind, "code": code,
              "is_active": "true"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("wallets", record,
                                                    base_dir=data_dir)


def credit(data_dir, wallet_id, amount, *, kind="topup"):
    return object_records.create_collection_record(
        "wallet_entries",
        {"id": object_ids.new_uuid4(), "wallet_id": wallet_id,
         "amount_minor": str(amount), "kind": kind, "owner_id": "shop"},
        base_dir=data_dir)


def run(object_id, payload, objects_root, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload),
        roots=[objects_root]).result


def cart(objects_root, action="get", **payload):
    return run("action_cart", {"session_token": TOKEN, "action": action,
                               **payload}, objects_root)


def checkout(objects_root, **payload):
    payload.setdefault("today", TODAY)
    return run("action_checkout", {"session_token": TOKEN, **payload},
               objects_root)


def basket_of(objects_root, data_dir, cents=10000, quantity=1):
    product(data_dir, "p1", "Enamel Mug", cents)
    stock_in(data_dir, "p1", 10)
    cart(objects_root, "add", product_id="p1", quantity=str(quantity))


def rows(data_dir, collection):
    return object_records.read_collection_records(collection, base_dir=data_dir)


TAXED = (("shop.tax_rate_bps", "1000"),)


def test_a_percentage_at_checkout_is_taxed_after_the_discount(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=TAXED)
    basket_of(objects_root, data_dir)
    promotion(data_dir)

    placed = checkout(objects_root, customer_email="ada@example.com",
                      promo_code="spring20")
    assert placed["ok"] is True
    assert placed["subtotal_cents"] == 10000
    assert placed["discount_cents"] == 2000
    assert placed["tax_cents"] == 800
    assert placed["total_cents"] == 8800


def test_the_invoice_total_is_still_the_sum_of_its_own_lines(tmp_path, monkeypatch):
    """The discount is a LINE, negative -- not a field beside the total.
    Anybody who adds the rows up gets the number at the bottom."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        settings=TAXED + (("shop.shipping_flat_cents", "500"),))
    basket_of(objects_root, data_dir)
    promotion(data_dir)

    checkout(objects_root, customer_email="ada@example.com",
             promo_code="SPRING20")
    invoice = rows(data_dir, "invoices")[0]
    lines = rows(data_dir, "invoice_lines")

    discount = [line for line in lines if "Discount" in line["description"]]
    assert len(discount) == 1
    assert discount[0]["line_total_cents"] == "-2000"
    assert "SPRING20" in discount[0]["description"]

    assert (sum(int(line["line_total_cents"]) for line in lines)
            == int(invoice["subtotal_cents"]) == 8500)
    assert (int(invoice["subtotal_cents"]) + int(invoice["tax_cents"])
            == int(invoice["total_cents"]) == 9300)


def test_a_fixed_code_larger_than_the_basket_leaves_a_bill_of_zero(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir, cents=1500)
    promotion(data_dir, kind="fixed", value=99999, code="TAKEITALL")

    placed = checkout(objects_root, customer_email="ada@example.com",
                      promo_code="TAKEITALL")
    assert placed["discount_cents"] == 1500
    assert placed["total_cents"] == 0
    invoice = rows(data_dir, "invoices")[0]
    assert int(invoice["total_cents"]) == 0


def test_a_bad_code_is_refused_beside_the_stock_problem_not_before_it(tmp_path, monkeypatch):
    """One 409 carrying everything. Revealing the code problem first, then
    the stock problem after they fix it, is how a checkout is abandoned."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "p1", "Mug", 1000)
    stock_in(data_dir, "p1", 1)
    cart(objects_root, "add", product_id="p1", quantity="5")
    promotion(data_dir, ends_on="2026-01-01")

    refused = checkout(objects_root, customer_email="ada@example.com",
                       promo_code="SPRING20")
    assert refused["status"] == 409
    assert refused["unavailable"]                      # the stock problem
    assert refused["promo_problems"] == [
        "The code SPRING20 ended on 2026-01-01."]      # and the code problem
    assert refused["money_problems"] == refused["promo_problems"]
    assert not rows(data_dir, "orders")


def test_the_terms_stamped_at_redemption_survive_a_later_edit(tmp_path, monkeypatch):
    """The whole reason the stamp exists. A shop that doubles SPRING20
    next month must not silently restate the receipt somebody is already
    holding."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir)
    promotion(data_dir, value=1000)

    checkout(objects_root, customer_email="ada@example.com",
             promo_code="SPRING20")
    redemption = rows(data_dir, "promotion_redemptions")[0]
    assert redemption["value"] == "1000"
    assert redemption["kind"] == "percent"
    assert redemption["code_used"] == "SPRING20"
    assert redemption["discount_cents"] == "1000"
    assert redemption["customer_email"] == "ada@example.com"

    object_records.update_collection_record(
        "promotions", "pr1", {"value": "5000", "kind": "fixed"},
        base_dir=data_dir, actor="operator")
    after = rows(data_dir, "promotion_redemptions")[0]
    assert after["value"] == "1000"
    assert after["kind"] == "percent"
    assert after["discount_cents"] == "1000"


def test_a_used_code_counts_against_its_limit_for_the_next_shopper(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir)
    promotion(data_dir, max_redemptions=1)
    assert checkout(objects_root, customer_email="ada@example.com",
                    promo_code="SPRING20")["ok"] is True

    # A second shopper, a second basket, the same code.
    object_records.create_collection_record(
        "carts", {"id": "c2", "session_token": "sess-two", "status": "open",
                  "currency": "USD", "owner_id": "shop"}, base_dir=data_dir)
    object_records.create_collection_record(
        "cart_items", {"id": "ci2", "cart_id": "c2", "product_id": "p1",
                       "description": "Enamel Mug", "quantity": "1",
                       "unit_price_cents": "10000", "owner_id": "shop"},
        base_dir=data_dir)
    refused = run("action_checkout",
                  {"session_token": "sess-two", "today": TODAY,
                   "customer_email": "grace@example.com",
                   "promo_code": "SPRING20"}, objects_root)
    assert refused["status"] == 409
    assert "has been used 1 times and its limit is 1" in refused["error"]


def test_a_preview_writes_no_redemption_and_no_wallet_entry(tmp_path, monkeypatch):
    """A preview that burned a single-use code or drained a gift card
    would be a shop charging people for looking."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir)
    promotion(data_dir, max_redemptions=1)
    wallet(data_dir)
    credit(data_dir, "gc1", 5000)

    quoted = checkout(objects_root, preview="true", promo_code="SPRING20",
                      gift_card_code="gc-test",
                      customer_email="ada@example.com")
    assert quoted["preview"] is True
    assert quoted["discount_cents"] == 2000
    assert quoted["credit_applied_cents"] == 5000
    assert quoted["amount_due_cents"] == 3000

    assert rows(data_dir, "promotion_redemptions") == []
    assert [row for row in rows(data_dir, "wallet_entries")
            if int(row["amount_minor"]) < 0] == []
    assert rows(data_dir, "payments") == []
    assert rows(data_dir, "orders") == []


def test_a_gift_card_pays_part_of_the_bill_and_is_debited_once(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=TAXED)
    basket_of(objects_root, data_dir)
    wallet(data_dir)
    credit(data_dir, "gc1", 4000)

    placed = checkout(objects_root, customer_email="ada@example.com",
                      gift_card_code="GC-TEST")
    assert placed["ok"] is True
    assert placed["total_cents"] == 11000              # the sale, taxed in full
    assert placed["credit_applied_cents"] == 4000
    assert placed["amount_due_cents"] == 7000
    assert placed["gift_card_applied_cents"] == 4000

    debits = [row for row in rows(data_dir, "wallet_entries")
              if int(row["amount_minor"]) < 0]
    assert len(debits) == 1
    assert debits[0]["amount_minor"] == "-4000"
    assert debits[0]["generated_from"].startswith("checkout/")
    # The balance is the entries, and they now come to nothing.
    assert object_records.get_collection_record(
        "wallets", "gc1", base_dir=data_dir)["balance_minor"] == "0"

    # Tender is a payment, so the bill knows it was partly settled.
    payment = rows(data_dir, "payments")[0]
    assert payment["amount_cents"] == "4000"
    assert payment["invoice_id"] == placed["invoice_id"]
    assert payment["reference"] == "wallets/gc1"


def test_a_card_cannot_overdraw_because_the_existing_gate_says_so(tmp_path, monkeypatch):
    """The ceiling is hook_wallet_entries', asked in process. A second
    opinion written into checkout would be a second thing to get wrong,
    and would quote a customer a different number depending on which door
    their spend came through."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir, cents=10000)
    wallet(data_dir)
    credit(data_dir, "gc1", 2500)

    placed = checkout(objects_root, customer_email="ada@example.com",
                      gift_card_code="GC-TEST")
    assert placed["gift_card_applied_cents"] == 2500
    assert placed["amount_due_cents"] == 7500

    balance = sum(int(row["amount_minor"])
                  for row in rows(data_dir, "wallet_entries")
                  if row["wallet_id"] == "gc1")
    assert balance == 0                                # spent, never overdrawn

    # And the gate itself refuses a debit past the floor, in its own words.
    verdict = run("hook_wallet_entries",
                  {"action": "create", "collection": "wallet_entries",
                   "record": {"id": "x", "wallet_id": "gc1",
                              "amount_minor": "-1", "kind": "debit"}},
                  objects_root, method="BEFORE_WRITE")
    assert verdict["status"] == 402


def test_an_empty_card_is_refused_with_the_other_problems(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir)
    wallet(data_dir)                                   # no entries at all

    refused = checkout(objects_root, customer_email="ada@example.com",
                       gift_card_code="GC-TEST")
    assert refused["status"] == 409
    assert refused["gift_card_problems"] == [
        "The gift card GC-TEST has nothing left on it."]
    assert not rows(data_dir, "orders")


def test_an_unknown_card_names_the_code_and_places_no_order(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir)

    refused = checkout(objects_root, customer_email="ada@example.com",
                       gift_card_code="GC-NOPE")
    assert refused["status"] == 409
    assert "There is no gift card with the code 'GC-NOPE'." in refused["error"]
    assert not rows(data_dir, "orders")


def test_store_credit_spends_through_exactly_the_same_gate(tmp_path, monkeypatch):
    """Store credit is a wallet issued by a refund or a dispute rather
    than bought -- action_resolve_dispute already writes into this same
    ledger. Given a code so a guest can reach it, it redeems by the same
    path a gift card does, because it IS the same path."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    basket_of(objects_root, data_dir, cents=3000)
    wallet(data_dir, wallet_id="sc1", code="SC-ADA", kind="store_credit")
    # The shape action_resolve_dispute writes: positive, kind adjustment.
    credit(data_dir, "sc1", 3000, kind="adjustment")

    placed = checkout(objects_root, customer_email="ada@example.com",
                      gift_card_code="SC-ADA")
    assert placed["amount_due_cents"] == 0
    debits = [row for row in rows(data_dir, "wallet_entries")
              if int(row["amount_minor"]) < 0]
    assert len(debits) == 1 and debits[0]["wallet_id"] == "sc1"


def test_a_checkout_that_names_no_code_and_no_card_is_the_one_it_was(tmp_path, monkeypatch):
    """The regression that matters: a shop running none of this reads no
    promotions, no wallets, and gets the same response body it always
    did."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=TAXED)
    basket_of(objects_root, data_dir)

    placed = checkout(objects_root, customer_email="ada@example.com")
    assert placed["ok"] is True
    assert placed["total_cents"] == 11000
    assert placed["discount_cents"] == 0
    assert placed["credit_applied_cents"] == 0
    assert placed["amount_due_cents"] == placed["total_cents"]
    assert "promo_code" not in placed and "gift_card_code" not in placed
    assert rows(data_dir, "promotion_redemptions") == []
    assert rows(data_dir, "payments") == []
    lines = rows(data_dir, "invoice_lines")
    assert not [line for line in lines if "Discount" in line["description"]]


# --- issuing a card, and the code that names it -------------------------------

def test_buying_a_gift_card_credits_a_wallet_when_the_money_settles(tmp_path, monkeypatch):
    """An ordinary product sale whose fulfilment credits a wallet instead
    of putting something in a box."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "gc", "Gift Card $50", 5000, is_gift_card="true",
            product_type="digital")
    cart(objects_root, "add", product_id="gc", quantity="1")
    placed = checkout(objects_root, customer_email="ada@example.com")

    payment = {"id": "pay1", "invoice_id": placed["invoice_id"],
               "amount_cents": "5000", "method": "card",
               "received_on": TODAY, "status": "received", "owner_id": "shop"}
    object_records.create_collection_record("payments", payment,
                                             base_dir=data_dir)
    issued = run("system_gift_card_issue",
                 {"collection": "payments", "record": payment}, objects_root)
    assert issued["issued"] == 1
    card = issued["cards"][0]
    assert card["amount_minor"] == 5000
    assert card["code"].startswith("GC-")

    opened = object_records.get_collection_record("wallets", card["wallet_id"],
                                                   base_dir=data_dir)
    assert opened["kind"] == "gift_card"
    assert opened["balance_minor"] == "5000"

    # And a replayed payment event issues nothing twice: at-least-once
    # delivery must not be the shop giving away money.
    again = run("system_gift_card_issue",
                {"collection": "payments", "record": payment}, objects_root)
    assert again["issued"] == 0
    assert len([row for row in rows(data_dir, "wallets")
                if row["kind"] == "gift_card"]) == 1


def test_a_card_is_not_issued_before_the_bill_is_settled(tmp_path, monkeypatch):
    """A card is money. It is issued when the money is here, not when
    somebody has promised it."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    product(data_dir, "gc", "Gift Card $50", 5000, is_gift_card="true",
            product_type="digital")
    cart(objects_root, "add", product_id="gc", quantity="1")
    placed = checkout(objects_root, customer_email="ada@example.com")

    part = {"id": "pay1", "invoice_id": placed["invoice_id"],
            "amount_cents": "1000", "method": "card", "received_on": TODAY,
            "status": "received", "owner_id": "shop"}
    object_records.create_collection_record("payments", part, base_dir=data_dir)
    outcome = run("system_gift_card_issue",
                  {"collection": "payments", "record": part}, objects_root)
    assert "not settled" in outcome["skipped"]
    assert not [row for row in rows(data_dir, "wallets")
                if row["kind"] == "gift_card"]


def test_two_cards_cannot_share_a_code(tmp_path, monkeypatch):
    """A gift card is spent by whoever holds its code, so two cards with
    one code is one customer spending another's money."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    wallet(data_dir, wallet_id="gc1", code="GC-SAME")

    refusal = run("hook_wallets",
                  {"action": "create", "collection": "wallets",
                   "record": {"id": "gc2", "code": "gc-same",
                              "kind": "gift_card", "owner_id": "shop"}},
                  objects_root, method="BEFORE_WRITE")
    assert refusal["status"] == 409
    assert "already carries the code GC-SAME" in refusal["error"]

    # A wallet with no code collides with nothing, and there are many.
    assert run("hook_wallets",
               {"action": "create", "collection": "wallets",
                "record": {"id": "w9", "owner_id": "dan"}},
               objects_root, method="BEFORE_WRITE") is None


# --- the public quote ---------------------------------------------------------

def test_the_basket_page_can_ask_without_spending_the_code(tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    promotion(data_dir, max_redemptions=1)

    quote = run("action_check_promotion",
                {"code": "spring20", "subtotal_cents": "10000",
                 "today": TODAY}, objects_root)
    assert quote["applies"] is True
    assert quote["discount_cents"] == 2000
    assert rows(data_dir, "promotion_redemptions") == []

    refused = run("action_check_promotion",
                  {"code": "nope", "subtotal_cents": "10000", "today": TODAY},
                  objects_root)
    assert refused["status"] == 409
    assert refused["problems"] == ["There is no promotion with the code 'NOPE'."]
