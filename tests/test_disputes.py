"""Disputes: a customer said something went wrong, and what we did about
it has to be checkable.

Every property in this file is about one failure, and it is the failure
nobody inside the shop can see: a dispute marked `resolved` with nothing
behind it. The row looks finished, the queue count goes down, the badge
turns green, and the only person who knows the money never moved is the
customer still waiting. So the gate is tested from both doors -- the
generic write path a form uses (hook_disputes) and the action that also
composes the compensating record -- because trusted server-side writes
bypass hooks by design, and a rule that only holds on one of the two
doors is a rule with a door left open.

The three that cost real money if they are wrong. A refund defers to
app-payments' existing ceiling and surfaces that gate's refusal in its
own words, so a customer is quoted the same number whichever door their
claim came through. A credit refuses honestly on a server with no wallet
app rather than closing the dispute as though credit had been issued --
"we cannot do that here" is a fine answer and "sorted!" is not. And a
dispute resolved twice refunds once: retries, double clicks and replayed
queue entries are ordinary, and a customer's money going back twice is
not.
"""

import csv
import json
import pathlib
import shutil

from conftest import stage_collection

import object_execution
import object_packages
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
APP_DISPUTES_DIR = PACKAGES / "app-disputes"
DISPUTES_OBJECTS = APP_DISPUTES_DIR / "objects"
PAYMENTS_OBJECTS = PACKAGES / "app-payments" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, wallets=True):
    """A data dir with every collection this slice touches, and ONE object
    root holding both packages' objects.

    The merged root is not a convenience: action_resolve_dispute asks
    app-payments' hook_refunds what a payment can give back rather than
    deciding for itself, and asks its own hook_disputes what a resolution
    must name, so both have to resolve by id the way they will in
    production.

    `wallets=False` stages a server with no app-billing at all -- no
    wallets, no wallet_entries -- which is the ordinary case for a shop
    that does not sell store credit and the case the credit path has to
    refuse honestly in.
    """
    data_dir = tmp_path / "data"
    staged = [("app-disputes", "disputes"),
              ("app-orders", "orders"), ("app-orders", "order_lines"),
              ("app-catalog", "products"),
              ("app-shipping", "shipments"),
              ("app-invoices", "invoices"),
              ("app-payments", "payments"), ("app-payments", "refunds")]
    if wallets:
        staged += [("app-billing", "wallets"), ("app-billing", "wallet_entries")]
    for pkg, name in staged:
        stage_collection(data_dir, pkg, name)

    objects_root = tmp_path / "objects"
    for source in (DISPUTES_OBJECTS, PAYMENTS_OBJECTS):
        shutil.copytree(source, objects_root, dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload=payload),
        roots=[objects_root]).result


# --- fixtures shaped like a real shop -----------------------------------------

def product(data_dir, product_id="p1", name="Enamel Mug", cents=1200):
    return object_records.create_collection_record(
        "products",
        {"id": product_id, "name": name, "sku": product_id.upper(),
         "product_type": "physical", "price_cents": str(cents),
         "currency": "USD", "is_active": "true", "owner_id": "shop"},
        base_dir=data_dir)


def order(data_dir, order_id="ord-1", *, status="delivered", number="SO-0001",
          owner="shop", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": number,
              "customer_name": "Ada Lovelace",
              "customer_email": "ada@example.test", "currency": "USD",
              "status": "draft", "order_date": "2026-07-01", "owner_id": owner}
    record.update({k: str(v) for k, v in fields.items()})
    object_records.create_collection_record("orders", record,
                                            base_dir=data_dir)
    for step in ("confirmed", "shipped", status):
        current = object_records.get_collection_record(
            "orders", order_id, base_dir=data_dir)["status"]
        if step not in ("draft", current):
            object_records.update_collection_record(
                "orders", order_id, {"status": step},
                base_dir=data_dir, actor="test")
        if step == status:
            break
    return object_records.get_collection_record("orders", order_id,
                                                base_dir=data_dir)


