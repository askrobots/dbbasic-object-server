"""Customer payment portal (plan/customer-payment-portal-spec.md): the door
a dunning email currently has no way to point at.

Fixture style mirrors tests/test_payments_slice2.py -- direct
object_execution.execute_object calls against the real package objects,
schemas copied straight from packages/app-invoices/schemas/*.json (so a
future schema edit can't silently drift out of sync with what this test
exercises), and app-payments' own system_invoice_status object reused
(read-only, never edited here) to produce a realistic partial/paid state
the same way a live deployment would, rather than hand-faking the
computed amount_paid_cents/balance_due_cents fields.

Security posture under test, per the spec's own sketch: portal_token
never surfaces in a list/search response for a non-owner (guaranteed by
the pre-existing row_filter on the invoices collection, unrelated to
anything this feature adds -- proven directly against
packages/app-invoices/permissions/rules.json); the portal page 404s on a
bad or blank token, never 403 (never confirm the token space exists);
void invoices refuse the portal's financial content; and regenerating a
link invalidates whatever token was live before.
"""

import json
import pathlib

import object_execution
import object_permissions
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
APP_INVOICES = PACKAGES / "app-invoices"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch, *, settings=(), with_invoice_lines=False):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)

    schemas = [("app-invoices", "invoices"), ("app-payments", "payments"),
               ("app-payments", "refunds"), ("app-settings", "app_settings"),
               ("app-email", "email_outbox")]
    if with_invoice_lines:
        schemas.append(("app-invoices", "invoice_lines"))
    for pkg, name in schemas:
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())

    def coll(name, header):
        d = data_dir / "collections" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "records.tsv").write_text(header)

    # The real seed header -- kept in sync with the schema by construction,
    # rather than a second hand-typed field list that could drift.
    invoices_header = (APP_INVOICES / "seed" / "invoices.tsv").read_text().splitlines()[0] + "\n"
    coll("invoices", invoices_header)
    coll("payments",
         "id\tinvoice_id\tamount_cents\tmethod\treceived_on\treference\tnotes"
         "\tstatus\trefunded_cents\towner_id\tcreated_at\n")
    coll("refunds",
         "id\tpayment_id\tinvoice_id\tamount_cents\treason\trefunded_on\towner_id\tcreated_at\n")
    coll("email_outbox",
         "id\tto\tfrom_addr\treply_to\tsubject\ttext_body\thtml_body\tstatus"
         "\tattempts\tmax_attempts\tlast_error\tnext_attempt_at\tcreated_at"
         "\tupdated_at\tsent_at\tsource_object_id\textra\n")
    if with_invoice_lines:
        coll("invoice_lines",
             "id\tinvoice_id\tdescription\tquantity\tunit_price_cents"
             "\tline_total_cents\ttax_rate_bps\tline_tax_cents\towner_id\tcreated_at\n")
    rows = "id\tkey\tvalue\tdescription\n"
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    coll("app_settings", rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def make_invoice(data_dir, iid="inv1", status="sent", total="10000",
                 due="2026-07-10", email="grace@acme.test", owner="dan", **extra):
    rec = {"id": iid, "number": f"N-{iid}", "customer_name": "Grace Hopper",
           "customer_email": email, "status": status, "due_date": due,
           "issue_date": "2026-06-10", "total_cents": total, "owner_id": owner}
    rec.update(extra)
    return object_records.create_collection_record("invoices", rec, base_dir=data_dir)


def pay(data_dir, pid, invoice_id, cents, status="received"):
    return object_records.create_collection_record(
        "payments",
        {"id": pid, "invoice_id": invoice_id, "amount_cents": cents,
         "method": "card", "received_on": "2026-07-09", "status": status,
         "owner_id": "dan"},
        base_dir=data_dir)


def fire_status(collection, record_id, action_raw):
    """Reuses app-payments' own system_invoice_status object (read-only
    import, not edited by this task) to produce realistic partial/paid
    invoice state -- amount_paid_cents/balance_due_cents/status all
    genuinely computed, not hand-faked."""
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_invoice_status", method="EVENT",
            payload={"event": f"{collection}.record.{action_raw}d",
                     "collection": collection, "record_id": record_id,
                     "action": action_raw}),
        roots=[PACKAGES / "app-payments" / "objects"])


def run_aging(today):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_invoice_aging", method="POST", payload={"today": today}),
        roots=[APP_INVOICES / "objects"])


def portal_get(token, query_extra=None):
    payload = {"token": token}
    if query_extra:
        payload.update(query_extra)
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_invoice_portal", method="GET", payload=payload),
        roots=[APP_INVOICES / "objects"])


def regenerate(invoice_id, user_id="dan", roles=None):
    identity = {"user_id": user_id, "roles": roles or []} if user_id else {}
    payload = {"invoice_id": invoice_id}
    if identity:
        payload["_identity"] = identity
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_regenerate_portal_link", method="POST", payload=payload),
        roots=[APP_INVOICES / "objects"])


