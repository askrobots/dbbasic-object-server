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

Mounts via `window.dbbasicDetail.mount(el, {collection, record_id})`.
"""

_JS = r"""
(function () {
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

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
      await window.dbbasicForm.readOnly(collection, {mount: mount, record: record});
    },
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
