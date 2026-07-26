"""The front door: the app switchboard, folded over the nav registry.

Reference implementation for the design system — no inline CSS of its
own; it links to the shared /style stylesheet (the site_style object) and
uses its shared classes. Retheme the whole instance by switching the
theme on /style; this page follows automatically.

This page used to BE a registry: a hardcoded `_APPS` tuple, one of three
hand-maintained lists of the same apps (the others being site_nav's JS
array and its search-hit URL map), which had drifted to 21 entries
against the switcher's 25 and named none of the newest apps. It is now a
fold over `action_nav_entries`, which reads the `nav_entries` collection
that every package writes on install. Shipping an app puts it on this
page; nobody edits this file to add a door.

`_APPS` survives underneath as the fallback, deliberately. A fresh
install renders this page before app-nav's own entries have landed, and a
blank switchboard is a worse first impression than a slightly stale one.
The fallback is what the box looked like when the registry was invented;
it is not maintained, and the comment on it says so.

Above the grid sits the NEEDS YOU band: every queue an installed package
says is waiting for a person, loudest first, each one a link straight
through to the list it counts. That is the only question a home screen is
actually asked -- does anything need me? -- and this server can answer it
because it is built out of gates and derived states that produce those
queues constantly and have always thrown them away.

**When everything is zero the band is absent entirely.** Not "0
pending", not an empty panel with a heading: gone. A board that says zero
every day trains people to stop reading it, and once they have stopped
they do not start again on the day it says three. A page that is empty
when nothing needs them gets read every time, and "nothing needs you" is
a complete and welcome answer that a blank space gives better than any
sentence would.

The counts are a rollup (`attention_counts`, written by the daemon), not
a live fold. Folding a dozen collections on every render is how a home
page becomes the slowest page -- and worse: this box went to 675MB
resident and swapped because something folded a big collection on a
timer. The page's cost is one small read no matter how many apps declare
a queue, and the age of the numbers is printed rather than pretended
away.
"""

from datetime import datetime, timezone

import object_execution
import python_object_runtime

# Fallback only -- see the module docstring. Shown when the nav registry
# is empty (a fresh box, or app-nav installed before anything else), and
# deliberately NOT kept up to date: the registry is the list now, and a
# second maintained list is the exact failure this page was rebuilt to
# end. Grouped like the registry so the two render identically.
_APPS = [
    ("Work", "/shell", "Shell", "Talk to everything; AI with your tools"),
    ("Work", "/notes", "Notes", "Quick capture, projects, public sharing"),
    ("Work", "/tasks", "Tasks", "Lifecycle with enforced transitions"),
    ("Work", "/projects", "Projects", "The hub everything links to"),
    ("Work", "/contacts", "Contacts", "People, organizations, interactions"),
    ("Work", "/links", "Links", "Saved bookmarks with tags"),
    ("Work", "/calendar", "Calendar", "Events, meetings, and gatherings"),
    ("Work", "/files", "Files", "Upload, share, and download"),
    ("Work", "/templates", "Templates", "Structured data templates"),
    ("Work", "/inbox", "Inbox", "Your private mailbox"),
    ("Work", "/activity", "Activity", "Everything you did, newest first"),
    ("Publishing", "/articles", "Articles", "Published writing with permalinks"),
    ("Publishing", "/forum", "Forum", "Discussion, threaded replies"),
    ("Publishing", "/profile/edit", "Profile", "Your public creator profile"),
    ("Money", "/invoices", "Invoices", "Bill customers; integer-cent totals"),
    ("Money", "/journals", "Finance", "Double-entry books and trial balance"),
    ("Commerce", "/products", "Products", "Things you sell; prices in cents"),
    ("Commerce", "/orders", "Orders", "Sales and purchase orders"),
    ("Warehouse", "/stock", "Stock", "On-hand levels, derived from moves"),
    ("System", "/dashboard", "Dashboard", "Live server health and activity"),
    ("System", "/appearance", "Appearance", "Theme and design system"),
]


def _escape(value):
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Severity, in the design system's own vocabulary. `warning` is work
# that is late and `danger` is something broken -- the stylesheet already
# owns what those colours mean, so this maps onto it rather than
# inventing a third palette nobody else uses.
_SEVERITY_CLASS = {"urgent": "badge danger", "warning": "badge warning",
                   "normal": "badge accent"}


def _badge(entry):
    """A tile's count, or nothing.

    `action_nav_entries` only attaches `count` when it is non-zero, so the
    absence of the key IS the empty queue -- this never has to decide
    whether a zero is worth rendering, because a zero never arrives.
    """
    count = entry.get("count")
    if not count:
        return ""
    css = _SEVERITY_CLASS.get(str(entry.get("severity") or ""), "badge accent")
    return f' <span class="{css}">{_escape(count)}</span>'


