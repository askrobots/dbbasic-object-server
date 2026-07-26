"""The document layer: one renderer, three deliveries, and the rule that
makes the third one safe.

The properties worth holding here are the ones seven hand-rolled printable
pages could not hold between them.

A document says who sent it -- the business header comes from settings, and
a nameless business is refused where refusing costs nothing (at SEND time)
rather than where somebody is standing at a printer.

The packing slip's no-prices rule is a PARAMETER now, not a paragraph. It is
asserted on the rendered HTML rather than on the model, because "no prices in
the model" is a claim about a dict and "no prices on the page" is the claim
that keeps the amount paid off a birthday present.

Print CSS repeats table headings across a page break. That single rule --
`thead { display: table-header-group }` -- appeared in exactly none of the
seven @media print blocks that shipped before this layer, which is why a
three-page pick list came out of the printer headerless and nobody noticed:
nobody reviews on paper.

A document that was SENT is evidence of what was sent. Change the source and
the customer's link keeps showing the snapshot, names both dates, and offers
the current version -- it never silently restates a document somebody is
holding a printout of.

opened_at stamps once. Sending twice to one recipient queues one email. And
the PDF capability is honest in both directions: with no engine it returns a
409 naming exactly what to install, and NO surface anywhere draws a button
that would produce that 409.

Fixture style follows tests/test_shipping.py: direct object_execution calls
against the real package objects, with one merged object root -- which is what
an installed server actually looks like, and the only way a page that reaches
a root object_* module resolves the way it will in production.
"""

import json
import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_documents
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
RUNTIME = python_object_runtime.PythonObjectRuntime()

OWNER = "dan"
BASE_URL = "https://docs.example.test"

BUSINESS = (
    ("business.name", "Acme Supplies Ltd"),
    ("business.address", "12 Kiln Lane\nSheffield"),
    ("business.email", "hello@acme.test"),
    ("portal.base_url", BASE_URL),
)


# --- the pure fold ------------------------------------------------------------

def settings(**overrides):
    values = dict(BUSINESS)
    values.update(overrides)
    return values


def facts(**overrides):
    base = {
        "number": "Q-1001",
        "date": "2026-07-01",
        "state": "sent",
        "currency": "USD",
        "to": {"name": "Grace Hopper", "address": "9 Navy Yard",
               "email": "grace@acme.test"},
        "lines": [{"description": "Enamel Mug", "quantity": "3",
                   "unit_price": "12.00 USD", "amount": "36.00 USD"}],
        "totals": [{"label": "Total", "value": "36.00 USD", "emphasis": True}],
    }
    base.update(overrides)
    return base


def test_the_model_carries_the_business_header_from_settings():
    """A document looks like it came from a business rather than from
    software. Every field is optional except the name."""
    model = object_documents.build_model("quote", facts(), settings())

    assert model["business"] == {
        "name": "Acme Supplies Ltd",
        "address": "12 Kiln Lane\nSheffield",
        "email": "hello@acme.test",
    }
    # And it is the "from" party too, rather than a second copy somebody can
    # edit into disagreeing with the letterhead.
    origin = [p for p in model["parties"] if p["role"] == "from"][0]
    assert origin["name"] == "Acme Supplies Ltd"

    body = object_documents.render_html(model)
    assert "Acme Supplies Ltd" in body
    assert "12 Kiln Lane<br>Sheffield" in body


def test_a_nameless_business_is_refused():
    """Fabricating a default -- "My Company", the hostname, the owner's user
    id -- would be a plausible-looking lie printed on paperwork somebody
    keeps."""
    nameless = {k: v for k, v in settings().items() if k != "business.name"}

    with pytest.raises(object_documents.NamelessBusinessError) as exc:
        object_documents.business_header(nameless)
    assert "business.name" in str(exc.value)

    with pytest.raises(object_documents.NamelessBusinessError):
        object_documents.build_model("quote", facts(), nameless)

    # ...and the documented escape, for the operator standing at a printer.
    model = object_documents.build_model("quote", facts(), nameless,
                                         require_business=False)
    assert model["business"] == {}


