"""Goals, funnels, and the one cookie this server sets.

Three things had to be true before `/visitors` could answer "did any of
that work", and each of them is a place where the obvious implementation
is dishonest rather than merely wrong.

**The cookie.** A first-party token is what turns Monday's reader and
Thursday's buyer into one person instead of two strangers. It is also the
point at which analytics products usually stop being honest, so every
rule in docs/analytics.md is asserted here as behaviour and not as prose:
set once and reused, never when Do Not Track or Global Privacy Control
says no, never on a path a browser did not choose to visit, and NEVER in
the same row as a `user_id`. That last one is one line of code away at all
times, which is exactly why it is pinned by a test rather than by a
comment.

**The conversion.** The change dispatcher promises at-least-once
delivery, and `status is confirmed` is a state an order SITS in rather
than an edge it crosses, so this handler WILL see the same order again
and again. A double-counted conversion is a permanent overcount in a
report nothing will ever correct, so the replay test is the point of this
file rather than a footnote to it.

**The folds.** A funnel that hides its own uncertainty, a returning-
visitor count presented as a census, and a median that quietly drops the
journeys whose beginning aged out are all numbers that mislead precisely
when somebody is relying on them. Each fold returns its caveat as DATA,
and the tests assert the caveat is there.
"""

import asyncio
import datetime
import json
import pathlib
import shutil

import pytest
from conftest import stage_collection

import object_analytics
import object_conversions
import object_execution
import object_records
import object_server
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
ANALYTICS_OBJECTS = PACKAGES / "app-analytics" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
BROWSER = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/128 Safari/537.36"


# ===========================================================================
# 1. The visitor cookie
# ===========================================================================

@pytest.fixture()
def site(tmp_path, monkeypatch):
    """An ASGI server with analytics on and one page object to hit.

    None of the cookie behaviour is testable in-process: the header is
    added by the response layer and the token is stamped by the capture
    hook, so a test that calls an object directly proves nothing about
    either. These drive the app, same as tests/test_site_cookies.py.
    """
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-analytics", "page_views")
    objects_dir = tmp_path / "objects"
    (objects_dir / "site").mkdir(parents=True)
    (objects_dir / "site" / "probe.py").write_text(
        "import json\n"
        "def GET(request):\n"
        "    return {'status': 200, 'content_type': 'application/json',\n"
        "            'body': json.dumps(request.get('_cookies'))}\n")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_SITE_ROUTES", "true")
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    return data_dir


