"""The tests that keep a generated privacy policy true.

A privacy policy is the one document in a system that nothing checks. It
is written once, by a template, and the software changes weekly; nothing
connects the two, so nothing notices when they part company. Generating
the page from the configuration removes most of that drift by
construction -- but only most of it, because the generator itself is code
somebody can forget to update.

So the first test in this file is a SWEEP, and it is the one that matters:
it walks every source file on the box looking for anything that sets a
cookie, and fails unless every cookie name it finds is disclosed in the
policy's table, naming the undisclosed cookie in the failure message. A
new cookie added anywhere in this repo breaks the build until somebody
writes down what it is for and whether it is strictly necessary. That is a
property no amount of care in the policy module can give you, because the
thing it guards against happens in a file the policy module has never
heard of.

The rest hold the fold honest in the directions it could go wrong: a
retention it states that the box does not keep, a section for an app that
is not installed, a sub-processor nobody configured, a policy nobody
signed, and an export that returns somebody else's rows.
"""

import importlib.util
import json
import pathlib
import re

import pytest

from conftest import stage_collection

import object_analytics
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
PRIVACY_OBJECTS = PACKAGES / "app-privacy" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def _module(relative):
    """Import a package object as a module, for the folds a test needs to
    call directly rather than through a rendered page."""
    path = PACKAGES / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


privacy = _module("app-privacy/objects/site/privacy.py")


# ===========================================================================
# 1. The sweep: every cookie this codebase sets is disclosed
# ===========================================================================

# A file is worth reading at all only if it mentions setting a cookie.
_COOKIE_FILE = re.compile(r"set[_-]cookie|max-age", re.I)
# ...and a candidate name counts only if a cookie ATTRIBUTE is nearby, so
# an ordinary `days=7` in a query string is not mistaken for a cookie.
_COOKIE_ATTRIBUTE = re.compile(r"Path=/|HttpOnly|SameSite|Max-Age|set[_-]cookie",
                               re.I)
# The two shapes a cookie name is written in here: a constant interpolated
# into the header (`f"{COOKIE}={token}; Path=/"`) or a literal
# (`"consent=yes; Path=/"`).
_NAME_FROM_CONSTANT = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\s*=")
_NAME_LITERAL = re.compile(r"""["']\s*([A-Za-z][A-Za-z0-9_-]*)=""")
_LITERAL_MAX_AGE = re.compile(r"Max-Age=(\d+)")
_WINDOW = 300

# Cookie attributes are not cookie names; they look identical to the
# literal pattern above and are the only false positives it produces.
_ATTRIBUTE_WORDS = {"path", "httponly", "samesite", "max-age", "secure",
                    "expires", "domain", "priority", "partitioned", "version"}


def _source_files():
    """Every Python file this server actually runs. Tests excluded: a test
    fixture is allowed to write `cart=xyz` without that being a cookie the
    product sets."""
    files = list(REPO_ROOT.glob("*.py")) + list(PACKAGES.rglob("*.py"))
    return [path for path in files if "__pycache__" not in path.parts]


def _constant_values():
    """Every `NAME = "literal"` on the box, so a cookie whose name is a
    constant resolves to the string a browser would actually see."""
    values = {}
    pattern = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*[\"']([^\"'\n]+)[\"']",
        re.M)
    for path in _source_files():
        for match in pattern.finditer(path.read_text(encoding="utf-8",
                                                     errors="replace")):
            values.setdefault(match.group(1), match.group(2))
    return values


def _cookies_set_in_source():
    """(cookie name, file, literal Max-Age or None) for everything on this
    box that puts a cookie in a browser.

    An unresolvable name is returned as-is rather than dropped: a cookie
    written in a shape this sweep cannot read must fail the test too,
    because the alternative is a sweep that goes quietly blind.
    """
    constants = _constant_values()
    found = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _COOKIE_FILE.search(text):
            continue
        for pattern, from_constant in ((_NAME_FROM_CONSTANT, True),
                                       (_NAME_LITERAL, False)):
            for match in pattern.finditer(text):
                window = text[max(0, match.start() - _WINDOW):
                              match.end() + _WINDOW]
                if not _COOKIE_ATTRIBUTE.search(window):
                    continue
                raw = match.group(1)
                if not from_constant and raw.lower() in _ATTRIBUTE_WORDS:
                    continue
                name = (constants.get(raw.split(".")[-1], raw) if from_constant
                        else raw)
                ages = set(_LITERAL_MAX_AGE.findall(window))
                age = int(ages.pop()) if len(ages) == 1 else None
                found.append((name, path.relative_to(REPO_ROOT).as_posix(), age))
    return found


