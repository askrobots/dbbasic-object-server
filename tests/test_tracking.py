"""The carrier-facing layer: labels, tracking, and the honest gap.

The property this whole slice is built around is that a shop with NO
carrier configured loses nothing. It types the tracking number on the
shipment, prints the paperwork, and the poll leaves every row exactly as
it found it -- counting no attempt against a parcel for the shop's missing
credentials, because an attempt counter that fills up while somebody is
still fetching an API key would retire a warehouse's worth of parcels for
a reason that has nothing to do with any of them.

The rest are the ones a carrier integration gets wrong. An unconfigured
button explains what to configure instead of 404ing or claiming to have
bought postage. A label is evidence, so its bytes are stored and pointed
at rather than described. A parcel the carrier cannot read stops being
asked about after three tries and starts being VISIBLE. A poll that runs
twice does not deliver anything twice. And a connector answering to a name
the shop did not configure is refused, because quietly using the wrong
carrier is worse than doing nothing and saying so.
"""

import base64
import json
import pathlib
import shutil
from datetime import date, timedelta

from conftest import stage_collection

import object_connectors
import object_execution
import object_packages
import object_records
import object_user_files
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
SHIPPING = PACKAGES / "app-shipping"
RUNTIME = python_object_runtime.PythonObjectRuntime()

TODAY = date.today().isoformat()


def setup_env(tmp_path, monkeypatch, *, settings=()):
    """A data dir with the collections this slice touches, and the package
    roots pinned explicitly.

    Pinning DBBASIC_PACKAGES_DIR at the real packages directory (rather
    than leaning on the cwd-relative default) is not tidiness: the carrier
    boundary is RESOLVED through the package roots, so a test that let the
    working directory decide which packages exist would be testing the
    shell it was launched from. The private overlay is pointed at a path
    that does not exist for the same reason -- a deployment's private
    carrier must not decide what these tests see.
    """
    data_dir = tmp_path / "data"
    for pkg, name in (("app-shipping", "shipments"),
                      ("app-shipping", "shipment_lines"),
                      ("app-catalog", "products"),
                      ("app-orders", "orders"), ("app-orders", "order_lines"),
                      ("app-files", "files")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)

    objects_root = tmp_path / "objects"
    shutil.copytree(SHIPPING / "objects", objects_root, dirs_exist_ok=True)

    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(PACKAGES))
    monkeypatch.setenv("DBBASIC_PRIVATE_PACKAGES_DIR", str(tmp_path / "nowhere"))
    return data_dir, objects_root


def run(objects_root, object_id, method, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload=payload),
        roots=[objects_root]).result


# --- a shop with one parcel on the road ----------------------------------------

def shop(tmp_path, monkeypatch, *, provider=None, stuck_days=None):
    settings = []
    if provider is not None:
        settings.append(("carrier.provider", provider))
    if stuck_days is not None:
        settings.append(("carrier.stuck_days", stuck_days))
    data_dir, objects_root = setup_env(tmp_path, monkeypatch, settings=settings)

    object_records.create_collection_record(
        "products",
        {"id": "p1", "name": "Enamel Mug", "sku": "P1", "product_type": "physical",
         "price_cents": "1200", "currency": "USD", "is_active": "true",
         "owner_id": "shop"}, base_dir=data_dir)
    object_records.create_collection_record(
        "orders",
        {"id": "ord-1", "doc_type": "sale", "number": "SO-0001",
         "customer_name": "Ada Lovelace", "customer_email": "ada@example.test",
         "currency": "USD", "status": "draft", "order_date": "2026-07-01",
         "owner_id": "shop"}, base_dir=data_dir)
    object_records.update_collection_record(
        "orders", "ord-1", {"status": "confirmed"}, base_dir=data_dir, actor="test")
    object_records.create_collection_record(
        "order_lines",
        {"id": "line-1", "order_id": "ord-1", "product_id": "p1",
         "description": "Enamel Mug", "quantity": "1", "unit_price_cents": "1200",
         "line_total_cents": "1200", "owner_id": "shop"}, base_dir=data_dir)
    return data_dir, objects_root