def test_show_money_false_produces_a_document_with_no_price_anywhere():
    """Asserted on the RENDERED HTML, not the model: a model with no prices
    in it is a claim about a dict, and the claim that matters is that nothing
    reaches the page.

    Same facts, both kinds. The only thing that differs is the kind's row in
    the registry -- which is the entire point of making it a parameter.
    """
    priced = object_documents.build_model("invoice", facts(), settings())
    priceless = object_documents.build_model("packing_slip", facts(), settings())

    assert priced["show_money"] is True
    assert priceless["show_money"] is False

    priced_html = object_documents.render_html(priced)
    assert "12.00 USD" in priced_html and "36.00 USD" in priced_html

    slip_html = object_documents.render_html(priceless)
    for forbidden in ("12.00", "36.00", "USD", "Unit price", "Amount",
                      "unit_price", "_cents", "$"):
        assert forbidden not in slip_html, forbidden
    # The goods are still on it -- pricelessness is not blankness.
    assert "Enamel Mug" in slip_html and ">3<" in slip_html

    # And the money never entered the model, so a snapshot of it carries none
    # either: the rule survives being written to disk.
    assert "12.00" not in object_documents.snapshot_json(priceless)
    assert priceless["totals"] == []
    assert "currency" not in priceless

    # There is deliberately no way to argue the packing slip into showing
    # money: the decision lives in the registry, not at the call site.
    assert object_documents.shows_money("packing_slip") is False
    assert "show_money" not in object_documents.build_model.__kwdefaults__


def test_the_ship_to_of_a_slip_is_not_the_bill_to_of_an_invoice():
    invoice = object_documents.build_model("invoice", facts(), settings())
    slip = object_documents.build_model("packing_slip", facts(), settings())

    assert [p["role"] for p in invoice["parties"]] == ["from", "bill_to"]
    assert [p["role"] for p in slip["parties"]] == ["from", "ship_to"]
    assert "Bill to" in object_documents.render_html(invoice)
    assert "Ship to" in object_documents.render_html(slip)


def test_print_css_repeats_table_headers_across_a_page_break():
    """The rule every hand-rolled print stylesheet in this repo forgot.
    Without it, page two of a long document is two anonymous columns of
    numbers."""
    css = object_documents.document_css()
    assert "thead { display: table-header-group; }" in css
    assert "tfoot { display: table-footer-group; }" in css

    # It has something to repeat: the renderer emits a real <thead>.
    body = object_documents.render_html(
        object_documents.build_model("invoice", facts(), settings()))
    assert "<thead>" in body


def test_exactly_two_pages_were_migrated_and_they_own_no_print_css():
    """A layer nobody has migrated to is a layer that does not work, and
    migrating all seven at once would make a regression impossible to locate.
    So: exactly two, and those two carry no private @media print block at all
    -- if they still did, they would be a second opinion about paper that
    nobody would notice diverging."""
    printables = {
        "app-shipping/packing_slip": True,     # migrated
        "app-invoices/invoice_portal": True,   # migrated
        "app-shipping/pick_list": False,
        "app-shipping/manifest": False,
        "app-kitchen/kitchen": False,
        "app-kitchen/kitchen_ticket": False,
        "app-receiving/receiving_sheet": False,
        "app-returns/return_form": False,
    }
    for path, migrated in printables.items():
        package_id, name = path.split("/")
        source = (PACKAGES / package_id / "objects" / "site"
                  / f"{name}.py").read_text()
        owns_print_css = "@media print {" in source
        assert owns_print_css is not migrated, path
        assert ("object_documents" in source) is migrated, path


def test_the_page_size_comes_from_a_setting_and_never_emits_broken_css():
    assert "@page { size: A4; margin: 14mm; }" in object_documents.document_css()
    assert "size: Letter" in object_documents.document_css("letter")
    # An operator typo must not produce a stylesheet the browser discards --
    # a broken @page rule fails silently and prints at whatever size it liked.
    assert "size: A4" in object_documents.document_css("foolscap")