def test_every_cookie_this_codebase_sets_is_disclosed_in_the_policy():
    """THE test that keeps the policy true.

    Nothing else in this file can stop the drift this one stops, because
    the drift happens in a file the policy module has never heard of: a
    package adds a cookie, ships, and the policy goes on describing a
    server that no longer exists. A policy that CAN be wrong about the
    software is the normal case on the web; the only thing that makes one
    that cannot is a check that reads the software rather than the policy.
    """
    disclosed = privacy.disclosed_cookie_names()
    undisclosed = sorted({(name, where)
                          for name, where, _age in _cookies_set_in_source()
                          if name not in disclosed})
    assert not undisclosed, (
        "these cookies are set by this codebase and disclosed nowhere in the "
        f"privacy policy's cookie table: {undisclosed}. Add each one to "
        "COOKIE_DISCLOSURES in packages/app-privacy/objects/site/privacy.py "
        "with what it is for, when it is set, and whether it is strictly "
        "necessary for something the visitor asked for -- that last answer is "
        "the one with a consent consequence. If the name shown is an "
        "unresolved identifier rather than a cookie name, the sweep could not "
        "read the shape it is written in; write the name as a module-level "
        "constant so it can.")


def test_the_sweep_can_actually_see_the_cookies_this_server_sets():
    """A guard on the guard. A regex that matches nothing passes the test
    above forever, and the failure would be silent and permanent -- the
    single most dangerous shape a test can have."""
    found = {name for name, _where, _age in _cookies_set_in_source()}
    assert {"dbbasic_session", "cart", object_analytics.VISITOR_COOKIE_NAME} <= found, (
        f"the sweep found {sorted(found)}; it should at minimum see the "
        "identity session cookie, the shop's basket cookie and the visitor "
        "cookie. If one has moved, fix the sweep -- do not narrow it.")


def test_a_disclosed_lifetime_matches_the_one_the_code_actually_writes():
    """Disclosing a cookie is not enough if the number beside it is
    wrong. Where the source writes a literal Max-Age, the policy must
    publish that same number of seconds."""
    wrong = []
    for name, where, age in _cookies_set_in_source():
        if age is None or name not in privacy.disclosed_cookie_names():
            continue
        published = privacy.cookie_max_age_seconds(name)
        if published != age:
            wrong.append(f"{name} in {where}: code sets {age}s, "
                         f"policy publishes {published}s")
    assert not wrong, (
        f"the policy publishes a lifetime the code does not set: {wrong}")


def test_the_policy_states_no_law_it_cannot_verify():
    """The refusal, held as a property rather than a promise in a
    docstring. Confident paragraphs about lawful bases and transfer
    mechanisms would be legal claims about somebody else's business,
    generated by a program that cannot know it -- and pasted into the one
    document a regulator reads. Wrong legal text is worse than none."""
    fold = privacy.policy(base="/nonexistent", env={})
    rendered = json.dumps(fold) + privacy.GET({"_path": "/privacy"})["body"]
    forbidden = ["standard contractual clause", "lawful basis", "lawful bases",
                 "legitimate interest", "article 6", "adequacy decision",
                 "gdpr compliant", "we are compliant"]
    present = [phrase for phrase in forbidden if phrase in rendered.lower()]
    assert not present, (
        f"the generated policy asserts legal conclusions it cannot verify: "
        f"{present}. Facts about the system go here; legal framing goes in "
        "privacy.extra_markdown, where a human signs it.")


# ===========================================================================
# 2. The fold: the policy says what the box is configured to do
# ===========================================================================

SIGNED = (
    ("privacy.controller_name", "Q9 Ltd"),
    ("privacy.contact_email", "privacy@example.test"),
    ("privacy.jurisdiction", "England and Wales"),
)


