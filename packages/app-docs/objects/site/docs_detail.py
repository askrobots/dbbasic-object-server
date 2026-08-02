"""One guide page, GET /docs/{slug}.

The `slug` route-capture param comes from the site_routes pattern
`/docs/{slug}` (see seed/site_routes.tsv): captured params are merged into
the object's request payload under their capture-group name, the same way
`/articles/{article_id}` hands site_view_render `request["article_id"]`
(see object_site_routes.py / site-routing.md) -- so this object reads
`request["slug"]`.

The record lookup happens client-side against the same cached manifest the
sidebar renders from (window.dbbasicDocsNav, /docs-nav) rather than a
second server round trip: the manifest is already being fetched to build
the sidebar on every /docs* page load, so a per-record fetch here would be
pure duplication for records that are, at most, a few hundred KB combined.

Body markdown renders through the ONE shared renderer at /markdown
(window.dbbasicMarkdown) -- never a second implementation -- into a
`.markdownblock` container, which is the same class /style already themes
for read-only textarea content (see app-theme/style.py).

Page-unique layout only; palette, chrome, and inputs come from /style. The
sidebar/off-canvas CSS below is intentionally identical to docs_index.py's
-- see that file's docstring for why it is duplicated rather than shared
through a second styling channel.
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
<div class="docscontent">
<div class="docbody" id="docmain"><div class="state">Loading&hellip;</div></div>
<aside class="doctoc" id="doctoc"></aside>
</div>
<div class="docpager" id="docpager"></div>
</main>
</div>
"""

import re

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# `SLUG` is injected as a Python-repr'd JS string literal, same convention
# as view_render.py's VIEW_ID/RECORD_ID.
_SCRIPT = """
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  function slugify(text) {
    const base = String(text == null ? "" : text).toLowerCase().trim()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return base || "section";
  }

  // Simple flat "on this page" list -- no scroll-spy, no nesting logic
  // beyond a lighter indent for h3s. Gives every h2/h3 an id (from its
  // text) if it doesn't already have one, deduping against ids already
  // used on the page.
  function buildToc(container) {
    if (!container) return "";
    const heads = container.querySelectorAll("h2, h3");
    if (!heads.length) return "";
    const used = new Set();
    let html = '<div class="doctochead">On this page</div><ul class="doctoclist">';
    heads.forEach((heading) => {
      if (!heading.id) {
        const base = slugify(heading.textContent);
        let id = base;
        let n = 2;
        while (used.has(id) || document.getElementById(id)) { id = base + "-" + n; n++; }
        heading.id = id;
      }
      used.add(heading.id);
      const subClass = heading.tagName === "H3" ? " doctocsub" : "";
      html += '<li class="doctocitem' + subClass + '"><a href="#' + esc(heading.id) + '">'
        + esc(heading.textContent || "") + '</a></li>';
    });
    html += "</ul>";
    return html;
  }

  function pagerHtml(prev, next) {
    let html = '<div class="docpagerside">';
    if (prev) {
      html += '<a class="docpagerlink prev" href="/docs/' + esc(prev.id) + '">&larr; '
        + esc(prev.title || prev.id) + '</a>';
    }
    html += '</div><div class="docpagerside">';
    if (next) {
      html += '<a class="docpagerlink next" href="/docs/' + esc(next.id) + '">'
        + esc(next.title || next.id) + ' &rarr;</a>';
    }
    html += '</div>';
    return html;
  }

  function renderNotFound() {
    document.title = "Page not found — DBBASIC Docs";
    const main = document.getElementById("docmain");
    main.innerHTML = '<h1>Page not found</h1>'
      + '<p>There is no guide at this address.</p>'
      + '<p><a href="/docs">&larr; Back to Docs</a></p>';
    document.getElementById("doctoc").innerHTML = "";
    document.getElementById("docpager").innerHTML = "";
  }

  async function init() {
    if (!window.dbbasicDocsNav) return;
    const records = await window.dbbasicDocsNav.mount({
      headerEl: document.getElementById("docsheader"),
      navListEl: document.getElementById("docsnav"),
      searchEl: document.getElementById("docsearch"),
      drawerEl: document.getElementById("sidebar"),
      toggleEl: document.getElementById("navtoggle"),
      scrimEl: document.getElementById("scrim"),
      activeSlug: SLUG,
    });

    const index = records.findIndex((r) => r && r.id === SLUG);
    if (index === -1) { renderNotFound(); return; }
    const record = records[index];

    document.title = (record.title || SLUG) + " — DBBASIC Docs";
    const rendered = window.dbbasicMarkdown ? window.dbbasicMarkdown(record.content || "") : "";
    const main = document.getElementById("docmain");
    main.innerHTML = "<h1>" + esc(record.title || "") + '</h1><div class="markdownblock" id="docbodycontent">'
      + rendered + "</div>";

    document.getElementById("doctoc").innerHTML = buildToc(document.getElementById("docbodycontent"));

    const prev = index > 0 ? records[index - 1] : null;
    const next = index < records.length - 1 ? records[index + 1] : null;
    document.getElementById("docpager").innerHTML = pagerHtml(prev, next);
  }

  init();
})();
"""


def GET(request):
    slug = str(request.get("slug") or "").strip()
    # Same defense as view_render.py's _RECORD_ID_RE gate: `slug` is
    # URL-derived and lands inside a <script> block below, where string
    # escaping alone cannot stop a "</script>" payload from closing the
    # tag. Doc slugs are our own seed ids, so the tight charset costs
    # nothing; anything else renders as the not-found state.
    if not _SLUG_RE.fullmatch(slug):
        slug = ""
    _logger.info("site_docs_detail served", slug=slug or "missing")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DBBASIC Docs</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
{_BODY}
<script src="/markdown"></script>
<script src="/docs-nav"></script>
<script>const SLUG = {slug!r};{_SCRIPT}</script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