def call(path, *, method="GET", headers=None):
    sent = {}

    async def run():
        scope = {"type": "http", "method": method, "path": path,
                 "query_string": b"",
                 "headers": [(k.lower().encode(), v.encode())
                             for k, v in (headers or {}).items()]}
        received = {"done": False}

        async def receive():
            if received["done"]:
                return {"type": "http.disconnect"}
            received["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = [(k.decode("latin-1"), v.decode("latin-1"))
                                   for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                sent.setdefault("body", b"")
                sent["body"] += message.get("body", b"")

        await object_server.app(scope, receive, send)

    asyncio.run(run())
    return sent


def visitor_cookies(sent):
    return [value for key, value in sent.get("headers", [])
            if key.lower() == "set-cookie"
            and value.startswith(object_analytics.VISITOR_COOKIE_NAME + "=")]


def page_views(data_dir):
    return object_records.read_collection_records("page_views",
                                                   base_dir=data_dir)


def test_the_cookie_is_set_once_and_then_reused(site):
    """Set on the first page and never again. A token reissued on every
    request is not a thread at all -- every visit would look new, which is
    the exact failure the cookie exists to fix, wearing a cookie's
    clothes."""
    first = call("/probe")
    minted = visitor_cookies(first)
    assert len(minted) == 1
    assert "Path=/" in minted[0] and "HttpOnly" in minted[0]
    assert "SameSite=Lax" in minted[0]
    assert f"Max-Age={object_analytics.DEFAULT_VISITOR_DAYS * 86400}" in minted[0]

    token = minted[0].split("=", 1)[1].split(";", 1)[0]
    assert token

    second = call("/probe", headers={
        "cookie": f"{object_analytics.VISITOR_COOKIE_NAME}={token}"})
    assert visitor_cookies(second) == []

    # And both requests are threaded to the same visitor -- including the
    # FIRST one, whose cookie the browser had not sent back yet.
    rows = page_views(site)
    assert [row["session_id"] for row in rows] == [token, token]


def test_the_token_is_opaque_and_carries_no_identity(site):
    """Rule 2. Nothing derived from the request, so nothing reversible
    back into one: a token hashed from IP and user agent would be a stable
    identifier nobody could ever clear."""
    tokens = set()
    for _ in range(3):
        tokens.add(visitor_cookies(call("/probe", headers={
            "user-agent": BROWSER, "x-forwarded-for": "203.0.113.9"}))[0])
    assert len(tokens) == 3


def test_do_not_track_suppresses_the_cookie_entirely(site):
    """Rule 5, and note what it does NOT do: the request is still counted
    as a page view. Refusing to be remembered is not refusing to be
    counted, and dropping the row would quietly under-report traffic in
    exactly the population most likely to notice."""
    sent = call("/probe", headers={"dnt": "1"})
    assert visitor_cookies(sent) == []
    rows = page_views(site)
    assert len(rows) == 1 and rows[0]["session_id"] == ""


def test_global_privacy_control_suppresses_it_too(site):
    """The same refusal in a different vocabulary, and it has to be
    honoured in both -- a server that reads one header and not the other
    honours whichever one its author happened to have heard of."""
    assert visitor_cookies(call("/probe", headers={"sec-gpc": "1"})) == []


def test_dnt_zero_is_consent_and_not_a_refusal(site):
    """`DNT: 0` is a positive statement, and an absent header is no
    statement at all. Reading either as a refusal would mean this server
    never remembered anybody, which is not honesty, just breakage."""
    assert len(visitor_cookies(call("/probe", headers={"dnt": "0"}))) == 1


def test_it_is_never_set_on_an_asset_or_api_path(site):
    """Two different refusals. An asset is not a page anyone chose to
    visit; the API is a surface a SCRIPT talks to, and handing a cookie to
    somebody's automation writes an identifier into a cron job."""
    assert visitor_cookies(call("/static/app.css")) == []
    assert visitor_cookies(call("/favicon.ico")) == []
    assert visitor_cookies(call("/healthz")) == []
    assert visitor_cookies(call("/api/mcp", method="POST")) == []
    assert visitor_cookies(call("/collections/notes/records")) == []


def test_nothing_is_set_when_analytics_is_off(tmp_path, monkeypatch, site):
    """An identifier collected for no purpose is the worst trade
    available: nothing is being recorded, so the cookie could not answer a
    question even in principle."""
    monkeypatch.delenv("DBBASIC_ANALYTICS", raising=False)
    assert visitor_cookies(call("/probe")) == []


def test_the_visitor_token_is_never_handed_to_a_page_object(site):
    """It is not a credential -- it is worth nothing to the object, which
    is the point. What an object COULD do with it is write it next to a
    user_id in a collection of its own: the forbidden join, performed by a
    package the operator installed rather than by anything in this repo. A
    token only the capture hook can see cannot be joined to anything."""
    sent = call("/probe", headers={
        "cookie": (f"cart=abc; {object_analytics.VISITOR_COOKIE_NAME}=TOKEN123; "
                   "dbbasic_session=SECRET")})
    assert json.loads(sent["body"]) == {"cart": "abc"}
    assert b"TOKEN123" not in sent["body"]
    assert b"SECRET" not in sent["body"]


# --- the join that must never happen ------------------------------------------

def test_a_page_view_never_carries_a_token_and_a_user_id_together():
    """docs/analytics.md, cookie rule 4. A row holding an opaque visitor
    token AND an account id de-anonymises every page view that token ever
    made, retroactively, for a person who was never asked. Enforced in
    build_page_view rather than trusted to call sites, because the
    enforcement has to survive a call site written by somebody who has not
    read the docstring."""
    row = object_analytics.build_page_view(
        path="/invoices", method="GET", status=200, ip="198.51.100.4",
        headers={"cookie": f"{object_analytics.VISITOR_COOKIE_NAME}=tok"},
        owners=frozenset(), user_id="dana")
    assert row["session_id"] == "tok"
    assert row["user_id"] == ""

    # Same for the token this very response is minting.
    minted = object_analytics.build_page_view(
        path="/", method="GET", status=200, ip="198.51.100.4", headers={},
        owners=frozenset(), user_id="dana", minted_visitor_token="fresh")
    assert minted["session_id"] == "fresh" and minted["user_id"] == ""

    # With no token there is nothing to correlate WITH, so a signed-in
    # member's traffic is still labelled -- which pages members use is one
    # of the most useful things this collection knows.
    plain = object_analytics.build_page_view(
        path="/invoices", method="GET", status=200, ip="198.51.100.4",
        headers={"cookie": "dbbasic_session=abc"}, owners=frozenset(),
        user_id="dana")
    assert plain["session_id"] == "" and plain["user_id"] == "dana"


def test_a_conversion_never_carries_a_token_and_a_user_id_together():
    both = object_conversions.build_conversion(
        event_type="order_confirmed", session_id="tok", user_id="dana")
    assert both["session_id"] == "tok" and both["user_id"] == ""

    named = object_conversions.build_conversion(
        event_type="order_confirmed", user_id="dana")
    assert named["session_id"] == "" and named["user_id"] == "dana"


def test_no_row_the_server_writes_ever_joins_the_two(site):
    """The property end-to-end rather than per-function: drive real
    requests, signed-in and not, and assert no page_views row on disk has
    both columns populated."""
    call("/probe")
    call("/probe", headers={"cookie": "dbbasic_session=SECRET"})
    call("/probe", headers={"dnt": "1"})
    rows = page_views(site)
    assert rows
    assert not any(row["session_id"] and row["user_id"] for row in rows)


def test_the_cookie_lifetime_is_stated_and_never_indefinite():
    """Rule 3. "Indefinite" is how a session identifier quietly becomes a
    permanent one, so junk and non-positive values fall back to the
    default rather than being read as unbounded."""
    assert object_analytics.visitor_days({"DBBASIC_ANALYTICS_VISITOR_DAYS": "30"}) == 30
    assert object_analytics.visitor_days({}) == 180
    assert object_analytics.visitor_days({"DBBASIC_ANALYTICS_VISITOR_DAYS": "0"}) == 180
    assert object_analytics.visitor_days({"DBBASIC_ANALYTICS_VISITOR_DAYS": "-1"}) == 180
    assert object_analytics.visitor_days({"DBBASIC_ANALYTICS_VISITOR_DAYS": "soon"}) == 180


def test_an_older_session_id_cookie_is_still_read():
    """`build_page_view` read a cookie named `session_id` before this
    existed. It is never SET now -- two names would be two populations of
    visitors that cannot be compared -- but a box that has one keeps its
    thread instead of being counted as a stranger on the day this
    shipped."""
    row = object_analytics.build_page_view(
        path="/", method="GET", status=200, ip="1.2.3.4",
        headers={"cookie": "session_id=legacy42"}, owners=frozenset())
    assert row["session_id"] == "legacy42"
    assert not object_analytics.should_set_visitor_cookie(
        "/", {"cookie": "session_id=legacy42"}, env={"DBBASIC_ANALYTICS": "on"})


# ===========================================================================
# 2. Recording a conversion
# ===========================================================================

def conversion_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for package, collection in (("app-analytics", "conversions"),
                                ("app-orders", "orders"),
                                ("app-invoices", "invoices"),
                                ("app-payments", "payments"),
                                ("app-intake", "scans")):
        stage_collection(data_dir, package, collection)
    objects_root = tmp_path / "objects"
    shutil.copytree(ANALYTICS_OBJECTS, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    return data_dir, objects_root


def fire(objects_root, collection, record_id, action="update"):
    """One change event, shaped exactly as object_change_dispatch sends
    them (raw verb in `action`, participle in `event`)."""
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "system_record_conversion", method="EVENT",
            payload={"event": f"{collection}.record.{action}d",
                     "collection": collection, "record_id": record_id,
                     "action": action}),
        roots=[objects_root]).result


