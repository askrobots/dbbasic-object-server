"""Every attention count must be openable.

A count is a promise that something is waiting and that clicking it will
show you the waiting things. Four of them were pointing at URLs nothing
served -- /scans?status=extracted, /time-logs?status=submitted,
/expenses?status=submitted -- and one was pointing at /orders, a path
borrowed from a package it has nothing to do with, listing none of the
rows it counted. The arithmetic was right and the link was a lie, which
is the worst combination available: a number people learn to stop
clicking teaches them to stop reading the band it sits in.

The fix was four seeded views. The reason the fix stays fixed is this
file, and specifically that it is a SWEEP rather than four cases. The
gap did not appear because somebody wrote a bad path; it appeared
because declaring a path and serving one are separate acts in separate
files, and nothing had ever compared them. `_normalize_attention` says
so out loud -- "whether anything answers it is a routing question this
module deliberately does not pretend to answer" -- so the answering has
to happen here, over every manifest on the box, including the ones
nobody has written yet.

Three properties, in order of how much they hurt when broken:

1. **The path resolves.** Against the union of every package's seeded
   site_routes rows, plus the `site_*` convention, which is the same
   pair object_server resolves against at request time.
2. **A site_view_render path has a view behind it.** A route to the
   generic renderer whose `views` record is missing (or whose route
   field disagrees) renders "View not found" -- a 404 with a 200's
   clothes on, which the first property alone would happily pass.
3. **No attention path carries a query string.** This one is a finding
   turned into a guard: the generic list builds its filter bar from the
   schema's `views.filter_fields` and reads NOTHING from the URL, so
   `?status=extracted` narrowed nothing and landed on the whole
   collection while claiming to be a queue. Until a query param actually
   filters something, a path that carries one is a promise the page
   cannot keep, and an honest broader list beats a silent no-op.
"""

import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

import object_site_routes

PACKAGES = Path(__file__).resolve().parents[1] / "packages"


def _manifests():
    return {path.parent.name: json.loads(path.read_text())
            for path in sorted(PACKAGES.glob("*/dbbasic-package.json"))}


