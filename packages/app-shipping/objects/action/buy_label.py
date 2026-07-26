"""action_buy_label -- put a tracking number and a label on a parcel,
whichever way this shop actually gets one.

POST {shipment_id, tracking_number?, carrier?, label_base64?,
      label_content_type?, label_filename?}

ONE VERB, BOTH DIRECTIONS. A return label is this action against an INBOUND
shipment; there is deliberately no action_buy_return_label. A return label
is the same purchase with the addresses the other way round, and a second
verb would be a second place for the credentials, the refusals, the file
handling and the stamping to drift -- which in practice means the return
half quietly rots, because it is used a tenth as often as the outbound half.

UNCONFIGURED IS A 409 THAT NAMES THE SETTING, never a broken button and
never a fake success. Same posture as action_stripe_checkout, and for the
same reason: the shop can see the button, so the button owes them a sentence
they can act on. `missing_settings` carries the same fact as a list, for a
client that wants to render the fix rather than the prose.

The whole flow works with no carrier at all -- that is the design, not a
consolation. With carrier.provider=manual the operator walks to the counter,
buys the postage, and brings back a tracking number (and, if they were
handed one, the label PDF). This action records both. It buys nothing and
says it bought nothing. What it gets in exchange for going through the
connector interface anyway is that the day a real adapter is installed, the
tracking number arrives on the same field, the label lands in the same file,
the manifest prints the same row and nothing downstream is rewritten.

THE LABEL FILE IS EVIDENCE (docs/logic-decisions.md #8), stored exactly the
way a scanned receipt is: bytes through object_user_files, a `files` row so
it can actually be opened at /api/files/{id}, and the id stamped on the
shipment. A re-bought label gets a NEW file id rather than overwriting the
old bytes -- the first label was really printed and may really be on a box,
and evidence is not edited to agree with the current plan.

A failure to store the FILE does not lose the TRACKING NUMBER. The number is
the thing a customer will be given and the thing the poll needs; the PDF is
a convenience the operator can re-upload. Refusing the whole action because
a file write failed would be the paperwork throwing away the fact to protect
the attachment.
"""

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import object_connectors
import object_ids
import object_packages
import object_records
import object_user_files

ACTOR = "action_buy_label"

# A shipping label is a few hundred kilobytes of PDF. The cap is here to
# refuse an accident, not to price a service.
MAX_BYTES = 10 * 1024 * 1024

# Journeys that are over. Buying a label for one of these is somebody on the
# wrong record, and stamping a new tracking number onto a delivered parcel
# would rewrite what the customer was told last week.
FINISHED = {"delivered", "returned_to_sender", "lost", "received",
            "dispositioned", "expired"}


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _setting(base, key, default=""):
    """Duplicated on purpose, same as every other package that reads
    app_settings (docs/logic-decisions.md #4)."""
    try:
        for row in object_records.read_collection_records("app_settings",
                                                          base_dir=base):
            if row.get("key") == key and _text(row.get("value")):
                return _text(row["value"])
    except Exception:
        pass
    return default


# --- the carrier boundary -----------------------------------------------------
#
# A deliberate second copy of the pair in system_tracking_poll, named in both
# files: objects on this platform cannot import one another (a sibling is
# executed, not imported), and #4 says the third occurrence is the one that
# earns the extraction.

def _carrier_declaration():
    """The connector declaration that owns `shipments` on this box, resolved
    through object_packages.iter_connectors -- the existing mechanism, which
    escape-checks the module path and lets a private overlay win."""
    packages = os.environ.get("DBBASIC_PACKAGES_DIR", object_packages.PACKAGES_DIR)
    private = (os.environ.get("DBBASIC_PRIVATE_PACKAGES_DIR")
               or str(Path(packages).parent / "packages-private"))
    roots = ([private] if Path(private).is_dir() else []) + [packages]
    for root in roots:
        for declaration in object_packages.iter_connectors(root=root):
            if declaration["collection"] == "shipments":
                return declaration
    return None