def conversions(data_dir):
    return object_records.read_collection_records("conversions",
                                                   base_dir=data_dir)


def an_order(data_dir, order_id="ord-1", *, status="confirmed", **fields):
    record = {"id": order_id, "doc_type": "sale", "number": "SO-0001",
              "customer_name": "Ada Lovelace", "customer_email": "ada@x.test",
              "currency": "USD", "status": status, "order_date": "2026-07-01",
              "subtotal_cents": "2400", "total_cents": "2400",
              "owner_id": "shop"}
    record.update({key: str(value) for key, value in fields.items()})
    return object_records.create_collection_record(
        "orders", record, base_dir=data_dir, preserve_read_only=True)


def an_invoice(data_dir, invoice_id="inv-1"):
    return object_records.create_collection_record(
        "invoices",
        {"id": invoice_id, "number": "INV-1", "customer_name": "Ada",
         "customer_email": "ada@x.test", "currency": "USD", "status": "sent",
         "issue_date": "2026-07-01", "due_date": "2026-07-15",
         "total_cents": "2400", "owner_id": "shop"},
        base_dir=data_dir, preserve_read_only=True)


def a_payment(data_dir, payment_id="pay-1", *, status="received", cents=2400):
    return object_records.create_collection_record(
        "payments",
        {"id": payment_id, "invoice_id": "inv-1", "amount_cents": str(cents),
         "method": "card", "received_on": "2026-07-02", "status": status,
         "owner_id": "shop"},
        base_dir=data_dir)


def a_scan(data_dir, scan_id="scan-1", *, status="confirmed"):
    return object_records.create_collection_record(
        "scans",
        {"id": scan_id, "filename": "receipt.jpg", "content_type": "image/jpeg",
         "source": "phone", "category_hint": "receipt", "status": status,
         "owner_id": "dana"},
        base_dir=data_dir)