def _seed_rows(package_id, relative_path):
    with open(PACKAGES / package_id / relative_path, newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _seeded(collection):
    """Every row any package seeds into one shared collection.

    Routing is a whole-box property: app-returns' count is allowed to
    lean on app-shipping's route and app-intake's on its own, so the
    union is the only honest thing to resolve against.
    """
    rows = []
    for package_id, manifest in _manifests().items():
        for entry in manifest.get("seed") or []:
            if entry.get("collection") == collection:
                rows.extend(_seed_rows(package_id, entry["path"]))
    return rows


def _site_objects():
    """Object ids that would satisfy convention routing (/about ->
    site_about), which resolves BEFORE site_routes records do."""
    return {entry["id"]
            for manifest in _manifests().values()
            for entry in manifest.get("objects") or []
            if entry["id"].startswith("site_")}


def _attention_sources():
    return [(package_id, entry)
            for package_id, manifest in _manifests().items()
            for entry in manifest.get("attention") or []]


def _resolve(path, routes, site_objects):
    """What would actually answer this path, or None. Mirrors
    object_server's own order: convention first, seeded routes second."""
    convention = object_site_routes.convention_object_id(path)
    if convention and convention in site_objects:
        return convention
    match = object_site_routes.match_records(path, routes)
    return match[0] if match else None


# --- the properties ----------------------------------------------------------

def test_every_attention_path_is_a_path_some_package_actually_serves():
    """The one that stops the gap coming back."""
    routes = _seeded("site_routes")
    site_objects = _site_objects()

    dead = []
    for package_id, entry in _attention_sources():
        path = urlsplit(entry["path"]).path
        if not _resolve(path, routes, site_objects):
            dead.append(f"{package_id}:{entry['id']} -> {entry['path']}")
    assert not dead, (
        f"these counts link to URLs nothing serves: {dead}. Seed a view + a "
        "site_routes row for the collection, or point the count at a page "
        "that exists -- a count nobody can open is a notification.")


def test_a_count_that_links_to_the_generic_renderer_has_a_view_behind_it():
    """site_view_render resolves a capture-less path by matching it against
    the `route` field of a `views` record. A seeded route with no seeded
    view is a 404 wearing a 200: the page loads and says "View not
    found", so the previous test passes and the human still cannot get
    at their queue."""
    routes = _seeded("site_routes")
    site_objects = _site_objects()
    view_routes = {row["route"] for row in _seeded("views") if row.get("route")}

    missing = []
    for package_id, entry in _attention_sources():
        path = urlsplit(entry["path"]).path
        if _resolve(path, routes, site_objects) != "site_view_render":
            continue
        if path not in view_routes:
            missing.append(f"{package_id}:{entry['id']} -> {path}")
    assert not missing, (
        f"these counts route to site_view_render with no matching views "
        f"record: {missing}. The views row's `route` field is what the "
        "renderer matches the request path against.")


def test_no_attention_path_pretends_to_filter_through_the_query_string():
    """The generic list takes its filters from the schema's
    `views.filter_fields`, rendered as a bar the reader picks from, and
    reads nothing at all from the URL. A path carrying ?status=submitted
    therefore lands on the entire collection while the badge beside it
    says nine -- worse than an honest index, because the reader has no
    way to tell the list is not the list they were promised.

    Delete this test the day a list block resolves a query param into
    its `where`. Until then it is the thing that keeps a plausible,
    silently broken link out of the manifests.
    """
    lying = [f"{package_id}:{entry['id']} -> {entry['path']}"
             for package_id, entry in _attention_sources()
             if urlsplit(entry["path"]).query]
    assert not lying, (
        f"these counts carry a query string nothing honours: {lying}. Point "
        "the count at the unfiltered index; the schema's filter_fields put "
        "the status filter one click away on the page itself.")


def test_a_count_bound_to_a_door_names_a_door_that_exists():
    """`nav_id` is how a count gets onto its app tile, and it is also
    what narrows the count to that door's audience. A nav_id naming
    nothing does not fail loudly -- the count simply never decorates
    anything -- so it is checked here rather than discovered later."""
    declared = {entry["id"]
                for manifest in _manifests().values()
                for entry in manifest.get("nav") or []}

    unknown = [f"{package_id}:{entry['id']} -> {entry['nav_id']}"
               for package_id, entry in _attention_sources()
               if entry.get("nav_id") and entry["nav_id"] not in declared]
    assert not unknown, f"these counts decorate a door nobody declares: {unknown}"


def test_the_four_queues_this_slice_was_about_now_have_their_own_index():
    """A named regression guard beside the sweep, because the sweep would
    also go green if somebody pointed all four counts back at /orders.

    Each pair is (attention source, the collection its provider actually
    counts). The list the page shows has to be the collection the count
    counted; a badge that disagrees with the page it opens is worse than
    no badge, and app-returns is the case that makes the point -- its
    count reads `shipments`, not its own `return_authorizations`, so its
    index is over shipments too.
    """
    expected = {
        "scans_to_confirm": ("/scans", "scans"),
        "time_to_approve": ("/time-logs", "time_logs"),
        "expenses_to_approve": ("/expenses", "expenses"),
        "returns_to_disposition": ("/returns", "shipments"),
    }
    by_route = {row["route"]: row for row in _seeded("views") if row.get("route")}
    sources = {entry["id"]: entry for _package, entry in _attention_sources()}

    for source_id, (path, collection) in expected.items():
        entry = sources[source_id]
        assert entry["path"] == path, source_id
        blocks = json.loads(by_route[path]["blocks"])
        listed = [block for block in blocks if block["kind"] == "list"]
        assert len(listed) == 1, source_id
        assert listed[0]["collection"] == collection, source_id