def invoice(data_dir, iid="inv1"):
    return object_records.get_collection_record("invoices", iid, base_dir=data_dir)


def outbox(data_dir):
    return object_records.read_collection_records("email_outbox", base_dir=data_dir)


def token_for(data_dir, iid="inv1", owner="dan"):
    """Mint a portal link the way an owner would, and return the bare token."""
    result = regenerate(iid, user_id=owner)
    assert result.ok, result.error
    path = result.result["path"]
    assert path.startswith("/pay/")
    return path[len("/pay/"):]


# --- states -----------------------------------------------------------------

def test_unpaid_state_shows_amount_due_and_instructions_with_no_pay_button(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("portal.payment_instructions",
                                    "Wire to Acme Bank, account 000123"),))
    make_invoice(data_dir, total="10000")
    token = token_for(data_dir)

    result = portal_get(token)
    assert result.ok, result.error
    body = result.result["body"]
    assert result.result.get("status", 200) == 200
    assert "Payment due" in body
    assert "100.00 USD" in body                      # balance_due_cents=10000
    assert "Wire to Acme Bank, account 000123" in body
    # No fake Pay button: there is no card-processing rail wired up yet, and
    # a button that posts nowhere is worse than no button at all.
    assert "Pay now" not in body and "<button" not in body and "<form" not in body


def test_unpaid_state_without_instructions_says_so_plainly(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)   # no portal.payment_instructions set
    make_invoice(data_dir, total="5000")
    token = token_for(data_dir)

    body = portal_get(token).result["body"]
    assert "has not published payment instructions" in body
    # Never an empty box standing in for the missing setting.
    assert '<div class="instructions"></div>' not in body


def test_partial_state_shows_received_and_remaining(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, total="10000")
    pay(data_dir, "p1", "inv1", "4000")
    fire_status("payments", "p1", "create")
    assert invoice(data_dir)["status"] == "partial"
    token = token_for(data_dir)

    body = portal_get(token).result["body"]
    assert "Partially paid" in body
    assert "40.00 USD" in body      # received so far
    assert "60.00 USD" in body      # still due
    assert "How to pay" in body


def test_paid_state_renders_receipt_with_no_payment_instructions(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("portal.payment_instructions", "Wire details here"),))
    make_invoice(data_dir, total="10000")
    pay(data_dir, "p1", "inv1", "10000")
    fire_status("payments", "p1", "create")
    assert invoice(data_dir)["status"] == "paid"
    token = token_for(data_dir)

    body = portal_get(token).result["body"]
    assert "Paid in full" in body
    assert "100.00 USD" in body
    assert "No further payment is due" in body
    assert "Wire details here" not in body   # receipt view, not a pay ask


def test_void_invoice_refuses_the_portal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("portal.payment_instructions", "Wire details here"),))
    make_invoice(data_dir, status="void", total="10000")
    token = token_for(data_dir)

    result = portal_get(token)
    body = result.result["body"]
    assert result.result.get("status", 200) == 200   # a real page, not a 404 -- it IS found
    assert "cancelled" in body.lower()
    # Refuses the financial content entirely: no balance, no instructions,
    # no line items -- showing any of that on a cancelled document would
    # invite paying a bill that no longer exists.
    assert "Amount due" not in body
    assert "Wire details here" not in body


# --- token lookup security ----------------------------------------------

