"""The design system, served as one themeable stylesheet object.

site_style is the single source of truth for DBBASIC's visual language.
It emits two things at /style:

  1. a `:root` block of semantic token ROLES (surface, text, accent,
     positive/warning/danger, spacing, type) — a theme is just a set of
     values for these roles, nothing more;
  2. the shared component primitives (layout, cards, tables, forms,
     toolbars, filters, search, buttons, badges, states) written against
     those roles, so every page and generated UI looks consistent.

Because a theme is data, changing the look never touches a page. The
active theme lives in this object's state; switching it (or installing a
theme package that sets custom tokens) reskins the whole instance on the
next request, versioned and reversible like any object.

  GET  /style              -> the active theme's CSS (public)
  GET  /style?info=true    -> {active, available, tokens} JSON (for pickers)
  POST /style {theme|tokens}  -> switch theme (admin session only)
"""

import json

# --- Themes: each is a set of values for the same token roles ----------------
# The base theme is the DBBASIC identity: a warm dark, per the earth-theme
# rule "warm, not cold blue-black", with a terracotta/ember accent.

BASE = {
    "color-scheme": "dark",
    "bg": "#17140f",
    "panel": "#201c16",
    "panel-2": "#28231b",
    "line": "#332c22",
    "text": "#ece7df",
    "muted": "#a89e90",
    "accent": "#d4956a",
    "accent-strong": "#e0a878",
    "accent-ink": "#17140f",
    "positive": "#7fa87f",
    "warning": "#d8b25e",
    "danger": "#d98668",
    "focus": "#e0a87866",
}

PAPER = {
    "color-scheme": "light",
    "bg": "#f3efe8",
    "panel": "#fbf8f2",
    "panel-2": "#f0ebe1",
    "line": "#ddd5c8",
    "text": "#33291d",
    "muted": "#6e6355",
    "accent": "#b8703f",
    "accent-strong": "#a15f31",
    "accent-ink": "#fbf8f2",
    "positive": "#4f7a52",
    "warning": "#9a7d2e",
    "danger": "#b5573a",
    "focus": "#b8703f55",
}

TERMINAL = {
    "color-scheme": "dark",
    "bg": "#08100a",
    "panel": "#0d1a10",
    "panel-2": "#12251699",
    "line": "#1d3a24",
    "text": "#c8f7d4",
    "muted": "#6fae82",
    "accent": "#3ddc84",
    "accent-strong": "#5cf29c",
    "accent-ink": "#08100a",
    "positive": "#3ddc84",
    "warning": "#e6c84f",
    "danger": "#ff6b6b",
    "focus": "#3ddc8455",
}

THEMES = {"base": BASE, "paper": PAPER, "terminal": TERMINAL}

# Shape, rhythm, and type are theme-independent for now (a theme could
# override these too by shipping custom tokens).
STRUCTURE = {
    "radius-sm": "6px",
    "radius-md": "8px",
    "radius-lg": "12px",
    "gap": "0.75rem",
    "pad": "1rem",
    "shadow": "0 1px 3px rgba(0,0,0,0.35)",
    "font-ui": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    "font-mono": "ui-monospace, SFMono-Regular, Menlo, monospace",
}

