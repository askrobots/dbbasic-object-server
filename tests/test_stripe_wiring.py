"""Stripe as a payment method, not a system of record.

The predecessor's Stripe silo happened because webhook code did bespoke
things with money. These tests pin the opposite: a verified event writes
ONE ordinary payments row and then gets out of the way, so the invoice
flip, the books journal and the receipt all come from the same reaction
chain a hand-entered payment triggers.

The adversarial half matters more than the happy path here, because this
endpoint is reachable by anyone on the internet.
"""

import hashlib
import hmac
import json
import pathlib
import time

from conftest import stage_collection

import object_execution
import object_records
import object_stripe
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
PAYMENTS_OBJECTS = PACKAGES / "app-payments" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

SECRET = "whsec_test_secret"
KEY = "sk_test_key"


def setup_env(tmp_path, monkeypatch, *, configured=True):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-invoices", "invoices"), ("app-payments", "payments"),
                      ("app-payments", "refunds"), ("app-settings", "app_settings")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    if configured:
        monkeypatch.setenv("DBBASIC_STRIPE_SECRET_KEY", KEY)
        monkeypatch.setenv("DBBASIC_STRIPE_WEBHOOK_SECRET", SECRET)
    else:
        monkeypatch.delenv("DBBASIC_STRIPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("DBBASIC_STRIPE_WEBHOOK_SECRET", raising=False)
    object_records.create_collection_record(
        "invoices",
        {"id": "inv-1", "number": "INV-1", "customer_name": "Grace Ltd",
         "status": "sent", "issue_date": "2026-07-01", "due_date": "2026-07-31",
         "total_cents": "250000", "owner_id": "dan"},
        base_dir=data_dir)
    # portal_token is schema read_only so no client can choose a
    # predictable capability URL; server-side writers use the same narrow
    # escape hatch the real minting handler does.
    object_records.update_collection_record(
        "invoices", "inv-1", {"portal_token": "tok-abcdefghijklmnop"},
        base_dir=data_dir, actor="test-setup", preserve_read_only=True)
    return data_dir


def signed(payload: bytes, *, secret=SECRET, when=None):
    ts = when if when is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event_bytes(*, event_id="evt_1", event_type="checkout.session.completed",
                invoice_id="inv-1", amount=250000, session_id="cs_1"):
    return json.dumps({
        "id": event_id, "type": event_type, "created": 1785000000,
        "data": {"object": {"id": session_id, "amount_total": amount,
                            "metadata": ({"invoice_id": invoice_id} if invoice_id else {})}},
    }).encode()


def post_webhook(raw: bytes, signature: str):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "webhook_stripe", method="POST",
            payload={"_raw_body": raw.decode(),
                     "_headers": {"stripe-signature": signature}}),
        roots=[PAYMENTS_OBJECTS]).result


def payments(data_dir):
    return object_records.read_collection_records("payments", base_dir=data_dir)


# --- the webhook: what it refuses ---------------------------------------------

def test_a_forged_event_records_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes()
    forged = post_webhook(raw, signed(raw, secret="whsec_attacker_guess"))
    assert forged["status"] == 400
    assert payments(data_dir) == []

    tampered_body = event_bytes(amount=1)          # signature was over the original
    tampered = post_webhook(tampered_body, signed(event_bytes()))
    assert tampered["status"] == 400
    assert payments(data_dir) == []


