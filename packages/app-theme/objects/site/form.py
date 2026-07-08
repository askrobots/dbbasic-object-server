"""Schema-driven form generator, served at /form as window.dbbasicForm.

The metadata builds the UI: given a collection, it fetches the schema
(GET /api/schema/{c}) and renders a form from it — field order from
forms.default.fields, controls from field semantics (enum -> select,
relation -> record picker, boolean -> checkbox, date -> date input,
textarea, number, text), labels/help/required/max-length from the schema.
Create (POST) and edit (PUT) modes; id, owner_id, and created_at are set
automatically; computed/read_only fields are never written. One generator,
every collection — the web counterpart of Scroll's schema_form.

Defined once here, so no page hand-writes a form again.
"""

_JS = r"""
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const human = (n) => n.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const qs = (m) => typeof m === "string" ? document.querySelector(m) : m;

  async function api(method, path, body) {
    const res = await fetch(path, {method, credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = await res.json(); } catch (e) {}
    return [res.ok, data];
  }

  function skip(f) {
    const t = String(f.type || "").toLowerCase();
    return f.name === "id" || f.name === "owner_id" || f.name === "created_at"
      || t === "computed" || f.computed || f.read_only || f.readonly || f.readOnly;
  }

  async function control(f, value) {
    const t = String(f.type || "text").toLowerCase();
    const name = esc(f.name);
    const req = f.required ? " required" : "";
    const ml = (f.validation && f.validation.max_length) ? ' maxlength="' + f.validation.max_length + '"' : "";
    const ph = f.placeholder ? ' placeholder="' + esc(f.placeholder) + '"' : "";
    const v = value == null ? "" : String(value);
    if (f.relation) {
      const col = typeof f.relation === "string" ? f.relation : f.relation.collection;
      const disp = (typeof f.relation === "object" && f.relation.display_field) || "name";
      let opts = '<option value="">—</option>';
      const [ok, body] = await api("GET", "/collections/" + col + "/records?limit=500");
      if (ok) for (const r of (body.records || []))
        opts += '<option value="' + esc(r.id) + '"' + (r.id === v ? " selected" : "") + '>' + esc(r[disp] || r.id) + '</option>';
      return '<select name="' + name + '"' + req + '>' + opts + '</select>';
    }
    if (f.enum || t === "enum") {
      let opts = f.required ? "" : '<option value="">—</option>';
      for (const o of (f.enum || [])) opts += '<option value="' + esc(o) + '"' + (String(o) === v ? " selected" : "") + '>' + esc(o) + '</option>';
      return '<select name="' + name + '"' + req + '>' + opts + '</select>';
    }
    if (t === "boolean") return '<label class="switch"><input type="checkbox" name="' + name + '"' + (v === "true" ? " checked" : "") + '> ' + esc(f.label || human(f.name)) + '</label>';
    if (t === "textarea") return '<textarea name="' + name + '" rows="3"' + req + ml + ph + '>' + esc(v) + '</textarea>';
    if (t === "date") return '<input type="date" name="' + name + '" value="' + esc(v.slice(0, 10)) + '"' + req + '>';
    if (t === "datetime" || t === "timestamp") return '<input type="datetime-local" name="' + name + '" value="' + esc(v.slice(0, 16)) + '"' + req + '>';
    if (["integer", "int", "number", "float", "currency"].indexOf(t) >= 0) return '<input type="number" name="' + name + '" value="' + esc(v) + '"' + req + ph + '>';
    return '<input type="text" name="' + name + '" value="' + esc(v) + '"' + req + ml + ph + '>';
  }

  window.dbbasicForm = async function (collection, opts) {
    opts = opts || {};
    const mount = qs(opts.mount);
    const record = opts.record || null;
    const [ok, meta] = await api("GET", "/api/schema/" + collection);
    if (!ok || !meta.schema) { mount.innerHTML = '<p class="error">Could not load form.</p>'; return; }
    const schema = meta.schema;
    const byName = {}; (schema.fields || []).forEach((f) => byName[f.name] = f);
    const order = (schema.forms && schema.forms.default && schema.forms.default.fields)
      || (schema.fields || []).map((f) => f.name);
    const ordered = order.map((n) => byName[n]).filter((f) => f && !skip(f));

    const rows = [];
    for (const f of ordered) {
      const ctrl = await control(f, record ? record[f.name] : f.default);
      if (String(f.type || "").toLowerCase() === "boolean")
        rows.push('<div class="field">' + ctrl + '</div>');
      else
        rows.push('<div class="field"><label>' + esc(f.label || human(f.name)) +
          (f.required ? ' <span class="req">*</span>' : "") + '</label>' + ctrl +
          (f.help ? '<div class="help">' + esc(f.help) + '</div>' : "") +
          '<div class="err" data-for="' + esc(f.name) + '"></div></div>');
    }
    mount.innerHTML = '<form class="genform stack">' + rows.join("") +
      '<div class="formactions"><button type="submit" class="btn primary">' +
      (record ? "Save Changes" : (opts.submitLabel || "Save")) + '</button>' +
      (opts.onCancel ? '<button type="button" class="btn" data-cancel>Cancel</button>' : "") +
      '<span class="error" data-formerror></span></div></form>';

    const form = mount.querySelector("form");
    if (opts.onCancel) form.querySelector("[data-cancel]").addEventListener("click", opts.onCancel);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const rec = {};
      for (const f of ordered) {
        const el = form.elements[f.name];
        if (!el) continue;
        rec[f.name] = (String(f.type || "").toLowerCase() === "boolean") ? (el.checked ? "true" : "false") : el.value;
      }
      let bad = false;
      form.querySelectorAll(".err").forEach((el) => el.textContent = "");
      for (const f of ordered) {
        if (f.required && !String(rec[f.name] || "").trim()) {
          const errEl = form.querySelector('.err[data-for="' + f.name + '"]');
          if (errEl) errEl.textContent = "Required";
          bad = true;
        }
      }
      if (bad) return;
      let ok2, body2;
      if (record) {
        [ok2, body2] = await api("PUT", "/collections/" + collection + "/records/" + encodeURIComponent(record.id), rec);
      } else {
        rec.id = crypto.randomUUID();
        if (opts.owner !== undefined) rec.owner_id = opts.owner;
        [ok2, body2] = await api("POST", "/collections/" + collection + "/records", rec);
      }
      const fe = form.querySelector("[data-formerror]");
      if (!ok2) { if (fe) fe.textContent = (body2 && body2.error) || "Save failed"; return; }
      if (opts.onSaved) opts.onSaved((body2 && body2.record) || rec);
    });
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
