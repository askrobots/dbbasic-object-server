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
"""

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


def _groups(request):
    answer = _call("action_nav_entries", {"_identity": request.get("_identity") or {}})
    groups = answer.get("groups") if isinstance(answer, dict) else None
    return (groups, False) if groups else (_fallback_groups(), True)


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")

    groups, fell_back = _groups(request)
    _logger.info(
        "site_home served",
        user_id=user_id or "anonymous",
        groups=len(groups),
        source="fallback" if fell_back else "nav_entries",
    )

    who = (
        f"signed in as <strong>{_escape(user_id)}</strong>"
        if user_id
        else '<a href="/login">sign in</a>'
    )
    sections = "\n".join(
        f'<h2>{_escape(group["group"])}</h2>\n<div class="grid">\n'
        + "\n".join(
            f'<a class="tile" href="{_escape(entry["path"])}">'
            f'<div class="name">{_escape(entry["label"])}</div>'
            f'<div class="desc">{_escape(entry.get("blurb"))}</div></a>'
            for entry in group["entries"]
        )
        + "\n</div>"
        for group in groups
    )
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
{sections}
<footer class="app">Open source:
<a href="https://github.com/askrobots/dbbasic-object-server">dbbasic-object-server</a>
&middot; operator console: <a href="https://github.com/askrobots/dbbasic-scroll">dbbasic-scroll</a></footer>
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
