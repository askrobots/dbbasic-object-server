"""Detail generator, served at /detail as window.dbbasicDetail.

Sibling to /list and /form (same convention: a public static script, no
server state). `59-detail-related-spec.md`'s detail mode is explicitly NOT
a second field-renderer -- it reuses /form's schema fetch, field order,
relation display-field resolution, and type-aware value formatting
(`window.dbbasicForm.readOnly`, in form.py) in forced-read-only mode. This
file is thin on purpose: it fetches the one record (the record read is the
only permission gate, the same posture /list and /form already have) and
hands it to that shared renderer. A page that mounts a `detail` block
loads both `<script src="/form">` and `<script src="/detail">` (see
packages/app-views/objects/site/view_render.py).

Owner-aware edit/delete (Stage 6 extension of 59): when a `detail` block
declares `editable`/`deletable` AND the viewer owns the record
(`record[owner_field] === viewer_id`, owner_field defaulting to owner_id),
the read-only view gains Edit and Delete affordances -- Edit swaps the same
mount into /form's EXISTING edit pipeline (window.dbbasicForm with a
record, which PUTs and calls onSaved) and Delete issues the ordinary record
DELETE then redirects. This is what lets the hand-written per-collection
`*_view.py` detail pages (each re-implementing fetch + render + owner-edit +
delete) collapse into one view record + this one renderer: read-only for
everyone, owner-editable for the owner, no bespoke page code. Non-owners
and anonymous visitors see exactly the read-only view they saw before --
the affordances are never shown, and the record write would be
permission-denied regardless (this only gates the UI, not the gate).

Mounts via window.dbbasicDetail.mount(el, {collection, record_id,
editable?, deletable?, delete_redirect?, owner_field?, viewer_id?}).
"""

_JS = r"""
(function () {
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

  async function renderView(mount, collection, record, opts) {
    mount.innerHTML = '<div class="detailview"></div><div class="detailtools"></div>';
    const viewEl = mount.querySelector(".detailview");
    const toolsEl = mount.querySelector(".detailtools");
    await window.dbbasicForm.readOnly(collection, {mount: viewEl, record: record});

    // Owner-only affordances. `viewer_id` comes from the page (VIEWER_ID,
    // embedded server-side by view_render); a field the viewer can't read
    // is already absent from `record`, so a hidden owner_field simply means
    // "not owner" here, failing closed.
    const ownerField = opts.owner_field || "owner_id";
    const isOwner = !!opts.viewer_id && !!record[ownerField] && record[ownerField] === opts.viewer_id;
    if (!isOwner || (!opts.editable && !opts.deletable)) return;

    const btns = [];
    if (opts.editable) btns.push('<button class="btn" data-act="edit">Edit</button>');
    if (opts.deletable) btns.push('<button class="btn danger" data-act="delete">Delete</button>');
    toolsEl.innerHTML = btns.join("");

    const editBtn = toolsEl.querySelector('[data-act="edit"]');
    if (editBtn) editBtn.addEventListener("click", () => startEdit(mount, viewEl, toolsEl, collection, record, opts));
    const delBtn = toolsEl.querySelector('[data-act="delete"]');
    if (delBtn) delBtn.addEventListener("click", () => doDelete(collection, record, opts));
  }

  function startEdit(mount, viewEl, toolsEl, collection, record, opts) {
    // Guard against view_render's subscribe-triggered re-mount clobbering an
    // in-progress edit (see mount() below).
    mount._dbbasicEditing = true;
    toolsEl.innerHTML = '<button class="btn" data-act="cancel">Cancel</button>';
    toolsEl.querySelector('[data-act="cancel"]').addEventListener("click", () => {
      mount._dbbasicEditing = false;
      renderView(mount, collection, record, opts);
    });
    // Reuse /form's edit pipeline unchanged: a record present => PUT, and
    // onSaved hands back the stored row so we re-render read-only in place.
    window.dbbasicForm(collection, {
      mount: viewEl,
      record: record,
      onSaved: (updated) => {
        mount._dbbasicEditing = false;
        renderView(mount, collection, updated || record, opts);
      },
    });
  }

  async function doDelete(collection, record, opts) {
    const label = String(collection).replace(/_/g, " ").replace(/s$/, "") || "record";
    if (!window.confirm("Delete this " + label + "?")) return;
    const res = await fetch("/collections/" + encodeURIComponent(collection) + "/records/" + encodeURIComponent(record.id),
      {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
    if (res.ok) window.location = opts.delete_redirect || ("/" + collection);
  }

  window.dbbasicDetail = {
    mount: async function (mount, opts) {
      mount = qs(mount);
      opts = opts || {};
      const collection = opts.collection;
      const recordId = opts.record_id;
      if (!collection || !recordId) {
        mount.innerHTML = '<div class="viewblock-error">detail needs a collection and record_id</div>';
        return;
      }
      if (!window.dbbasicForm || !window.dbbasicForm.readOnly) {
        mount.innerHTML = '<div class="viewblock-error">detail generator unavailable</div>';
        return;
      }
      // A subscribe-driven reload must not wipe out an edit the owner is in
      // the middle of; the flag lives on the mount element so it survives
      // across these independent mount() calls.
      if (mount._dbbasicEditing) return;
      const res = await fetch("/collections/" + encodeURIComponent(collection) + "/records/" + encodeURIComponent(recordId),
        {credentials: "same-origin", headers: {accept: "application/json"}});
      if (!res.ok) {
        mount.innerHTML = res.status === 404
          ? '<div class="state">Not found.</div>'
          : '<div class="state denied">Not available.</div>';
        return;
      }
      const body = await res.json();
      const record = body.record || body;
      await renderView(mount, collection, record, opts);
    },
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
