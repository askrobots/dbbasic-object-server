"""Tests for 64 (Feed): GET /api/feed, its blocks.feed schema discovery,
and the get_feed MCP verb.

The point of this suite is the privacy invariant the spec (plan/
vocabulary/64-feed-spec.md, Permissions Posture) states up front: the feed
composes two EXISTING permission-gated /collections/{c}/records reads (58)
through _internal_request, forwarding the viewer's own credentials --
never a direct read of the record layer. So a followed account's PRIVATE
content can never enter the feed; following only changes which already-
PUBLIC rows get selected in. Tests use real minted sessions (not trusted
headers) so the Authorization-header forwarding in object_server._handle_
feed is actually exercised end-to-end, not just the outer request's own
identity resolution.
"""

import json

import object_server

from test_object_server import (
    create_identity_session,
    enable_admin_token,
    request,
    save_permission_policy,
    session_headers,
    write_records,
)

ARTICLES_FIELDS = [
    {"name": "id"},
    {"name": "title", "type": "text"},
    {"name": "owner_id", "type": "text"},
    {"name": "is_public", "type": "boolean"},
    {"name": "published_on", "type": "date"},
]

FOLLOWS_FIELDS = [
    {"name": "id"},
    {"name": "follower_id", "type": "text"},
    {"name": "following_id", "type": "text"},
    {"name": "owner_id", "type": "text"},
]

# Mirrors packages/app-articles/permissions/rules.json and
# packages/app-worker/permissions/rules.json's real posture: owners manage
# their own rows, everyone (including anonymous) may read public articles
# and the follow graph.
FEED_TEST_POLICY = {
    "access_mode": "role_based",
    "rules": [
        {
            "effect": "allow",
            "principal": "public",
            "actions": ["read"],
            "collection": "follows",
        },
        {
            "effect": "allow",
            "principal": "registered",
            "actions": ["create", "read", "update", "delete"],
            "collection": "follows",
            "row_filter": {"owner_id": "$user_id"},
        },
        {
            "effect": "allow",
            "principal": "registered",
            "actions": ["create", "read", "update", "delete"],
            "collection": "articles",
            "row_filter": {"owner_id": "$user_id"},
        },
        {
            "effect": "allow",
            "principal": "public",
            "actions": ["read"],
            "collection": "articles",
            "row_filter": {"is_public": "true"},
        },
    ],
}


def write_articles_schema(data_dir, *, with_feed_block=True):
    path = data_dir / "schemas" / "articles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fields": ARTICLES_FIELDS}
    if with_feed_block:
        payload["blocks"] = {
            "feed": {
                "owner_field": "owner_id",
                "visibility_field": "is_public",
                "visibility_true_value": "true",
                "time_field": "published_on",
                "summary_fields": ["title"],
                "link_field": "id",
            }
        }
    path.write_text(json.dumps(payload))
    return path


def write_follows_schema(data_dir):
    path = data_dir / "schemas" / "follows.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": FOLLOWS_FIELDS}))
    return path


def setup_feed_env(tmp_path, monkeypatch, *, with_feed_block=True):
    data_dir = tmp_path / "data"
    write_articles_schema(data_dir, with_feed_block=with_feed_block)
    write_follows_schema(data_dir)
    save_permission_policy(data_dir, FEED_TEST_POLICY)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    enable_admin_token(monkeypatch)
    return data_dir


def viewer_session_headers(user_id):
    token, _ = create_identity_session({"user_id": user_id})
    return session_headers(token)


def write_base_fixture(data_dir):
    """Viewer v1 follows a1 (not b1). a1 has one public + one private
    article; b1 has one public article. A second public article by a1,
    dated later, exists to exercise newest-first ordering."""
    write_records(
        data_dir,
        "follows",
        "id\tfollower_id\tfollowing_id\towner_id\n"
        "f1\tv1\ta1\tv1\n",
    )
    write_records(
        data_dir,
        "articles",
        "id\ttitle\towner_id\tis_public\tpublished_on\n"
        "art1\tPublic A1\ta1\ttrue\t2026-01-01\n"
        "art2\tPrivate A1\ta1\tfalse\t2026-01-02\n"
        "art3\tPublic B1\tb1\ttrue\t2026-01-03\n"
        "art4\tPublic A2\ta1\ttrue\t2026-02-01\n",
    )