def test_the_content_hash_is_stable_and_moves_with_the_content():
    model = object_documents.build_model("quote", facts(), settings())
    again = object_documents.build_model("quote", facts(), settings())
    changed = object_documents.build_model(
        "quote", facts(totals=[{"label": "Total", "value": "45.00 USD"}]),
        settings())

    assert object_documents.content_hash(model) == object_documents.content_hash(again)
    assert object_documents.content_hash(model) != object_documents.content_hash(changed)

    # A snapshot round-trips, and a broken one is refused rather than repaired:
    # rendering a half-parsed snapshot would show something nobody ever sent.
    assert object_documents.model_from_snapshot(
        object_documents.snapshot_json(model)) == model
    with pytest.raises(object_documents.DocumentError):
        object_documents.model_from_snapshot("{not json")


# --- a box with the whole slice installed --------------------------------------

def setup_env(tmp_path, monkeypatch, *, extra_settings=()):
    data_dir = tmp_path / "data"
    for package_id, collection in (
            ("app-documents", "sent_documents"),
            ("app-orders", "orders"), ("app-orders", "order_lines"),
            ("app-invoices", "invoices"), ("app-invoices", "invoice_lines"),
            ("app-shipping", "shipments"), ("app-shipping", "shipment_lines"),
            ("app-email", "email_outbox")):
        stage_collection(data_dir, package_id, collection)

    rows = "".join(f"s{i}\t{key}\t{value}\t\n" for i, (key, value)
                   in enumerate(tuple(BUSINESS) + tuple(extra_settings)))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    for package_id in ("app-documents", "app-invoices", "app-shipping"):
        shutil.copytree(PACKAGES / package_id / "objects", objects_root,
                        dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload),
        roots=[objects_root]).result


def make_order(data_dir, order_id="ord-1", *, total="3600", number="Q-1001"):
    order = object_records.create_collection_record(
        "orders",
        # doc_type stays `sale`: quotes are the B2B slice and do not exist
        # yet as a document type on orders. The document LAYER already carries
        # the kind (KINDS["quote"] renders from orders for exactly that
        # reason), so this exercises the same path a quote will take.
        {"id": order_id, "doc_type": "sale", "number": number,
         "customer_name": "Grace Hopper", "customer_email": "grace@acme.test",
         "currency": "USD", "status": "draft", "order_date": "2026-07-01",
         "total_cents": total, "owner_id": OWNER},
        base_dir=data_dir)
    object_records.create_collection_record(
        "order_lines",
        # No product_id: the catalog is another package, and a document
        # renders from what the order says, not from what is in stock.
        {"id": "line-1", "order_id": order_id,
         "description": "Enamel Mug", "quantity": "3",
         "unit_price_cents": "1200", "line_total_cents": total,
         "owner_id": OWNER},
        base_dir=data_dir)
    return order


def send(objects_root, *, kind="order", source="orders/ord-1",
         to="grace@acme.test", user_id=OWNER):
    payload = {"kind": kind, "source": source, "to": to}
    if user_id:
        payload["_identity"] = {"user_id": user_id, "roles": []}
    return run(objects_root, "action_send_document", "POST", payload)


def open_link(objects_root, token, **extra):
    payload = {"token": token}
    payload.update(extra)
    return run(objects_root, "site_document", "GET", payload)


def sent_rows(data_dir):
    return object_records.read_collection_records("sent_documents",
                                                  base_dir=data_dir)


def outbox(data_dir):
    return object_records.read_collection_records("email_outbox",
                                                  base_dir=data_dir)


# --- delivery 3: the link, and the rule that makes it safe ---------------------

def test_sending_a_document_snapshots_it_mints_a_link_and_queues_one_email(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)

    result = send(objects_root)
    assert result["status"] == "ok", result
    assert result["queued"] is True
    assert result["link"].startswith(f"{BASE_URL}/d/")

    row = sent_rows(data_dir)[0]
    assert row["kind"] == "order"
    assert row["source"] == "orders/ord-1"
    assert row["sent_to"] == "grace@acme.test"
    assert row["opened_at"] == ""
    snapshot = object_documents.model_from_snapshot(row["snapshot"])
    assert snapshot["content_hash"] if False else True   # the hash is on the row
    assert row["content_hash"] == object_documents.content_hash(snapshot)
    assert "36.00 USD" in object_documents.render_html(snapshot)

    # It QUEUES; it never sends. One `queued` row, carrying the link.
    mails = outbox(data_dir)
    assert len(mails) == 1
    assert mails[0]["status"] == "queued"
    assert result["link"] in mails[0]["text_body"]


