"""Docs site landing page, GET /docs.

A real, public, unauthenticated documentation site in the Rails Guides /
Django docs mold: a left sidebar grouped by category with a client-side
search box (window.dbbasicDocsNav, /docs-nav), a clean content pane, and
no dependency on the internal signed-in app's chrome -- no /nav script, no
sign-in prompt, nothing that assumes a session. This landing pane itself
is deliberately small: a heading and one paragraph inviting the visitor to
pick a topic from the sidebar. The guides themselves live at
/docs/{slug} (site_docs_detail).

Page-unique layout only; palette, chrome, and inputs come from /style (see
app-theme/style.py), so this page reskins with the active theme like every
other page. The sidebar/off-canvas layout is duplicated verbatim in
docs_detail.py -- package objects cannot import each other (see
app-documents' manifest description for why that split exists), so a small
shared layout block is repeated rather than invented as a second styling
mechanism on top of /style.
"""

_STYLE = """
.docslayout { display: flex; align-items: flex-start; gap: 0; max-width: 1180px; margin: 0 auto; }
.navtoggle { display: none; }
.scrim { display: none; }

.docsidebar { width: 260px; flex: none; box-sizing: border-box; padding: 1rem 1.25rem 1rem 0;
              position: sticky; top: 0; align-self: flex-start; max-height: 100vh; overflow-y: auto; }
.docsbrand { display: flex; align-items: center; gap: 0.5rem; font-weight: 700; color: var(--text);
             margin-bottom: 1rem; }
.docsbrand:hover { text-decoration: none; color: var(--accent-strong); }
.docslogo { background: var(--accent); color: var(--accent-ink); border-radius: var(--radius-sm);
            padding: 0.05rem 0.4rem; font-size: 0.78rem; font-weight: 700; }
#docsearch { margin-bottom: 0.75rem; }
.navempty { color: var(--muted); font-size: 0.85rem; }
.navheading { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em;
              margin: 1rem 0 0.3rem; }
.navgroup:first-child .navheading { margin-top: 0; }
.navlist { list-style: none; margin: 0; padding: 0; }
.navitem a { display: block; padding: 0.3rem 0.5rem; border-radius: var(--radius-sm); color: var(--text);
             font-size: 0.88rem; }
.navitem a:hover { background: var(--panel-2); text-decoration: none; }
.navitem.active a { background: var(--accent); color: var(--accent-ink); font-weight: 600; }

.docsmain { flex: 1; min-width: 0; padding: 1.5rem 1rem 3rem 2rem; border-left: 1px solid var(--line); }
.docsmain h1 { margin-top: 0; }
.docscontent { display: flex; align-items: flex-start; gap: 2rem; }
.docbody { flex: 1; min-width: 0; }
.doctoc { width: 200px; flex: none; padding-left: 1rem; border-left: 1px solid var(--line);
          font-size: 0.82rem; position: sticky; top: 1rem; }
.doctochead { color: var(--muted); text-transform: uppercase; font-size: 0.72rem; margin-bottom: 0.4rem; }
.doctoclist { list-style: none; margin: 0; padding: 0; }
.doctocitem a { display: block; padding: 0.15rem 0; color: var(--muted); }
.doctocitem a:hover { color: var(--accent-strong); }
.doctocitem.doctocsub { padding-left: 0.75rem; }

.docpager { display: flex; justify-content: space-between; gap: 1rem; margin-top: 2rem;
            padding-top: 1rem; border-top: 1px solid var(--line); }
.docpagerside { flex: 1; display: flex; }
.docpagerlink { display: block; width: 100%; color: var(--text); border: 1px solid var(--line);
                border-radius: var(--radius-md); padding: 0.6rem 0.8rem; font-size: 0.85rem; }
.docpagerlink:hover { border-color: var(--accent); text-decoration: none; }
.docpagerlink.next { text-align: right; }

@media (max-width: 900px) {
  .doctoc { display: none; }
}

@media (max-width: 720px) {
  .navtoggle { display: inline-flex; align-items: center; gap: 0.4rem; margin: 0.75rem 0 0 0.75rem;
               background: var(--panel); border: 1px solid var(--line); color: var(--text);
               border-radius: var(--radius-sm); padding: 0.4rem 0.7rem; font: inherit; cursor: pointer; }
  .docslayout { display: block; }
  .docsidebar { position: fixed; top: 0; left: 0; bottom: 0; width: 280px; max-width: 82vw;
                background: var(--panel); border-right: 1px solid var(--line); padding: 1rem;
                transform: translateX(-100%); transition: transform 160ms ease; z-index: 80; }
  .docsidebar.open { transform: translateX(0); }
  .scrim.open { display: block; position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 70; }
  .docsmain { border-left: none; padding: 1rem; }
  .docscontent { display: block; }
  .doctoc { display: none; }
}
"""

_BODY = """
<button type="button" id="navtoggle" class="navtoggle" aria-label="Toggle documentation navigation" aria-expanded="false">&#9776; Menu</button>
<div id="scrim" class="scrim"></div>
<div class="docslayout">
<aside id="sidebar" class="docsidebar">
<div id="docsheader"></div>
<input id="docsearch" type="search" class="search" placeholder="Search docs&hellip;" aria-label="Search documentation">
<div id="docsnav"><div class="navempty">Loading&hellip;</div></div>
</aside>
<main class="docsmain">
<h1>DBBASIC Object Server Documentation</h1>
<p>Guides for building on the DBBASIC Object Server &mdash; packages, objects, routing, permissions,
and everything else that turns a directory of source into a running app. Pick a topic from the
sidebar to get started.</p>
</main>
</div>
"""

_SCRIPT = """
(function () {
  document.title = "DBBASIC Object Server Documentation";
  if (!window.dbbasicDocsNav) return;
  window.dbbasicDocsNav.mount({
    headerEl: document.getElementById("docsheader"),
    navListEl: document.getElementById("docsnav"),
    searchEl: document.getElementById("docsearch"),
    drawerEl: document.getElementById("sidebar"),
    toggleEl: document.getElementById("navtoggle"),
    scrimEl: document.getElementById("scrim"),
    activeSlug: null,
  });
})();
"""


def GET(request):
    _logger.info("site_docs_index served")
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBBASIC Object Server Documentation</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
{_BODY}
<script src="/markdown"></script>
<script src="/docs-nav"></script>
<script>{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