def shipment(data_dir, shipment_id="shp-1", *, status="shipped", direction="outbound",
             tracking="TRK-1", shipped_on=TODAY, **fields):
    """A shipment already on the road. Written straight through the ladder,
    the way the packing bench moves it."""
    record = {"id": shipment_id, "order_id": "ord-1", "direction": direction,
              "status": "authorized" if direction == "inbound" else "open",
              "service": "ground", "carrier": "Van Lines",
              "tracking_number": tracking, "ship_to_name": "Ada Lovelace",
              "shipped_on": shipped_on, "owner_id": "shop"}
    record.update({key: str(value) for key, value in fields.items()})
    object_records.create_collection_record("shipments", record, base_dir=data_dir)
    object_records.create_collection_record(
        "shipment_lines",
        {"id": f"{shipment_id}-l1", "shipment_id": shipment_id,
         "order_line_id": "line-1", "product_id": "p1",
         "description": "Enamel Mug", "quantity": "1", "owner_id": "shop"},
        base_dir=data_dir)
    # Up the real ladder, one rung at a time, the way the bench moves it --
    # the transition map is part of what is being tested everywhere else.
    ladder = (["open", "packed", "shipped", "in_transit", "delivered"]
              if direction == "outbound"
              else ["authorized", "in_transit", "received"])
    for step in ladder[1:ladder.index(status) + 1]:
        object_records.update_collection_record(
            "shipments", shipment_id, {"status": step},
            base_dir=data_dir, actor="test")
    return object_records.get_collection_record("shipments", shipment_id,
                                                base_dir=data_dir)


def read(data_dir, shipment_id="shp-1"):
    return object_records.get_collection_record("shipments", shipment_id,
                                                base_dir=data_dir)


def poll(objects_root, **payload):
    return run(objects_root, "system_tracking_poll", "POST", payload)


def buy_label(objects_root, **payload):
    return run(objects_root, "action_buy_label", "POST", payload)


def attention(objects_root):
    return run(objects_root, "system_shipment_attention", "COUNT", {})


def fulfil(objects_root, shipment_id):
    """The dispatcher's own payload shape, which is how the order learns."""
    return run(objects_root, "system_order_fulfillment", "EVENT",
               {"event": "shipments.record.updated", "collection": "shipments",
                "record_id": shipment_id, "action": "update"})


# --- a carrier that is not this package's ----------------------------------------

CARRIER_TEMPLATE = '''
PROVIDER = "fakeco"

def track(shipment, *, base_dir=None):
    return {outcome}

def buy_label(shipment, *, base_dir=None, tracking_number="", label_bytes=None,
              label_content_type="", label_filename=""):
    return {{"ok": True, "tracking_number": "FAKE-1",
             "label_bytes": b"%PDF-fake", "label_content_type": "application/pdf"}}

def reconcile(record, *, base_dir=None):
    return {{"skip": True}}
'''


def install_carrier(tmp_path, monkeypatch, outcome, *, provider="fakeco"):
    """A second package declaring a connector for shipments -- the whole
    extension story in eight lines. Nothing in app-shipping is edited to
    make this work: the declaration is resolved through the same
    object_packages.iter_connectors the daemon's reconcile pass uses."""
    root = tmp_path / "carrier-packages"
    package = root / "app-fakeco"
    (package / "connectors").mkdir(parents=True)
    (package / "connectors" / "fakeco.py").write_text(
        CARRIER_TEMPLATE.format(outcome=outcome).replace(
            'PROVIDER = "fakeco"', f'PROVIDER = "{provider}"'))
    (package / "dbbasic-package.json").write_text(json.dumps({
        "id": "app-fakeco", "name": "FakeCo", "version": "1.0.0",
        "connectors": [{"collection": "shipments",
                        "module": "connectors/fakeco.py", "entry": "reconcile"}],
    }))
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(root))
    return root


# --- the shop with no carrier at all ----------------------------------------------

