"""Attachment widget, served at /attachments as window.dbbasicAttachments.

The files sibling of app-thread's comment widget: what a collection's
`capabilities.attachments` flag mounts under a detail page. It lists the
files attached to one record via the polymorphic (parent_collection,
parent_id) pair, uploads new ones (multipart to /api/files, which owns
owner_id/size/content-type and saves the blob), links to download, and lets
the owner delete (blob-aware, via /api/files/{id}). One widget, any
collection -- no per-collection FK column on `files`.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

  function fmtSize(n) {
    const size = Number(n) || 0;
    if (size >= 1048576) return (size / 1048576).toFixed(1) + " MB";
    if (size >= 1024) return (size / 1024).toFixed(1) + " KB";
    return size + " B";
  }

  async function jget(path) {
    const res = await fetch(path, {credentials: "same-origin", headers: {accept: "application/json"}});
    let data = null; try { data = await res.json(); } catch (e) {}
    return [res.ok, data];
  }

  window.dbbasicAttachments = {
    mount: async function (mountEl, opts) {
      mountEl = qs(mountEl); opts = opts || {};
      const pc = opts.parent_collection, pid = opts.parent_id;
      const viewer = opts.viewer_id
        || (typeof VIEWER_ID !== "undefined" ? VIEWER_ID : "") || "";
      if (!mountEl) return;
      if (!pc || !pid) {
        mountEl.innerHTML = '<div class="viewblock-error">attachments need a parent_collection and parent_id</div>';
        return;
      }

      mountEl.innerHTML =
        '<div class="attachments"><h3 class="threadhead">Attachments</h3>'
        + '<div class="attachlist"><div class="state">loading&hellip;</div></div>'
        + (viewer
            ? '<form class="attachupload"><input type="file" name="file" required>'
              + '<button type="submit" class="btn primary">Upload</button>'
              + '<span class="error" data-err></span></form>'
            : '<div class="state"><a href="/login">Sign in</a> to attach files.</div>')
        + '</div>';
      const listEl = mountEl.querySelector(".attachlist");
      const form = mountEl.querySelector(".attachupload");

      function fileRow(f) {
        const mine = viewer && f.owner_id && f.owner_id === viewer;
        const del = mine
          ? '<button class="rowbtn danger" data-del="' + esc(f.id) + '" title="Delete">✕</button>' : "";
        const meta = [fmtSize(f.size), f.content_type].filter(Boolean).join(" · ");
        return '<div class="attachrow"><div class="attachicon">📎</div>'
          + '<div class="attachbody"><a class="attachname" href="/api/files/' + esc(f.id) + '">'
          + esc(f.filename || "file") + '</a>'
          + '<div class="attachmeta">' + esc(meta) + '</div></div>'
          + '<div class="attachactions">' + del + '</div></div>';
      }

      async function load() {
        const [ok, body] = await jget("/collections/files/records?limit=500"
          + "&parent_collection=" + encodeURIComponent(pc) + "&parent_id=" + encodeURIComponent(pid));
        if (!ok) { listEl.innerHTML = '<div class="state">Could not load attachments.</div>'; return; }
        const rows = (body.records || []).filter((f) => f.parent_collection === pc && f.parent_id === pid);
        rows.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
        listEl.innerHTML = rows.length ? rows.map(fileRow).join("") : '<div class="state">No attachments yet.</div>';
      }

      if (form) {
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const input = form.elements["file"];
          const errEl = form.querySelector("[data-err]"); if (errEl) errEl.textContent = "";
          if (!input.files || !input.files.length) return;
          // Multipart to /api/files: the server owns owner_id/size/content_type
          // and saves the blob; we pass the polymorphic parent so it lands on
          // this record's attachment list.
          const data = new FormData();
          data.append("file", input.files[0]);
          data.append("parent_collection", pc);
          data.append("parent_id", pid);
          const res = await fetch("/api/files", {method: "POST", credentials: "same-origin", body: data});
          let resp = null; try { resp = await res.json(); } catch (x) {}
          if (!res.ok) { if (errEl) errEl.textContent = (resp && resp.error) || "Upload failed"; return; }
          form.reset(); load();
        });
      }
      listEl.addEventListener("click", async (e) => {
        const del = e.target.closest("[data-del]"); if (!del) return;
        if (!window.confirm("Delete this attachment?")) return;
        // Blob-aware delete: /api/files/{id} removes the file bytes AND the
        // metadata record (a plain record delete would orphan the blob).
        await fetch("/api/files/" + encodeURIComponent(del.getAttribute("data-del")),
          {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
        load();
      });

      (function sub() {
        if (window.dbbasicSubscribe) window.dbbasicSubscribe("files", load);
        else setTimeout(sub, 400);
      })();
      load();
    },
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