def test_a_confirmed_order_is_counted_once_and_a_replay_counts_nothing(
        tmp_path, monkeypatch):
    """THE test in this file. The dispatcher is at-least-once by design
    and `confirmed` is a state an order sits in, so this handler sees the
    same order on every later write. Twice counted is a report that says
    the shop did double the business it did, and nothing downstream will
    ever correct it."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir)

    result = fire(objects_root, "orders", "ord-1")
    assert result["recorded"] == ["order_confirmed"]

    rows = conversions(data_dir)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "order_confirmed"
    assert json.loads(rows[0]["metadata"]) == {
        "amount_cents": 2400, "currency": "USD", "order_number": "SO-0001",
        "source": "orders/ord-1"}

    replay = fire(objects_root, "orders", "ord-1")
    assert replay["recorded"] == []
    assert replay["skipped_already_counted"] == ["order_confirmed"]
    assert len(conversions(data_dir)) == 1

    # And a third time, through the `created` door rather than `updated`.
    fire(objects_root, "orders", "ord-1", action="create")
    assert len(conversions(data_dir)) == 1


def test_the_marker_is_the_source_in_the_metadata_blob(tmp_path, monkeypatch):
    """The claim being recorded is "this transition was counted", so it is
    recorded on the count. A stamped flag on the order would be a second
    record that can disagree with the first -- and the schema is fixed and
    correct, so a private bookkeeping column would be the worse trade."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir)
    fire(objects_root, "orders", "ord-1")
    assert object_conversions.already_recorded(
        conversions(data_dir), "order_confirmed", "orders/ord-1")


def test_a_collected_order_is_a_second_goal_not_a_duplicate(tmp_path, monkeypatch):
    """The money was taken when the order was confirmed and the food was
    handed over when somebody walked in for it. A shop that wants to know
    how many ready orders were actually collected cannot ask that of
    `order_confirmed`."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir, status="collected", fulfillment_method="pickup")

    result = fire(objects_root, "orders", "ord-1")
    assert sorted(result["recorded"]) == ["order_collected", "order_confirmed"]
    assert len(conversions(data_dir)) == 2
    assert fire(objects_root, "orders", "ord-1")["recorded"] == []


def test_a_draft_order_is_not_a_conversion(tmp_path, monkeypatch):
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir, status="draft")
    assert fire(objects_root, "orders", "ord-1")["recorded"] == []
    assert conversions(data_dir) == []


def test_a_purchase_order_is_money_going_out_and_is_never_counted(
        tmp_path, monkeypatch):
    """One shared schema carries both sales and purchases here. Counting a
    confirmed PO as a conversion would report a shop's own buying as its
    business won."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir, doc_type="purchase")
    result = fire(objects_root, "orders", "ord-1")
    assert result["recorded"] == []
    assert "purchase" in result["skipped"]


def test_a_payment_is_counted_once_and_a_bounced_one_never_is(
        tmp_path, monkeypatch):
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_invoice(data_dir)
    a_payment(data_dir)
    a_payment(data_dir, "pay-2", status="bounced", cents=900)

    assert fire(objects_root, "payments", "pay-1",
                action="create")["recorded"] == ["payment_received"]
    assert fire(objects_root, "payments", "pay-2",
                action="create")["recorded"] == []
    assert fire(objects_root, "payments", "pay-1",
                action="create")["recorded"] == []

    rows = conversions(data_dir)
    assert len(rows) == 1
    assert json.loads(rows[0]["metadata"])["amount_cents"] == 2400


def test_a_confirmed_scan_is_counted_and_an_unconfirmed_one_is_not(
        tmp_path, monkeypatch):
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    a_scan(data_dir)
    a_scan(data_dir, "scan-2", status="extracted")

    assert fire(objects_root, "scans", "scan-1")["recorded"] == ["scan_confirmed"]
    assert fire(objects_root, "scans", "scan-2")["recorded"] == []
    assert fire(objects_root, "scans", "scan-1")["recorded"] == []
    assert len(conversions(data_dir)) == 1