_PRIMITIVES = """
* { box-sizing: border-box; }
html { color-scheme: var(--_scheme); }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--font-ui); }
a { color: var(--accent-strong); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3 { line-height: 1.25; }

.wrap { max-width: 960px; margin: 0 auto; padding: 1.5rem; }
.wrap.narrow { max-width: 720px; }
.stack > * + * { margin-top: var(--gap); }

header.app { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.25rem; }
header.app h1 { font-size: 1.15rem; margin: 0; }
header.app .who { margin-left: auto; color: var(--muted); font-size: 0.85rem; }

/* Switchboard tiles */
.grid { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
a.tile { display: block; background: var(--panel); border: 1px solid var(--line);
         border-radius: var(--radius-lg); padding: 1rem 1.1rem; color: var(--text); }
a.tile:hover { border-color: var(--accent); text-decoration: none; }
a.tile .name { font-weight: 600; }
a.tile .desc { color: var(--muted); font-size: 0.82rem; margin-top: 0.25rem; }

/* Cards */
.cards { display: grid; gap: var(--gap); grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
        padding: 0.85rem 1rem; word-break: break-word; }
.card .title, .card .name { font-weight: 600; }
.card .meta { margin-top: 0.5rem; color: var(--muted); font-size: 0.78rem; }
.card .meta a { color: var(--accent-strong); }

/* Toolbar: search + filters above a list */
.toolbar { display: flex; gap: var(--gap); align-items: center; flex-wrap: wrap; margin-bottom: var(--gap); }
.toolbar .grow { flex: 1; min-width: 12rem; }
.filters { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.chip { border: 1px solid var(--line); background: var(--panel); color: var(--muted);
        border-radius: 999px; padding: 0.2rem 0.7rem; font-size: 0.78rem; cursor: pointer; }
.chip[aria-pressed="true"] { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }

/* Inputs */
input, textarea, select { background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; font: inherit; width: 100%; }
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--focus); }
input.search { background-image: none; }
[aria-invalid="true"], .invalid { border-color: var(--danger); }
textarea { min-height: 4rem; resize: vertical; }

/* Forms */
form.stack { display: grid; gap: var(--gap); }
.field { display: grid; gap: 0.3rem; }
.field > label { font-size: 0.85rem; color: var(--muted); }
.field .req { color: var(--danger); }
.field .help { font-size: 0.78rem; color: var(--muted); }
.field .err { font-size: 0.8rem; color: var(--danger); min-height: 1rem; }

/* Buttons — intents, not colors */
.btn { display: inline-flex; align-items: center; gap: 0.4rem; border: 1px solid var(--line);
       background: var(--panel-2); color: var(--text); border-radius: var(--radius-sm);
       padding: 0.5rem 1rem; font: inherit; font-weight: 600; cursor: pointer; }
.btn:hover { border-color: var(--accent); }
.btn.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
.btn.ghost { background: transparent; }
.btn.sm { padding: 0.25rem 0.6rem; font-size: 0.8rem; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Tables */
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden; }
th, td { text-align: left; font-size: 0.85rem; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable::after { content: " \\2195"; color: var(--muted); opacity: 0.5; }
th.sortable.asc::after { content: " \\2191"; opacity: 1; }
th.sortable.desc::after { content: " \\2193"; opacity: 1; }
tbody tr:hover { background: var(--panel-2); }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.muted, .muted { color: var(--muted); }

/* Pagination */
.pagination { display: flex; gap: 0.4rem; align-items: center; margin-top: var(--gap);
              color: var(--muted); font-size: 0.82rem; }

/* Badges */
.badge { display: inline-block; border-radius: 999px; padding: 0.05rem 0.5rem; font-size: 0.72rem;
         border: 1px solid var(--line); color: var(--muted); }
.badge.positive { color: var(--positive); border-color: var(--positive); }
.badge.warning { color: var(--warning); border-color: var(--warning); }
.badge.danger { color: var(--danger); border-color: var(--danger); }
.badge.accent { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }

/* States: empty / loading / denied / hint / alert */
.state, .hint { color: var(--muted); background: var(--panel); border: 1px solid var(--line);
                border-radius: var(--radius-md); padding: 0.9rem 1rem; }
.state.denied { color: var(--danger); }
.error { color: var(--danger); font-size: 0.85rem; min-height: 1.2rem; }
.alert { border-radius: var(--radius-md); padding: 0.7rem 1rem; border: 1px solid var(--line); }
.alert.danger { color: var(--danger); border-color: var(--danger); }
.alert.positive { color: var(--positive); border-color: var(--positive); }

footer.app { margin-top: 2.5rem; color: var(--muted); font-size: 0.78rem; }
"""


def _active():
    try:
        return _state_manager.get("active_theme", "base") or "base"
    except Exception:
        return "base"


def _custom():
    try:
        return _state_manager.get("custom_tokens") or None
    except Exception:
        return None


def _tokens():
    tokens = dict(THEMES.get(_active(), BASE))
    custom = _custom()
    if isinstance(custom, dict):
        tokens.update({k: v for k, v in custom.items() if isinstance(v, str)})
    return tokens


def _css():
    tokens = _tokens()
    scheme = tokens.get("color-scheme", "dark")
    lines = [f"  --{key}: {value};" for key, value in tokens.items() if key != "color-scheme"]
    lines += [f"  --{key}: {value};" for key, value in STRUCTURE.items()]
    lines.append(f"  --_scheme: {scheme};")
    root = ":root {\n  color-scheme: " + scheme + ";\n" + "\n".join(lines) + "\n}\n"
    return root + _PRIMITIVES


def GET(request):
    if request.get("info"):
        body = json.dumps(
            {
                "status": "ok",
                "active": _active(),
                "available": sorted(THEMES),
                "tokens": _tokens(),
                "custom": bool(_custom()),
            }
        )
        return {"content_type": "application/json", "body": body}
    return {"content_type": "text/css; charset=utf-8", "body": _css()}


def POST(request):
    identity = request.get("_identity", {})
    if "admin" not in (identity.get("roles") or []):
        return {
            "content_type": "application/json",
            "status": 403,
            "body": json.dumps({"status": "error", "error": "Theme changes require an admin session"}),
        }

    theme = request.get("theme")
    tokens = request.get("tokens")
    changed = {}
    if isinstance(theme, str) and theme in THEMES:
        _state_manager.set("active_theme", theme)
        _state_manager.set("custom_tokens", None)
        changed["active_theme"] = theme
    elif isinstance(theme, str):
        return {
            "content_type": "application/json",
            "status": 400,
            "body": json.dumps({"status": "error", "error": f"Unknown theme; available: {sorted(THEMES)}"}),
        }
    if isinstance(tokens, dict):
        clean = {k: v for k, v in tokens.items() if isinstance(k, str) and isinstance(v, str)}
        _state_manager.set("custom_tokens", clean)
        changed["custom_tokens"] = len(clean)

    if not changed:
        return {
            "content_type": "application/json",
            "status": 400,
            "body": json.dumps({"status": "error", "error": "Send a known theme name or a tokens object"}),
        }
    _logger.info("theme changed", **{k: str(v) for k, v in changed.items()})
    return {
        "content_type": "application/json",
        "body": json.dumps({"status": "ok", "active": _active(), **changed}),
    }