def _carrier(base, entry):
    """(function, provider, problem, missing) -- the configured carrier's
    `entry`, or the honest reason there is not one plus the settings whose
    absence caused it."""
    provider = _setting(base, "carrier.provider", "none").lower() or "none"
    if provider == "none":
        return None, provider, (
            "No carrier is configured on this server, so there is no label to "
            "buy. Set app_settings carrier.provider to `manual` and pass the "
            "tracking number from the post office counter (with the label PDF "
            "if you were handed one), or set it to an installed carrier "
            "adapter's name. Until then the shipment still works: type the "
            "tracking number on it and print the packing slip."
        ), ["carrier.provider"]

    declaration = _carrier_declaration()
    if declaration is None:
        return None, provider, (
            f"app_settings carrier.provider is {provider!r}, but no installed "
            f"package declares a connector for the shipments collection. "
            f"Install the carrier adapter package, or set carrier.provider to "
            f"`manual`."
        ), ["carrier.provider"]
    try:
        function = object_connectors.load_connector(declaration["module"], entry)
    except object_connectors.ConnectorLoadError as exc:
        return None, provider, (
            f"The carrier connector for {provider!r} could not be loaded: "
            f"{str(exc)[:200]}"
        ), []

    # The module's own name for itself, read off the loaded function's
    # globals: load_connector deliberately keeps the module out of
    # sys.modules, and a second loader to read one constant would be a
    # second loader.
    declared = _text(function.__globals__.get("PROVIDER"))
    if declared and declared.lower() != provider:
        return None, provider, (
            f"app_settings carrier.provider is {provider!r}, but the connector "
            f"installed for shipments answers to {declared!r}. Nothing was "
            f"bought: using a carrier the shop did not configure is worse than "
            f"a refusal."
        ), ["carrier.provider"]
    return function, provider, "", []


def _store_label(base, shipment, outcome):
    """Bytes to disk, a `files` row so somebody can open them, and the id.

    Returns (file_id, url, error). The `files` row is best-effort: a
    deployment without app-files installed still gets the bytes and the
    stamp, and says so, because a label held and unopenable beats a label
    refused.
    """
    content = outcome.get("label_bytes")
    if not content:
        return "", "", ""
    if not isinstance(content, (bytes, bytearray)):
        return "", "", "the carrier returned a label that was not bytes"

    owner = _text(shipment.get("owner_id")) or "shipping"
    # A NEW id per label, never a rewrite of the last one: the previous label
    # may be on a box somewhere, and evidence is not edited (#8).
    file_id = f"label-{object_ids.new_uuid4()}"
    try:
        size = object_user_files.save_file(owner, file_id, bytes(content),
                                           base_dir=base)
    except Exception as exc:                          # noqa: BLE001
        return "", "", f"the label could not be stored: {str(exc)[:160]}"

    try:
        object_records.create_collection_record(
            "files",
            {
                "id": file_id,
                "filename": (_text(outcome.get("label_filename"))
                             or f"label-{shipment['id'][:8]}.pdf"),
                "content_type": (_text(outcome.get("label_content_type"))
                                 or "application/pdf"),
                "size": str(size),
                "description": (f"Shipping label for shipment "
                                f"{shipment['id']}"),
                "parent_collection": "shipments",
                "parent_id": shipment["id"],
                "owner_id": owner,
            },
            base_dir=base, actor=ACTOR)
    except Exception as exc:                          # noqa: BLE001
        return file_id, "", (f"the label bytes were stored but no files record "
                             f"could be written ({str(exc)[:120]}), so it will "
                             f"not open at /api/files")
    return file_id, f"/api/files/{file_id}", ""


