"""Anchoring a ledger: one digest that stands for a whole prefix, lodged
where the server that took it cannot reach.

Three claims carry this file.

A PREFIX DIGEST IS A CHAIN HEAD. For an append-only log, a hash chain is
just an incremental way of computing prefix digests. Folding directly
gives the same value, which is why the cheap version could ship without a
storage-layer change AND why moving the computation into the append path
later would not orphan an anchor already published. The test that pins it
is the one showing `head()` equals a hand-rolled fold of `link()`.

THE DIGEST MUST SURVIVE ORDINARY MAINTENANCE. Compaction rewrites append
files and migrations add columns. Either one invalidating every historical
anchor would make the check cry wolf on a schedule, and a check that cries
wolf is one nobody reads. Both are tested here as the load-bearing cases
they are.

AN ANCHOR NOBODY ELSE HOLDS IS NOT EVIDENCE. The page must say so in
words, because a green tick for a digest sitting in the same directory as
the ledger it describes would be the most misleading thing this package
could do.
"""

import json
import pathlib

import pytest
from conftest import stage_collection

import object_execution
import object_ledger_head
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
INTEGRITY_OBJECTS = PACKAGES / "app-integrity" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

FIELDS = ["id", "wallet_id", "amount_minor", "kind"]


def entry(row_id, amount, wallet="w1", kind="topup"):
    return {"id": row_id, "wallet_id": wallet,
            "amount_minor": str(amount), "kind": kind}


LEDGER = [entry("e1", 1000), entry("e2", -250), entry("e3", 5000)]


# --- the pure fold ------------------------------------------------------------

def test_the_head_is_exactly_a_chain_fold_over_the_rows():
    """The property the whole design rests on. If these two ever differ,
    then moving the computation into the append path later WOULD orphan
    every anchor already published, and the cheap version stops being a
    safe place to start."""
    by_hand = object_ledger_head.genesis("wallet_entries", FIELDS)
    for row in LEDGER:
        by_hand = object_ledger_head.link(by_hand, row, FIELDS)

    folded = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    assert folded["digest"] == by_hand
    assert folded["row_count"] == 3


def test_a_head_is_bound_to_its_collection_and_its_field_list():
    """Otherwise an anchor taken over wallet_entries could be checked
    against payments and come out clean, and a field list differing by one
    column would be a coincidence rather than a different chain."""
    a = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    b = object_ledger_head.head(LEDGER, FIELDS, collection="payments")
    c = object_ledger_head.head(LEDGER, FIELDS + ["note"],
                                collection="wallet_entries")
    assert len({a["digest"], b["digest"], c["digest"]}) == 3


def test_appending_never_disturbs_an_earlier_prefix():
    """The property that makes an anchor checkable a year later: a ledger
    that has grown is the normal case, not a break."""
    anchor = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    grown = LEDGER + [entry("e4", 99), entry("e5", 1)]
    verdict = object_ledger_head.verify(grown, {**anchor})
    assert verdict["verified"] is True
    assert verdict["present_rows"] == 5
    assert verdict["anchored_rows"] == 3


def test_editing_a_row_inside_the_prefix_is_caught():
    anchor = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    tampered = [dict(LEDGER[0]), {**LEDGER[1], "amount_minor": "-25"}, LEDGER[2]]
    verdict = object_ledger_head.verify(tampered, anchor)
    assert verdict["verified"] is False
    assert verdict["status"] == "mismatch"
    assert "changed after the fact" in verdict["detail"]


def test_removing_rows_reports_truncation_rather_than_a_mismatch():
    """Different remedy, so a different word: a mismatch means find what
    changed, a truncation means find what is missing. Retention pruning an
    anchored ledger lands here, and it SHOULD -- pruning really does
    destroy the evidence the anchor was taken over."""
    anchor = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    verdict = object_ledger_head.verify(LEDGER[:2], anchor)
    assert verdict["status"] == "truncated"
    assert verdict["anchored_rows"] == 3 and verdict["present_rows"] == 2