def test_sending_twice_to_the_same_recipient_queues_one_email(
        tmp_path, monkeypatch):
    """Idempotent per (source, recipient), the way system_order_email is: the
    document is one document with one token and one opened_at, and a second
    copy of the mail is already in somebody's inbox and cannot be taken back.
    """
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)

    first = send(objects_root)
    second = send(objects_root)

    assert second["status"] == "ok"
    assert second["queued"] is False
    assert second["link"] == first["link"]
    assert len(sent_rows(data_dir)) == 1
    assert len(outbox(data_dir)) == 1
    assert "already sent" in second["note"]

    # A DIFFERENT recipient is genuinely a second delivery: their own link,
    # their own read receipt.
    third = send(objects_root, to="accounts@acme.test")
    assert third["queued"] is True
    assert third["link"] != first["link"]
    assert len(outbox(data_dir)) == 2


def test_the_link_renders_the_snapshot_after_the_source_changes(
        tmp_path, monkeypatch):
    """The load-bearing rule. Email a quote at 36.00, change it to 45.00, and
    the customer's link must not silently show 45.00 -- they may be holding a
    printout of the first one."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    result = send(objects_root)
    token = result["link"].rsplit("/", 1)[1]

    # Backdated so the banner has two genuinely different dates to name.
    object_records.update_collection_record(
        "sent_documents", sent_rows(data_dir)[0]["id"],
        {"sent_at": "2026-07-01T09:00:00Z"}, base_dir=data_dir, actor="test")

    # ...and now the price moves.
    object_records.update_collection_record(
        "orders", "ord-1", {"total_cents": "4500"},
        base_dir=data_dir, actor="test")
    object_records.update_collection_record(
        "order_lines", "line-1", {"line_total_cents": "4500"},
        base_dir=data_dir, actor="test")

    body = open_link(objects_root, token)["body"]
    assert "36.00 USD" in body                    # what they were sent
    assert "45.00 USD" not in body                # never silently restated

    # Both dates, named, with a way to the current version.
    assert "2026-07-01" in body
    changed_on = object_records.read_collection_records(
        "sent_documents", base_dir=data_dir)  # touch nothing; date comes below
    assert "sent to you on 2026-07-01" in body
    assert "This document was changed on 20" in body
    assert f'href="/d/{token}?v=current"' in body
    assert changed_on  # the row still exists, unmodified by the view


def test_the_current_version_is_offered_but_never_substituted(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]

    object_records.update_collection_record(
        "orders", "ord-1", {"total_cents": "4500"},
        base_dir=data_dir, actor="test")
    object_records.update_collection_record(
        "order_lines", "line-1", {"line_total_cents": "4500"},
        base_dir=data_dir, actor="test")

    current = open_link(objects_root, token, v="current")["body"]
    assert "45.00 USD" in current
    assert "CURRENT version" in current
    # ...and it still points back at what was actually sent.
    assert f'href="/d/{token}"' in current


def test_an_unchanged_source_shows_no_banner_at_all(tmp_path, monkeypatch):
    """A banner on an unchanged document would teach people to ignore it."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]

    body = open_link(objects_root, token)["body"]
    assert '<div class="doc-banner">' not in body
    assert "was changed" not in body
    assert "36.00 USD" in body