def box(tmp_path, monkeypatch, *, settings=SIGNED, apps=(), analytics=False,
        visitor_cookie=False, retention=None, env=()):
    """One configured server. `apps` names packages whose collection is
    staged, which is what makes them installed as far as the policy is
    concerned."""
    data_dir = tmp_path / "data"
    rows = "".join(f"s{index}\t{key}\t{value}\t\n"
                   for index, (key, value) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    for package_id, collection in apps:
        stage_collection(data_dir, package_id, collection)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    if analytics:
        monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    else:
        monkeypatch.delenv("DBBASIC_ANALYTICS", raising=False)
    if visitor_cookie:
        monkeypatch.setenv("DBBASIC_ANALYTICS_VISITOR_COOKIE", "on")
    else:
        monkeypatch.delenv("DBBASIC_ANALYTICS_VISITOR_COOKIE", raising=False)
    if retention is not None:
        monkeypatch.setenv("DBBASIC_ANALYTICS_RETENTION_DAYS", str(retention))
    else:
        monkeypatch.delenv("DBBASIC_ANALYTICS_RETENTION_DAYS", raising=False)
    for key, value in env:
        monkeypatch.setenv(key, value)
    return data_dir


def render(path="/privacy"):
    """Through the real execution path, exactly as a visitor reaches it."""
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_privacy", method="GET", payload={"_path": path}),
        roots=[PRIVACY_OBJECTS]).result


def test_it_states_the_retention_the_box_is_actually_configured_with(
        tmp_path, monkeypatch):
    """The property the whole design is for. A policy that claims 30 days
    while the box keeps 180 is the normal case on the web, and it is a
    claim nobody can be held to because nobody wrote it deliberately."""
    box(tmp_path, monkeypatch, analytics=True, retention=45)
    assert "45 days" in render()["body"]


def test_changing_the_setting_changes_the_page(tmp_path, monkeypatch):
    """Not merely "a number appears" -- the number follows the box. This
    is what makes the page a fold rather than a template with a variable
    in it."""
    box(tmp_path, monkeypatch, analytics=True, retention=7)
    assert "7 days" in render()["body"]
    monkeypatch.setenv("DBBASIC_ANALYTICS_RETENTION_DAYS", "180")
    body = render()["body"]
    assert "180 days" in body and "7 days" not in body


def test_analytics_off_removes_the_section_entirely(tmp_path, monkeypatch):
    """Not "0 page views kept" -- the honest statement is that nothing is
    collected at all, and a section full of zeros would read as a
    collection that happens to be empty."""
    box(tmp_path, monkeypatch, analytics=False)
    body = render()["body"]
    assert "This server keeps no log of page views" in body
    assert "User agent" not in body and "Referrer" not in body
    fold = privacy.policy()
    assert fold["analytics"]["fields"] == []
    assert fold["analytics"]["retention_days"] == 0


def test_analytics_on_names_the_fields_actually_recorded(tmp_path, monkeypatch):
    """"Usage data" is the phrasing that lets a policy mean anything. The
    fields are named one by one because they are knowable."""
    box(tmp_path, monkeypatch, analytics=True)
    body = render()["body"]
    for field in ("IP address", "User agent", "Referrer", "Path and method"):
        assert field in body, field


def test_the_cookie_table_follows_the_configuration(tmp_path, monkeypatch):
    """Off -- the default -- and the analytics cookie is not in the table,
    because it cannot be set. On, and it appears with the real Max-Age
    this server writes, not a number somebody typed."""
    box(tmp_path, monkeypatch, analytics=True, visitor_cookie=False)
    off = render("/cookies")["body"]
    assert object_analytics.VISITOR_COOKIE_NAME not in off
    assert "dbbasic_session" in off        # the necessary one is still listed

    box(tmp_path, monkeypatch, analytics=True, visitor_cookie=True)
    on = render("/cookies")["body"]
    assert object_analytics.VISITOR_COOKIE_NAME in on
    assert f"Max-Age={object_analytics.DEFAULT_VISITOR_DAYS * 86400}" in on
    # And the consent-relevant column is answered honestly: this is the
    # one cookie here that is not strictly necessary.
    rows = {row["name"]: row for row in privacy.cookie_table()}
    assert rows[object_analytics.VISITOR_COOKIE_NAME]["strictly_necessary"] is False
    assert rows["dbbasic_session"]["strictly_necessary"] is True


def test_the_basket_cookie_appears_only_where_there_is_a_shop(
        tmp_path, monkeypatch):
    box(tmp_path, monkeypatch)
    assert "cart" not in {row["name"] for row in privacy.cookie_table()}

    box(tmp_path, monkeypatch, apps=[("app-shop", "carts")])
    row = {entry["name"]: entry for entry in privacy.cookie_table()}["cart"]
    assert row["strictly_necessary"] is True
    assert row["max_age_seconds"] == privacy.CART_COOKIE_MAX_AGE


