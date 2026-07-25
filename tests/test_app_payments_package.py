"""app-payments (plan/payments-spec.md): the first ERP-tail module and the
deliberate stress test of the whole primitive stack — append-only money
movements, hook-enforced invariants (overpayment gate, refund <= refundable,
invoice_id stamped from the payment), and rollup/formula-derived paid and
balance amounts on invoices.

Placement per docs/business-logic-patterns.md: gates in hooks, derived
positions in rollups/formulas, policy in app_settings, corrections as
compensating records.
"""

import json
import pathlib

import object_records
import object_server
from conftest import schema_header, stage_collection
from test_object_server import (
    TEST_ADMIN_TOKEN,
    enable_admin_token,
    request,
)

AUTH = [("authorization", f"Token {TEST_ADMIN_TOKEN}")]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"


def setup_env(tmp_path, monkeypatch, *, overpayment_policy=None):
    data_dir = tmp_path / "data"

    # inv1 is seeded straight into the TSV (not via create_collection_record)
    # so it starts life already "sent" with no payments -- pin the row to
    # the real schema's field order so a later schema edit can't silently
    # shift these values into the wrong columns.
    invoice_fields = schema_header("app-invoices", "invoices").strip("\n").split("\t")
    invoice_values = {"id": "inv1", "number": "INV-1", "customer_name": "Acme",
                       "status": "sent", "total_cents": "10000", "owner_id": "admin"}
    invoice_row = "\t".join(invoice_values.get(f, "") for f in invoice_fields) + "\n"
    stage_collection(data_dir, "app-invoices", "invoices", rows=invoice_row)

    stage_collection(data_dir, "app-payments", "payments")
    stage_collection(data_dir, "app-payments", "refunds")

    settings_rows = ""
    if overpayment_policy:
        settings_rows = f"s1\tpayments.overpayment_policy\t{overpayment_policy}\t\n"
    stage_collection(data_dir, "app-settings", "app_settings", rows=settings_rows)

    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(PACKAGES / "app-payments" / "objects"))
    enable_admin_token(monkeypatch)
    return data_dir


def pay(record):
    # admin-token writes don't session-stamp owner_id; set it so the
    # owner-guarded received->bounced transition works in tests.
    body = {"method": "transfer", "received_on": "2026-07-24", "owner_id": "admin", **record}
    return request(
        "/collections/payments/records",
        method="POST",
        body=json.dumps(body).encode("utf-8"),
        headers=AUTH,
    )


def refund(record):
    body = {"refunded_on": "2026-07-24", **record}
    return request(
        "/collections/refunds/records",
        method="POST",
        body=json.dumps(body).encode("utf-8"),
        headers=AUTH,
    )


def invoice(data_dir):
    return object_records.get_collection_record("invoices", "inv1", base_dir=data_dir)


def test_payment_rolls_up_paid_and_balance_on_the_invoice(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    status, _, payload = pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "4000"})
    assert status in (200, 201), payload
    row = invoice(data_dir)
    assert row["payments_received_cents"] == "4000"
    assert row["amount_paid_cents"] == "4000"
    assert row["balance_due_cents"] == "6000"


def test_bounced_payment_leaves_the_rollup(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "4000"})
    status, _, _ = request(
        "/collections/payments/records/p1",
        method="PUT",
        body=json.dumps({"status": "bounced"}).encode("utf-8"),
        headers=AUTH,
    )
    assert status == 200
    row = invoice(data_dir)
    assert row["payments_received_cents"] == "0"
    assert row["balance_due_cents"] == "10000"


def test_overpayment_rejected_by_default_allowed_by_setting(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "9000"})
    status, _, payload = pay({"id": "p2", "invoice_id": "inv1", "amount_cents": "2000"})
    assert status == 409
    assert payload["code"] == "hook_rejected"
    assert "remaining balance of 1000" in payload["error"]
    assert invoice(data_dir)["payments_received_cents"] == "9000"

    # Exactly the remaining balance is fine.
    status, _, _ = pay({"id": "p3", "invoice_id": "inv1", "amount_cents": "1000"})
    assert status in (200, 201)
    assert invoice(data_dir)["balance_due_cents"] == "0"


def test_overpayment_allowed_when_policy_says_allow(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, overpayment_policy="allow")
    status, _, _ = pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "15000"})
    assert status in (200, 201)
    assert invoice(data_dir)["balance_due_cents"] == "-5000"


def test_nonpositive_amounts_rejected(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    status, _, payload = pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "0"})
    assert status == 400 and payload["code"] == "hook_rejected"
    pay({"id": "p2", "invoice_id": "inv1", "amount_cents": "1000"})
    status, _, payload = refund({"id": "r1", "payment_id": "p2", "amount_cents": "-5"})
    assert status == 400 and payload["code"] == "hook_rejected"


def test_refund_invariant_and_stamp(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "5000"})

    # Client-supplied invoice_id is overridden by the stamp from the payment.
    status, _, payload = refund(
        {"id": "r1", "payment_id": "p1", "amount_cents": "2000", "invoice_id": "WRONG"}
    )
    assert status in (200, 201), payload
    stored = object_records.get_collection_record("refunds", "r1", base_dir=data_dir)
    assert stored["invoice_id"] == "inv1"

    # Rollups: payment.refunded_cents and invoice paid/balance reflect it.
    payment = object_records.get_collection_record("payments", "p1", base_dir=data_dir)
    assert payment["refunded_cents"] == "2000"
    row = invoice(data_dir)
    assert row["refunded_cents"] == "2000"
    assert row["amount_paid_cents"] == "3000"
    assert row["balance_due_cents"] == "7000"

    # Over-refunding what remains is rejected with the refundable amount.
    status, _, payload = refund({"id": "r2", "payment_id": "p1", "amount_cents": "3500"})
    assert status == 409
    assert "refundable 3000" in payload["error"]

    # Exactly the remainder is fine; the chain zeroes out.
    status, _, _ = refund({"id": "r3", "payment_id": "p1", "amount_cents": "3000"})
    assert status in (200, 201)
    assert invoice(data_dir)["amount_paid_cents"] == "0"


def test_cannot_refund_a_bounced_payment(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    pay({"id": "p1", "invoice_id": "inv1", "amount_cents": "1000"})
    request(
        "/collections/payments/records/p1",
        method="PUT",
        body=json.dumps({"status": "bounced"}).encode("utf-8"),
        headers=AUTH,
    )
    status, _, payload = refund({"id": "r1", "payment_id": "p1", "amount_cents": "500"})
    assert status == 409
    assert "bounced" in payload["error"]


def test_package_manifest_shape():
    import object_packages

    package = object_packages.get_package("app-payments", root=PACKAGES)
    assert package["id"] == "app-payments"
    assert {s["collection"] for s in package["schemas"]} == {"payments", "refunds"}
    assert {o["id"] for o in package["objects"]} == {
        "site_payments", "hook_payments", "hook_refunds",
        "system_books",  # journal composer (books spine)
        "system_invoice_status",  # event-driven paid/partial flips (slice 2)
        # 0.2.0: Stripe as a payment METHOD -- the webhook writes an ordinary
        # payments row and the existing reaction chain does the rest.
        "webhook_stripe", "action_stripe_checkout",
    }
    assert "app-invoices" in {d["id"] for d in package["dependencies"]}
    for schema_name in ("payments", "refunds"):
        schema = json.loads(
            (PACKAGES / "app-payments" / "schemas" / f"{schema_name}.json").read_text()
        )
        assert schema["storage"] == "append"  # money moves, never mutates
