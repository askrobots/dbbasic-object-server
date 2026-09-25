"""Tests for packages/app-sites: websites published as site_pages records and
served at /s/{site} and /s/{site}/{page} by site_sites_page.

What these guard, and why it would matter:

- The author's HTML is served sandboxed without allow-same-origin: a page's
  scripts running as this server's origin could read the visitor's session
  and act as them. This is the one claim that must never regress.
- A private page and a missing page look the same (404): otherwise the
  address alone tells a stranger what someone has drafted.
- The owner sees their own unpublished pages; nobody else does.
"""

import csv
import importlib.util
import json
from pathlib import Path

import object_packages
import object_records

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_DIR = PACKAGES_ROOT / "app-sites"


def _page_module():
    spec = importlib.util.spec_from_file_location("sites_page", APP_DIR / "objects" / "site" / "sites_page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROWS = [
    {"id": "1", "site": "bakery", "slug": "index", "title": "Home", "html": "<h1>Sunrise Bakery</h1>",
     "is_public": "true", "owner_id": "dan"},
    {"id": "2", "site": "bakery", "slug": "menu", "title": "Menu", "html": "<h1>Menu</h1>",
     "is_public": "true", "owner_id": "dan"},
    {"id": "3", "site": "bakery", "slug": "secret", "title": "Draft", "html": "<h1>Draft</h1>",
     "is_public": "false", "owner_id": "dan"},
]


def _get(monkeypatch, request, rows=ROWS):
    page = _page_module()
    monkeypatch.setattr(object_records, "read_collection_records", lambda *a, **k: list(rows))
    return page.GET(request)


def test_manifest_ships_the_page_object_schema_rules_and_routes():
    package = object_packages.get_package("app-sites", root=PACKAGES_ROOT)
    assert {o["id"] for o in package["objects"]} == {"site_sites_page"}
    assert {s["collection"] for s in package["schemas"]} == {"site_pages"}
    with open(APP_DIR / "seed" / "site_routes.tsv", newline="") as fh:
        routes = {r["pattern"]: r for r in csv.DictReader(fh, delimiter="\t")}
    assert routes["/s/{site}/{page}"]["object_id"] == "site_sites_page"
    assert routes["/s/{site}"]["object_id"] == "site_sites_page"
    # the more specific pattern wins
    assert int(routes["/s/{site}/{page}"]["priority"]) < int(routes["/s/{site}"]["priority"])


def test_pages_are_served_sandboxed_without_same_origin(monkeypatch):
    status, headers, body = _get(monkeypatch, {"site": "bakery", "page": "menu"})
    assert status == 200 and body == "<h1>Menu</h1>"
    csp = headers["content-security-policy"]
    assert csp.startswith("sandbox")
    assert "allow-same-origin" not in csp
    assert headers["x-content-type-options"] == "nosniff"


def test_the_site_home_goes_to_its_index_page(monkeypatch):
    """/s/bakery redirects into the site's folder, so the index page's
    relative links ("about.html") resolve inside the site (from /s/bakery a
    browser would send them to /s/about.html)."""
    status, headers, _ = _get(monkeypatch, {"site": "bakery"})
    assert status == 302 and headers["location"] == "/s/bakery/index"
    status, _, body = _get(monkeypatch, {"site": "bakery", "page": "index"})
    assert status == 200 and "Sunrise Bakery" in body


def test_builder_links_ending_in_html_resolve(monkeypatch):
    status, _, body = _get(monkeypatch, {"site": "bakery", "page": "menu.html"})
    assert status == 200 and body == "<h1>Menu</h1>"


def test_a_private_page_is_a_404_like_a_missing_one(monkeypatch):
    private = _get(monkeypatch, {"site": "bakery", "page": "secret"})
    missing = _get(monkeypatch, {"site": "bakery", "page": "nothing-here"})
    other = _get(monkeypatch, {"site": "bakery", "page": "secret", "_identity": {"user_id": "eve"}})
    assert private[0] == missing[0] == other[0] == 404
    assert private[2] == missing[2] == other[2]


def test_the_owner_sees_their_own_draft(monkeypatch):
    status, _, body = _get(monkeypatch, {"site": "bakery", "page": "secret", "_identity": {"user_id": "dan"}})
    assert status == 200 and body == "<h1>Draft</h1>"


def test_names_outside_the_pattern_never_reach_a_lookup(monkeypatch):
    for request in ({"site": "../etc", "page": "index"}, {"site": "bakery", "page": "a/b"},
                    {"site": ""}, {"site": "Bakery!"}):
        assert _get(monkeypatch, request)[0] == 404


def test_without_the_collection_nothing_is_published(monkeypatch):
    page = _page_module()

    def missing(*a, **k):
        raise FileNotFoundError("no collection")

    monkeypatch.setattr(object_records, "read_collection_records", missing)
    assert page.GET({"site": "bakery", "page": "index"})[0] == 404


def test_rules_let_owners_write_their_own_and_everyone_read_only_public():
    rules = json.loads((APP_DIR / "permissions" / "rules.json").read_text())["rules"]
    owner = next(r for r in rules if r.get("collection") == "site_pages" and r["principal"] == "registered")
    public = next(r for r in rules if r.get("collection") == "site_pages" and r["principal"] == "public")
    assert owner["row_filter"] == {"owner_id": "$user_id"}
    assert public["actions"] == ["read"] and public["row_filter"] == {"is_public": "true"}