def order_line(data_dir, line_id, order_id="ord-1", *, product_id="p1",
               description="Enamel Mug", quantity="3", cents=1200):
    total = int(float(quantity) * cents)
    return object_records.create_collection_record(
        "order_lines",
        {"id": line_id, "order_id": order_id, "product_id": product_id,
         "description": description, "quantity": str(quantity),
         "unit_price_cents": str(cents), "line_total_cents": str(total),
         "tax_rate_bps": "0", "line_tax_cents": "0", "owner_id": "shop"},
        base_dir=data_dir)


def paid(data_dir, *, cents=3600, order_id="ord-1", invoice_id="inv-1"):
    """An order paid for the way app-shop's checkout leaves it."""
    object_records.create_collection_record(
        "invoices",
        {"id": invoice_id, "number": "SO-0001", "customer_name": "Ada Lovelace",
         "customer_email": "ada@example.test", "status": "sent",
         "issue_date": "2026-07-01", "due_date": "2026-07-15",
         "subtotal_cents": str(cents), "total_cents": str(cents),
         "owner_id": "shop"},
        base_dir=data_dir)
    object_records.update_collection_record(
        "orders", order_id, {"invoice_id": invoice_id},
        base_dir=data_dir, actor="test")
    return object_records.create_collection_record(
        "payments",
        {"id": "pay-1", "invoice_id": invoice_id, "amount_cents": str(cents),
         "method": "card", "received_on": "2026-07-02", "status": "received",
         "owner_id": "shop"},
        base_dir=data_dir)


def dispute(data_dir, dispute_id="dis-1", *, kind="delivery", status="open",
            **fields):
    record = {"id": dispute_id, "kind": kind, "order_id": "ord-1",
              "customer_email": "ada@example.test",
              "summary": "Parcel never arrived", "status": status,
              "opened_by": "shop", "owner_id": "shop"}
    record.update({k: str(v) for k, v in fields.items()})
    return object_records.create_collection_record("disputes", record,
                                                   base_dir=data_dir)


def wallet(data_dir, wallet_id="wal-1", owner="ada@example.test"):
    return object_records.create_collection_record(
        "wallets",
        # balance_minor is a rollup over the entries and refuses to be
        # written, which is the point of app-billing's ledger: the entries
        # ARE the balance.
        {"id": wallet_id, "owner_id": owner, "is_active": "true"},
        base_dir=data_dir)


def disputes(data_dir):
    return object_records.read_collection_records("disputes", base_dir=data_dir)


def refunds(data_dir):
    return object_records.read_collection_records("refunds", base_dir=data_dir)


def orders(data_dir):
    return object_records.read_collection_records("orders", base_dir=data_dir)


def entries(data_dir):
    return object_records.read_collection_records("wallet_entries",
                                                  base_dir=data_dir)


def gate(objects_root, record, action="update"):
    """The generic HTTP write path's door: what a form posting straight at
    the collection would hit."""
    return run(objects_root, "hook_disputes", "BEFORE_WRITE",
               {"action": action, "collection": "disputes", "record": record})


def resolve(objects_root, **payload):
    return run(objects_root, "action_resolve_dispute", "POST", payload)


def shop_with_a_dispute(tmp_path, monkeypatch, *, cents=3600, wallets=True,
                        **dispute_fields):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, wallets=wallets)
    product(data_dir)
    order(data_dir)
    order_line(data_dir, "line-1")
    paid(data_dir, cents=cents)
    dispute(data_dir, **dispute_fields)
    return data_dir, objects_root


# --- the gate: a dispute cannot be resolved without a resolution ----------------

def test_resolving_with_no_resolution_at_all_is_refused(tmp_path, monkeypatch):
    """The load-bearing rule, at the door a form would come through. A
    dispute closed with nothing behind it is how a customer gets told it
    is sorted when nothing happened."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "summary": "Parcel never arrived"})
    assert refused["status"] == 400
    assert "dis-1" in refused["error"]
    assert "resolution_kind" in refused["error"]
    assert "told it is sorted when nothing happened" in refused["error"]


def test_a_dispute_that_is_not_being_resolved_is_none_of_the_gates_business(
        tmp_path, monkeypatch):
    """The gate is on the ENDING. Every other status is somebody's work in
    progress, and a hook with opinions about all of them would be a hook
    people route around."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    for status in ("open", "investigating", "withdrawn"):
        assert gate(objects_root, {"id": "dis-1", "status": status}) is None