def test_a_schema_gaining_a_column_does_not_turn_old_anchors_red():
    """The subtler trap, and the reason the field list is stored IN the
    anchor. Reading today's schema instead would make an ordinary
    migration indistinguishable from an attack, which teaches an operator
    to ignore the alarm."""
    anchor = object_ledger_head.head(LEDGER, FIELDS, collection="wallet_entries")
    migrated = [{**row, "reference": ""} for row in LEDGER]
    assert object_ledger_head.verify(migrated, anchor)["verified"] is True

    # And a row that populates the NEW field still verifies, because the
    # anchor never covered it. That is correct: an anchor cannot make a
    # claim about a column that did not exist when it was taken.
    populated = [{**row, "reference": "later"} for row in LEDGER]
    assert object_ledger_head.verify(populated, anchor)["verified"] is True


def test_the_canonical_form_is_injective_across_field_boundaries():
    """A naive tab-join is only injective while no value can contain a
    tab, which couples the integrity digest to the storage format. Length
    prefixing means an attacker cannot move characters across a field
    boundary and keep the digest."""
    left = [{"a": "xy", "b": "z"}]
    right = [{"a": "x", "b": "yz"}]
    fields = ["a", "b"]
    assert (object_ledger_head.head(left, fields)["digest"]
            != object_ledger_head.head(right, fields)["digest"])

    # And the delimiter itself cannot be smuggled in.
    assert (object_ledger_head.head([{"a": "1:x|", "b": ""}], fields)["digest"]
            != object_ledger_head.head([{"a": "", "b": "1:x|"}], fields)["digest"])


def test_a_ladder_of_anchors_brackets_where_the_break_is():
    """One anchor says 'something in the first N rows changed'. A ladder
    says which day. That resolution is the reason the pass is daily."""
    long_ledger = [entry(f"e{n}", n * 10) for n in range(1, 21)]
    ladder = [object_ledger_head.head(long_ledger, FIELDS,
                                      collection="wallet_entries", count=n)
              for n in (5, 10, 15, 20)]

    tampered = list(long_ledger)
    tampered[12] = {**tampered[12], "amount_minor": "999999"}

    where = object_ledger_head.locate(tampered, ladder)
    assert where["broken"] is True
    assert where["last_good_row_count"] == 10      # row 13 is inside 11..15
    assert where["first_bad_row_count"] == 15
    assert "between row 11 and row 15" in where["detail"]


def test_an_untouched_ledger_reports_no_break_at_all():
    ladder = [object_ledger_head.head(LEDGER, FIELDS,
                                      collection="wallet_entries", count=n)
              for n in (1, 2, 3)]
    assert object_ledger_head.locate(LEDGER, ladder)["broken"] is False


def test_an_anchor_with_no_field_list_is_unusable_rather_than_verified():
    """Failing open here would mean a corrupted anchor row reads as a
    clean bill of health, which is the wrong direction for every possible
    caller."""
    verdict = object_ledger_head.verify(LEDGER, {"digest": "abc", "fields": [],
                                                 "row_count": 3})
    assert verdict["status"] == "unusable"
    assert verdict["verified"] is False


# --- through the objects ------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    base = tmp_path / "data"
    stage_collection(base, "app-integrity", "anchors")
    stage_collection(base, "app-settings", "app_settings")
    stage_collection(base, "app-billing", "wallet_entries")
    stage_collection(base, "app-billing", "wallets")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(base))
    # A notary on 127.0.0.1 is the only notary a test has. In
    # production this is refused -- see the self-anchoring tests below.
    monkeypatch.setenv("DBBASIC_NOTARY_ALLOW_LOOPBACK", "1")
    return base


def run(object_id, payload=None, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload or {}),
        roots=[INTEGRITY_OBJECTS]).result


def setting(data_dir, key, value):
    return object_records.create_collection_record(
        "app_settings", {"key": key, "value": value}, base_dir=data_dir)


def wallet_entry(data_dir, amount, wallet="w1"):
    if not object_records.read_collection_records("wallets", base_dir=data_dir):
        object_records.create_collection_record(
            "wallets", {"id": wallet, "owner_id": "shop", "kind": "gift_card",
                        "code": "GC-1", "is_active": "true"},
            base_dir=data_dir)
    return object_records.create_collection_record(
        "wallet_entries",
        {"wallet_id": wallet, "amount_minor": str(amount), "kind": "topup",
         "owner_id": "shop"},
        base_dir=data_dir)