def _call(object_id, payload):
    """Run a sibling object in-process.

    The page owns no visibility rules of its own: it asks the object that
    does. Which tier of door this caller may see is decided once, in
    action_nav_entries, and never restated here.
    """
    runtime = python_object_runtime.PythonObjectRuntime()
    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest(
            object_id, method="POST", payload=payload))
    return result.result if result.ok else {}


def _fallback_groups():
    groups = []
    for group, path, label, blurb in _APPS:
        if not groups or groups[-1]["group"] != group:
            groups.append({"group": group, "entries": []})
        groups[-1]["entries"].append({"label": label, "path": path, "blurb": blurb})
    return groups


def _menu(request):
    """The registry's answer: the doors, and what needs a human.

    One call for both, because both are folds over the same visibility
    rule and asking twice would be asking the same question two ways.
    """
    answer = _call("action_nav_entries", {"_identity": request.get("_identity") or {}})
    if not isinstance(answer, dict):
        answer = {}
    groups = answer.get("groups")
    attention = answer.get("attention") or []
    if groups:
        return groups, attention, False
    return _fallback_groups(), attention, True


def _age(stamp):
    """"as of 6 minutes ago", or "" when the stamp says nothing.

    The staleness is stated rather than hidden. A number a page implies is
    live, and is not, is worse than the same number honestly dated.
    """
    text = str(stamp or "").strip().replace("Z", "+00:00")
    if not text:
        return ""
    try:
        computed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    minutes = int((datetime.now(timezone.utc) - computed).total_seconds() // 60)
    if minutes < 1:
        return "as of just now"
    if minutes < 60:
        return f"as of {minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"as of {hours} hour{'s' if hours != 1 else ''} ago"
    return f"as of {hours // 24} days ago"


def _attention_band(attention):
    """The band, or nothing at all.

    Returning the empty string when there is nothing waiting is the whole
    design: no heading, no panel, no zero. The caller concatenates, so
    "absent" costs exactly one branch and cannot decay into "present but
    empty" the way a component with an empty state does.
    """
    if not attention:
        return ""
    rows = "\n".join(
        f'<a class="tile" href="{_escape(row.get("path"))}">'
        f'<div class="name">{_escape(row.get("label"))}{_badge(row)}</div>'
        f'<div class="desc">{_escape(row.get("detail"))}</div></a>'
        for row in attention
    )
    freshest = max((str(row.get("computed_at") or "") for row in attention),
                   default="")
    age = _age(freshest)
    stamp = f' <span class="muted">{_escape(age)}</span>' if age else ""
    return (f"<h2>Needs you{stamp}</h2>\n<div class=\"grid\">\n{rows}\n</div>")


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")

    groups, attention, fell_back = _menu(request)
    _logger.info(
        "site_home served",
        user_id=user_id or "anonymous",
        groups=len(groups),
        attention=len(attention),
        source="fallback" if fell_back else "nav_entries",
    )

    who = (
        f"signed in as <strong>{_escape(user_id)}</strong>"
        if user_id
        else '<a href="/login">sign in</a>'
    )
    # The same number twice, deliberately: once in the band, where it is
    # the thing you came for, and once as a badge on the tile, where it
    # catches the person who was on their way somewhere else. A tile with
    # no queue gets no badge -- not a zero.
    sections = "\n".join(
        f'<h2>{_escape(group["group"])}</h2>\n<div class="grid">\n'
        + "\n".join(
            f'<a class="tile" href="{_escape(entry["path"])}">'
            f'<div class="name">{_escape(entry["label"])}{_badge(entry)}</div>'
            f'<div class="desc">{_escape(entry.get("blurb"))}</div></a>'
            for entry in group["entries"]
        )
        + "\n</div>"
        for group in groups
    )
    band = _attention_band(attention)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBBASIC Object Server</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap">
<header class="app"><h1>DBBASIC Object Server</h1><div class="who">{who}</div></header>
<p class="muted">Apps are packages: a schema, permission rules, and one page object each.
Everything here is served by a single small server, live-editable, permission-checked per record.
This page is a fold over the nav registry &mdash; installing an app puts it here.</p>
{band}
{sections}
<footer class="app">Open source:
<a href="https://github.com/askrobots/dbbasic-object-server">dbbasic-object-server</a>
&middot; operator console: <a href="https://github.com/askrobots/dbbasic-scroll">dbbasic-scroll</a></footer>
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
