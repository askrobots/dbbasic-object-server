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

MIDNIGHT = {
    "color-scheme": "dark",
    "bg": "#0b0b10",
    "panel": "#17171f",
    "panel-2": "#1f1f2b",
    "line": "#2b2b37",
    "text": "#f4f4f7",
    "muted": "#a2a2ad",
    "accent": "#5aa7ff",
    "accent-strong": "#7ab6ff",
    "accent-ink": "#0b0b10",
    "positive": "#52d273",
    "warning": "#f1b747",
    "danger": "#ff6b6b",
    "focus": "#5aa7ff55",
}

THEMES = {"base": BASE, "midnight": MIDNIGHT, "paper": PAPER, "terminal": TERMINAL}

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

/* Page head + breadcrumb */
.breadcrumb { color: var(--muted); font-size: 0.85rem; margin-bottom: 0.6rem; }
.breadcrumb a { color: var(--accent-strong); }
.pagehead { display: flex; align-items: center; gap: 1rem; margin-bottom: 1rem; }
.pagehead h1 { margin: 0; font-size: 1.5rem; flex: 1; }

/* Rich list rows (the shared window.dbbasicList renderer) */
.listrow { display: flex; gap: 0.8rem; align-items: flex-start; background: var(--panel);
           border: 1px solid var(--line); border-radius: var(--radius-md);
           padding: 0.85rem 1rem; margin-bottom: 0.6rem; }
.listrow .av { width: 2rem; height: 2rem; flex: none; border-radius: var(--radius-sm);
               background: var(--panel-2); color: var(--muted); display: flex; align-items: center;
               justify-content: center; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
.listrow .body { flex: 1; min-width: 0; }
.listrow .rowtitle { font-weight: 600; word-break: break-word; }
.listrow .rowtitle a { color: var(--accent-strong); }
.listrow .rowsub { color: var(--muted); font-size: 0.82rem; word-break: break-all; margin-top: 0.1rem; }
.listrow .rowmeta { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; margin-top: 0.45rem; }
.listrow .rowmeta .when { color: var(--muted); font-size: 0.75rem; }
.pill { border: 1px solid var(--line); background: var(--panel-2); color: var(--muted);
        border-radius: 999px; padding: 0.05rem 0.55rem; font-size: 0.72rem; white-space: nowrap; }
.listrow .rowactions { display: flex; gap: 0.3rem; flex: none; }
.rowbtn { border: 1px solid var(--line); background: var(--panel-2); color: var(--muted);
          border-radius: var(--radius-sm); padding: 0.25rem 0.5rem; font: inherit; cursor: pointer; line-height: 1; }
.rowbtn:hover { border-color: var(--accent); color: var(--text); }
.rowbtn.danger:hover { border-color: var(--danger); color: var(--danger); }
.listmore { margin-top: 0.5rem; border: 1px solid var(--line); background: var(--panel-2);
            color: var(--muted); border-radius: var(--radius-sm); padding: 0.4rem 0.8rem;
            font: inherit; cursor: pointer; width: 100%; }
.listmore:hover { border-color: var(--accent); color: var(--text); }
/* table list_mode: a dense, sortable, live table over list_fields. */
.dtablewrap { overflow-x: auto; max-width: 100%; border: 1px solid var(--line);
              border-radius: var(--radius-md); background: var(--panel); }
.dtable { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.dtable th { text-align: left; padding: 0.55rem 0.7rem; border-bottom: 2px solid var(--line);
             color: var(--muted); font-weight: 600; white-space: nowrap; cursor: pointer;
             user-select: none; position: sticky; top: 0; background: var(--panel); }
.dtable th:hover { color: var(--text); }
.dtable td { padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--line); vertical-align: top; }
.dtable tbody tr:last-child td { border-bottom: none; }
.dtable tbody tr.clickrow { cursor: pointer; }
.dtable tbody tr.clickrow:hover { background: var(--panel-2); }

/* 60: board (kanban / lead-pipeline), tree (self-relation nesting), and
   calendar (month grid) -- the three schema-driven list_mode renderers in
   window.dbbasicList (list.py). */
.board { display: flex; gap: var(--gap); align-items: flex-start; overflow-x: auto; padding-bottom: 0.25rem; }
.boardcol { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
            flex: 1 1 200px; min-width: 200px; max-width: 340px; padding: 0.6rem; }
.boardcolhead { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
                font-weight: 600; font-size: 0.85rem; margin-bottom: 0.5rem; color: var(--muted); }
.boardcolcount { background: var(--panel-2); border-radius: 999px; padding: 0.05rem 0.5rem; font-size: 0.72rem; }
.boardcolbody { display: grid; gap: 0.5rem; min-height: 2rem; }
.boardcard { background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius-sm);
             padding: 0.6rem 0.7rem; cursor: grab; word-break: break-word; }
.boardcard:active { cursor: grabbing; }
.boardcardtitle { font-weight: 600; font-size: 0.88rem; }
.boardcardfield { color: var(--muted); font-size: 0.78rem; margin-top: 0.2rem; }

.tree { display: grid; gap: 0.2rem; }
.treenode { }
.treerow { display: flex; align-items: center; gap: 0.4rem; padding: 0.35rem 0.2rem;
           padding-left: calc(var(--depth, 0) * 1.25rem); }