def test_a_pass_anchors_the_ledgers_that_exist_and_skips_the_rest(data_dir):
    setting(data_dir, "ledger.anchored_collections",
            "wallet_entries,fin_journal_lines")
    wallet_entry(data_dir, 1000)
    wallet_entry(data_dir, 2500)

    result = run("system_publish_head")
    assert result["ok"] is True
    by_collection = {r["collection"]: r for r in result["results"]}
    assert by_collection["wallet_entries"]["row_count"] == 2
    assert by_collection["fin_journal_lines"]["status"] == "skipped"

    anchors = object_records.read_collection_records("anchors", base_dir=data_dir)
    assert len(anchors) == 1
    assert anchors[0]["collection"] == "wallet_entries"
    assert anchors[0]["row_count"] == "2"
    assert anchors[0]["covered_fields"]


def test_a_dormant_ledger_writes_no_second_anchor(data_dir):
    """Not tidiness: an anchor's whole content is (row_count, digest), so
    an identical second one carries nothing a reader could act on while
    making the ladder longer with no extra resolution."""
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    wallet_entry(data_dir, 1000)

    run("system_publish_head")
    second = run("system_publish_head")

    assert second["results"][0]["status"] == "unchanged"
    assert len(object_records.read_collection_records(
        "anchors", base_dir=data_dir)) == 1

    wallet_entry(data_dir, 77)
    third = run("system_publish_head")
    assert third["results"][0]["row_count"] == 2
    assert len(object_records.read_collection_records(
        "anchors", base_dir=data_dir)) == 2


def test_with_no_notary_configured_the_pass_says_so_in_the_result(data_dir):
    """A digest nobody else holds is a bookkeeping entry. The pass records
    it anyway -- it is worth more than nothing and tomorrow's pass covers
    the same history under a longer prefix -- but it must not be quiet
    about what is missing."""
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    wallet_entry(data_dir, 1000)

    result = run("system_publish_head")
    assert result["independent_lodgements"] == 0
    assert "notary.endpoints" in result["warning"]
    assert "held only by the server that took them" in result["warning"]

    anchors = object_records.read_collection_records("anchors", base_dir=data_dir)
    assert anchors[0]["status"] == "recorded"
    assert anchors[0]["notary_count"] == "0"


def test_an_unreachable_notary_still_leaves_the_anchor_recorded(data_dir):
    """One dead endpoint must not cost the anchor. Failing closed here
    would mean an outage silently produces a hole in the ladder, which is
    exactly the period an incident would later be traced to."""
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    setting(data_dir, "notary.endpoints", "http://127.0.0.1:9/nope")
    wallet_entry(data_dir, 1000)

    result = run("system_publish_head")
    assert result["results"][0]["status"] == "failed"

    anchors = object_records.read_collection_records("anchors", base_dir=data_dir)
    assert len(anchors) == 1
    assert anchors[0]["notary_count"] == "0"
    assert "unreachable" in anchors[0]["note"]


def test_the_pass_declares_EVENT_because_that_is_what_the_daemon_calls():
    """Declaring only POST is a scheduled pass that silently never runs,
    and that has been a real production bug here more than once."""
    source = (PACKAGES / "app-integrity" / "objects" / "system"
              / "publish_head.py").read_text()
    assert "def EVENT(" in source
    assert "POST = EVENT" in source


def test_page_views_is_not_anchored_by_default_and_the_default_says_why():
    """Anchor what somebody would profit from changing. page_views is the
    highest-volume writer on the box and the least valuable thing in it,
    so anchoring it would cost a full read of the largest file here every
    day to protect traffic nobody profits from forging."""
    source = (PACKAGES / "app-integrity" / "objects" / "system"
              / "publish_head.py").read_text()
    assert "page_views" in source                  # named, as a decision
    default_block = source.split("DEFAULT_COLLECTIONS = (")[1].split(")")[0]
    assert "page_views" not in default_block
    assert "wallet_entries" in default_block
    assert "notarizations" in default_block        # the notary watches itself


# --- the page -----------------------------------------------------------------

def integrity_page(*, path="/ledger-integrity"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest("site_ledger_integrity",
                                                method="GET",
                                                payload={"_path": path}),
        roots=[INTEGRITY_OBJECTS]).result