def test_a_conversion_row_never_carries_a_visitor_token(tmp_path, monkeypatch):
    """Honest rather than lazy. These four goals are back-office
    transitions with no browser anywhere near them, and the basket's
    `carts.session_token` is a DIFFERENT identifier in a different
    namespace -- stamping it here would silently merge two populations and
    produce a funnel that looks stitched and is not."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    an_order(data_dir)
    fire(objects_root, "orders", "ord-1")
    row = conversions(data_dir)[0]
    assert row["session_id"] == "" and row["user_id"] == ""


def test_no_analytics_installed_degrades_honestly(tmp_path, monkeypatch):
    """Silence here would look exactly like "already counted", and the
    shop would never find out it had no numbers."""
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-orders", "orders")
    objects_root = tmp_path / "objects"
    shutil.copytree(ANALYTICS_OBJECTS, objects_root, dirs_exist_ok=True)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    an_order(data_dir)

    result = fire(objects_root, "orders", "ord-1")
    assert result["ok"] is True
    assert result["recorded"] == []
    assert "app-analytics not installed" in result["skipped"]


def test_a_vanished_record_is_a_skip_and_never_a_crash(tmp_path, monkeypatch):
    """A reaction may never fail the write that triggered it."""
    data_dir, objects_root = conversion_env(tmp_path, monkeypatch)
    assert fire(objects_root, "orders", "gone")["skipped"] == "order gone"
    assert fire(objects_root, "contacts", "c-1")["recorded"] == []


# ===========================================================================
# 3. The folds
# ===========================================================================

def view(token, path, *, ip="10.0.0.1", at="2026-07-20T09:00:00Z"):
    return {"session_id": token, "ip": ip, "path": path, "created_at": at,
            "status": "200", "user_agent": BROWSER, "is_owner": "false"}


def conversion(event_type, token="", *, at="2026-07-20T10:00:00Z", source=""):
    row = object_conversions.build_conversion(
        event_type=event_type, session_id=token, source=source)
    row["created_at"] = at
    return row


# --- the funnel -----------------------------------------------------------------

def test_the_funnel_counts_distinct_visitors_at_each_step():
    """Distinct visitors, not requests. Somebody who reloads the checkout
    four times is one person deciding, and counting the reloads turns a
    hesitation into a crowd."""
    rows = [
        view("a", "/shop", at="2026-07-20T09:00:00Z"),
        view("a", "/shop", at="2026-07-20T09:01:00Z"),
        view("a", "/checkout", at="2026-07-20T09:05:00Z"),
        view("a", "/checkout", at="2026-07-20T09:06:00Z"),
        view("b", "/shop", ip="10.0.0.2", at="2026-07-20T10:00:00Z"),
        view("c", "/checkout", ip="10.0.0.3", at="2026-07-20T11:00:00Z"),
    ]
    fold = object_conversions.funnel(rows, [], ["/shop", "/checkout"])
    assert [step["visitors"] for step in fold["steps"]] == [2, 1]
    assert fold["steps"][1]["drop_off"] == 1
    assert fold["steps"][1]["drop_off_pct"] == 50.0
    # c reached /checkout without ever passing /shop: a funnel is a
    # sequence, not a set of independent counts.
    assert fold["entered"] == 2 and fold["converted"] == 1


def test_a_step_must_happen_after_the_one_before_it():
    """Somebody who bought last week and browsed today did not convert
    today. Without the ordering rule a funnel is a Venn diagram wearing a
    funnel's labels, and it always flatters."""
    rows = [view("a", "/checkout", at="2026-07-20T09:00:00Z"),
            view("a", "/shop", at="2026-07-21T09:00:00Z")]
    fold = object_conversions.funnel(rows, [], ["/shop", "/checkout"])
    assert [step["visitors"] for step in fold["steps"]] == [1, 0]


def test_the_funnel_reports_how_much_of_itself_is_ip_stitched():
    """The point of the whole module. Rows with no token can only be
    threaded by address, which is better than dropping them and is not the
    same as knowing: an office behind one connection is one thread that
    looks like a very decisive shopper. So the fraction is returned as
    data, next to the numbers it qualifies."""
    rows = [
        view("a", "/shop"), view("a", "/checkout"),
        view("", "/shop", ip="10.0.0.9"), view("", "/checkout", ip="10.0.0.9"),
        view("", "/shop", ip="10.0.0.8"),
    ]
    fold = object_conversions.funnel(rows, [], ["/shop", "/checkout"])
    assert fold["entered"] == 3
    assert fold["ip_stitched"] == 2
    assert fold["ip_stitched_pct"] == 66.7
    assert fold["steps"][1]["ip_stitched"] == 1
    assert any("stitched by IP address" in text for text in fold["caveats"])


def test_an_event_step_matches_a_conversion_on_the_same_thread():
    rows = [view("a", "/shop", at="2026-07-20T09:00:00Z"),
            view("b", "/shop", ip="10.0.0.2", at="2026-07-20T09:00:00Z")]
    goals = [conversion("order_confirmed", "a", at="2026-07-20T09:30:00Z")]
    fold = object_conversions.funnel(rows, goals, ["/shop", "order_confirmed"])
    assert [step["visitors"] for step in fold["steps"]] == [2, 1]
    assert fold["steps"][1]["kind"] == "event"