def test_feed_includes_public_article_from_followed_account(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v1"))

    assert status == 200
    assert payload["authenticated"] is True
    ids = [item["source_id"] for item in payload["items"]]
    assert "art1" in ids


def test_feed_excludes_private_article_from_followed_account(tmp_path, monkeypatch):
    """Core privacy test: a1 is followed, but art2 is a1's PRIVATE article.
    Following someone must never widen what the viewer can read -- art2
    must never appear, even though its author is in the followed set."""
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v1"))

    assert status == 200
    ids = [item["source_id"] for item in payload["items"]]
    assert "art2" not in ids


def test_feed_excludes_public_article_from_unfollowed_account(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v1"))

    assert status == 200
    ids = [item["source_id"] for item in payload["items"]]
    assert "art3" not in ids  # public, but authored by b1, whom v1 does not follow


def test_feed_anonymous_returns_empty_not_error(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)

    status, _, payload = request("/api/feed")

    assert status == 200
    assert payload == {"status": "ok", "items": [], "count": 0, "authenticated": False}


def test_feed_disabled_returns_empty_with_flag(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)
    monkeypatch.setenv(object_server.FEED_ENABLED_ENV, "false")

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v1"))

    assert status == 200
    assert payload == {"status": "ok", "items": [], "count": 0, "enabled": False}


def test_feed_viewer_follows_nobody_returns_empty(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)  # v1 follows a1; v2 follows no one

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v2"))

    assert status == 200
    assert payload["authenticated"] is True
    assert payload["items"] == []
    assert payload["count"] == 0


def test_feed_items_sorted_newest_first(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v1"))

    assert status == 200
    times = [item["time"] for item in payload["items"]]
    assert times == sorted(times, reverse=True)
    # art4 (2026-02-01) must sort ahead of art1 (2026-01-01).
    ids_in_order = [item["source_id"] for item in payload["items"]]
    assert ids_in_order.index("art4") < ids_in_order.index("art1")


def test_feed_truncates_following_over_cap_and_flags(tmp_path, monkeypatch):
    data_dir = setup_feed_env(tmp_path, monkeypatch)
    write_base_fixture(data_dir)
    rows = ["id\tfollower_id\tfollowing_id\towner_id"]
    for i in range(object_server.FILTER_IN_MAX_VALUES + 1):
        rows.append(f"f{i}\tv3\tacct{i}\tv3")
    write_records(data_dir, "follows", "\n".join(rows) + "\n")

    status, _, payload = request("/api/feed", headers=viewer_session_headers("v3"))

    assert status == 200
    assert payload["authenticated"] is True
    assert payload["truncated_following"] is True


def test_feed_sources_discovers_blocks_feed_metadata(tmp_path, monkeypatch):
    """_feed_sources() discovers a collection's `blocks.feed` key through
    the normal get_schema path (blocks is whitelisted in
    object_schemas._normalize_schema, so it survives normalization and
    install) -- the additive-opt-in metadata a collection declares to
    become a feed source (spec's Parameterization section)."""
    data_dir = setup_feed_env(tmp_path, monkeypatch)

    sources = object_server._feed_sources()

    assert len(sources) == 1
    source = sources[0]
    assert source["collection"] == "articles"
    assert source["owner_field"] == "owner_id"
    assert source["visibility_field"] == "is_public"
    assert source["visibility_true_value"] == "true"
    assert source["time_field"] == "published_on"
    assert source["summary_fields"] == ["title"]
    assert source["link_field"] == "id"


def test_feed_sources_skips_collections_without_blocks_feed(tmp_path, monkeypatch):
    """A collection with no blocks.feed key (here, follows) is simply not
    included -- additive opt-in, nothing swept in by default."""
    data_dir = setup_feed_env(tmp_path, monkeypatch, with_feed_block=False)

    sources = object_server._feed_sources()

    assert sources == []


def test_mcp_get_feed_tool_route_maps_to_get_api_feed():
    import urllib.parse

    import object_mcp

    method, path, query, body = object_mcp.tool_route("get_feed", {"limit": 5, "offset": 10})

    assert method == "GET"
    assert path == "/api/feed"
    pairs = dict(urllib.parse.parse_qsl(query))
    assert pairs["limit"] == "5"
    assert pairs["offset"] == "10"
    assert body == b""
