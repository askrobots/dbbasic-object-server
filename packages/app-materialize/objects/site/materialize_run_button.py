"""Run-now button for materialize_definitions' detail page.

Implements plan/vocabulary/61-materialize-spec.md's Surfaces section:
"a system object, materialize_run, exposed as a slot button
(window.dbbasicSlots, the same injection point 10-flow uses for its
action buttons) on the materialize_definitions detail page: after_fields
(ctx) renders a 'Run now' button." Mirrors packages/app-theme/objects/
site/slots.py's own serving convention (GET returns a JS body); the
generated form page (packages/app-theme/objects/site/form.py) calls
window.dbbasicSlots.render(collection, "after_fields", ctx) when present
and no-ops when absent, so this registration is purely additive.

No other package in this repo registers a dbbasicSlots renderer yet, so
whatever mechanism ultimately gets this script's <script> tag onto the
materialize_definitions detail page (the page shell, out of this
package's scope) is unexercised precedent -- the button's own logic
(register, render, POST, show the result) is complete and correct against
the documented window.dbbasicSlots contract regardless.
"""


_JS = r"""
(function () {
  if (!window.dbbasicSlots) return;

  window.dbbasicSlots.register("materialize_definitions", "after_fields", function (ctx) {
    var record = (ctx && ctx.record) || {};
    var id = record.id;
    if (!id) return "";

    var btnId = "materialize-run-now-" + id;

    setTimeout(function () {
      var btn = document.getElementById(btnId);
      if (!btn || btn._materializeBound) return;
      btn._materializeBound = true;
      btn.addEventListener("click", function () {
        btn.disabled = true;
        var out = document.getElementById(btnId + "-result");
        if (out) out.textContent = "Running...";

        fetch("/objects/system_materialize_run", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({definition_id: id})
        }).then(function (res) {
          return res.json();
        }).then(function (data) {
          btn.disabled = false;
          if (!out) return;
          if (data && data.status === "ok") {
            var errorCount = (data.errors && data.errors.length) || 0;
            out.textContent = "Checked " + data.checked + ", generated " + data.generated +
              ", skipped " + data.skipped_already_generated +
              (errorCount ? ", " + errorCount + " error(s)" : "");
          } else {
            out.textContent = "Error: " + ((data && data.error) || "run failed");
          }
        }).catch(function (err) {
          btn.disabled = false;
          if (out) out.textContent = "Error: " + err;
        });
      });
    }, 0);

    return (
      '<div class="materialize-run-now">' +
      '<button type="button" id="' + btnId + '">Run now</button>' +
      '<span id="' + btnId + '-result" class="materialize-run-now-result"></span>' +
      '</div>'
    );
  });
})();
"""


def GET(request):
    return {"content_type": "application/javascript; charset=utf-8", "body": _JS}
