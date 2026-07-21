"""Analytics: traffic capture (object_analytics + the server hook), the
retention/rotation primitive (prune_collection_records), and the daemon pass.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import object_analytics
import object_daemon
import object_packages
import object_records
import object_server

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"


# ---- pure engine ----------------------------------------------------------

def test_config_from_env():
    assert object_analytics.analytics_enabled({"DBBASIC_ANALYTICS": "on"})
    assert object_analytics.analytics_enabled({"DBBASIC_ANALYTICS": "1"})
    assert not object_analytics.analytics_enabled({})
    assert not object_analytics.analytics_enabled({"DBBASIC_ANALYTICS": "off"})
    assert object_analytics.owner_ips({"DBBASIC_ANALYTICS_OWNER_IPS": "1.1.1.1, 2.2.2.2"}) == {"1.1.1.1", "2.2.2.2"}
    assert object_analytics.retention_days({"DBBASIC_ANALYTICS_RETENTION_DAYS": "7"}) == 7
    assert object_analytics.retention_days({}) == 30
    assert object_analytics.retention_days({"DBBASIC_ANALYTICS_RETENTION_DAYS": "junk"}) == 30


def test_should_capture_skips_assets_but_keeps_api_and_errors():
    assert object_analytics.should_capture("/dashboard")
    assert object_analytics.should_capture("/api/mcp")       # bots hit APIs -- capture
    assert object_analytics.should_capture("/some/missing")  # 404 targets captured (status is separate)
    assert not object_analytics.should_capture("/static/app.css")
    assert not object_analytics.should_capture("/favicon.ico")
    assert not object_analytics.should_capture("/healthz")
    assert not object_analytics.should_capture("/realtime/stream")


def test_build_page_view_extracts_request_fields():
    row = object_analytics.build_page_view(
        path="/pricing", method="get", status=200, ip="9.9.9.9",
        headers={"user-agent": "curl/8", "referer": "https://x.com/", "cookie": "a=1; session_id=sess42"},
        owners=frozenset({"9.9.9.9"}),
    )
    assert row["path"] == "/pricing" and row["method"] == "GET" and row["status"] == "200"
    assert row["ip"] == "9.9.9.9" and row["user_agent"] == "curl/8"
    assert row["referrer"] == "https://x.com/" and row["session_id"] == "sess42"
    assert row["is_owner"] == "true"   # ip is in owners
    # a non-owner ip
    row2 = object_analytics.build_page_view(
        path="/", method="GET", status=404, ip="5.5.5.5", headers={}, owners=frozenset({"9.9.9.9"}))
    assert row2["is_owner"] == "false" and row2["user_agent"] == "" and row2["session_id"] == ""


# ---- retention primitive (prune) ------------------------------------------

def _install(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for pkg in ("app-views", "app-rollup", "app-analytics"):
        object_packages.install_package(pkg, root=PACKAGES_ROOT, base_dir=data_dir,
                                        object_roots=[object_root], allow_replace=True)
    return data_dir


def _append(data_dir, path, created_at):
    # created_at is read_only, so set it explicitly with preserve_read_only
    return object_records.create_collection_record(
        "page_views", {"path": path, "method": "GET", "status": "200", "created_at": created_at},
        base_dir=data_dir, actor="t", preserve_read_only=True)


def test_prune_drops_old_keeps_recent_and_undateable(tmp_path):
    data_dir = _install(tmp_path)
    _append(data_dir, "/old", "2026-01-01T00:00:00Z")
    _append(data_dir, "/recent", "2026-07-20T00:00:00Z")
    _append(data_dir, "/undateable", "")  # no timestamp -> never dropped

    result = object_records.prune_collection_records(
        "page_views", keep_newer_than="2026-06-01T00:00:00Z", base_dir=data_dir)
    assert result["pruned"] and result["removed"] == 1
    paths = {r["path"] for r in object_records.read_collection_records("page_views", base_dir=data_dir)}
    assert paths == {"/recent", "/undateable"}  # /old aged out

    # idempotent: nothing older than the cutoff remains
    again = object_records.prune_collection_records(
        "page_views", keep_newer_than="2026-06-01T00:00:00Z", base_dir=data_dir)
    assert again["pruned"] is False and again["removed"] == 0


# ---- daemon retention pass ------------------------------------------------

def test_retention_pass_gated_and_prunes(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    _append(data_dir, "/old", old)
    _append(data_dir, "/fresh", fresh)

    # disabled -> no-op
    monkeypatch.delenv("DBBASIC_ANALYTICS", raising=False)
    assert object_daemon.process_analytics_retention(base_dir=data_dir) is None

    # enabled, 30d retention -> the 90d-old row is aged out, the 1d row stays
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    monkeypatch.setenv("DBBASIC_ANALYTICS_RETENTION_DAYS", "30")
    result = object_daemon.process_analytics_retention(base_dir=data_dir)
    assert result and result["removed"] == 1
    paths = {r["path"] for r in object_records.read_collection_records("page_views", base_dir=data_dir)}
    assert paths == {"/fresh"}

    # marker-gated: an immediate re-run is skipped (returns None, not a re-prune)
    assert object_daemon.process_analytics_retention(base_dir=data_dir) is None


# ---- the server capture hook ----------------------------------------------

def test_capture_hook_appends_a_row_when_enabled(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv("DBBASIC_ANALYTICS", "on")
    scope = {"client": ("8.8.8.8", 4321)}
    headers = {"user-agent": "Mozilla", "referer": "https://ref/", "cookie": "session_id=s1"}

    asyncio.run(object_server._capture_page_view(scope, "GET", "/pricing", 200, headers))
    rows = object_records.read_collection_records("page_views", base_dir=data_dir)
    assert len(rows) == 1 and rows[0]["path"] == "/pricing" and rows[0]["ip"] == "8.8.8.8"
    assert rows[0]["session_id"] == "s1" and rows[0]["created_at"]

    # a skip-path is not captured
    asyncio.run(object_server._capture_page_view(scope, "GET", "/static/x.js", 200, headers))
    assert len(object_records.read_collection_records("page_views", base_dir=data_dir)) == 1


def test_rollups_aggregate_traffic_and_exclude_owner(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for pkg in ("app-views", "app-rollup", "app-analytics"):
        object_packages.install_package(pkg, root=PACKAGES_ROOT, base_dir=data_dir,
                                        object_roots=[object_root], allow_replace=True)
    # the reporting rollups seeded cleanly into the shared rollup_definitions
    defs = {d["id"] for d in object_records.read_collection_records("rollup_definitions", base_dir=data_dir)}
    assert {"ana_top_paths", "ana_top_ips", "ana_status", "ana_daily"} <= defs

    def hit(path, ip, status, owner=False):
        object_records.create_collection_record(
            "page_views",
            {"path": path, "method": "GET", "status": str(status), "ip": ip,
             "is_owner": "true" if owner else "false"},
            base_dir=data_dir, actor="t")

    for _ in range(3):
        hit("/a", "6.6.6.6", 200)          # a "bot" hammering /a from one IP
    hit("/b", "1.2.3.4", 404)              # a 404
    hit("/admin", "9.9.9.9", 200, owner=True)  # owner traffic -> excluded

    result = object_daemon.process_rollups(base_dir=data_dir)
    assert result is not None

    top_paths = {r["path"]: int(r["hits"]) for r in object_records.read_collection_records("analytics_top_paths", base_dir=data_dir)}
    assert top_paths == {"/a": 3, "/b": 1}   # /admin excluded (is_owner)
    top_ips = {r["ip"]: int(r["hits"]) for r in object_records.read_collection_records("analytics_top_ips", base_dir=data_dir)}
    assert top_ips.get("6.6.6.6") == 3       # the bot is visible, ranked
    status = {r["status"]: int(r["hits"]) for r in object_records.read_collection_records("analytics_status", base_dir=data_dir)}
    assert status.get("404") == 1 and status.get("200") == 3


def test_conversions_collection_and_analytics_view_seeded(tmp_path):
    import json
    data_dir = _install(tmp_path)
    # conversions collection installs and records a goal event (app-driven write)
    conv = object_records.create_collection_record(
        "conversions", {"event_type": "signup", "session_id": "s1", "metadata": json.dumps({"plan": "pro"})},
        base_dir=data_dir, actor="app")
    assert conv["event_type"] == "signup"
    assert object_records.read_collection_records("conversions", base_dir=data_dir)[0]["event_type"] == "signup"

    # the /analytics generative view + route seeded
    views = {v["id"]: v for v in object_records.read_collection_records("views", base_dir=data_dir)}
    assert "view_analytics" in views and views["view_analytics"]["route"] == "/analytics"
    blocks = json.loads(views["view_analytics"]["blocks"])
    listed = {b["collection"] for b in blocks if b.get("kind") == "list"}
    assert listed == {"analytics_top_ips", "analytics_top_paths", "analytics_status", "analytics_daily"}
    routes = {r["pattern"]: r["object_id"] for r in object_records.read_collection_records("site_routes", base_dir=data_dir)}
    assert routes.get("/analytics") == "site_view_render"


def test_capture_hook_noop_when_disabled(tmp_path, monkeypatch):
    data_dir = _install(tmp_path)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.delenv("DBBASIC_ANALYTICS", raising=False)
    asyncio.run(object_server._capture_page_view({"client": ("1.1.1.1", 1)}, "GET", "/", 200, {}))
    assert object_records.read_collection_records("page_views", base_dir=data_dir) == []