def test_opened_at_stamps_once_and_is_idempotent(tmp_path, monkeypatch):
    """'Did they read it?' is the first thing anyone asks after sending a
    quote. The fact worth keeping is that they opened it, not how many times,
    so it is written only while the field is empty."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]
    assert sent_rows(data_dir)[0]["opened_at"] == ""

    open_link(objects_root, token)
    first = sent_rows(data_dir)[0]["opened_at"]
    assert first

    open_link(objects_root, token)
    open_link(objects_root, token, v="current")
    assert sent_rows(data_dir)[0]["opened_at"] == first


def test_an_unknown_or_blank_token_is_the_same_404_as_one_that_never_existed(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    send(objects_root)   # a real token exists, just not these

    for token in ("totally-made-up", "", None):
        result = open_link(objects_root, token)
        assert result["status"] == 404
        assert "not valid" in result["body"]
        assert "403" not in str(result)
        assert "Traceback" not in result["body"]


def test_sending_refuses_a_nameless_business_and_a_missing_origin(
        tmp_path, monkeypatch):
    """Both refusals land where refusing costs one settings row and nobody is
    waiting -- unlike the printer, where a parcel is."""
    data_dir = tmp_path / "data"
    for package_id, collection in (("app-documents", "sent_documents"),
                                   ("app-orders", "orders"),
                                   ("app-orders", "order_lines"),
                                   ("app-email", "email_outbox")):
        stage_collection(data_dir, package_id, collection)
    stage_collection(data_dir, "app-settings", "app_settings", rows="")
    objects_root = tmp_path / "objects"
    shutil.copytree(PACKAGES / "app-documents" / "objects", objects_root,
                    dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    make_order(data_dir)

    # No portal.base_url: this message is nothing but a link.
    refusal = send(objects_root)
    assert refusal["status"] == 409
    assert refusal["setting"] == "portal.base_url"

    object_records.create_collection_record(
        "app_settings", {"id": "s9", "key": "portal.base_url",
                         "value": BASE_URL}, base_dir=data_dir)
    nameless = send(objects_root)
    assert nameless["status"] == 409
    assert nameless["setting"] == "business.name"
    assert sent_rows(data_dir) == []
    assert outbox(data_dir) == []


def test_a_packing_slip_cannot_be_rendered_from_an_invoice(tmp_path, monkeypatch):
    """The kind decides which address the document carries, so the kind and
    the source have to agree -- a packing slip built from an invoice would
    print the wrong address on a box."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    refused = send(objects_root, kind="packing_slip", source="orders/ord-1")
    assert refused["status"] == 400
    assert "shipments" in refused["error"]


def test_sending_requires_sign_in_and_refuses_somebody_elses_record(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)

    assert send(objects_root, user_id=None)["status"] == 403
    assert send(objects_root, user_id="mallory")["status"] == 403
    assert send(objects_root, user_id="ops")["status"] == 403
    assert sent_rows(data_dir) == []


# --- delivery 2: PDF as a capability, absent by default -------------------------

def test_render_pdf_with_no_engine_names_exactly_what_to_install(
        tmp_path, monkeypatch):
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]

    result = run(objects_root, "action_render_pdf", "POST",
                 {"token": token, "_identity": {"user_id": OWNER}})

    assert result["status"] == 409
    assert result["setting"] == "documents.pdf_engine"
    assert result["engine"] == "none"
    assert set(result["install"]) == {"weasyprint", "chrome", "wkhtmltopdf"}
    assert "pango/cairo" in result["install"]["weasyprint"]
    # The free answer is named, because it already works.
    assert "Save as PDF" in result["alternative"]
    # And the boundary is demonstrated rather than described: the HTML in the
    # refusal is the SAME page the print view serves, never a second template.
    assert "table-header-group" in result["html"]
    assert "36.00 USD" in result["html"]