def test_an_unconfigured_carrier_leaves_the_manual_path_fully_working(
        tmp_path, monkeypatch):
    """The sentence the whole slice is built around. Nothing configured, and
    the parcel still has a tracking number, still prints, and is left
    exactly as the operator typed it."""
    data_dir, objects_root = shop(tmp_path, monkeypatch)
    shipment(data_dir, tracking="1Z-TYPED-BY-HAND")
    object_records.update_collection_record(
        "shipments", "shp-1", {"tracking_status": "Handed to the driver"},
        base_dir=data_dir, actor="operator")

    result = poll(objects_root)
    assert result["provider"] == "none"
    assert "carrier.provider" in result["skipped"]
    assert result["polled"] == 0

    row = read(data_dir)
    assert row["tracking_number"] == "1Z-TYPED-BY-HAND"
    assert row["tracking_status"] == "Handed to the driver"
    assert row["tracking_attempts"] in ("", "0")
    assert row["tracking_checked_at"] == ""
    assert row["status"] == "shipped"

    slip = run(objects_root, "site_packing_slip", "GET", {"shipment_id": "shp-1"})
    assert "1Z-TYPED-BY-HAND" in slip["body"]


def test_buying_a_label_with_no_provider_is_a_409_that_names_the_setting(
        tmp_path, monkeypatch):
    """Stripe's precedent: the shop can see the button, so the button owes
    them a sentence they can act on rather than a 404 or a fake success."""
    data_dir, objects_root = shop(tmp_path, monkeypatch)
    shipment(data_dir, tracking="")

    refused = buy_label(objects_root, shipment_id="shp-1")
    assert refused["status"] == 409
    assert refused["missing_settings"] == ["carrier.provider"]
    assert "carrier.provider" in refused["error"]
    assert "manual" in refused["error"]
    assert read(data_dir)["tracking_number"] == ""


def test_an_unknown_shipment_and_a_finished_one_are_refused_before_any_carrier(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    assert buy_label(objects_root, shipment_id="no-such")["status"] == 404
    assert buy_label(objects_root)["status"] == 400

    shipment(data_dir, "shp-done", status="delivered")
    refused = buy_label(objects_root, shipment_id="shp-done",
                        tracking_number="TOO-LATE")
    assert refused["status"] == 409
    assert "delivered" in refused["error"]


# --- the manual carrier ------------------------------------------------------------

LABEL = base64.b64encode(b"%PDF-1.4 fake label").decode()


def test_a_manual_label_stamps_the_tracking_number_and_the_file(
        tmp_path, monkeypatch):
    """The label is evidence (docs/logic-decisions.md #8): the bytes are
    stored and pointed at, not described."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir, tracking="")

    result = buy_label(objects_root, shipment_id="shp-1",
                       tracking_number="1Z-COUNTER-42", label_base64=LABEL,
                       label_filename="counter.pdf")
    assert result["ok"] and result["provider"] == "manual"
    assert result["kind"] == "shipping label"
    assert result["tracking_number"] == "1Z-COUNTER-42"

    row = read(data_dir)
    assert row["tracking_number"] == "1Z-COUNTER-42"
    assert row["label_file_id"] == result["label_file_id"]
    assert result["label_url"] == f"/api/files/{result['label_file_id']}"
    # The bytes are really there, and openable through the ordinary door.
    assert object_user_files.read_file("shop", result["label_file_id"],
                                       base_dir=data_dir) == b"%PDF-1.4 fake label"
    record = object_records.get_collection_record(
        "files", result["label_file_id"], base_dir=data_dir)
    assert record["parent_collection"] == "shipments"
    assert record["parent_id"] == "shp-1"
    assert record["filename"] == "counter.pdf"
    # The label did not move the parcel: printing is not handing over.
    assert row["status"] == "shipped"


def test_a_second_label_is_a_second_artefact_not_an_overwrite(
        tmp_path, monkeypatch):
    """Evidence is never edited to agree with the current plan -- the first
    label may be on a box somewhere."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir, tracking="")

    first = buy_label(objects_root, shipment_id="shp-1",
                      tracking_number="ONE", label_base64=LABEL)
    second = buy_label(objects_root, shipment_id="shp-1",
                       tracking_number="TWO", label_base64=LABEL)
    assert first["label_file_id"] != second["label_file_id"]
    assert object_user_files.read_file("shop", first["label_file_id"],
                                       base_dir=data_dir)
    assert read(data_dir)["label_file_id"] == second["label_file_id"]


def test_a_manual_label_needs_the_number_the_counter_gave_you(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir, tracking="")

    refused = buy_label(objects_root, shipment_id="shp-1")
    assert refused["status"] == 400
    assert "tracking number" in refused["error"]
    assert read(data_dir)["tracking_number"] == ""


def test_a_label_without_a_pdf_still_records_the_tracking_number(
        tmp_path, monkeypatch):
    """Plenty of shops print from the carrier's own site and never hold the
    bytes; refusing them a tracking number over a missing PDF would be
    paperwork for its own sake."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir, tracking="")

    result = buy_label(objects_root, shipment_id="shp-1", tracking_number="NO-PDF")
    assert result["ok"] and result["label_file_id"] == ""
    assert read(data_dir)["tracking_number"] == "NO-PDF"


def test_a_return_label_is_the_same_verb_against_an_inbound_shipment(
        tmp_path, monkeypatch):
    """No action_buy_return_label: a return label is the same purchase with
    the addresses reversed, and a second verb is a second place to drift."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir, "rma-1", direction="inbound", status="authorized",
             tracking="", shipped_on="")

    result = buy_label(objects_root, shipment_id="rma-1",
                       tracking_number="RETURN-7", label_base64=LABEL)
    assert result["ok"]
    assert result["direction"] == "inbound"
    assert result["kind"] == "return label"
    assert read(data_dir, "rma-1")["tracking_number"] == "RETURN-7"