def test_no_action_needs_a_reason(tmp_path, monkeypatch):
    """A legitimate ending -- plenty of claims are not ours to compensate --
    but a bare no_action is indistinguishable from a queue somebody
    cleared to make the number go down."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "no_action"})
    assert refused["status"] == 400
    assert "no reason" in refused["error"]
    assert "resolution_note" in refused["error"]

    allowed = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "no_action",
                                  "resolution_note": "Claimed 40 days after "
                                                     "delivery, outside the "
                                                     "30-day window."})
    assert allowed is None


def test_a_compensating_resolution_must_name_a_reference(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    for kind, collection in (("refund", "refunds"),
                             ("replacement", "orders"),
                             ("credit", "wallet_entries")):
        refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                      "resolution_kind": kind})
        assert refused["status"] == 400, kind
        assert f"{collection}/{{id}}" in refused["error"], kind


def test_a_reference_into_the_wrong_collection_is_refused_by_name(
        tmp_path, monkeypatch):
    """refunds hold money that went back, orders hold goods that will go
    out, wallet_entries hold credit somebody can spend. A refund pointing
    at an order is a story about something that did not happen."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "refund",
                                  "resolution_ref": "orders/ord-1"})
    assert refused["status"] == 409
    assert "says refund but points at orders/ord-1" in refused["error"]
    assert "A refund resolution lives in refunds" in refused["error"]


def test_a_reference_that_resolves_to_nothing_is_refused(tmp_path, monkeypatch):
    """A plausible-looking pointer at nothing is strictly worse than a
    blank one: a blank field is visibly unfinished and 'refunds/abc123'
    reads as done."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "refund",
                                  "resolution_ref": "refunds/never-existed"})
    assert refused["status"] == 409
    assert "names no record in refunds" in refused["error"]


def test_a_reference_that_resolves_is_allowed_through(tmp_path, monkeypatch):
    """The gate has to say yes to a real one, or it is not a gate, it is a
    wall."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "refunds",
        {"id": "ref-1", "payment_id": "pay-1", "invoice_id": "inv-1",
         "amount_cents": "1200", "refunded_on": "2026-07-20",
         "owner_id": "shop"},
        base_dir=data_dir)
    assert gate(objects_root, {"id": "dis-1", "status": "resolved",
                               "resolution_kind": "refund",
                               "resolution_ref": "refunds/ref-1"}) is None


def test_a_bare_id_is_not_a_reference(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "refund",
                                  "resolution_ref": "ref-1"})
    assert refused["status"] == 400
    assert "is not a reference" in refused["error"]


def test_the_credit_gate_names_the_missing_app_rather_than_shrugging(
        tmp_path, monkeypatch):
    """On a server with no app-billing there is no wallet_entries
    collection at all, and 'credit' is not a resolution this shop can
    carry out. Saying which app is missing is the difference between a
    refusal somebody can act on and one they can only be annoyed by."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch,
                                                 wallets=False)
    refused = gate(objects_root, {"id": "dis-1", "status": "resolved",
                                  "resolution_kind": "credit",
                                  "resolution_ref": "wallet_entries/x"})
    assert refused["status"] == 409
    assert "no wallet_entries collection on this server" in refused["error"]
    assert "app-billing" in refused["error"]


# --- the action: the one place a dispute and its compensation compose ----------

def test_resolving_with_a_refund_writes_the_refund_and_closes_the_dispute(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    result = resolve(objects_root, dispute_id="dis-1",
                     resolution_kind="refund", amount_cents="1200",
                     note="Carrier lost it", today="2026-07-20")
    assert result["ok"] is True
    assert result["resolution_kind"] == "refund"
    assert result["refund_id"]
    assert result["resolution_ref"] == f"refunds/{result['refund_id']}"

    row = refunds(data_dir)[0]
    assert row["amount_cents"] == "1200"
    assert row["payment_id"] == "pay-1"
    # Stamped by hook_refunds from the payment, never trusted from us --
    # which is the proof the refund really went through that gate.
    assert row["invoice_id"] == "inv-1"
    assert "disputes/dis-1" in row["reason"]

    closed = object_records.get_collection_record("disputes", "dis-1",
                                                  base_dir=data_dir)
    assert closed["status"] == "resolved"
    assert closed["resolution_ref"] == result["resolution_ref"]
    assert closed["resolution_note"] == "Carrier lost it"
    assert closed["resolved_at"]


def test_a_refund_resolution_defers_to_the_existing_ceiling_and_says_its_words(
        tmp_path, monkeypatch):
    """The ceiling is not reimplemented here: app-payments' hook already
    knows what a payment can give back, and its words are what surfaces --
    so a customer is quoted the same number whether they came through a
    return or a dispute."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="refund", amount_cents="99999")
    assert refused["status"] == 409
    assert "exceeds the refundable" in refused["error"]
    assert "3600" in refused["error"]              # what the payment can give
    assert "pay-1" in refused["error"]

    # And nothing happened: no refund, and the dispute is still open for
    # somebody to get right.
    assert refunds(data_dir) == []
    assert object_records.get_collection_record(
        "disputes", "dis-1", base_dir=data_dir)["status"] == "open"