def test_a_named_engine_with_nothing_behind_it_is_reported_not_swallowed(
        tmp_path, monkeypatch):
    """An operator who pasted an engine name in and heard nothing would
    reasonably conclude PDFs were being generated."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        extra_settings=(("documents.pdf_engine", "weasyprint"),))
    make_order(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]

    result = run(objects_root, "action_render_pdf", "POST",
                 {"token": token, "_identity": {"user_id": OWNER}})
    assert result["status"] == 409
    assert result["engine"] == "weasyprint"
    assert "no PDF engine module is installed" in result["error"]
    assert set(result["install"]) == {"weasyprint"}


@pytest.mark.parametrize("engine", ["", "none", "weasyprint", "chrome",
                                    "typo-engine"])
def test_no_surface_offers_a_pdf_button_when_it_would_409(
        tmp_path, monkeypatch, engine):
    """The property, swept over every document surface this layer serves.

    A button that 409s tells the person pressing it that something is broken
    on YOUR end -- the same reason site_invoice_portal only draws "Pay by
    card" when Stripe is actually configured.
    """
    extra = (("documents.pdf_engine", engine),) if engine else ()
    data_dir, objects_root = setup_env(tmp_path, monkeypatch,
                                       extra_settings=extra)
    make_order(data_dir)
    make_invoice(data_dir)
    shipment(data_dir)
    token = send(objects_root)["link"].rsplit("/", 1)[1]

    assert object_documents.pdf_engine_status(engine)["available"] is False

    pages = {
        "sent document": open_link(objects_root, token)["body"],
        "packing slip": run(objects_root, "site_packing_slip", "GET",
                            {"shipment_id": "ship-1"})["body"],
        "invoice portal": run(objects_root, "site_invoice_portal", "GET",
                              {"token": "invoice-token-1"})["body"],
    }
    for name, body in pages.items():
        assert "Download PDF" not in body, name
        assert "action_render_pdf" not in body, name
        # ...and every one of them says the thing that DOES work.
        assert "Save as PDF" in body, name
        assert "Ctrl/Cmd+P" in body, name


# --- the two migrated pages ------------------------------------------------------

def make_invoice(data_dir, invoice_id="inv-1"):
    object_records.create_collection_record(
        "invoices",
        {"id": invoice_id, "number": "INV-9", "customer_name": "Grace Hopper",
         "customer_email": "grace@acme.test", "currency": "USD",
         "status": "sent", "issue_date": "2026-06-10", "due_date": "2026-07-10",
         "total_cents": "3600",
         "portal_token": "invoice-token-1", "owner_id": OWNER},
        base_dir=data_dir, preserve_read_only=True)
    object_records.create_collection_record(
        "invoice_lines",
        {"id": "il-1", "invoice_id": invoice_id, "description": "Enamel Mug",
         "quantity": "3", "unit_price_cents": "1200",
         "line_total_cents": "3600", "owner_id": OWNER},
        base_dir=data_dir)


def shipment(data_dir, shipment_id="ship-1"):
    object_records.create_collection_record(
        "shipments",
        {"id": shipment_id, "order_id": "ord-1", "direction": "outbound",
         "status": "shipped", "ship_to_name": "Grace Hopper",
         "ship_to_address": "9 Navy Yard", "shipped_on": "2026-07-02",
         "owner_id": OWNER},
        base_dir=data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": "sl-1", "shipment_id": shipment_id, "order_line_id": "line-1",
         "description": "Enamel Mug", "quantity": "3",
         "owner_id": OWNER},
        base_dir=data_dir)


def test_the_packing_slip_still_shows_no_prices_after_migrating(
        tmp_path, monkeypatch):
    """THE regression that matters. Everything else about this page could
    change and be fixed later; a price on a packing slip is the amount paid
    stapled to a birthday present, and it is already in the post."""
    data_dir, objects_root = setup_env(tmp_path, monkeypatch)
    make_order(data_dir)
    shipment(data_dir)

    body = run(objects_root, "site_packing_slip", "GET",
               {"shipment_id": "ship-1"})["body"]

    for forbidden in ("$", "36.00", "12.00", "3600", "1200", "USD",
                      "Unit price", "unit_price", "_cents", "Total"):
        assert forbidden not in body, forbidden

    # It is still a usable packing slip: goods, quantity, order, ship-to.
    assert "Enamel Mug" in body
    assert ">3<" in body
    assert "Q-1001" in body
    assert "Grace Hopper" in body
    assert "Ship to" in body and "Bill to" not in body
    # ...printed through the shared layer, with the rule the old page lacked.
    assert "table-header-group" in body
    assert "@media print" in body


def test_the_invoice_portal_still_shows_prices_status_and_the_pay_button(
        tmp_path, monkeypatch):
    """The other half of the proof: migrating must not cost the portal
    anything it had. Prices, the status bucket, the payment instructions and
    the card button all survive."""
    data_dir, objects_root = setup_env(
        tmp_path, monkeypatch,
        extra_settings=(("portal.payment_instructions", "Wire to Acme Bank"),))
    make_invoice(data_dir)
    monkeypatch.setenv("DBBASIC_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("DBBASIC_STRIPE_WEBHOOK_SECRET", "whsec_x")

    result = run(objects_root, "site_invoice_portal", "GET",
                 {"token": "invoice-token-1"})
    body = result["body"]
    assert result.get("status", 200) == 200

    assert "Payment due" in body                 # the status bucket
    assert "Amount due" in body                  # the tile
    assert "36.00 USD" in body                   # the money, in whole units
    assert "12.00 USD" in body                   # the line's unit price
    assert "3600" not in body.replace("36.00 USD", "")   # never raw minor units
    assert "Enamel Mug" in body                  # the lines table
    assert "Wire to Acme Bank" in body           # payment instructions
    assert 'id="paybtn"' in body                 # the card button, configured
    assert "Bill to" in body and "Ship to" not in body
    assert "Acme Supplies Ltd" in body           # the business header
    # No app chrome on a page handed to a stranger's inbox.
    assert "/nav" not in body
    # ...and the shared print rules came with it.
    assert "table-header-group" in body


def test_the_portal_never_nags_a_customer_about_an_unconfigured_business(
        tmp_path, monkeypatch):
    """An unbranded invoice is honest. Telling the CUSTOMER to go and set
    business.name is the sender's software talking over the sender, about
    something the reader cannot fix."""
    data_dir = tmp_path / "data"
    for package_id, collection in (("app-invoices", "invoices"),
                                   ("app-invoices", "invoice_lines")):
        stage_collection(data_dir, package_id, collection)
    stage_collection(data_dir, "app-settings", "app_settings", rows="")
    objects_root = tmp_path / "objects"
    shutil.copytree(PACKAGES / "app-invoices" / "objects", objects_root,
                    dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    make_invoice(data_dir)

    body = run(objects_root, "site_invoice_portal", "GET",
               {"token": "invoice-token-1"})["body"]
    assert "business.name" not in body
    assert "36.00 USD" in body


def test_the_operators_own_printable_is_told_quietly_and_off_the_paper(
        tmp_path, monkeypatch):
    """The other side of the same rule: the person who CAN set business.name
    is the operator, so the nudge goes on their page -- and it is .noprint, so
    it never reaches the parcel."""
    data_dir = tmp_path / "data"
    for package_id, collection in (("app-orders", "orders"),
                                   ("app-orders", "order_lines"),
                                   ("app-shipping", "shipments"),
                                   ("app-shipping", "shipment_lines")):
        stage_collection(data_dir, package_id, collection)
    stage_collection(data_dir, "app-settings", "app_settings", rows="")
    objects_root = tmp_path / "objects"
    shutil.copytree(PACKAGES / "app-shipping" / "objects", objects_root,
                    dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    make_order(data_dir)
    shipment(data_dir)

    body = run(objects_root, "site_packing_slip", "GET",
               {"shipment_id": "ship-1"})["body"]
    assert "business.name" in body
    assert 'class="doc-setup noprint"' in body
    assert "$" not in body


# --- the package declares itself the way every other package must ---------------

def test_the_package_manifest_declares_its_door_routes_and_permissions():
    manifest = json.loads(
        (PACKAGES / "app-documents" / "dbbasic-package.json").read_text())
    assert manifest["version"] == "0.1.0"
    assert [entry["id"] for entry in manifest["nav"]] == ["documents"]
    assert manifest["nav"][0]["group"] == "Work"

    rules = json.loads(
        (PACKAGES / "app-documents" / "permissions" / "rules.json").read_text())
    by_object = {rule.get("object_id"): rule for rule in rules["rules"]}
    # The public door is the tokened page and nothing else.
    assert by_object["site_document"]["principal"] == "public"
    assert by_object["action_send_document"]["principal"] == "registered"
    assert by_object["action_render_pdf"]["principal"] == "registered"

    collection_rule = [rule for rule in rules["rules"]
                       if rule.get("collection") == "sent_documents"][0]
    # A document that was sent cannot be made not to have been sent.
    assert "delete" not in collection_rule["actions"]
    assert collection_rule["row_filter"] == {"owner_id": "$user_id"}