def test_the_page_refuses_a_green_tick_for_a_digest_nobody_else_holds(data_dir):
    """The single most important assertion in this file. Every anchor
    verifies and the page must still say the check is worth nothing
    against intent, because the anchors sit in the same directory as the
    ledger under the same permissions."""
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    wallet_entry(data_dir, 1000)
    run("system_publish_head")

    # Normalised, because the assertions here are about the SENTENCES the
    # page commits to and those wrap across source lines. A test that
    # breaks when a paragraph is rewrapped teaches people to weaken it.
    body = " ".join(integrity_page()["body"].split())
    assert "Anchored, but only here" in body
    assert "no independent party holds any of them" in body
    assert "anybody able to rewrite a ledger can rewrite its anchor" in body
    assert "notary.endpoints" in body
    assert "Verified, and held elsewhere" not in body


def test_the_page_catches_a_ledger_edited_behind_its_anchor(data_dir):
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    first = wallet_entry(data_dir, 1000)
    wallet_entry(data_dir, 2000)
    run("system_publish_head")

    object_records.update_collection_record(
        "wallet_entries", first["id"], {"amount_minor": "999999"},
        base_dir=data_dir)

    fold = json.loads(integrity_page(path="/ledger-integrity.json")["body"])
    assert fold["broken"] == ["wallet_entries"]

    body = integrity_page()["body"]
    assert "An anchor does not verify" in body
    assert "wallet_entries" in body


def test_the_page_and_the_json_are_the_same_fold(data_dir):
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    wallet_entry(data_dir, 1000)
    run("system_publish_head")

    fold = json.loads(integrity_page(path="/ledger-integrity.json")["body"])
    assert fold["ledger_count"] == 1
    assert fold["evidence"] is False
    assert fold["ledgers"][0]["collection"] == "wallet_entries"
    assert fold["ledgers"][0]["anchors"][0]["verdict"]["verified"] is True


def test_with_nothing_anchored_the_page_says_that_rather_than_looking_fine(data_dir):
    body = integrity_page()["body"]
    assert "Nothing is anchored yet" in body
    assert "system_publish_head" in body


# --- the wire contract between the two packages -------------------------------
#
# The one place a bug here would hide indefinitely. system_publish_head
# decides a lodgement succeeded by reading `found` out of the notary's
# answer; if that shape ever changed, every pass would quietly record
# "failed" and the integrity page would go on saying nothing independent
# holds the digests -- an alarm that is technically correct and completely
# misleading. Nothing else in either test file crosses the HTTP boundary
# these two packages actually talk over, so this is where the contract
# gets pinned.