def test_resolving_twice_refunds_once(tmp_path, monkeypatch):
    """The most important test in the slice. Retries, double clicks and
    replayed queue entries are ordinary; a customer's money going back
    twice is not."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    first = resolve(objects_root, dispute_id="dis-1",
                    resolution_kind="refund", amount_cents="1200")
    assert first["ok"] and first["refund_id"]
    assert len(refunds(data_dir)) == 1

    again = resolve(objects_root, dispute_id="dis-1",
                    resolution_kind="refund", amount_cents="1200")
    assert again["ok"] is True
    assert "already resolved" in again["note"]
    assert again["refund_id"] == ""
    assert again["resolution_ref"] == first["resolution_ref"]
    assert len(refunds(data_dir)) == 1

    # Even asked for a different ending, and even for one that would have
    # composed a different record: the dispute is over.
    third = resolve(objects_root, dispute_id="dis-1",
                    resolution_kind="replacement")
    assert third["ok"] is True and "already resolved" in third["note"]
    assert len(refunds(data_dir)) == 1
    assert len(orders(data_dir)) == 1


def test_no_action_through_the_action_still_needs_a_reason(tmp_path, monkeypatch):
    """The action does not restate the rule -- it asks hook_disputes -- so
    the refusal a caller sees here is word for word the one the generic
    write path gives."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="no_action")
    assert refused["status"] == 400
    assert "resolution_note" in refused["error"]
    assert object_records.get_collection_record(
        "disputes", "dis-1", base_dir=data_dir)["status"] == "open"

    done = resolve(objects_root, dispute_id="dis-1",
                   resolution_kind="no_action",
                   note="Tracking shows delivered and signed for by the "
                        "customer's neighbour.")
    assert done["ok"] is True
    assert done["resolution_ref"] == ""
    closed = object_records.get_collection_record("disputes", "dis-1",
                                                  base_dir=data_dir)
    assert closed["status"] == "resolved"
    assert closed["resolution_kind"] == "no_action"
    assert "neighbour" in closed["resolution_note"]
    assert refunds(data_dir) == []


