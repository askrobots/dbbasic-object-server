"""Shared docs-site chrome, served as one object at /docs-nav.

Mirrors how /markdown and /style ship one script/stylesheet used by every
page rather than each page carrying its own copy (see app-theme/markdown.py
and app-theme/nav.py). Every /docs and /docs/{slug} page includes
<script src="/docs-nav"></script> and gets `window.dbbasicDocsNav`:

  - loadManifest()      fetch the full doc_pages manifest exactly once per
                         browser tab (cached in sessionStorage), sorted by
                         Number(nav_order) then title.
  - renderHeader(el)     the public docs-site brand/logo, linking to /docs.
                         No sign-in prompt, no dependency on the internal
                         app's /nav -- this is a public site with its own
                         chrome.
  - renderSidebar(el, records, activeSlug)
                         groups records by category (first-appearance order
                         across the sorted manifest), renders a heading per
                         category and a list of pages below it, and marks
                         the current page active.
  - wireSearch(inputEl, listEl)
                         live, case-insensitive substring filter over
                         title + summary + category. No server round-trip,
                         no debounce -- the manifest tops out around three
                         dozen records.
  - wireDrawer(toggleEl, drawerEl, scrimEl)
                         collapses the sidebar into a hidden-by-default
                         off-canvas drawer on narrow viewports; the toggle
                         button and a click on the scrim both close it.
  - mount(options)       convenience wrapper that does all of the above in
                         one call and returns the sorted manifest, so a
                         page can look up its own record by id afterward.

The manifest fetch is the one genuinely expensive thing here (combined doc
content across every page is substantial, and every /docs/{slug} click is a
full page load in this architecture, not a client-side route change), so it
is cached in sessionStorage under one key and only re-fetched once per tab.
"""

_JS = r"""
(function () {
  const MANIFEST_KEY = "dbbasicDocsManifest";

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  function sortManifest(records) {
    const sorted = records.slice();
    sorted.sort((a, b) => {
      const na = Number(a && a.nav_order);
      const nb = Number(b && b.nav_order);
      const va = Number.isFinite(na) ? na : Infinity;
      const vb = Number.isFinite(nb) ? nb : Infinity;
      if (va !== vb) return va - vb;
      return String((a && a.title) || "").localeCompare(String((b && b.title) || ""));
    });
    return sorted;
  }

  async function loadManifest() {
    try {
      const cached = sessionStorage.getItem(MANIFEST_KEY);
      if (cached) {
        const parsed = JSON.parse(cached);
        if (Array.isArray(parsed)) return sortManifest(parsed);
      }
    } catch (e) { /* sessionStorage unavailable or the cached value is corrupt -- fetch instead */ }

    let records = [];
    try {
      const res = await fetch("/collections/doc_pages/records?limit=100",
        {credentials: "same-origin", headers: {accept: "application/json"}});
      const body = await res.json();
      records = (body && body.records) || [];
    } catch (e) {
      records = [];
    }
    try {
      sessionStorage.setItem(MANIFEST_KEY, JSON.stringify(records));
    } catch (e) { /* storage full or disabled -- proceed uncached for this load */ }
    return sortManifest(records);
  }

  function renderHeader(el) {
    if (!el) return;
    el.innerHTML = '<a class="docsbrand" href="/docs">'
      + '<span class="docslogo">DB</span><span>DBBASIC Docs</span></a>';
  }

  // Groups preserve first-appearance order across the already-sorted
  // manifest -- the category of the lowest-nav_order page in that category
  // decides where the whole group falls, with no separate category-order
  // field to keep in sync.
  function groupByCategory(records) {
    const groups = [];
    const byName = new Map();
    records.forEach((record) => {
      const name = String((record && record.category) || "").trim() || "General";
      let group = byName.get(name);
      if (!group) {
        group = {category: name, items: []};
        byName.set(name, group);
        groups.push(group);
      }
      group.items.push(record);
    });
    return groups;
  }

  function renderSidebar(el, records, activeSlug) {
    if (!el) return;
    const groups = groupByCategory(records || []);
    if (!groups.length) {
      el.innerHTML = '<div class="navempty">No pages yet.</div>';
      return;
    }
    let html = "";
    groups.forEach((group) => {
      html += '<div class="navgroup" data-navgroup>';
      html += '<div class="navheading">' + esc(group.category) + '</div>';
      html += '<ul class="navlist">';
      group.items.forEach((record) => {
        const id = (record && record.id) || "";
        const title = (record && record.title) || id;
        const active = activeSlug && id === activeSlug ? " active" : "";
        const haystack = esc(title + " " + ((record && record.summary) || "")
          + " " + ((record && record.category) || "")).toLowerCase();
        html += '<li class="navitem' + active + '" data-search="' + haystack + '">'
          + '<a href="/docs/' + esc(id) + '">' + esc(title) + '</a></li>';
      });
      html += '</ul></div>';
    });
    el.innerHTML = html;
  }

  function wireSearch(inputEl, listEl) {
    if (!inputEl || !listEl) return;
    inputEl.addEventListener("input", () => {
      const query = inputEl.value.trim().toLowerCase();
      const items = listEl.querySelectorAll(".navitem");
      items.forEach((item) => {
        const haystack = item.getAttribute("data-search") || "";
        const show = !query || haystack.indexOf(query) !== -1;
        item.style.display = show ? "" : "none";
      });
      const groups = listEl.querySelectorAll("[data-navgroup]");
      groups.forEach((group) => {
        const hasVisible = Array.prototype.some.call(
          group.querySelectorAll(".navitem"), (item) => item.style.display !== "none");
        group.style.display = hasVisible ? "" : "none";
      });
    });
  }

  // Off-canvas drawer for narrow viewports: hidden by default (the CSS in
  // each page keeps .docsidebar off-screen under the breakpoint), the
  // toggle button opens/closes it, and a tap on the scrim behind it closes
  // it too -- nothing here ever grows the page wider than the viewport.
  function wireDrawer(toggleEl, drawerEl, scrimEl) {
    if (!toggleEl || !drawerEl) return;
    function setOpen(open) {
      drawerEl.classList.toggle("open", open);
      if (scrimEl) scrimEl.classList.toggle("open", open);
      toggleEl.setAttribute("aria-expanded", open ? "true" : "false");
    }
    toggleEl.addEventListener("click", () => {
      setOpen(!drawerEl.classList.contains("open"));
    });
    if (scrimEl) scrimEl.addEventListener("click", () => setOpen(false));
    drawerEl.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => setOpen(false));
    });
  }

  async function mount(options) {
    const opts = options || {};
    const records = await loadManifest();
    renderHeader(opts.headerEl);
    renderSidebar(opts.navListEl, records, opts.activeSlug || null);
    wireSearch(opts.searchEl, opts.navListEl);
    wireDrawer(opts.toggleEl, opts.drawerEl, opts.scrimEl);
    return records;
  }

  window.dbbasicDocsNav = {
    loadManifest: loadManifest,
    renderHeader: renderHeader,
    renderSidebar: renderSidebar,
    wireSearch: wireSearch,
    wireDrawer: wireDrawer,
    mount: mount,
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
