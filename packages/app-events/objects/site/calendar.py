"""Calendar page over the events collection.

The collection is named events (matching a private predecessor-system audit,
not part of this repo); this page lives at /calendar because /events is the
server's built-in event-delivery API. The month-grid display is the shared
/list generator in calendar mode (events.views.list_mode == "calendar", spec
60) -- this page keeps only its custom quick-add form and hands the display to
window.dbbasicList, which reads the schema and renders the month grid.
"""

# Page-unique layout only (the quick-add form); the calendar grid + everything
# else come from the shared /style sheet.
_STYLE = """
form.capture { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
               padding: var(--pad); display: grid; gap: var(--gap); margin-bottom: var(--gap);
               grid-template-columns: 2fr 1fr 1fr; }
form.capture .btn { justify-self: start; }
form.capture .error { grid-column: 1 / -1; }
"""

_SCRIPT = """
// The month-grid display is the shared generator in calendar mode; this page
// only owns the quick-add form and asks the list to reload after a save (the
// realtime subscription would refresh it anyway, this just makes it instant).
const cal = window.dbbasicList("events", {mount: "#events-mount"});

document.getElementById("capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = event.target.elements;
  const starts = fields["starts"].value;
  const record = {id: crypto.randomUUID(), title: fields["title"].value.trim(),
                  starts_at: starts ? starts + ":00" : "",
                  location: fields["location"].value.trim(),
                  purpose: fields["purpose"].value, owner_id: OWNER_ID};
  const res = await fetch("/collections/events/records", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(record),
  });
  const body = await res.json();
  document.getElementById("form-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { event.target.reset(); if (cal && cal.reload) cal.reload(); }
});
"""


def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_calendar served", user_id=user_id or "anonymous")

    if not user_id:
        body = '<p class="hint"><a href="/login?next=/calendar">Sign in</a> to see your events.</p>'
        script = ""
    else:
        body = """
<form class="capture" id="capture-form">
<input name="title" placeholder="Event title" required maxlength="255">
<input name="starts" type="datetime-local" required>
<input name="location" placeholder="Location" maxlength="255">
<select name="purpose">
<option value="meeting">meeting</option>
<option value="workshop">workshop</option>
<option value="recreation">recreation</option>
<option value="others">others</option>
</select>
<button type="submit" class="btn primary">Add Event</button>
<div class="error" id="form-error"></div>
</form>
<div id="events-mount"><div class="state">loading&hellip;</div></div>
"""
        script = (
            '<script src="/list"></script>'
            f"<script>const OWNER_ID = {user_id!r};{_SCRIPT}</script>"
        )

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/calendar">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calendar</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
<header class="app"><h1>Calendar</h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