def test_a_credit_with_no_wallet_app_refuses_honestly(tmp_path, monkeypatch):
    """Store credit is a wallet entry and wallets are app-billing's, which
    is deliberately not a dependency of this package. A shop that does not
    sell credit should hear 'we cannot do that here', not have a dispute
    closed as though credit had been issued."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch,
                                                 wallets=False)
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="credit", amount_cents="1200")
    assert refused["status"] == 409
    assert refused["missing_app"] == "app-billing"
    assert "app-billing" in refused["error"]
    assert "not installed" in refused["error"]
    assert object_records.get_collection_record(
        "disputes", "dis-1", base_dir=data_dir)["status"] == "open"


def test_a_credit_with_a_wallet_writes_one_entry_the_customer_can_spend(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    wallet(data_dir)
    result = resolve(objects_root, dispute_id="dis-1",
                     resolution_kind="credit", amount_cents="500",
                     note="Goodwill for the delay")
    assert result["ok"] is True
    assert result["resolution_ref"] == f"wallet_entries/{result['wallet_entry_id']}"

    row = entries(data_dir)[0]
    assert row["wallet_id"] == "wal-1"
    # Positive: credit is money going TO the customer, and the sign
    # convention is the ledger's rather than ours.
    assert row["amount_minor"] == "500"
    assert row["generated_from"] == "disputes/dis-1"
    assert refunds(data_dir) == []


def test_a_credit_with_nowhere_to_land_refuses_rather_than_inventing_a_wallet(
        tmp_path, monkeypatch):
    """A shop that sells to guests has no wallet for most of its
    customers, and a credit issued into a wallet nobody owns is a number
    that never becomes money."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="credit", amount_cents="500")
    assert refused["status"] == 409
    assert refused["missing_wallet"] == "no_wallet_for_customer"
    assert entries(data_dir) == []


def test_a_replacement_raises_a_confirmed_no_charge_order_that_references_the_original(
        tmp_path, monkeypatch):
    """Confirmed, not draft: the order schema's own words are that a draft
    is not a commitment, and a resolution pointing at a document that
    promises nothing is exactly what the gate exists to prevent. Zero
    price, because the customer already paid on the order being argued
    about."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    result = resolve(objects_root, dispute_id="dis-1",
                     resolution_kind="replacement", today="2026-07-20")
    assert result["ok"] is True
    replacement = object_records.get_collection_record(
        "orders", result["order_id"], base_dir=data_dir)

    assert replacement["doc_type"] == "sale"
    assert replacement["number"] == "SO-0001-R"
    assert replacement["status"] == "confirmed"
    assert replacement["customer_email"] == "ada@example.test"
    assert "disputes/dis-1" in replacement["notes"]
    assert "orders/ord-1" in replacement["notes"]
    # NOT linked_order_id: that field means the drop-ship counterpart and
    # nothing else, because action_dropship_order's stock rule reads it.
    assert replacement["linked_order_id"] == ""

    lines = [row for row in object_records.read_collection_records(
        "order_lines", base_dir=data_dir)
        if row["order_id"] == result["order_id"]]
    assert len(lines) == 1
    assert lines[0]["description"] == "Enamel Mug"
    assert lines[0]["quantity"] == "3"
    assert lines[0]["unit_price_cents"] == "0"
    assert lines[0]["line_total_cents"] == "0"

    closed = object_records.get_collection_record("disputes", "dis-1",
                                                  base_dir=data_dir)
    assert closed["resolution_ref"] == f"orders/{result['order_id']}"


def test_a_withdrawn_dispute_is_not_something_to_compensate(tmp_path,
                                                            monkeypatch):
    """The customer dropped it. Paying out on a claim nobody is making is
    a payment with no argument behind it -- and filing withdrawals under
    resolved would salt every 'what did we pay out' report with claims
    that cost nothing."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch,
                                                 status="withdrawn")
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="refund", amount_cents="1200")
    assert refused["status"] == 409
    assert "withdrawn" in refused["error"]
    assert refunds(data_dir) == []


