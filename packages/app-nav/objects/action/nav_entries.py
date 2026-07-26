"""action_nav_entries -- the doors this caller may be shown, grouped.

POST/GET {} -> {ok, groups: [{group, entries: [...]}], attention: [...], count}

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

It also carries the ATTENTION counts, because they are the cheapest half
of a home screen and belong on a list people already look at: an entry
whose queue is non-empty comes back with `count`, `detail` and
`severity`, so the app list reads "Invoices - 5 overdue" with no second
surface to maintain. The numbers are read from `attention_counts`, the
small table the daemon's rollup writes; nothing here folds a business
collection, which is the rule that keeps this object's cost flat as
packages are installed.

**Zero contributes nothing.** A row whose count is zero is dropped
entirely rather than returned as a zero, because an empty queue is not
news, and a surface that is handed zeros will eventually render them.
"""

import os

import object_records

ACTOR = "action_nav_entries"

_DEFAULT_GROUP = "Apps"
_DEFAULT_ORDER = 100
_TRUE = ("true", "1", "yes", "on")

# Loudest first on every surface that sorts by it. Three steps, because a
# scale with more is a scale nobody calibrates.
_SEVERITY_RANK = {"urgent": 0, "warning": 1, "normal": 2}
_DEFAULT_SEVERITY = "normal"


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


def _count(row):
    try:
        return int(_text(row.get("count")) or 0)
    except (TypeError, ValueError):
        # A hand-edited count that will not parse is not a queue of
        # unknown size, it is a broken cell: it contributes nothing rather
        # than taking the whole menu down with it.
        return 0


def _attention_rows(identity):
    """Every non-zero attention count this caller is entitled to see.

    Sorted loudest first: severity, then size, then label. That order is
    decided here rather than in each renderer for the same reason the
    visibility tiers are -- a rule copied into two surfaces is a rule that
    will be enforced two different ways within a year.

    Anonymous visitors get nothing at all. These are internal work
    queues, and how many receipts are unconfirmed is a fact about how a
    business is running, not part of the public web. Rows that name a
    nav entry are additionally held to that entry's own tier, so an
    operator-only door does not leak a count to a member.
    """
    if not _text((identity or {}).get("user_id")):
        return []
    try:
        rows = object_records.read_collection_records(
            "attention_counts", base_dir=_base_dir())
    except Exception:
        # The rollup is not installed, or has never run. No counts is a
        # real answer and exactly what "nothing needs you" looks like.
        return []

    attention = []
    for row in rows:
        count = _count(row)
        if count <= 0:
            continue          # an empty queue is not news
        severity = _text(row.get("severity")).lower() or _DEFAULT_SEVERITY
        attention.append({
            "id": _text(row.get("id")),
            "label": _text(row.get("label")),
            "path": _text(row.get("path")),
            "nav_id": _text(row.get("nav_id")),
            "group": _text(row.get("group")) or _DEFAULT_GROUP,
            "severity": severity,
            "count": count,
            "detail": _text(row.get("detail")),
            "computed_at": _text(row.get("computed_at")),
            "error": _text(row.get("error")),
        })
    attention.sort(key=lambda row: (_SEVERITY_RANK.get(row["severity"], 9),
                                    -row["count"], row["label"]))
    return attention


def _decorate(entries, attention):
    """Hang each entry's loudest count on it, in place.

    One count per door, deliberately. A menu line can carry one number,
    and the full list -- including the queues whose package ships no door
    at all -- is what the home band is for. When two sources name the
    same entry the louder one wins, which is already the order
    `attention` arrives in.
    """
    by_nav = {}
    for row in attention:
        if row["nav_id"] and row["nav_id"] not in by_nav:
            by_nav[row["nav_id"]] = row
    for entry in entries:
        row = by_nav.get(entry["id"])
        if row is None:
            continue
        entry["count"] = row["count"]
        entry["detail"] = row["detail"]
        entry["severity"] = row["severity"]


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
    attention = _attention_rows(request.get("_identity") or {})
    # A count attached to a door nobody may see would leak the door. The
    # visible set is already decided above, so the filter is a membership
    # test rather than a second copy of the tier rule.
    visible_ids = {entry["id"] for entry in entries}
    attention = [row for row in attention
                 if not row["nav_id"] or row["nav_id"] in visible_ids]
    _decorate(entries, attention)
    return {"ok": True, "groups": _grouped(entries), "entries": entries,
            "attention": attention, "count": len(entries)}


def GET(request):
    # Same answer either way: reading the menu is a read. GET is what the
    # nav script fetches; POST is what a sibling object calls in-process.
    return POST(request)