def test_sections_appear_only_when_the_thing_exists(tmp_path, monkeypatch):
    """A fulfilment paragraph on a box that ships nothing is invented
    processing, and invented processing in a privacy policy is the
    failure mode this page exists to remove."""
    box(tmp_path, monkeypatch)
    bare = render()["body"]
    assert "Delivery" not in bare and "Payments" not in bare

    box(tmp_path, monkeypatch, apps=[("app-shipping", "shipments"),
                                     ("app-payments", "payments")])
    full = render()["body"]
    assert "Delivery" in full and "Payments" in full


def test_the_subprocessor_list_contains_only_configured_third_parties(
        tmp_path, monkeypatch):
    """Derived from credentials that are actually present, so it is a list
    nobody has to remember to update: a key removed from the environment
    removes the row."""
    box(tmp_path, monkeypatch, apps=[("app-payments", "payments")])
    assert privacy.subprocessors() == []
    assert "No third party is configured" in render()["body"]

    monkeypatch.setenv("DBBASIC_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("DBBASIC_STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("DBBASIC_SMTP_MODE", "live")
    monkeypatch.setenv("DBBASIC_SMTP_HOST", "smtp.example.test")
    names = {row["name"] for row in privacy.subprocessors()}
    assert names == {"Stripe", "smtp.example.test"}

    # Half-wired Stripe is not a sub-processor: without the webhook secret
    # the integration cannot run at all, so claiming a data flow that
    # cannot happen would be as wrong as omitting one that can.
    monkeypatch.delenv("DBBASIC_STRIPE_WEBHOOK_SECRET")
    assert "Stripe" not in {row["name"] for row in privacy.subprocessors()}


def test_a_carrier_is_a_subprocessor_only_when_one_is_connected(
        tmp_path, monkeypatch):
    """`manual` means the operator walks to the counter: no address
    leaves this server, so no carrier belongs on the list."""
    for provider in ("", "none", "manual"):
        settings = SIGNED + (("carrier.provider", provider),) if provider else SIGNED
        box(tmp_path, monkeypatch, settings=settings,
            apps=[("app-shipping", "shipments")])
        assert privacy.subprocessors() == [], provider

    box(tmp_path, monkeypatch, settings=SIGNED + (("carrier.provider", "ups"),),
        apps=[("app-shipping", "shipments")])
    assert [row["name"] for row in privacy.subprocessors()] == ["ups"]


def test_it_refuses_to_publish_a_policy_nobody_signed(tmp_path, monkeypatch):
    """An unsigned policy is worse than none: it looks like compliance
    while giving a reader nobody to write to. So the page states no
    claims at all and says exactly which settings are missing."""
    box(tmp_path, monkeypatch, settings=(), analytics=True, retention=45)
    body = render()["body"]
    assert "No privacy policy has been published" in body
    for key, _value in SIGNED:
        assert key in body
    # And it makes no claims while unsigned -- not even true ones.
    assert "45 days" not in body


def test_the_refusal_names_only_the_settings_actually_missing(
        tmp_path, monkeypatch):
    box(tmp_path, monkeypatch, settings=SIGNED[:1])
    fold = privacy.policy()
    assert fold["controller"]["missing_settings"] == [
        "privacy.contact_email", "privacy.jurisdiction"]
    assert fold["controller"]["signed"] is False

    box(tmp_path, monkeypatch, settings=SIGNED)
    assert privacy.policy()["controller"]["signed"] is True


def test_the_cookie_table_is_published_even_when_the_policy_is_not(
        tmp_path, monkeypatch):
    """The browser-facing facts are true regardless of who signed them,
    and a visitor asking what is in their browser deserves an answer even
    on a box whose operator has not filled the form in."""
    box(tmp_path, monkeypatch, settings=())
    body = render("/cookies")["body"]
    assert "dbbasic_session" in body


def test_a_signed_policy_names_the_controller_and_how_to_reach_them(
        tmp_path, monkeypatch):
    box(tmp_path, monkeypatch)
    body = render()["body"]
    assert "Q9 Ltd" in body
    assert "England and Wales" in body
    assert "privacy@example.test" in body


def test_the_operators_own_legal_text_is_rendered_and_attributed(
        tmp_path, monkeypatch):
    """Under its own heading, so a reader can tell which half of the page
    is machine-generated fact and which half is a human's statement."""
    box(tmp_path, monkeypatch,
        settings=SIGNED + (("privacy.extra_markdown",
                            "Disputes go to the courts of England and Wales."),))
    body = render()["body"]
    assert "Additional terms from Q9 Ltd" in body
    assert "Disputes go to the courts of England and Wales." in body
    assert "the operator&#x27;s own text" in body or "operator's own text" in body


def test_the_three_surfaces_are_one_fold(tmp_path, monkeypatch):
    """/privacy, /cookies and /privacy.json cannot disagree because there
    is nothing for them to disagree about."""
    box(tmp_path, monkeypatch, analytics=True, visitor_cookie=True, retention=90)
    payload = json.loads(render("/privacy.json")["body"])
    assert payload["analytics"]["retention_days"] == 90
    assert {row["name"] for row in payload["cookies"]} == {
        row["name"] for row in privacy.cookie_table()}
    assert "90 days" in render()["body"]
    for row in payload["cookies"]:
        assert row["name"] in render("/cookies")["body"]


def test_the_json_carries_no_timestamp_so_it_can_be_diffed(
        tmp_path, monkeypatch):
    """A generated-at stamp would make every fetch differ from the last
    one and hide the changes that matter -- which is the entire reason
    somebody would fetch it twice."""
    box(tmp_path, monkeypatch, analytics=True)
    assert render("/privacy.json")["body"] == render("/privacy.json")["body"]


def test_the_json_is_served_even_unsigned_and_says_it_is_unsigned(
        tmp_path, monkeypatch):
    """404ing would leave a monitor guessing. The honest answer is the
    fold, with `signed: false` in it."""
    box(tmp_path, monkeypatch, settings=())
    payload = json.loads(render("/privacy.json")["body"])
    assert payload["controller"]["signed"] is False
    assert payload["controller"]["missing_settings"]


def test_the_default_box_tells_a_visitor_nothing_is_stored_about_them(
        tmp_path, monkeypatch):
    """The posture the whole design is aimed at, read from the visitor's
    side: analytics off, no cookie but the necessary ones, no third
    party. This is the page a default install publishes."""
    box(tmp_path, monkeypatch)
    body = render()["body"]
    assert "This server keeps no log of page views" in body
    assert "No third party is configured" in body
    assert object_analytics.VISITOR_COOKIE_NAME not in body


# ===========================================================================
# 3. The export: this subject's rows, and nobody else's
# ===========================================================================

EXPORT_OBJECT = "action_export_subject_data"
ADMIN = {"_identity": {"user_id": "dan", "roles": ["admin"]}}

ANNA = "anna@example.test"
BEN = "ben@example.test"


def export(payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            EXPORT_OBJECT, method="POST", payload=payload),
        roots=[PRIVACY_OBJECTS]).result