def test_an_unknown_resolution_kind_is_refused_before_anything_is_written(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = resolve(objects_root, dispute_id="dis-1",
                      resolution_kind="apology", amount_cents="1200")
    assert refused["status"] == 400
    assert "Unknown resolution_kind" in refused["error"]
    assert refunds(data_dir) == []


def test_an_unknown_dispute_is_a_friendly_404(tmp_path, monkeypatch):
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    refused = resolve(objects_root, dispute_id="nope",
                      resolution_kind="no_action", note="x")
    assert refused["status"] == 404
    assert "No such dispute" in refused["error"]


# --- the queue and the door ----------------------------------------------------

def test_open_disputes_surface_as_attention_and_investigating_ones_do_not(
        tmp_path, monkeypatch):
    """The number exists to find the claims nobody has picked up. A count
    that also included work in progress could never reach zero, which is
    how a badge stops meaning anything."""
    data_dir, objects_root = shop_with_a_dispute(tmp_path, monkeypatch)
    dispute(data_dir, "dis-2", kind="payment", summary="Charged twice")
    dispute(data_dir, "dis-3", kind="product", summary="Wrong size",
            status="open")
    object_records.update_collection_record(
        "disputes", "dis-3", {"status": "investigating"},
        base_dir=data_dir, actor="test")

    counted = run(objects_root, "system_dispute_attention", "COUNT", {})
    assert counted["count"] == 2

    resolve(objects_root, dispute_id="dis-1", resolution_kind="no_action",
            note="Delivered and signed for.")
    assert run(objects_root, "system_dispute_attention", "COUNT",
               {})["count"] == 1


def test_the_count_has_no_opinion_on_a_server_with_no_disputes_collection(
        tmp_path, monkeypatch):
    """A missing collection reads as zero -- a server with no disputes app
    has no disputes -- rather than raising and parking a stale number in
    the rollup."""
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-orders", "orders")
    objects_root = tmp_path / "objects"
    shutil.copytree(DISPUTES_OBJECTS, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert run(objects_root, "system_dispute_attention", "COUNT",
               {}) == {"count": 0}


def _seed_rows(name):
    with open(APP_DISPUTES_DIR / "seed" / f"{name}.tsv", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_the_bench_lists_the_collection_the_count_actually_reads():
    """A badge that disagrees with the page it opens is worse than no
    badge. The provider counts disputes, so the index is over disputes."""
    provider = (DISPUTES_OBJECTS / "system" / "dispute_attention.py").read_text()
    assert 'object_records.read_collection_records("disputes"' in provider

    view = next(row for row in _seed_rows("views")
                if row["id"] == "view_disputes_bench")
    assert view["route"] == "/disputes"
    blocks = json.loads(view["blocks"])
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "list"
    assert blocks[0]["collection"] == "disputes"


def test_the_bench_shows_the_whole_ladder_not_only_the_untouched():
    """`open` is what the badge counts, and the bench deliberately shows
    more: somebody standing at it needs to see what is in flight and what
    was settled yesterday. The narrowing is one pick in the filter bar the
    schema already declares."""
    view = next(row for row in _seed_rows("views")
                if row["id"] == "view_disputes_bench")
    assert "where" not in json.loads(view["blocks"])[0]

    schema = json.loads(
        (APP_DISPUTES_DIR / "schemas" / "disputes.json").read_text())
    assert "status" in schema["views"]["filter_fields"]


def test_the_count_opens_a_door_this_package_actually_serves():
    """The sweep in tests/test_attention_paths.py holds this for every
    package; asserted here too because a count nobody can open is a
    notification, and this is the package that just added one."""
    package = object_packages.get_package("app-disputes", root=PACKAGES)
    source = package["attention"][0]
    assert source["path"] == "/disputes"
    assert "?" not in source["path"]

    route = next(row for row in _seed_rows("site_routes")
                 if row["pattern"] == "/disputes")
    assert route["object_id"] == "site_view_render"

    door = package["nav"][0]
    assert door["path"] == "/disputes"
    assert door["group"] == "Commerce"


def test_the_gate_is_declared_on_the_collection_not_only_written():
    """A hook object nobody wired up is a docstring. The schema's
    `hooks.before_write` is what makes the generic write path run it."""
    schema = json.loads(
        (APP_DISPUTES_DIR / "schemas" / "disputes.json").read_text())
    assert schema["hooks"]["before_write"] == "hook_disputes"


def test_the_review_freeze_is_named_as_a_decision_rather_than_forgotten():
    """plan/fulfillment-logistics-spec.md wants a disputed order's review
    window frozen. There is no reviews system on this server, so the field
    would be a column of blanks -- the doctrine is written down instead of
    guessed at in code, and this test is what stops that being an
    accident."""
    schema = json.loads(
        (APP_DISPUTES_DIR / "schemas" / "disputes.json").read_text())
    assert "review" in schema["description"].lower()
    assert "no reviews system exists" in schema["description"]
