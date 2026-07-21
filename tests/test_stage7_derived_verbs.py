"""Stage 7 derived-read verbs: GET /api/stock (get_stock_levels) and
GET /api/finance/summary (get_finance_summary). Folded/computed data an agent
can't get as a plain collection read. Thin, owner-scoped wrappers over
object_stock.stock_levels / object_finance.trial_balance -- so the test asserts
the endpoint returns exactly what the (separately-tested) pure fold returns for
that owner, plus the empty/anonymous degradation and the MCP routes.
"""

import json

import object_finance
import object_mcp
import object_stock

from test_object_server import (
    create_identity_session,
    enable_admin_token,
    request,
    session_headers,
    write_records,
)


def _setup(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    # stock_moves + fin_journals/lines for owner "7"
    write_records(
        data_dir, "stock_moves",
        "id\tproduct_id\tfrom_location_id\tto_location_id\tquantity\towner_id\n"
        "m1\tp1\t\tloc1\t10\t7\n"
        "m2\tp1\tloc1\t\t3\t7\n",
    )
    write_records(
        data_dir, "fin_accounts",
        "id\tname\tcode\taccount_type\towner_id\n"
        "a1\tCash\t1010\tasset\t7\n"
        "a2\tIncome\t4000\tincome\t7\n",
    )
    write_records(
        data_dir, "fin_journals",
        "id\tstatus\towner_id\tdate\n"
        "j1\tposted\t7\t2026-01-01\n"
        "j2\tdraft\t7\t2026-01-02\n",
    )
    write_records(
        data_dir, "fin_journal_lines",
        "id\tjournal_id\taccount_id\tdebit_cents\tcredit_cents\towner_id\n"
        "l1\tj1\ta1\t5000\t0\t7\n"
        "l2\tj1\ta2\t0\t5000\t7\n"
        "l3\tj2\ta1\t9999\t0\t7\n",  # draft -> must NOT contribute
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)
    return data_dir


def _viewer_headers(user_id):
    token, _ = create_identity_session({"user_id": user_id})
    return session_headers(token)


def test_stock_levels_endpoint_matches_the_pure_fold(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    status, _, payload = request("/api/stock", headers=_viewer_headers("7"))
    assert status == 200 and payload["status"] == "ok"
    direct = object_stock.stock_levels(base_dir=data_dir, owner="7")
    for key in ("levels", "totals"):
        assert payload[key] == direct[key]


def test_finance_summary_endpoint_matches_the_pure_trial_balance(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    status, _, payload = request("/api/finance/summary", headers=_viewer_headers("7"))
    assert status == 200 and payload["status"] == "ok"
    assert payload["rows"] == object_finance.trial_balance(base_dir=data_dir, owner="7")
    # the draft journal's line (9999) must not appear in any account total
    dumped = json.dumps(payload["rows"])
    assert "9999" not in dumped


def test_owner_scoped_a_different_owner_sees_their_own_empty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # owner 8 has no stock/journals -> empty, but still a 200 ok (not another
    # owner's data).
    _, _, stock = request("/api/stock", headers=_viewer_headers("8"))
    assert stock["levels"] == [] and stock["totals"] == []
    _, _, fin = request("/api/finance/summary", headers=_viewer_headers("8"))
    assert fin["rows"] == []


def test_anonymous_gets_empty_not_error(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s1, _, stock = request("/api/stock")
    assert s1 == 200 and stock["authenticated"] is False and stock["totals"] == []
    s2, _, fin = request("/api/finance/summary")
    assert s2 == 200 and fin["authenticated"] is False and fin["rows"] == []


def test_missing_collections_do_not_500(tmp_path, monkeypatch):
    # a fresh data dir with no stock_moves / journals at all
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "empty"))
    enable_admin_token(monkeypatch)
    s1, _, stock = request("/api/stock", headers=_viewer_headers("7"))
    assert s1 == 200 and stock["totals"] == []
    s2, _, fin = request("/api/finance/summary", headers=_viewer_headers("7"))
    assert s2 == 200 and fin["rows"] == []


def test_mcp_verbs_route_to_the_derived_endpoints():
    m1, p1, _, _ = object_mcp.tool_route("get_stock_levels", {})
    assert (m1, p1) == ("GET", "/api/stock")
    m2, p2, _, _ = object_mcp.tool_route("get_finance_summary", {})
    assert (m2, p2) == ("GET", "/api/finance/summary")


def test_derived_verbs_are_in_the_tool_catalog():
    names = {t["name"] for t in object_mcp.TOOLS}
    assert {"get_stock_levels", "get_finance_summary"} <= names