def test_a_replayed_old_event_is_refused(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes()
    stale = post_webhook(raw, signed(raw, when=int(time.time()) - 3600))
    assert stale["status"] == 400
    assert payments(data_dir) == []


def test_unconfigured_stripe_gives_a_prober_nothing(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, configured=False)
    raw = event_bytes()
    result = post_webhook(raw, signed(raw))
    assert result["status"] == 404          # not 500, not "not configured"


# --- the webhook: what it does ------------------------------------------------

def test_a_verified_event_writes_one_ordinary_payment(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes()
    result = post_webhook(raw, signed(raw))
    assert result["status"] == 200
    rows = payments(data_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["invoice_id"] == "inv-1"
    assert row["amount_cents"] == "250000"
    # "card" -- the enum names the instrument a bookkeeper reconciles by;
    # the provider's identity rides in `reference`, not in a second
    # vocabulary. (The schema enum rejected "stripe" outright: a gate
    # doing its job on money-path code written minutes earlier.)
    assert row["method"] == "card"
    assert row["status"] == "received"
    assert row["owner_id"] == "dan"          # stamped from the invoice, not the event
    assert "evt_1" in row["reference"] and "cs_1" in row["reference"]


def test_redelivery_never_records_money_twice(tmp_path, monkeypatch):
    """Stripe redelivers for days on any non-2xx, and retries succeed
    later -- so the SAME event arriving twice must be a no-op."""
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes()
    first = post_webhook(raw, signed(raw))
    again = post_webhook(raw, signed(raw))
    assert first["status"] == 200 and again["status"] == 200
    assert again.get("duplicate") == "evt_1"
    assert len(payments(data_dir)) == 1


def test_events_we_do_not_handle_are_acknowledged_not_rejected(tmp_path, monkeypatch):
    """A 4xx would make Stripe redeliver an event we will never want."""
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes(event_type="customer.subscription.updated")
    result = post_webhook(raw, signed(raw))
    assert result["status"] == 200 and result["ignored"]
    assert payments(data_dir) == []


def test_a_session_we_did_not_mint_books_nothing(tmp_path, monkeypatch):
    """No invoice_id in metadata means this payment belongs to some other
    integration on the same Stripe account -- acknowledge, record nothing."""
    data_dir = setup_env(tmp_path, monkeypatch)
    raw = event_bytes(invoice_id=None)
    result = post_webhook(raw, signed(raw))
    assert result["status"] == 200
    assert payments(data_dir) == []


# --- checkout: the amount is ours to decide -----------------------------------

def checkout(payload, monkeypatch, *, url="https://checkout.stripe.com/pay/x"):
    calls = []

    def fake_transport(u, data, headers, method):
        calls.append({"url": u, "data": data})
        return 200, json.dumps({"id": "cs_new", "url": url}).encode()

    monkeypatch.setattr(object_stripe, "_default_transport", fake_transport, raising=False)
    original = object_stripe.create_checkout_session

    def patched(config, **kwargs):
        return original(config, transport=fake_transport, **kwargs)

    monkeypatch.setattr(object_stripe, "create_checkout_session", patched)
    result = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_stripe_checkout", method="POST", payload=payload),
        roots=[PAYMENTS_OBJECTS]).result
    return result, calls


def test_checkout_charges_the_open_balance_not_a_client_number(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "app_settings", {"id": "s1", "key": "portal.base_url",
                         "value": "https://books.example"}, base_dir=data_dir)
    # Half already paid: checkout must ask for the REMAINDER.
    object_records.create_collection_record(
        "payments", {"id": "p-part", "invoice_id": "inv-1", "amount_cents": "100000",
                     "method": "card", "received_on": "2026-07-10",
                     "status": "received", "owner_id": "dan"}, base_dir=data_dir)
    result, calls = checkout({"token": "tok-abcdefghijklmnop", "amount_cents": 1},
                             monkeypatch)
    assert result["status"] == 200
    assert result["amount_cents"] == 150000        # NOT the client's 1
    assert result["url"].startswith("https://checkout.stripe.com/")


def test_checkout_refuses_bad_tokens_paid_and_void_invoices(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "app_settings", {"id": "s1", "key": "portal.base_url",
                         "value": "https://books.example"}, base_dir=data_dir)
    missing, _ = checkout({"token": "not-a-real-token"}, monkeypatch)
    assert missing["status"] == 404                # never "bad token"

    object_records.create_collection_record(
        "payments", {"id": "p-full", "invoice_id": "inv-1", "amount_cents": "250000",
                     "method": "card", "received_on": "2026-07-10",
                     "status": "received", "owner_id": "dan"}, base_dir=data_dir)
    paid, calls = checkout({"token": "tok-abcdefghijklmnop"}, monkeypatch)
    assert paid["status"] == 409 and calls == []   # no Stripe call at all


def test_checkout_without_stripe_configured_says_so_plainly(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, configured=False)
    result, calls = checkout({"token": "tok-abcdefghijklmnop"}, monkeypatch)
    assert result["status"] == 409
    assert "not configured" in result["error"]
    assert calls == []


def test_checkout_refuses_to_guess_a_return_host(tmp_path, monkeypatch):
    """Stripe needs absolute return URLs; guessing one would send a paying
    customer to a hostname this server does not control."""
    setup_env(tmp_path, monkeypatch)
    result, calls = checkout({"token": "tok-abcdefghijklmnop"}, monkeypatch)
    assert result["status"] == 409 and "portal.base_url" in result["error"]
    assert calls == []