@pytest.fixture()
def two_customers(tmp_path, monkeypatch):
    """Anna and Ben, each with an order, an invoice, a payment against
    that invoice and a shipment against that order. The whole point of
    the fixture is that there are two of them."""
    data_dir = tmp_path / "data"
    for package_id, collection in (("app-orders", "orders"),
                                   ("app-invoices", "invoices"),
                                   ("app-payments", "payments"),
                                   ("app-shipping", "shipments"),
                                   ("app-shop", "carts"),
                                   ("app-disputes", "disputes"),
                                   ("app-contacts", "contacts"),
                                   ("app-analytics", "conversions")):
        stage_collection(data_dir, package_id, collection)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    for who, tag in ((ANNA, "a"), (BEN, "b")):
        object_records.create_collection_record(
            "orders", {"id": f"ord-{tag}", "number": f"SO-{tag}",
                       "customer_email": who,
                       "customer_name": who.split("@")[0], "status": "confirmed"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "invoices", {"id": f"inv-{tag}", "number": f"INV-{tag}",
                         "customer_name": who.split("@")[0],
                         "customer_email": who, "status": "sent"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "payments", {"id": f"pay-{tag}", "invoice_id": f"inv-{tag}",
                         "amount_cents": "1000", "received_on": "2026-07-01"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "shipments", {"id": f"shp-{tag}", "order_id": f"ord-{tag}",
                          "status": "shipped"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "carts", {"id": f"cart-{tag}", "session_token": f"tok-{tag}",
                      "customer_email": who, "status": "open"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "disputes", {"id": f"dis-{tag}", "customer_email": who,
                         "kind": "delivery", "summary": "missing parcel",
                         "status": "open"},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "contacts", {"id": f"con-{tag}", "first_name": who.split("@")[0],
                         "email": who},
            base_dir=data_dir, actor="test")
        object_records.create_collection_record(
            "conversions", {"id": f"cv-{tag}", "event_type": "order_confirmed",
                            "metadata": json.dumps({"source": f"orders/ord-{tag}"})},
            base_dir=data_dir, actor="test")
    return data_dir


def test_the_export_returns_this_subjects_rows_and_nobody_elses(two_customers):
    """The test that matters in this file's second half.

    An export that leaks a second customer's order into the first
    customer's subject request is not a partial success; it is a data
    breach committed by the compliance feature.
    """
    result = export({**ADMIN, "email": ANNA})
    assert result["ok"] is True

    blob = json.dumps(result)
    assert BEN not in blob
    for row_id in ("ord-b", "inv-b", "pay-b", "shp-b", "cart-b", "dis-b",
                   "con-b", "cv-b"):
        assert row_id not in blob, row_id

    ids = {collection: {row["id"] for row in rows}
           for collection, rows in result["collections"].items()}
    assert ids["orders"] == {"ord-a"}
    assert ids["invoices"] == {"inv-a"}
    assert ids["carts"] == {"cart-a"}
    assert ids["disputes"] == {"dis-a"}
    assert ids["contacts"] == {"con-a"}


def test_linked_rows_are_reached_only_through_the_subjects_own_records(
        two_customers):
    """Payments hang off an invoice and shipments off an order, so the
    join runs in the safe direction only: the subject's ids select the
    linked rows, and a payment whose invoice is not theirs is never
    looked at again."""
    result = export({**ADMIN, "email": ANNA})
    assert {row["id"] for row in result["collections"]["payments"]} == {"pay-a"}
    assert {row["id"] for row in result["collections"]["shipments"]} == {"shp-a"}


def test_a_conversion_is_returned_only_when_it_genuinely_names_the_subject(
        two_customers):
    """By provenance -- the row names one of their own orders. Never by
    visitor token: that token is anonymous by construction and joining it
    to a person here would perform the exact de-anonymisation the
    analytics rules forbid, inside the feature meant to protect them."""
    result = export({**ADMIN, "email": ANNA})
    assert {row["id"] for row in result["collections"]["conversions"]} == {"cv-a"}


def test_the_export_says_what_it_cannot_search_rather_than_omitting_it(
        two_customers):
    """A silent omission reads as "we hold nothing about you", which is a
    different and untrue answer."""
    result = export({**ADMIN, "email": ANNA})
    collections = {entry["collection"] for entry in result["not_searchable"]}
    assert "page_views" in collections


def test_an_absent_collection_is_not_an_empty_one(tmp_path, monkeypatch):
    """"This server does not record shipments" and "you have no
    shipments" are different answers to a subject request, and only one
    of them is about them."""
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-orders", "orders")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    result = export({**ADMIN, "email": ANNA})
    assert "shipments" in result["unavailable"]
    assert "shipments" not in result["collections"]
    assert result["counts"]["orders"] == 0


def test_the_export_refuses_anybody_who_is_not_an_operator(two_customers):
    """No self-service door: it would be a lookup oracle for anyone who
    knows a customer's email address."""
    assert export({"email": ANNA})["status"] == 403
    assert export({"_identity": {"user_id": "anna", "roles": []},
                   "email": ANNA})["status"] == 403
    assert export({**ADMIN})["status"] == 400          # no email


def test_the_export_does_not_pretend_to_offer_erasure(two_customers):
    """Deliberately absent rather than quietly half-implemented: erasure
    needs a redaction-with-tombstone design and a doctrine decision about
    which fields are contact and which are evidence. Code written before
    that decision would make the choice silently, in the one place it can
    never be undone."""
    result = export({**ADMIN, "email": ANNA})
    assert "Not available through this action" in result["erasure"]


def test_the_email_match_is_case_and_space_insensitive(two_customers):
    """A subject request arrives typed by a human, not normalized by a
    form."""
    result = export({**ADMIN, "email": "  ANNA@Example.Test  "})
    assert result["counts"]["orders"] == 1
