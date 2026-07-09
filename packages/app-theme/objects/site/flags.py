"""Shared feature-flag helper, served at /flags as window.dbbasicFlags.

Wraps GET /api/flags (packages/app-settings): effective value = the
caller's own user_prefs override ("flag:<name>") -> the instance-wide
feature_flags value, resolved server-side per docs/upgrade-and-customization.md
Rule 5. This module never talks to a collection directly -- it is a thin,
dependency-free cache in front of that one endpoint.

API:
  await window.dbbasicFlags.load()   // fetch + cache the flag map (once);
                                      // safe to call again, re-fetches fresh
  window.dbbasicFlags(name)          // sync lookup: cached string value, or
                                      // null if unknown / not loaded yet
  window.dbbasicFlags.on(name)       // sync boolean: true when the cached
                                      // value is "on" or "true"

Typical use: `await window.dbbasicFlags.load()` once on page init, then
`window.dbbasicFlags.on("kanban_view")` anywhere in the page's render path.
Calling `dbbasicFlags(name)` before `load()` has resolved simply returns
null (fail dark), so gating a feature behind a flag never throws.

Defined once here, so no page hand-rolls its own /api/flags fetch.
"""

_JS = r"""
(function () {
  let cache = null;
  let pending = null;

  async function load() {
    if (pending) return pending;
    pending = fetch("/api/flags", {credentials: "same-origin", headers: {accept: "application/json"}})
      .then((res) => res.ok ? res.json() : {status: "error", flags: {}})
      .then((data) => { cache = (data && data.flags) || {}; pending = null; return cache; })
      .catch(() => { cache = cache || {}; pending = null; return cache; });
    return pending;
  }

  function dbbasicFlags(name) {
    if (!cache || !(name in cache)) return null;
    return cache[name];
  }
  dbbasicFlags.load = load;
  dbbasicFlags.on = function (name) {
    const v = dbbasicFlags(name);
    return v === "on" || v === "true";
  };

  window.dbbasicFlags = dbbasicFlags;
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