def test_the_manual_carrier_never_pretends_to_have_tracked_anything(
        tmp_path, monkeypatch):
    """A poll that "succeeded" every hour by writing back the status a human
    typed would burn tracking_checked_at into a timestamp meaning nothing."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="manual")
    shipment(data_dir)

    result = poll(objects_root)
    assert result["provider"] == "manual"
    assert result["considered"] == 1
    assert result["declined"] == 1 and result["polled"] == 0
    assert read(data_dir)["tracking_attempts"] in ("", "0")
    assert read(data_dir)["tracking_checked_at"] == ""


# --- a real carrier, plugged in ------------------------------------------------------

def test_the_poll_marks_delivered_and_the_order_follows(tmp_path, monkeypatch):
    """The one derived move this pass makes, and the reason it is allowed to
    make it: the carrier just reported the fact. Everything after the stamp
    is system_order_fulfillment's already-tested job."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": True, "status": "Delivered, front porch", '
                    '"delivered": True, "delivered_on": "2026-07-24"}')
    # The order is already `shipped` off the handover, the way the ladder
    # runs in life: this pass is only ever the last rung.
    assert fulfil(objects_root, "shp-1")["order_status"] == "shipped"

    result = poll(objects_root)
    assert result["polled"] == 1 and result["delivered"] == 1

    row = read(data_dir)
    assert row["status"] == "delivered"
    assert row["delivered_on"] == "2026-07-24"
    assert row["tracking_status"] == "Delivered, front porch"
    assert row["tracking_checked_at"]
    assert row["tracking_attempts"] == "0"

    # The write fires the change dispatcher in production; here we call the
    # handler with the payload it would receive.
    assert fulfil(objects_root, "shp-1")["order_status"] == "delivered"
    assert object_records.get_collection_record(
        "orders", "ord-1", base_dir=data_dir)["status"] == "delivered"


