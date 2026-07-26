"""action_nav_entries -- the doors this caller may be shown, grouped.

POST/GET {} -> {ok, groups: [{group, entries: [...]}], count}

Every navigation surface on this server is meant to be a FOLD over one
registry rather than a list somebody maintains: the app switcher in
site_nav, the tile grid on site_home, and anything built next. This is
that fold, and it lives in an object rather than in each surface because
the visibility rule -- who is allowed to SEE a door -- is a rule, and a
rule copied into two renderers is a rule that will be enforced two
different ways within a year.

Three tiers, decided here once:

- `public` entries are returned to anyone, signed in or not.
- `member` entries need a signed-in identity.
- `operator` entries need an admin role, the same check system_scheduler
  makes before it will draw the task board.

Two kinds of row are never returned at all: `surface: hidden` (the
package registered a door it does not want rendered) and
`operator_hidden` (the operator took it off this deployment's menu). The
second is the reason they are separate columns -- the package says what
the app IS, the operator says what they want to SEE, and an install
restates the first without ever touching the second.

This is a MENU, not a gate. Permissions decide who may actually open a
page; leaving a door off the menu hides nothing that was not already
protected, and putting one on it grants nothing.
"""

import os

import object_records

ACTOR = "action_nav_entries"

_DEFAULT_GROUP = "Apps"
_DEFAULT_ORDER = 100
_TRUE = ("true", "1", "yes", "on")


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _order(row):
    try:
        return int(_text(row.get("order")) or _DEFAULT_ORDER)
    except (TypeError, ValueError):
        # A hand-edited order that will not parse sorts to the back
        # rather than taking the whole menu down with it.
        return _DEFAULT_ORDER


def _visible_surfaces(identity):
    """Which tiers this caller may be shown.

    An anonymous visitor sees the public web; any signed-in user is a
    member; an admin role additionally sees the operator surfaces. The
    admin test is `"admin" in identity["roles"]`, copied from
    system_scheduler rather than reinvented, so there is one answer on
    this server to "is this an operator?".
    """
    identity = identity or {}
    surfaces = {"public"}
    if _text(identity.get("user_id")):
        surfaces.add("member")
    if "admin" in (identity.get("roles") or []):
        surfaces.add("operator")
    return surfaces


def _entries(request):
    identity = request.get("_identity") or {}
    surfaces = _visible_surfaces(identity)
    try:
        rows = object_records.read_collection_records("nav_entries", base_dir=_base_dir())
    except Exception:
        # The registry is not installed yet. An empty menu is a real
        # answer; the callers fall back to their own shipped list.
        return []

    visible = []
    for row in rows:
        if _text(row.get("operator_hidden")).lower() in _TRUE:
            continue
        surface = _text(row.get("surface")).lower() or "member"
        if surface not in surfaces:
            continue
        path = _text(row.get("path"))
        if not path:
            continue
        visible.append({
            "id": _text(row.get("id")),
            "label": _text(row.get("label")) or path,
            "path": path,
            "blurb": _text(row.get("blurb")),
            "group": _text(row.get("group")) or _DEFAULT_GROUP,
            "surface": surface,
            "order": _order(row),
            "package": _text(row.get("package")),
        })

    # A group sorts where its earliest entry sorts. Packages band their
    # `order` values (Work in the 0s, Publishing the 200s, Money the
    # 300s, and so on), so the reading order of the menu falls out of the
    # same number that orders entries within a group -- rather than out
    # of the alphabet, which would put System before Work, or out of a
    # hardcoded group table, which would be a fourth list to maintain.
    rank = {}
    for entry in visible:
        group = entry["group"]
        rank[group] = min(rank.get(group, entry["order"]), entry["order"])
    visible.sort(key=lambda entry: (rank[entry["group"]], entry["group"],
                                    entry["order"], entry["label"]))
    return visible


def _grouped(entries):
    """Group-preserving fold over an already-sorted list.

    The sort put every group together, so grouping is one pass and the
    group order is the sort's order -- no second sort key to disagree
    with the first one.
    """
    groups = []
    for entry in entries:
        if not groups or groups[-1]["group"] != entry["group"]:
            groups.append({"group": entry["group"], "entries": []})
        groups[-1]["entries"].append(entry)
    return groups


def POST(request):
    entries = _entries(request)
    return {"ok": True, "groups": _grouped(entries), "entries": entries,
            "count": len(entries)}


def GET(request):
    # Same answer either way: reading the menu is a read. GET is what the
    # nav script fetches; POST is what a sibling object calls in-process.
    return POST(request)
