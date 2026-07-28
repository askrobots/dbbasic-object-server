"""Appearance: the theme chooser.

Reads the available themes from /style?info=true and, for an admin,
switches the instance theme with POST /style {theme}. The change is
instance-wide and reskins every page that links /style — which is all of
them. Non-admins see the current theme read-only.
"""

# Page-unique: the swatch preview grid. Colors come from the fetched
# theme previews (inline styles), everything else from /style tokens.
_STYLE = """
.themes { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
          margin-top: var(--gap); }
.theme { border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden;
         background: var(--panel); cursor: pointer; text-align: left; padding: 0; font: inherit; color: inherit; }
.theme[aria-disabled="true"] { cursor: default; }
.theme.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent); }
.theme .swatch { display: flex; height: 64px; }
.theme .swatch span { flex: 1; }
.theme .label { padding: 0.55rem 0.75rem; display: flex; align-items: center; gap: 0.5rem; }
.theme .label .dot { width: 0.8rem; height: 0.8rem; border-radius: 999px; border: 1px solid var(--line); }
.theme .label .on { margin-left: auto; color: var(--accent); font-size: 0.75rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const grid = document.getElementById("themes");

function card(name, p, active) {
  const swatch = [p.bg, p.panel, p.accent, p.text]
    .map((c) => `<span style="background:${esc(c)}"></span>`).join("");
  return `<button class="theme${active ? " active" : ""}" data-theme="${esc(name)}"` +
         `${ADMIN ? "" : ' aria-disabled="true"'}>` +
         `<div class="swatch">${swatch}</div>` +
         `<div class="label"><span class="dot" style="background:${esc(p.accent)}"></span>` +
         `${esc(name)}${active ? '<span class="on">active</span>' : ""}</div></button>`;
}

async function load() {
  const res = await fetch("/style?info=true", {credentials: "same-origin",
                          headers: {accept: "application/json"}});
  const info = await res.json();
  grid.innerHTML = info.available
    .map((n) => card(n, (info.previews || {})[n] || {}, n === info.active)).join("");
}

grid.addEventListener("click", async (event) => {
  const btn = event.target.closest("button.theme");
  if (!btn || !ADMIN) return;
  const res = await fetch("/style", {
    method: "POST", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({theme: btn.dataset.theme}),
  });
  const body = await res.json();
  document.getElementById("msg").textContent =
    res.ok ? `Theme set to ${body.active}. Reloading…` : (body.error || "Failed");
  if (res.ok) setTimeout(() => location.reload(), 500);
});
load();
"""


_TZ_SCRIPT = r"""
// The timezone picker. The pref is display.timezone in user_prefs and has
// been writable over PUT /prefs/... since the formatter shipped -- it just
// had nowhere a person would ever find it, which is the same as not
// existing. Blank means "use the browser", which stays the default because
// it is right more often and requires nobody to decide anything.
(function () {
  const sel = document.getElementById("tz");
  const msg = document.getElementById("tzmsg");
  if (!sel) return;
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "unknown";
  const label = document.getElementById("browsertz");
  if (label) label.textContent = browserTz;

  // Intl.supportedValuesOf is the standard list where it exists; the
  // fallback is short and deliberate rather than a bundled tz database --
  // anybody needing an exotic zone can PUT the pref directly, and shipping
  // 400 names to render a dropdown nobody scrolls is not worth the bytes.
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch (e) {
    zones = ["UTC", "America/New_York", "America/Chicago", "America/Denver",
             "America/Los_Angeles", "Europe/London", "Europe/Paris",
             "Europe/Berlin", "Asia/Tokyo", "Asia/Kolkata", "Australia/Sydney"];
  }
  if (browserTz && zones.indexOf(browserTz) < 0) zones.unshift(browserTz);
  for (const z of zones) {
    const o = document.createElement("option");
    o.value = z; o.textContent = z;
    sel.appendChild(o);
  }

  fetch("/prefs", {credentials: "same-origin"})
    .then((r) => (r.ok ? r.json() : null))
    .then((p) => { sel.value = ((p && p.prefs) || {})["display.timezone"] || ""; })
    .catch(() => {});

  sel.addEventListener("change", () => {
    msg.textContent = "saving…";
    fetch("/prefs/display.timezone", {
      method: "PUT", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({value: sel.value}),
    }).then((r) => {
      if (!r.ok) throw new Error(r.status);
      msg.textContent = sel.value
        ? "saved — times now show in " + sel.value
        : "saved — using your browser's zone";
      // Re-render what is already on screen rather than asking for a reload.
      if (window.dbbasicTime) {
        document.querySelectorAll("time[datetime]").forEach((el) => {
          delete el.dataset.localized;
        });
      }
      setTimeout(() => location.reload(), 700);
    }).catch(() => { msg.textContent = "could not save"; });
  });
})();
"""

def GET(request):
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    is_admin = "admin" in (identity.get("roles") or [])
    _logger.info("site_appearance served", user_id=user_id or "anonymous", admin=is_admin)

    note = (
        "Click a theme to reskin the whole instance. Changes are instance-wide and live."
        if is_admin
        else "This is the current instance theme. Switching requires an admin session."
    )
    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else '<a href="/login?next=/appearance">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Appearance</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><h1><a href="/">Home</a> / appearance</h1><div class="who">{who}</div></header>
<p class="muted">{note}</p>
<div class="themes" id="themes"><p class="hint">loading&hellip;</p></div>
<p class="error" id="msg"></p>

<h2 style="margin-top:2rem">Time</h2>
<p class="muted">Timestamps are stored in UTC and shown in <strong>your
browser's timezone</strong> — <span id="browsertz">detecting&hellip;</span>.
Nothing is sent to the server to work that out, and nothing about your
timezone is stored unless you choose one below.</p>
<p>
<label for="tz">Show times in</label>
<select id="tz" style="max-width:22rem"><option value="">Whatever my browser says (recommended)</option></select>
<span class="hint" id="tzmsg"></span>
</p>
<p class="muted" style="font-size:0.8rem">Pick a fixed zone only if you want
times to read the same wherever you happen to be — books kept in one place,
a rota everyone reads in the shop's own hours. Otherwise leave it on the
browser, which follows you across daylight saving and across a plane.</p>

<p class="muted" style="margin-top:1.5rem;font-size:0.8rem">A theme is a set of values for the
design system's token roles. Themes also install as packages — see docs/design-system.md.</p>
</div>
<script>const ADMIN = {"true" if is_admin else "false"};{_SCRIPT}{_TZ_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