def test_a_replayed_poll_is_idempotent(tmp_path, monkeypatch):
    """Delivered is terminal, so the second pass has nothing in flight to
    ask about -- and the delivery date the carrier gave is not rewritten by
    a later run that would have used today's."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": True, "status": "Delivered", "delivered": True, '
                    '"delivered_on": "2026-07-24"}')

    fulfil(objects_root, "shp-1")                     # the order is `shipped`
    assert poll(objects_root)["delivered"] == 1
    fulfil(objects_root, "shp-1")
    settled = read(data_dir)

    again = poll(objects_root)
    fulfil(objects_root, "shp-1")
    assert again["considered"] == 0 and again["delivered"] == 0
    # Not "nearly the same": the row is untouched, tracking_checked_at
    # included, because a delivered parcel was never asked about again.
    assert read(data_dir) == settled
    assert settled["delivered_on"] == "2026-07-24"
    assert object_records.get_collection_record(
        "orders", "ord-1", base_dir=data_dir)["status"] == "delivered"


def test_a_parcel_still_moving_keeps_its_status_and_gains_the_carriers_words(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="shipped")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": True, "status": "In transit, Memphis TN", '
                    '"delivered": False}')

    assert poll(objects_root)["delivered"] == 0
    row = read(data_dir)
    assert row["status"] == "shipped"          # ours is not theirs to set
    assert row["tracking_status"] == "In transit, Memphis TN"


def test_a_shipment_that_keeps_failing_stops_being_polled_and_becomes_visible(
        tmp_path, monkeypatch):
    """Churning a metered carrier API forever against a mistyped tracking
    number is how a queue silently spends money."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": False, "error": "unknown tracking number"}')

    for expected in (1, 2, 3):
        result = poll(objects_root)
        assert result["failed"] == 1
        assert read(data_dir)["tracking_attempts"] == str(expected)

    row = read(data_dir)
    assert row["tracking_error"] == "unknown tracking number"
    assert row["status"] == "in_transit"       # the parcel is not cancelled

    quiet = poll(objects_root)
    assert quiet["considered"] == 0 and quiet["failed"] == 0
    assert quiet["needing_a_human"] == ["shp-1"]


def test_a_permanent_refusal_retires_a_parcel_at_once(tmp_path, monkeypatch):
    """Three polite retries against a typo is three more chances to be wrong
    slowly."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": False, "permanent": True, '
                    '"error": "no such tracking number, and there never was"}')

    assert poll(objects_root)["failed"] == 1
    assert read(data_dir)["tracking_attempts"] == "3"
    assert poll(objects_root)["needing_a_human"] == ["shp-1"]


def test_a_carrier_that_raises_costs_one_attempt_and_not_the_pass(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, "shp-1", status="in_transit")
    shipment(data_dir, "shp-2", status="in_transit", tracking="TRK-2")
    install_carrier(tmp_path, monkeypatch, '1 / 0')

    result = poll(objects_root)
    assert result["failed"] == 2               # both asked, neither lost
    assert "raised" in read(data_dir, "shp-2")["tracking_error"]


def test_a_connector_answering_to_another_name_is_never_used(
        tmp_path, monkeypatch):
    """Quietly shipping with a carrier the shop did not configure is worse
    than a pass that did nothing and said so."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    # app-shipping's own manual connector is the one installed, and it
    # answers to "manual".
    result = poll(objects_root)
    assert result["polled"] == 0
    assert "'manual'" in result["skipped"] and "'fakeco'" in result["skipped"]
    assert read(data_dir)["tracking_attempts"] in ("", "0")

    refused = buy_label(objects_root, shipment_id="shp-1", tracking_number="X")
    assert refused["status"] == 409
    assert "manual" in refused["error"]


def test_a_provider_nobody_installed_is_a_decline_not_a_failure(
        tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, status="in_transit")
    monkeypatch.setenv("DBBASIC_PACKAGES_DIR", str(tmp_path / "no-packages"))

    result = poll(objects_root)
    assert result["polled"] == 0
    assert "declares a connector" in result["skipped"]
    assert read(data_dir)["tracking_attempts"] in ("", "0")


def test_only_outbound_parcels_with_a_number_are_ever_asked_about(
        tmp_path, monkeypatch):
    """An inbound RMA's `received` means a human has the box and has looked
    at it; a carrier saying delivered is not that fact. A parcel with no
    tracking number is not failing, it simply cannot be asked about."""
    data_dir, objects_root = shop(tmp_path, monkeypatch, provider="fakeco")
    shipment(data_dir, "rma-1", direction="inbound", status="in_transit",
             tracking="RET-1", shipped_on="")
    shipment(data_dir, "shp-blank", status="shipped", tracking="")
    shipment(data_dir, "shp-done", status="delivered")
    shipment(data_dir, "shp-live", status="in_transit", tracking="TRK-LIVE")
    install_carrier(tmp_path, monkeypatch,
                    '{"ok": True, "status": "Moving", "delivered": False}')

    result = poll(objects_root)
    assert result["considered"] == 1
    assert [entry["shipment"] for entry in result["results"]] == ["shp-live"]
    assert read(data_dir, "rma-1")["tracking_status"] == ""


