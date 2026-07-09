"""Shared slot registry, served at /slots as window.dbbasicSlots.

Named extension points for the generated list/form UIs (Phase 5b, page hooks —
docs/upgrade-and-customization.md Rule 4). An operator or a page registers a
renderer for a (collection, slot) pair; the generators call it at the matching
hook point and inject its HTML — so behavior can be added to every list/form
without editing the page or the generator itself. Register against a specific
collection or "*" for all collections.

Slots today:
  /list  -> before_list(ctx), after_list(ctx), row_actions(record), empty(ctx)
  /form  -> before_fields(ctx), after_fields(ctx)
A renderer receives a context object and returns an HTML string (or "").
Interactive slot markup should attach its own delegated listeners (list/form
mounts re-render), e.g. document.addEventListener on a data-attribute.

Defined once here; the generators call window.dbbasicSlots when present and
no-op when it is absent, so slots are purely additive.
"""

_JS = r"""
(function () {
  const reg = {};
  const key = (collection, slot) => collection + "::" + slot;
  window.dbbasicSlots = {
    register: function (collection, slot, fn) {
      if (typeof fn !== "function") return;
      const k = key(collection, slot);
      (reg[k] = reg[k] || []).push(fn);
    },
    render: function (collection, slot, ctx) {
      let out = "";
      for (const scope of [collection, "*"]) {
        const fns = reg[key(scope, slot)];
        if (!fns) continue;
        for (const fn of fns) {
          try { const html = fn(ctx); if (html) out += html; } catch (e) {}
        }
      }
      return out;
    }
  };
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