def test_unknown_token_is_404_not_403(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    token_for(data_dir)  # a real token exists in the system, just not this one

    result = portal_get("totally-made-up-token-value")
    assert result.result["status"] == 404
    assert "not found" in result.result["body"].lower()
    assert "403" not in str(result.result)


def test_blank_token_is_404_even_when_no_invoice_has_minted_one(tmp_path, monkeypatch):
    """A blank submitted token must never match a blank stored portal_token
    (an invoice that has never had a link generated) -- otherwise "no link
    yet" would accidentally be an open door."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)   # portal_token left at its default: blank

    result = portal_get("")
    assert result.result["status"] == 404


# --- view counters ------------------------------------------------------

def test_portal_view_stamps_counter_and_timestamp_on_every_view(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    token = token_for(data_dir)

    assert invoice(data_dir)["portal_views"] in ("0", "", None)
    portal_get(token)
    row = invoice(data_dir)
    assert row["portal_views"] == "1"
    assert row["last_viewed_at"]

    portal_get(token)
    assert invoice(data_dir)["portal_views"] == "2"


# --- line items -----------------------------------------------------------

def test_line_items_render_when_invoice_lines_is_installed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, with_invoice_lines=True)
    make_invoice(data_dir, total="10000")
    object_records.create_collection_record(
        "invoice_lines",
        {"id": "l1", "invoice_id": "inv1", "description": "Consulting hours",
         "quantity": "2", "unit_price_cents": "5000", "line_total_cents": "10000",
         "owner_id": "dan"},
        base_dir=data_dir)
    token = token_for(data_dir)

    body = portal_get(token).result["body"]
    assert "Consulting hours" in body
    assert "50.00 USD" in body   # unit price


def test_line_items_skip_gracefully_when_invoice_lines_not_installed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, with_invoice_lines=False)
    make_invoice(data_dir, total="10000")
    token = token_for(data_dir)

    result = portal_get(token)
    assert result.ok, result.error   # must not crash just because the collection is absent
    body = result.result["body"]
    assert result.result.get("status", 200) == 200
    assert '<table class="lines">' not in body


# --- regeneration ---------------------------------------------------------

def test_regenerate_invalidates_the_old_token(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    old_token = token_for(data_dir)
    assert portal_get(old_token).result.get("status", 200) == 200

    new_result = regenerate("inv1", user_id="dan")
    new_token = new_result.result["path"][len("/pay/"):]
    assert new_token != old_token

    assert portal_get(old_token).result["status"] == 404
    assert portal_get(new_token).result.get("status", 200) == 200


def test_regenerate_requires_owner_or_admin(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, owner="dan")

    stranger = regenerate("inv1", user_id="mallory")
    assert stranger.result["status"] == 403

    anonymous = regenerate("inv1", user_id=None)
    assert anonymous.result["status"] == 403

    owner = regenerate("inv1", user_id="dan")
    assert owner.result["status"] == 200

    admin = regenerate("inv1", user_id="ops", roles=["admin"])
    assert admin.result["status"] == 200


def test_regenerate_missing_or_unknown_invoice(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert regenerate("", user_id="dan").result["status"] == 400
    assert regenerate("does-not-exist", user_id="dan").result["status"] == 404


# --- dunning email carries the door ----------------------------------------

def test_dunning_email_contains_the_pay_link_when_base_url_configured(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("portal.base_url", "https://pay.example.test"),))
    make_invoice(data_dir)   # due 2026-07-10, unpaid, no portal_token minted yet

    run_aging("2026-07-11")
    mails = outbox(data_dir)
    assert len(mails) == 1
    body = mails[0]["text_body"]
    assert "https://pay.example.test/pay/" in body

    # The link must actually resolve -- proves aging minted a real token,
    # not a placeholder.
    token = body.split("https://pay.example.test/pay/", 1)[1].split()[0].strip()
    assert invoice(data_dir)["portal_token"] == token
    assert portal_get(token).result.get("status", 200) == 200


def test_dunning_email_omits_link_cleanly_when_base_url_unset(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)   # portal.base_url never set
    make_invoice(data_dir)

    run_aging("2026-07-11")
    mails = outbox(data_dir)
    assert len(mails) == 1
    body = mails[0]["text_body"]
    assert "/pay/" not in body
    assert "http" not in body
    # No link was ever built, so no token needed to be minted to build one.
    assert invoice(data_dir)["portal_token"] in ("", None)


# --- amounts in whole currency units ---------------------------------------

def test_amounts_render_via_format_amount_in_whole_units_never_raw_cents(tmp_path, monkeypatch):
    import object_money
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir, total="123456")   # $1,234.56
    token = token_for(data_dir)

    body = portal_get(token).result["body"]
    expected = object_money.format_amount("123456", "USD", base_dir=data_dir)
    assert expected in body
    assert "123456 cents" not in body
    assert "123456" not in body.replace(expected, "")  # raw minor units never leak elsewhere


# --- security: token cannot surface for a non-owner ------------------------

def _app_invoices_policy():
    payload = json.loads((APP_INVOICES / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})


def test_portal_token_never_reaches_a_non_owner_via_list_or_search():
    """portal_token is not itself access-controlled field-by-field -- it
    relies entirely on the invoices collection's existing row_filter
    ({"owner_id": "$user_id"}), unmodified by this feature. Proving that
    filter still denies READ to a non-owner is exactly proving the token
    can never appear in a list/search response for anyone but the owner:
    a row nobody may read is a row whose fields, portal_token included,
    never leave the server for that caller."""
    policy = _app_invoices_policy()
    stranger = object_permissions.PermissionSubject(user_id="mallory")
    record = {"owner_id": "dan", "number": "N-inv1", "customer_name": "Grace Hopper",
              "status": "sent", "portal_token": "super-secret-capability-token"}

    decision = object_permissions.check_permission(
        stranger, object_permissions.READ, policy=policy, collection="invoices", record=record)
    assert decision.allowed is False

    owner = object_permissions.PermissionSubject(user_id="dan")
    decision = object_permissions.check_permission(
        owner, object_permissions.READ, policy=policy, collection="invoices", record=record)
    assert decision.allowed is True


def test_portal_token_excluded_from_generic_form_list_and_search_surfaces():
    schema = json.loads((APP_INVOICES / "schemas" / "invoices.json").read_text())
    assert "portal_token" not in schema["forms"]["default"]["fields"]
    assert "portal_token" not in schema["views"]["list_fields"]
    assert "portal_token" not in schema["search"]["fields"]
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["portal_token"]["read_only"] is True