def test_conversions_with_no_token_are_named_rather_than_dropped():
    """Today that is most of them, because a back-office transition has no
    browser anywhere near it. A funnel whose last step reads zero without
    explaining why is a funnel somebody will conclude is broken -- or
    worse, believe."""
    rows = [view("a", "/shop")]
    goals = [conversion("order_confirmed"), conversion("order_confirmed")]
    fold = object_conversions.funnel(rows, goals, ["/shop", "order_confirmed"])
    assert fold["unthreaded_conversions"] == 2
    assert fold["steps"][1]["visitors"] == 0
    assert any("no visitor token" in text for text in fold["caveats"])


def test_a_path_step_is_a_prefix_and_an_event_step_is_exact():
    rows = [view("a", "/shop/product/mug")]
    goals = [conversion("order_confirmed", "a", at="2026-07-20T12:00:00Z")]
    fold = object_conversions.funnel(rows, goals, ["/shop", "order_confirmed"])
    assert [step["visitors"] for step in fold["steps"]] == [1, 1]
    assert object_conversions.funnel(
        rows, goals, ["/shop", "order"])["steps"][1]["visitors"] == 0


def test_steps_can_be_written_as_strings_or_as_labelled_objects():
    assert object_conversions.normalize_steps(["/shop", "order_confirmed"]) == [
        {"label": "/shop", "kind": "path", "match": "/shop"},
        {"label": "order_confirmed", "kind": "event", "match": "order_confirmed"}]
    assert object_conversions.normalize_steps(
        [{"label": "Browsed", "path": "/shop"}]) == [
        {"label": "Browsed", "kind": "path", "match": "/shop"}]


def test_normalizing_steps_is_idempotent():
    """A page validates the setting once so it can report a bad row before
    rendering anything, then hands the result to `funnel`, which
    normalizes again. A normalizer that rejects its own output turns that
    entirely reasonable sequence into a 500 -- which is how this was
    found."""
    once = object_conversions.normalize_steps(["/shop", "order_confirmed"])
    assert object_conversions.normalize_steps(once) == once


def test_a_malformed_funnel_setting_is_reported_not_raised():
    """An operator who typed bad JSON into a settings row needs to be told
    which row and why, on the screen where they typed it. A misconfigured
    funnel and a funnel nobody entered look identical, and only one of
    them is your fault."""
    steps, error = object_conversions.parse_funnel_steps("{not json")
    assert steps == [] and "not valid JSON" in error
    steps, error = object_conversions.parse_funnel_steps('"/shop"')
    assert steps == [] and "JSON list" in error
    steps, error = object_conversions.parse_funnel_steps(
        '[{"path": "/shop", "event_type": "x"}]')
    assert steps == [] and "one or the other" in error
    assert object_conversions.parse_funnel_steps("") == ([], "")
    assert object_conversions.parse_funnel_steps(
        '["/shop", "order_confirmed"]')[0][0]["match"] == "/shop"


def test_an_unconfigured_funnel_is_not_an_empty_one():
    fold = object_conversions.funnel([view("a", "/shop")], [], [])
    assert fold["configured"] is False and fold["steps"] == []


# --- new versus returning ----------------------------------------------------------

def test_returning_visitors_are_counted_by_token_across_days():
    """The most useful ratio a site has, and invisible without the cookie:
    somebody who reads the pitch on Monday and comes back on Thursday was
    three strangers before this."""
    rows = [view("a", "/", at="2026-07-24T09:00:00Z"),
            view("a", "/shop", at="2026-07-26T09:00:00Z"),
            view("b", "/", ip="10.0.0.2", at="2026-07-26T09:00:00Z")]
    fold = object_conversions.returning_visitors(rows, 7, now=NOW)
    assert fold["returning"] == 1 and fold["new"] == 1
    assert fold["returning_pct"] == 50.0


def test_several_pages_in_one_visit_is_not_a_return():
    """A person reading three pages over lunch has not returned; they are
    still here."""
    rows = [view("a", "/", at="2026-07-26T09:00:00Z"),
            view("a", "/shop", at="2026-07-26T09:05:00Z"),
            view("a", "/pricing", at="2026-07-26T09:09:00Z")]
    fold = object_conversions.returning_visitors(rows, 7, now=NOW)
    assert fold == dict(fold, new=1, returning=0)


def test_a_token_first_seen_before_the_window_is_returning():
    rows = [view("a", "/", at="2026-06-01T09:00:00Z"),
            view("a", "/shop", at="2026-07-26T09:00:00Z")]
    fold = object_conversions.returning_visitors(rows, 7, now=NOW)
    assert fold["returning"] == 1 and fold["new"] == 0


