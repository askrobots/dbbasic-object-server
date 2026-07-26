"""Staging home: the app switchboard.

Reference implementation for the design system — no inline CSS of its
own; it links to the shared /style stylesheet (the site_style object) and
uses its shared classes. Retheme the whole instance by switching the
theme on /style; this page follows automatically.
"""

_APPS = [
    ("/shell", "Shell", "Talk to everything; AI with your tools"),
    ("/notes", "Notes", "Quick capture, projects, public sharing"),
    ("/tasks", "Tasks", "Lifecycle with enforced transitions"),
    ("/projects", "Projects", "The hub everything links to"),
    ("/contacts", "Contacts", "People, organizations, interactions"),
    ("/articles", "Articles", "Published writing with permalinks"),
    ("/links", "Links", "Saved bookmarks with tags"),
    ("/calendar", "Calendar", "Events, meetings, and gatherings"),
    ("/files", "Files", "Upload, share, and download"),
    ("/templates", "Templates", "Structured data templates"),
    ("/inbox", "Inbox", "Your private mailbox"),
    ("/profile/edit", "Profile", "Your public creator profile"),
    ("/forum", "Forum", "Discussion, threaded replies"),
    ("/activity", "Activity", "Everything you did, newest first"),
    ("/stock", "Stock", "On-hand levels, derived from moves"),
    ("/orders", "Orders", "Sales and purchase orders"),
    ("/products", "Products", "Things you sell; prices in cents"),
    ("/invoices", "Invoices", "Bill customers; integer-cent totals"),
    ("/journals", "Finance", "Double-entry books and trial balance"),
    ("/dashboard", "Dashboard", "Live server health and activity"),
    ("/appearance", "Appearance", "Theme and design system"),
]


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_home served", user_id=user_id or "anonymous")

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login">sign in</a>'
    )
    tiles = "\n".join(
        f'<a class="tile" href="{path}"><div class="name">{name}</div>'
        f'<div class="desc">{desc}</div></a>'
        for path, name, desc in _APPS
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
Everything here is served by a single small server, live-editable, permission-checked per record.</p>
<div class="grid">
{tiles}
</div>
<footer class="app">Open source:
<a href="https://github.com/askrobots/dbbasic-object-server">dbbasic-object-server</a>
&middot; operator console: <a href="https://github.com/askrobots/dbbasic-scroll">dbbasic-scroll</a></footer>
</div>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