.treetoggle { background: transparent; border: 0; color: var(--muted); cursor: pointer; font: inherit;
              width: 1.2rem; text-align: center; }
.treeleaf { display: inline-block; width: 1.2rem; }
.treetitle { font-weight: 500; }
.treekids.collapsed { display: none; }

.calheader { display: flex; align-items: center; justify-content: center; gap: 1rem; margin-bottom: 0.6rem; }
.calmonth { font-weight: 600; min-width: 10rem; text-align: center; }
.calundated { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
              padding: 0.5rem 0.7rem; margin-bottom: 0.6rem; }
.calundatedlabel { display: block; color: var(--muted); font-size: 0.75rem; margin-bottom: 0.3rem; }
.calgrid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; background: var(--line);
           border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden; }
.calcell { background: var(--panel); min-height: 5.5rem; padding: 0.3rem; font-size: 0.78rem; }
.calcell.dim { background: var(--bg); color: var(--muted); }
.caldate { font-size: 0.75rem; color: var(--muted); margin-bottom: 0.2rem; }
.calevent { background: var(--panel-2); border-radius: 4px; padding: 0.1rem 0.35rem; margin-top: 0.15rem;
            font-size: 0.72rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.state.notice { border-color: var(--warning); color: var(--warning); margin-bottom: var(--gap); }

/* Generated form (window.dbbasicForm) */
.genform { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
           padding: var(--pad); }
.formactions { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.5rem; }
.switch { display: flex; align-items: center; gap: 0.5rem; color: var(--muted); font-size: 0.9rem; }
.switch input { width: auto; }

/* Generated detail (window.dbbasicForm.readOnly / /detail, 59) — global
   so a detail block renders correctly wherever it's mounted, not just
   inside view_render's own page shell. */
.detailcard { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-md);
              padding: 0.25rem 1rem; }
.detailrow { display: grid; grid-template-columns: 10rem 1fr; gap: 0.5rem; padding: 0.5rem 0;
             border-bottom: 1px solid var(--line); }
.detailrow:last-child { border-bottom: 0; }
.detaillabel { color: var(--muted); font-size: 0.82rem; }
.detailvaluewrap, .detailvalue { word-break: break-word; }
.detailvalue.empty { color: var(--muted); }
/* Money fields (integer cents rendered in whole units by /form's read-only
   renderer) -- tabular figures so columns of amounts line up. */
.detailvalue.money { font-variant-numeric: tabular-nums; }
/* Owner-aware Edit/Delete affordances on a detail block (59 Stage-6
   extension) -- shown only to the record's owner by window.dbbasicDetail. */
.detailtools { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
.detailtools:empty { display: none; }

/* App shell / navigation bar (injected by /nav) */
body.has-appbar { padding-top: 3rem; }
.appbar { position: fixed; top: 0; left: 0; right: 0; height: 3rem; display: flex; align-items: center;
          gap: 0.6rem; padding: 0 0.9rem; background: var(--panel); border-bottom: 1px solid var(--line);
          z-index: 50; font: 14px/1.4 var(--font-ui); }
.appbar .brand { font-weight: 700; color: var(--text); padding: 0.2rem 0.3rem; }
.appbar .brand:hover { text-decoration: none; color: var(--accent-strong); }
.appbar .search { flex: 1; max-width: 480px; position: relative; }
.appbar .search input { padding-right: 2.6rem; height: 2rem; }
.appbar .search .kbd { position: absolute; right: 0.45rem; top: 50%; transform: translateY(-50%);
  color: var(--muted); font-size: 0.7rem; border: 1px solid var(--line); border-radius: 4px; padding: 0 0.3rem;
  pointer-events: none; }
.appbar .spacer { flex: 1; }
.appbar .navbtn { background: transparent; border: 1px solid transparent; color: var(--muted); cursor: pointer;
  border-radius: var(--radius-sm); padding: 0.3rem 0.55rem; position: relative; font: inherit; white-space: nowrap; }
.appbar .navbtn:hover { border-color: var(--line); color: var(--text); }
.appbar .navbtn.accent { color: var(--accent-strong); }
.appbar .count { position: absolute; top: -3px; right: -3px; background: var(--accent); color: var(--accent-ink);
  border-radius: 999px; font-size: 0.6rem; line-height: 1; padding: 0.12rem 0.28rem; }
.navmenu { position: fixed; top: 2.7rem; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius-md); box-shadow: var(--shadow); min-width: 190px; z-index: 60; display: none;
  overflow: hidden; }
.navmenu.open { display: block; }
.navmenu a, .navmenu .item { display: block; padding: 0.5rem 0.8rem; color: var(--text); font-size: 0.85rem;
  cursor: pointer; background: none; border: 0; width: 100%; text-align: left; font: inherit; }
.navmenu a:hover, .navmenu .item:hover { background: var(--panel-2); text-decoration: none; }
.navmenu .head { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; padding: 0.5rem 0.8rem 0.2rem; }
.navmenu.results { width: 480px; max-width: 92vw; max-height: 62vh; overflow-y: auto; }
.navmenu .hit .sub { color: var(--muted); font-size: 0.75rem; }
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
        previews = {
            name: {key: theme.get(key, "") for key in ("bg", "panel", "accent", "text")}
            for name, theme in THEMES.items()
        }
        body = json.dumps(
            {
                "status": "ok",
                "active": _active(),
                "available": sorted(THEMES),
                "previews": previews,
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