def test_the_returning_count_says_it_is_a_floor():
    """Returned as DATA, not as prose in a docstring, so a surface cannot
    render the number without being handed the caveat to render beside
    it. Every error in it points the same way -- a cleared cookie, a
    private window, a second browser and a phone are each a new visitor --
    which is the right direction to be wrong in, and still wrong."""
    fold = object_conversions.returning_visitors(
        [view("a", "/", at="2026-07-26T09:00:00Z")], 7, now=NOW)
    assert fold["floor"] is True
    assert "never a census" in fold["caveat"]
    assert "cleared cookie" in fold["caveat"]


def test_the_size_of_the_blind_spot_is_reported_beside_the_number():
    """Anyone who sent Do Not Track has no token and cannot appear on
    either side of the split. How many of them there are belongs on the
    same screen as the ratio they are missing from."""
    rows = [view("a", "/", at="2026-07-26T09:00:00Z"),
            view("", "/", ip="10.0.0.7", at="2026-07-26T09:00:00Z"),
            view("", "/shop", ip="10.0.0.8", at="2026-07-26T09:00:00Z")]
    fold = object_conversions.returning_visitors(rows, 7, now=NOW)
    assert fold["counted"] == 1 and fold["no_token_addresses"] == 2


# --- time to conversion -------------------------------------------------------------

def test_time_to_conversion_measures_from_the_first_page_seen():
    rows = [view("a", "/", at="2026-07-20T09:00:00Z"),
            view("a", "/shop", at="2026-07-22T09:00:00Z"),
            view("b", "/", ip="10.0.0.2", at="2026-07-25T09:00:00Z")]
    goals = [conversion("order_confirmed", "a", at="2026-07-24T09:00:00Z"),
             conversion("order_confirmed", "b", at="2026-07-25T21:00:00Z")]
    fold = object_conversions.time_to_conversion(rows, goals)
    assert fold["count"] == 2
    assert fold["max_days"] == 4.0
    assert fold["min_days"] == 0.5
    assert fold["median_days"] == 2.25
    assert fold["same_day"] == 1


def test_a_visitor_whose_first_page_predates_the_window_is_counted_apart():
    """page_views is bounded by days AND rows, so the slowest journeys are
    exactly the ones whose beginning ages out first. Dropping them
    silently would bias the median toward fast conversions -- the number
    would improve every time retention was shortened, which is the most
    dangerous shape a metric can have."""
    rows = [view("a", "/", at="2026-07-25T09:00:00Z")]
    goals = [conversion("order_confirmed", "a", at="2026-07-26T09:00:00Z"),
             conversion("order_confirmed", "gone", at="2026-07-26T09:00:00Z")]
    fold = object_conversions.time_to_conversion(rows, goals)
    assert fold["count"] == 1 and fold["median_days"] == 1.0
    assert fold["no_first_view"] == 1
    assert any("aged out" in text for text in fold["caveats"])


def test_a_first_page_far_outside_any_window_still_measures_correctly():
    """The row is here, so the duration is knowable, and it is knowably
    long: 40 days is the answer, not a number clipped to the report's
    window."""
    rows = [view("a", "/", at="2026-06-01T00:00:00Z"),
            view("a", "/shop", at="2026-07-10T00:00:00Z")]
    goals = [conversion("order_confirmed", "a", at="2026-07-11T00:00:00Z")]
    assert object_conversions.time_to_conversion(rows, goals)["median_days"] == 40.0


def test_a_conversion_before_its_first_page_view_is_clamped_and_counted():
    rows = [view("a", "/", at="2026-07-26T09:00:00Z")]
    goals = [conversion("order_confirmed", "a", at="2026-07-20T09:00:00Z")]
    fold = object_conversions.time_to_conversion(rows, goals)
    assert fold["median_days"] == 0.0 and fold["before_first_view"] == 1


def test_unthreaded_conversions_are_reported_apart_from_aged_out_ones():
    """Different causes with different fixes: one is a goal recorded away
    from any browser, the other is retention. Merging them into one
    'unmatched' count would hide which."""
    fold = object_conversions.time_to_conversion(
        [], [conversion("order_confirmed"), conversion("x", "ghost")])
    assert fold["unthreaded"] == 1 and fold["no_first_view"] == 1
    assert fold["count"] == 0 and fold["median_days"] is None


def test_conversions_group_by_event_type_with_their_threaded_share():
    goals = [conversion("order_confirmed", at="2026-07-20T09:00:00Z"),
             conversion("order_confirmed", "a", at="2026-07-21T09:00:00Z"),
             conversion("payment_received", at="2026-07-21T09:00:00Z")]
    summary = object_conversions.by_event_type(goals)
    assert summary[0] == {"event_type": "order_confirmed", "count": 2,
                          "threaded": 1, "first": "2026-07-20T09:00:00Z",
                          "last": "2026-07-21T09:00:00Z"}
    assert summary[1]["event_type"] == "payment_received"


