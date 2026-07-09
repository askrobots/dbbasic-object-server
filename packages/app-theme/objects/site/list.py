"""Shared list generator, served at /list as window.dbbasicList.

Renders a collection as rich rows (avatar, title/link, subtitle, relative
date, tag pills, per-row edit + delete), with a search box (over
/api/search) and a newest/oldest sort. It subscribes to the collection
over the /ws websocket, so the list auto-refreshes when a record changes
in another tab, by another user, or by an agent — a thing the old stack's
lists did not do. Display accessors come from a small config; search and
live updates are automatic.

Defined once here — every list page reuses it instead of hand-writing rows.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

  function relDate(iso) {
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const ms = Date.now() - d.getTime();
    if (ms < 3600000) { const m = Math.floor(ms / 60000); return m < 1 ? "just now" : m + "m"; }
    if (ms < 86400000) return Math.floor(ms / 3600000) + "h";
    if (ms < 7 * 86400000) return Math.floor(ms / 86400000) + "d";
    return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  }
  function pills(v) {
    return String(v || "").split(",").map((t) => t.trim()).filter(Boolean)
      .map((t) => '<span class="pill">' + esc(t) + '</span>').join("");
  }

  window.dbbasicList = function (collection, cfg) {
    cfg = cfg || {};
    const mount = qs(cfg.mount);
    const searchEl = cfg.search ? qs(cfg.search) : null;
    const sortEl = cfg.sort ? qs(cfg.sort) : null;
    let all = [];

    // Page hooks: cfg.slots[name] (per page) + window.dbbasicSlots (cross-page,
    // operator-registered). Both return HTML; the generator injects it at the
    // named hook. No-op when neither is present.
    function slotHtml(name, ctx) {
      let out = "";
      const local = cfg.slots && cfg.slots[name];
      if (local) { try { const h = local(ctx); if (h) out += h; } catch (e) {} }
      if (window.dbbasicSlots) out += window.dbbasicSlots.render(collection, name, ctx);
      return out;
    }

    function row(r) {
      const title = (cfg.title ? cfg.title(r) : (r.title || r.name || r.id)) || "(untitled)";
      const av = String(title).trim().charAt(0) || "?";
      const href = cfg.href && cfg.href(r);
      const titleHtml = href
        ? '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(title) + '</a>'
        : esc(title);
      const sub = cfg.subtitle ? cfg.subtitle(r) : "";
      const tags = cfg.tags ? cfg.tags(r) : "";
      const created = cfg.created ? cfg.created(r) : r.created_at;
      const mine = r.owner_id === cfg.owner;
      const acts = mine
        ? '<button class="rowbtn" data-act="edit" data-id="' + esc(r.id) + '" title="Edit">✎</button>'
          + '<button class="rowbtn danger" data-act="delete" data-id="' + esc(r.id) + '" title="Delete">✕</button>'
        : "";
      return '<div class="listrow"><div class="av">' + esc(av) + '</div><div class="body">'
        + '<div class="rowtitle">' + titleHtml + '</div>'
        + (sub ? '<div class="rowsub">' + esc(sub) + '</div>' : "")
        + '<div class="rowmeta">' + (created ? '<span class="when">' + esc(relDate(created)) + '</span>' : "")
        + pills(tags) + '</div></div><div class="rowactions">' + acts
        + slotHtml("row_actions", r) + '</div></div>';
    }
    function sortList(list) {
      const s = list.slice().sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
      return (sortEl && sortEl.value === "oldest") ? s : s.reverse();
    }
    function render(list) {
      const rows = sortList(list).map(row).join("");
      const ctx = {collection: collection, count: list.length};
      const body = rows || slotHtml("empty", ctx) || '<div class="state">Nothing yet.</div>';
      mount.innerHTML = slotHtml("before_list", ctx) + body + slotHtml("after_list", ctx);
    }
    async function load() {
      const res = await fetch("/collections/" + collection + "/records?limit=500",
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) { render([]); return; }
      const body = await res.json(); all = body.records || []; render(all);
    }
    async function search(q) {
      if (!q) { render(all); return; }
      const res = await fetch("/api/search?q=" + encodeURIComponent(q) + "&collections=" + collection + "&limit=50",
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) return;
      const body = await res.json(); render((body.results || {})[collection] || []);
    }

    if (searchEl) searchEl.addEventListener("input", (e) => search(e.target.value.trim()));
    if (sortEl) sortEl.addEventListener("change", () => render(all));
    mount.addEventListener("click", async (e) => {
      const btn = e.target.closest("button.rowbtn"); if (!btn) return;
      const id = btn.dataset.id;
      if (btn.dataset.act === "delete") {
        if (!confirm("Delete this?")) return;
        await fetch("/collections/" + collection + "/records/" + encodeURIComponent(id),
          {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
        load();
      } else if (btn.dataset.act === "edit" && cfg.onEdit) {
        const r = all.find((x) => x.id === id); if (r) cfg.onEdit(r);
      }
    });
    (function sub() {
      if (window.dbbasicSubscribe) window.dbbasicSubscribe(collection, load);
      else setTimeout(sub, 400);
    })();
    load();
    return {reload: load};
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
