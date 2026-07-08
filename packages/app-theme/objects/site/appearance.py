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
<p class="muted" style="margin-top:1.5rem;font-size:0.8rem">A theme is a set of values for the
design system's token roles. Themes also install as packages — see docs/design-system.md.</p>
</div>
<script>const ADMIN = {"true" if is_admin else "false"};{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
