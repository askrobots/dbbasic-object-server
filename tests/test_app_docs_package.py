"""Tests for packages/app-docs: the public documentation site at /docs and
/docs/{slug}. A Rails-Guides-shaped guide site -- sidebar grouped by
category, client-side search, per-page table of contents, prev/next paging
-- built as bespoke page objects (not the view_render block system) on top
of the shared /style theme and the ONE shared markdown renderer at
/markdown. Content is seeded doc_pages records adapted from the internal
docs/ tree; the whole surface is public and unauthenticated.

Behavioral JS tests follow the repo's node-execution pattern (see
tests/test_markdown_object.py): extract the real function source from the
Python-embedded script string, run it under node with stubbed globals,
assert on real output -- not just source-text greps.
"""

import csv
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import object_packages

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_DOCS_DIR = PACKAGES_ROOT / "app-docs"

NAV_SOURCE = (APP_DOCS_DIR / "objects" / "site" / "docs_nav.py").read_text()
INDEX_SOURCE = (APP_DOCS_DIR / "objects" / "site" / "docs_index.py").read_text()
DETAIL_SOURCE = (APP_DOCS_DIR / "objects" / "site" / "docs_detail.py").read_text()


def _seed_rows(name):
    with open(APP_DOCS_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    return node


# --- package shape -----------------------------------------------------------


def test_manifest_ships_three_objects_schema_permissions_and_both_seeds():
    package = object_packages.get_package("app-docs", root=PACKAGES_ROOT)
    assert {o["id"] for o in package["objects"]} == {
        "site_docs_nav", "site_docs_index", "site_docs_detail",
    }
    assert {s["collection"] for s in package["schemas"]} == {"doc_pages"}
    assert {entry["collection"] for entry in package["seed"]} == {
        "site_routes", "doc_pages",
    }


def test_routes_seed_detail_and_index_with_articles_style_priorities():
    rows = _seed_rows("site_routes")
    by_id = {r["id"]: r for r in rows}
    detail = by_id["route_docs_detail"]
    index = by_id["route_docs_index"]
    assert detail["pattern"] == "/docs/{slug}"
    assert detail["object_id"] == "site_docs_detail"
    assert index["pattern"] == "/docs"
    assert index["object_id"] == "site_docs_index"
    # Same convention as app-articles: the more specific pattern gets the
    # lower (higher-precedence) priority number.
    assert int(detail["priority"]) < int(index["priority"])


def test_permissions_make_every_surface_public():
    payload = json.loads((APP_DOCS_DIR / "permissions" / "rules.json").read_text())
    rules = payload["rules"]
    assert all(rule["principal"] == "public" for rule in rules)
    read_rules = [r for r in rules if "read" in r["actions"]]
    assert any(r.get("collection") == "doc_pages" for r in read_rules)
    # A doc page is public unconditionally -- no row_filter gate like
    # articles' is_public, because nothing non-public is ever seeded.
    assert all("row_filter" not in r for r in read_rules)
    executable = {r.get("object_id") for r in rules if "execute" in r["actions"]}
    # All three page objects, including the shared nav script -- without
    # site_docs_nav here the sidebar 403s for anonymous visitors and the
    # whole site is broken for exactly the audience it exists for.
    assert executable == {"site_docs_nav", "site_docs_index", "site_docs_detail"}


def test_doc_pages_seed_is_nonempty_and_every_row_is_complete():
    rows = _seed_rows("doc_pages")
    assert len(rows) >= 30
    slug_re = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    seen = set()
    for row in rows:
        assert slug_re.fullmatch(row["id"]), row["id"]
        assert row["id"] not in seen
        seen.add(row["id"])
        assert row["title"].strip()
        assert row["category"].strip()
        assert int(row["nav_order"]) > 0
        assert row["summary"].strip()
        assert len(row["content"]) > 500, f"{row['id']} content suspiciously short"
        # The page template renders the title itself -- content starting
        # with an H1 would double the title on every page.
        assert not row["content"].lstrip().startswith("# ")


def test_seeded_cross_links_resolve_to_seeded_slugs_only():
    rows = _seed_rows("doc_pages")
    slugs = {row["id"] for row in rows}
    link_re = re.compile(r"\]\(/docs/([A-Za-z0-9_-]+)")
    for row in rows:
        for slug in link_re.findall(row["content"]):
            assert slug in slugs, f"{row['id']} links to unseeded /docs/{slug}"
        # No internal-tree .md links should survive the seed build.
        assert ".md)" not in row["content"], f"{row['id']} kept a raw .md link"


def test_no_disallowed_org_names_leak_into_the_package():
    banned = re.compile(r"\bq9\b|askrobots|\bwold\b", re.IGNORECASE)
    for path in sorted(APP_DOCS_DIR.rglob("*")):
        if path.is_file():
            hits = banned.findall(path.read_text(errors="replace"))
            assert not hits, f"{path} contains banned names: {hits}"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-docs",
        root=PACKAGES_ROOT,
        base_dir=tmp_path / "data",
        object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


# --- page structure ----------------------------------------------------------


def test_pages_are_public_chrome_not_the_signed_in_apps():
    for source in (INDEX_SOURCE, DETAIL_SOURCE):
        # The shared building blocks every themed page uses...
        assert '<link rel="stylesheet" href="/style">' in source
        assert '<script src="/markdown"></script>' in source
        assert '<script src="/docs-nav"></script>' in source
        # ...but never the internal signed-in shell's nav or a login nudge.
        assert 'src="/nav"' not in source
        assert "/login" not in source


def test_detail_gates_the_slug_before_interpolating_into_script():
    # `slug` is URL-derived and lands inside a <script> block via {slug!r};
    # repr-escaping alone cannot stop a "</script>" payload from closing
    # the tag, so the same regex gate view_render.py uses must run first.
    assert "_SLUG_RE" in DETAIL_SOURCE
    gate = DETAIL_SOURCE.index("_SLUG_RE.fullmatch(slug)")
    interpolate = DETAIL_SOURCE.index("{slug!r}")
    assert gate < interpolate
    pattern = re.search(r'_SLUG_RE = re\.compile\(r"([^"]+)"\)', DETAIL_SOURCE)
    compiled = re.compile(pattern.group(1))
    assert not compiled.fullmatch("</script><script>alert(1)</script>")
    assert not compiled.fullmatch("")
    assert compiled.fullmatch("single-vm-deployment")


def test_nav_manifest_fetch_is_cached_per_tab_not_per_navigation():
    # Every /docs/{slug} click is a full page load; the manifest (all page
    # bodies included) is a few hundred KB -- so it must come from
    # sessionStorage after the first load, not refetch each navigation.
    assert "sessionStorage.getItem" in NAV_SOURCE
    assert "sessionStorage.setItem" in NAV_SOURCE
    assert NAV_SOURCE.count("/collections/doc_pages/records") == 1


# --- real JS behavior (node-executed) ---------------------------------------


def _extract_js(source, marker):
    """Extract a top-level `function name(...) {...}` from the embedded JS
    by brace counting, starting at `marker`."""
    start = source.index(marker)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces after {marker}")


def test_sidebar_sorts_by_nav_order_and_groups_by_first_appearance(tmp_path):
    node = _node()
    sort_fn = _extract_js(NAV_SOURCE, "function sortManifest(")
    group_fn = _extract_js(NAV_SOURCE, "function groupByCategory(")
    probe = tmp_path / "probe.js"
    probe.write_text(
        sort_fn + "\n" + group_fn + "\n" + """
const records = [
  {id: "c", title: "C", category: "Two", nav_order: "300"},
  {id: "a", title: "A", category: "One", nav_order: "100"},
  {id: "d", title: "D", category: "One", nav_order: "110"},
  {id: "b", title: "B", category: "Two", nav_order: "250"},
  {id: "x", title: "X", category: "", nav_order: "junk"},
];
const sorted = sortManifest(records);
const groups = groupByCategory(sorted);
console.log(JSON.stringify({
  order: sorted.map((r) => r.id),
  groups: groups.map((g) => [g.category, g.items.map((i) => i.id)]),
}));
""")
    out = subprocess.run([node, str(probe)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    # Numeric sort (not string sort -- "300" > "1000" as strings), with the
    # unparseable nav_order sinking to the end.
    assert result["order"] == ["a", "d", "b", "c", "x"]
    # Groups keep first-appearance order over the sorted list; a blank
    # category lands in "General".
    assert result["groups"] == [
        ["One", ["a", "d"]],
        ["Two", ["b", "c"]],
        ["General", ["x"]],
    ]


def test_toc_builder_assigns_ids_dedupes_and_indents_h3(tmp_path):
    node = _node()
    slug_fn = _extract_js(DETAIL_SOURCE, "function slugify(")
    toc_fn = _extract_js(DETAIL_SOURCE, "function buildToc(")
    probe = tmp_path / "probe.js"
    probe.write_text("""
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
""" + slug_fn + "\n" + toc_fn + "\n" + """
global.document = {getElementById: () => null};
function head(tag, text) {
  return {tagName: tag.toUpperCase(), textContent: text, id: ""};
}
const heads = [head("h2", "Setup"), head("h3", "Setup"), head("h2", "Run & verify")];
const container = {querySelectorAll: () => heads};
const html = buildToc(container);
console.log(JSON.stringify({ids: heads.map((h) => h.id), html: html}));
""")
    out = subprocess.run([node, str(probe)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    # Duplicate heading text gets a deduped id, punctuation slugifies away.
    assert result["ids"] == ["setup", "setup-2", "run-verify"]
    assert '#setup"' in result["html"]
    assert '#setup-2"' in result["html"]
    assert "doctocsub" in result["html"]  # the h3 is indented


def test_search_filter_hides_nonmatching_items_and_empty_groups(tmp_path):
    node = _node()
    wire_fn = _extract_js(NAV_SOURCE, "function wireSearch(")
    probe = tmp_path / "probe.js"
    probe.write_text(wire_fn + "\n" + """
function item(haystack) {
  return {
    style: {display: ""},
    getAttribute: (name) => (name === "data-search" ? haystack : null),
  };
}
const one = item("quickstart from a fresh vm start here");
const two = item("permissions model security");
const groupOne = {style: {display: ""}, querySelectorAll: () => [one]};
const groupTwo = {style: {display: ""}, querySelectorAll: () => [two]};
let handler = null;
const inputEl = {
  value: "",
  addEventListener: (evt, fn) => { handler = fn; },
};
const listEl = {
  querySelectorAll: (sel) =>
    (sel === ".navitem" ? [one, two] : [groupOne, groupTwo]),
};
wireSearch(inputEl, listEl);
inputEl.value = "PERMISS";
handler();
const filtered = [one.style.display, two.style.display, groupOne.style.display, groupTwo.style.display];
inputEl.value = "";
handler();
const restored = [one.style.display, two.style.display, groupOne.style.display, groupTwo.style.display];
console.log(JSON.stringify({filtered, restored}));
""")
    out = subprocess.run([node, str(probe)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    # Case-insensitive match keeps only the permissions item and its group.
    assert result["filtered"] == ["none", "", "none", ""]
    # Clearing the box restores everything.
    assert result["restored"] == ["", "", "", ""]


def test_prev_next_pager_walks_the_manifest_sequence(tmp_path):
    node = _node()
    pager_fn = _extract_js(DETAIL_SOURCE, "function pagerHtml(")
    probe = tmp_path / "probe.js"
    probe.write_text("""
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
""" + pager_fn + "\n" + """
const middle = pagerHtml({id: "why", title: "Why"}, {id: "quickstart", title: "Quickstart"});
const first = pagerHtml(null, {id: "quickstart", title: "Quickstart"});
const last = pagerHtml({id: "why", title: "Why"}, null);
console.log(JSON.stringify({middle, first, last}));
""")
    out = subprocess.run([node, str(probe)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert '/docs/why' in result["middle"] and '/docs/quickstart' in result["middle"]
    assert '/docs/why' not in result["first"] and '/docs/quickstart' in result["first"]
    assert '/docs/quickstart' not in result["last"] and '/docs/why' in result["last"]