def _notary_server(data_dir):
    """A real HTTP server that dispatches to the real action_notarize.

    Not a stub returning a canned body -- a stub would pass whatever shape
    it was written against, which is exactly the bug being guarded here.
    """
    import http.server
    import threading

    notary_objects = PACKAGES / "app-notary" / "objects"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            payload["_identity"] = {"user_id": "peer"}
            result = object_execution.execute_object(
                RUNTIME,
                object_execution.ObjectExecutionRequest(
                    "action_notarize", method="POST", payload=payload),
                roots=[notary_objects]).result
            body = json.dumps(result).encode("utf-8")
            self.send_response(int(result.get("status") or 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_a_head_lodged_with_a_real_notary_comes_back_as_evidence(data_dir):
    stage_collection(data_dir, "app-notary", "notarizations")
    server, url = _notary_server(data_dir)
    try:
        setting(data_dir, "ledger.anchored_collections", "wallet_entries")
        setting(data_dir, "notary.endpoints", url)
        wallet_entry(data_dir, 1000)
        wallet_entry(data_dir, 2000)

        result = run("system_publish_head")
        assert result["results"][0]["status"] == "published"
        assert result["independent_lodgements"] == 1
        assert result["warning"] == ""

        # The digest the notary now holds is the digest the anchor claims.
        anchors = object_records.read_collection_records("anchors",
                                                         base_dir=data_dir)
        lodged = object_records.read_collection_records("notarizations",
                                                        base_dir=data_dir)
        assert len(lodged) == 1
        assert lodged[0]["digest"] == anchors[0]["digest"]
        assert lodged[0]["label"] == "wallet_entries head @ 2 rows"

        # And the page stops hedging, because now somebody else holds it.
        body = " ".join(integrity_page()["body"].split())
        assert "Verified, and held elsewhere" in body
        assert "Anchored, but only here" not in body
    finally:
        server.shutdown()


def test_re_lodging_an_unchanged_history_is_treated_as_success(data_dir):
    """The notary answers `already_recorded` for a digest it has seen, and
    that is a SUCCESS -- it means the history has an even older independent
    timestamp than this pass. Reading it as a refusal would make every
    re-anchor of a dormant ledger look like a failing notary."""
    stage_collection(data_dir, "app-notary", "notarizations")
    server, url = _notary_server(data_dir)
    try:
        setting(data_dir, "ledger.anchored_collections", "wallet_entries")
        setting(data_dir, "notary.endpoints", url)
        wallet_entry(data_dir, 1000)
        run("system_publish_head")

        # A new row, so a new prefix and a new digest -- then remove the
        # anchor's evidence of having lodged the FIRST digest and re-run
        # against the same notary, which still holds it.
        wallet_entry(data_dir, 500)
        second = run("system_publish_head")
        assert second["results"][0]["status"] == "published"

        lodged = object_records.read_collection_records("notarizations",
                                                        base_dir=data_dir)
        assert len(lodged) == 2                      # two prefixes, two digests

        again = _relodge_first(data_dir, url)
        assert again is True
    finally:
        server.shutdown()


def _relodge_first(data_dir, url):
    """Submit an already-held digest and confirm publish_head calls it ok."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ph", PACKAGES / "app-integrity" / "objects" / "system" / "publish_head.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = object_records.read_collection_records("anchors",
                                                   base_dir=data_dir)[0]
    ok, detail = module._lodge(url, first["digest"], "re-lodge")
    assert detail == "already held", detail
    return ok


# --- self-anchoring, which would undo the whole honesty design ----------------

def test_a_notary_that_is_this_server_is_not_independent():
    """The hole worth closing before anyone finds it: point
    notary.endpoints at your own box, every lodgement succeeds, and the
    page swaps "anchored, but only here" for "verified, and held
    elsewhere" -- a specific false claim, which is worse than the honest
    missing one it replaced."""
    import object_notary

    assert object_notary.is_self_endpoint("http://localhost:8000") is True
    assert object_notary.is_self_endpoint("http://127.0.0.1:9000") is True
    assert object_notary.is_self_endpoint("https://127.53.1.9") is True
    assert object_notary.is_self_endpoint(
        "https://object.dbbasic.com", "https://object.dbbasic.com") is True
    # Same host, different scheme and path -- still the same server.
    assert object_notary.is_self_endpoint(
        "http://object.dbbasic.com/notary", "https://object.dbbasic.com/") is True

    # And a genuinely different party is not caught.
    assert object_notary.is_self_endpoint(
        "https://notary.example.org", "https://object.dbbasic.com") is False
    assert object_notary.is_self_endpoint("https://notary.example.org") is False


def test_pointing_the_pass_at_itself_records_zero_independent_copies(data_dir,
                                                                     monkeypatch):
    """End to end: the pass must not count it, must say why in the note,
    and the page must keep hedging."""
    monkeypatch.delenv("DBBASIC_NOTARY_ALLOW_LOOPBACK", raising=False)
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    setting(data_dir, "portal.base_url", "https://books.example.com")
    setting(data_dir, "notary.endpoints", "https://books.example.com")
    wallet_entry(data_dir, 1000)

    result = run("system_publish_head")
    assert result["results"][0]["status"] == "failed"
    assert result["independent_lodgements"] == 0

    anchor = object_records.read_collection_records("anchors",
                                                    base_dir=data_dir)[0]
    assert anchor["notary_count"] == "0"
    assert "is this server" in anchor["note"]
    assert "somebody else" in anchor["note"]

    body = " ".join(integrity_page()["body"].split())
    assert "Anchored, but only here" in body
    assert "Verified, and held elsewhere" not in body


def test_the_loopback_escape_is_an_env_var_and_not_a_setting(data_dir,
                                                             monkeypatch):
    """A settings page is exactly where somebody about to fool themselves
    would go looking for the switch, so the switch is not there."""
    monkeypatch.delenv("DBBASIC_NOTARY_ALLOW_LOOPBACK", raising=False)
    setting(data_dir, "ledger.anchored_collections", "wallet_entries")
    setting(data_dir, "notary.endpoints", "http://127.0.0.1:9")
    setting(data_dir, "notary.allow_loopback", "true")     # ignored, on purpose
    wallet_entry(data_dir, 1000)

    refused = run("system_publish_head")
    assert refused["results"][0]["status"] == "failed"
    assert "is this server" in object_records.read_collection_records(
        "anchors", base_dir=data_dir)[0]["note"]
