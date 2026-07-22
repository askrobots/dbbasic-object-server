"""Comment thread widget, served at /thread as window.dbbasicThread.

The missing piece of app-thread: the `thread_comments` schema and the
`thread` block reference landed early (see the schema's own note), with the
final polymorphic shape (parent_collection + parent_id) so no data
migration is ever needed once the widget arrives. This is that widget.

Sibling to /list, /form, /detail: a public static script, no server state.
`window.dbbasicThread.mount(el, {parent_collection, parent_id, viewer_id?})`
renders the comment thread for one record -- the comments filtered to that
(collection, id) pair, a compose box for signed-in visitors, owner-only
delete, relative timestamps, and the same change-log realtime every other
surface uses. It is what a collection's `capabilities.comments` flag mounts
under a detail page (see app-views/objects/site/view_render.py), so any
collection gets comments by declaring one key -- no per-app comment table
(the task_comments/interactions/profile_comments duplication this replaces).
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

  async function api(method, path, body) {
    const res = await fetch(path, {method, credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = await res.json(); } catch (e) {}
    return [res.ok, data];
  }

  window.dbbasicThread = {
    mount: async function (mountEl, opts) {
      mountEl = qs(mountEl); opts = opts || {};
      const pc = opts.parent_collection, pid = opts.parent_id;
      // The signed-in viewer -- passed by the caller (view_render embeds
      // VIEWER_ID). Enables the compose box and marks a comment deletable
      // by its author. Empty => read-only thread with a sign-in prompt.
      const viewer = opts.viewer_id
        || (typeof VIEWER_ID !== "undefined" ? VIEWER_ID : "") || "";
      if (!mountEl) return;
      if (!pc || !pid) {
        mountEl.innerHTML = '<div class="viewblock-error">comments need a parent_collection and parent_id</div>';
        return;
      }

      mountEl.innerHTML =
        '<div class="thread"><h3 class="threadhead">Comments</h3>'
        + '<div class="threadlist"><div class="state">loading&hellip;</div></div>'
        + (viewer
            ? '<form class="threadcompose"><textarea name="body" rows="2" placeholder="Add a comment&hellip;" required></textarea>'
              + '<div class="threadcomposeactions"><button type="submit" class="btn primary">Comment</button>'
              + '<span class="error" data-err></span></div></form>'
            : '<div class="state"><a href="/login">Sign in</a> to comment.</div>')
        + '</div>';
      const listEl = mountEl.querySelector(".threadlist");
      const form = mountEl.querySelector(".threadcompose");

      function bubble(c) {
        const who = c.author_name || c.owner_id || "someone";
        const av = String(who).trim().charAt(0).toUpperCase() || "?";
        const mine = viewer && c.owner_id && c.owner_id === viewer;
        const del = mine
          ? '<button class="rowbtn danger" data-del="' + esc(c.id) + '" title="Delete">✕</button>' : "";
        return '<div class="comment"><div class="av">' + esc(av) + '</div>'
          + '<div class="commentbody"><div class="commentmeta">'
          + '<span class="commentwho">' + esc(who) + '</span>'
          + (c.created_at ? '<span class="when">' + esc(relDate(c.created_at)) + '</span>' : "")
          + del + '</div>'
          + '<div class="commenttext">' + esc(c.body).replace(/\n/g, "<br>") + '</div></div></div>';
      }

      async function load() {
        // Query narrows to THIS record's thread server-side (58 field=value),
        // so even a read-all permission only returns this record's comments.
        const [ok, body] = await api("GET", "/collections/thread_comments/records?limit=500"
          + "&parent_collection=" + encodeURIComponent(pc) + "&parent_id=" + encodeURIComponent(pid));
        if (!ok) { listEl.innerHTML = '<div class="state">Could not load comments.</div>'; return; }
        let rows = (body.records || []).filter((c) => c.parent_collection === pc && c.parent_id === pid);
        // Moderated comments are hidden from everyone but their author.
        rows = rows.filter((c) => {
          const st = c.status || "published";
          return st === "published" || (viewer && c.owner_id === viewer);
        });
        rows.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
        listEl.innerHTML = rows.length ? rows.map(bubble).join("") : '<div class="state">No comments yet.</div>';
      }

      if (form) {
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const ta = form.elements["body"], text = String(ta.value || "").trim();
          const errEl = form.querySelector("[data-err]"); if (errEl) errEl.textContent = "";
          if (!text) return;
          // Minimal payload: the server owns created_at (read-only) and
          // defaults status to "published"; parent_* pin the thread.
          const rec = {
            id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()),
            parent_collection: pc, parent_id: pid, body: text,
          };
          // Do NOT send owner_id: it carries a `public: hidden` policy and is
          // server-owned (set from the session on create; a client value is
          // rejected as not-editable). author_name IS a normal display field --
          // stamp it, because owner_id is redacted from other readers, so
          // without it every other person's comment would read as "someone".
          if (viewer) rec.author_name = viewer;
          const [ok, resp] = await api("POST", "/collections/thread_comments/records", rec);
          if (!ok) { if (errEl) errEl.textContent = (resp && resp.error) || "Could not post comment"; return; }
          ta.value = ""; load();
        });
      }
      listEl.addEventListener("click", async (e) => {
        const del = e.target.closest("[data-del]"); if (!del) return;
        if (!window.confirm("Delete this comment?")) return;
        await api("DELETE", "/collections/thread_comments/records/" + encodeURIComponent(del.getAttribute("data-del")));
        load();
      });

      (function sub() {
        if (window.dbbasicSubscribe) window.dbbasicSubscribe("thread_comments", load);
        else setTimeout(sub, 400);
      })();
      load();
    },
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