# --- the connector declaration itself ------------------------------------------------

def test_the_declared_reconcile_entry_declines_so_the_daemon_writes_nothing():
    """app-shipping declares a connector for shipments so the module is
    discoverable by the mechanism that already resolves connector modules
    safely. The daemon's desired-state pass therefore calls `reconcile`
    against every shipment on the box, and it must never converge anything:
    we cannot make a van arrive by wishing, and a skip leaves the row
    untouched and uncounted."""
    declaration = next(entry for entry in object_packages.iter_connectors(root=PACKAGES)
                       if entry["collection"] == "shipments")
    assert declaration["package_id"] == "app-shipping"
    reconcile = object_connectors.load_connector(declaration["module"],
                                                 declaration["entry"])
    outcome = reconcile({"id": "shp-1"}, base_dir="data")
    assert set(outcome) == {"skip", "reason"} and outcome["skip"] is True
    assert "system_tracking_poll" in outcome["reason"]


# --- the parcel that stopped moving ----------------------------------------------------

def days_ago(count):
    return (date.today() - timedelta(days=count)).isoformat()


def test_the_stuck_count_keys_on_in_transit_and_on_age(tmp_path, monkeypatch):
    """`shipped` is every parcel forever in a manual shop, and a band that
    is permanently lit is a band nobody reads."""
    data_dir, objects_root = shop(tmp_path, monkeypatch)
    shipment(data_dir, "old-transit", status="in_transit", shipped_on=days_ago(24))
    shipment(data_dir, "new-transit", status="in_transit", shipped_on=days_ago(2))
    shipment(data_dir, "old-shipped", status="shipped", shipped_on=days_ago(30))
    shipment(data_dir, "old-delivered", status="delivered", shipped_on=days_ago(30))
    shipment(data_dir, "no-date", status="in_transit", shipped_on="")
    shipment(data_dir, "inbound", direction="inbound", status="in_transit",
             shipped_on=days_ago(30))

    result = attention(objects_root)
    assert result["count"] == 1
    assert result["detail"] == "oldest 24 days out"


def test_the_stuck_threshold_is_a_setting(tmp_path, monkeypatch):
    data_dir, objects_root = shop(tmp_path, monkeypatch, stuck_days="3")
    shipment(data_dir, "four-days", status="in_transit", shipped_on=days_ago(4))
    assert attention(objects_root)["count"] == 1

    data_dir, objects_root = shop(tmp_path / "b", monkeypatch, stuck_days="30")
    shipment(data_dir, "four-days", status="in_transit", shipped_on=days_ago(4))
    assert attention(objects_root)["count"] == 0


def test_the_count_is_zero_where_shipping_is_not_installed(tmp_path, monkeypatch):
    """The pass runs every few minutes on every box; an app nobody installed
    must not log an error each time."""
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "empty"))
    objects_root = tmp_path / "objects"
    shutil.copytree(SHIPPING / "objects", objects_root, dirs_exist_ok=True)
    assert attention(objects_root) == {"count": 0}


# --- what the manifest declares ----------------------------------------------------------

def test_the_poll_is_declared_hourly_and_the_count_opens_a_real_index():
    package = object_packages.get_package("app-shipping", root=PACKAGES)
    schedule = next(entry for entry in package["schedules"]
                    if entry["object_id"] == "system_tracking_poll")
    assert schedule["schedule"].endswith("* * * *")      # hourly
    assert schedule["method"] == "POST"
    assert "hourly" in schedule["description"].lower()

    source = package["attention"][0]
    assert source["object_id"] == "system_shipment_attention"
    assert source["severity"] == "warning"
    assert source["path"] == "/shipments"                # no query string
    assert source["nav_id"] == "shipments"

    # The page the count opens lists the collection the count counted.
    view = next(row for row in _seed_rows("views") if row["id"] == "view_shipments_index")
    block = json.loads(view["blocks"])[0]
    assert block["kind"] == "list" and block["collection"] == "shipments"
    assert block["where"] == {"direction": "outbound"}


def _seed_rows(name):
    import csv
    with open(SHIPPING / "seed" / f"{name}.tsv", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))