def test_a_metadata_blob_that_is_not_json_never_breaks_a_report():
    """It is a free-text column; a report must survive whatever ended up
    in it."""
    assert object_conversions.parse_metadata("not json at all") == {}
    assert object_conversions.parse_metadata("[1,2,3]") == {}
    assert object_conversions.parse_metadata('{"source": "orders/1"}') == {
        "source": "orders/1"}
    assert object_conversions.recorded_sources(
        [{"event_type": "x", "metadata": "junk"}]) == set()


# ===========================================================================
# 4. The page
# ===========================================================================

def render(payload=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "site_visitors", method="GET", payload=payload or {}),
        roots=[ANALYTICS_OBJECTS]).result


def page_env(tmp_path, monkeypatch, *, settings=(), views=(), goals=()):
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-analytics", "page_views")
    stage_collection(data_dir, "app-analytics", "conversions")
    rows = "".join(f"s{index}\t{key}\t{value}\t\n"
                   for index, (key, value) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    for row in views:
        row = dict(row)
        row.pop("created_at", None)
        object_records.create_collection_record("page_views", row,
                                                 base_dir=data_dir, actor="test")
    for row in goals:
        row = dict(row)
        row.pop("created_at", None)
        object_records.create_collection_record("conversions", row,
                                                 base_dir=data_dir, actor="test")
    return data_dir


def test_the_page_says_what_to_configure_when_no_funnel_is_set(
        tmp_path, monkeypatch):
    """An empty funnel table would read as "nobody converted". A funnel
    nobody has described yet is a settings row away, and the example is
    built from what THIS server actually recorded, so it can be copied
    rather than invented."""
    page_env(tmp_path, monkeypatch,
             views=[view("a", "/shop", at="") for _ in range(2)],
             goals=[conversion("order_confirmed", source="orders/ord-1")])
    body = render({"_identity": {"user_id": "dan"}})["body"]

    assert "No funnel is configured" in body
    assert "analytics.funnel_steps" in body
    assert "order_confirmed" in body        # copied from the real goals
    assert "/shop" in body


def test_the_page_renders_a_configured_funnel(tmp_path, monkeypatch):
    page_env(
        tmp_path, monkeypatch,
        settings=(("analytics.funnel_steps",
                   '["/shop", "/checkout", "order_confirmed"]'),),
        views=[view("a", "/shop", at=""), view("a", "/checkout", at=""),
               view("b", "/shop", ip="10.0.0.2", at="")],
        goals=[conversion("order_confirmed", source="orders/ord-1")])
    body = render({"_identity": {"user_id": "dan"}})["body"]

    assert "No funnel is configured" not in body
    assert "IP-stitched" in body
    assert "/checkout" in body


def test_the_page_reports_a_broken_funnel_setting_rather_than_an_empty_one(
        tmp_path, monkeypatch):
    page_env(tmp_path, monkeypatch,
             settings=(("analytics.funnel_steps", "{nope"),),
             views=[view("a", "/shop", at="")])
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "could not be read" in body


def test_the_page_shows_new_versus_returning_with_the_floor_note(
        tmp_path, monkeypatch):
    page_env(tmp_path, monkeypatch, views=[view("a", "/shop", at="")])
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "New versus returning" in body
    assert "floor, never a census" in body.lower() or "A floor, never a census" in body
    assert "cleared cookie" in body


def test_the_page_lists_goals_by_event_type(tmp_path, monkeypatch):
    page_env(tmp_path, monkeypatch, views=[view("a", "/shop", at="")],
             goals=[conversion("order_confirmed", source="orders/ord-1"),
                    conversion("payment_received", source="payments/pay-1")])
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "order_confirmed" in body and "payment_received" in body


def test_the_page_says_so_when_conversions_was_never_installed(
        tmp_path, monkeypatch):
    """A confident zero where the truth is "nothing was ever recorded" is
    the exact failure this whole page is written against."""
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-analytics", "page_views")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "No <code>conversions</code> collection" in body


def test_the_page_still_says_an_ip_is_not_a_person(tmp_path, monkeypatch):
    """The cookie improves the RETURNING question and moves no ceiling at
    all. The day that note disappears is the day this page starts
    over-claiming."""
    page_env(tmp_path, monkeypatch, views=[view("a", "/shop", at="")])
    body = render({"_identity": {"user_id": "dan"}})["body"]
    assert "distinct IP address" in body
    assert "dbbasic_visitor" in body
    assert "Do Not Track" in body
