"""Share widget, served at /share as window.dbbasicShare.

What a collection's `capabilities.shareable` flag mounts under a detail page.
It is owner-only: it asks /api/share for the record's grants and, if the
viewer isn't the owner (403), renders nothing. For the owner it lists who has
access and lets them grant another user read/write or revoke. All grant writes
go through /api/share, which checks the requester owns the target record --
the widget never writes record_shares directly.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

  async function api(method, path, body) {
    const res = await fetch(path, {method, credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = await res.json(); } catch (e) {}
    return [res.status, res.ok, data];
  }

  window.dbbasicShare = {
    mount: async function (mountEl, opts) {
      mountEl = qs(mountEl); opts = opts || {};
      const pc = opts.parent_collection, pid = opts.parent_id;
      const viewer = opts.viewer_id
        || (typeof VIEWER_ID !== "undefined" ? VIEWER_ID : "") || "";
      if (!mountEl) return;
      // Sharing is owner-only: without a signed-in viewer, or if the access
      // read comes back forbidden (not the owner), render nothing at all.
      if (!pc || !pid || !viewer) { mountEl.innerHTML = ""; return; }

      const base = "/api/share?collection=" + encodeURIComponent(pc) + "&record_id=" + encodeURIComponent(pid);

      async function load() {
        const [status, ok, data] = await api("GET", base);
        if (status === 401 || status === 403) { mountEl.innerHTML = ""; return; }
        if (!ok) { mountEl.innerHTML = ""; return; }
        const shares = (data && data.shares) || [];
        const rows = shares.map((s) =>
          '<div class="sharerow"><div class="sharewho">' + esc(s.user_id) + '</div>'
          + '<div class="sharemeta">' + esc(s.permission || "read") + '</div>'
          + '<button class="rowbtn danger" data-revoke="' + esc(s.id) + '" title="Revoke">✕</button></div>'
        ).join("") || '<div class="state">Not shared with anyone yet.</div>';
        mountEl.innerHTML =
          '<div class="share"><h3 class="threadhead">Sharing</h3>'
          + '<div class="sharelist">' + rows + '</div>'
          + '<form class="shareadd"><input name="user_id" placeholder="Share with (user id)" autocomplete="off" required>'
          + '<select name="permission"><option value="read">can view</option><option value="write">can edit</option></select>'
          + '<button type="submit" class="btn primary">Share</button>'
          + '<span class="error" data-err></span></form></div>';
        const form = mountEl.querySelector(".shareadd");
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const errEl = form.querySelector("[data-err]"); if (errEl) errEl.textContent = "";
          const uid = String(form.elements["user_id"].value || "").trim();
          if (!uid) return;
          const [st, ok2, data2] = await api("POST", "/api/share",
            {collection: pc, record_id: pid, user_id: uid, permission: form.elements["permission"].value});
          if (!ok2) { if (errEl) errEl.textContent = (data2 && data2.error) || "Could not share"; return; }
          load();
        });
        mountEl.querySelector(".sharelist").addEventListener("click", async (e) => {
          const btn = e.target.closest("[data-revoke]"); if (!btn) return;
          await api("DELETE", "/api/share/" + encodeURIComponent(btn.getAttribute("data-revoke")));
          load();
        });
      }

      (function sub() {
        if (window.dbbasicSubscribe) window.dbbasicSubscribe("record_shares", load);
        else setTimeout(sub, 400);
      })();
      load();
    },
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