def POST(request):
    base = _base_dir()
    shipment_id = _text(request.get("shipment_id"))
    if not shipment_id:
        return {"status": 400, "error": "shipment_id is required"}

    try:
        shipment = object_records.get_collection_record("shipments",
                                                        shipment_id,
                                                        base_dir=base)
    except Exception:
        shipment = None
    if not shipment:
        return {"status": 404, "error": f"No such shipment: {shipment_id}"}

    status = _text(shipment.get("status"))
    if status in FINISHED:
        return {"status": 409,
                "error": (f"This shipment is {status}: its journey is over, so "
                          f"a new label would only overwrite what the customer "
                          f"was already told. Raise a new shipment if goods "
                          f"have to move again.")}

    inbound = _text(shipment.get("direction")) == "inbound"

    label_bytes = None
    raw = request.get("label_base64")
    if raw:
        try:
            label_bytes = base64.b64decode(_text(raw), validate=True)
        except Exception:
            return {"status": 400, "error": "label_base64 is not valid base64."}
        if len(label_bytes) > MAX_BYTES:
            return {"status": 413,
                    "error": (f"That label is larger than the "
                              f"{MAX_BYTES // (1024 * 1024)}MB cap.")}

    buy, provider, problem, missing = _carrier(base, "buy_label")
    if problem:
        return {"status": 409, "error": problem, "provider": provider,
                "missing_settings": missing,
                "note": ("the manual path still works with no carrier at all: "
                         "type the tracking number onto the shipment and print "
                         "the packing slip")}

    try:
        outcome = buy(shipment, base_dir=base,
                      tracking_number=_text(request.get("tracking_number")),
                      label_bytes=label_bytes,
                      label_content_type=_text(request.get("label_content_type")),
                      label_filename=_text(request.get("label_filename")))
        if not isinstance(outcome, dict):
            outcome = {"ok": False,
                       "error": (f"the carrier connector returned "
                                 f"{type(outcome).__name__}, expected a dict")}
    except Exception as exc:                          # noqa: BLE001
        outcome = {"ok": False,
                   "error": f"the carrier connector raised: {str(exc)[:200]}"}

    if outcome.get("skip"):
        # The connector declined rather than failed -- the host is not ready.
        return {"status": 409, "provider": provider,
                "error": _text(outcome.get("reason")) or
                         "the carrier connector declined to sell a label."}
    if not outcome.get("ok"):
        # `permanent` means the request as posed can never work (a missing
        # tracking number, an address the carrier will not accept), which is
        # the caller's to fix -- 400. Anything else is the provider having a
        # bad minute, which is 502 and worth retrying, the same split
        # action_stripe_checkout makes.
        return {"status": 400 if outcome.get("permanent") else 502,
                "provider": provider,
                "error": _text(outcome.get("error")) or
                         "the carrier could not produce a label."}

    tracking_number = _text(outcome.get("tracking_number"))
    file_id, url, file_error = _store_label(base, shipment, outcome)

    update = {}
    if tracking_number:
        update["tracking_number"] = tracking_number
    if file_id:
        update["label_file_id"] = file_id
    carrier = _text(outcome.get("carrier")) or _text(request.get("carrier"))
    if carrier:
        update["carrier"] = carrier
    if update:
        object_records.update_collection_record(
            "shipments", shipment_id, update, base_dir=base, actor=ACTOR)

    return {
        "ok": True,
        "shipment_id": shipment_id,
        "direction": "inbound" if inbound else "outbound",
        "kind": "return label" if inbound else "shipping label",
        "provider": provider,
        "tracking_number": tracking_number,
        "label_file_id": file_id,
        "label_url": url,
        "carrier": carrier or _text(shipment.get("carrier")),
        "status_of_shipment": status,
        "bought_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "slip_path": f"/shipments/{shipment_id}/slip",
        # The status ladder is not this action's business: a label on the
        # bench is not a parcel out of the door, and moving it would let a
        # printer jam look like a handover.
        "note": (_text(outcome.get("note"))
                 or "label recorded; the shipment's status is unchanged"),
        **({"label_error": file_error} if file_error else {}),
    }
