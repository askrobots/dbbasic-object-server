"""site_notary -- the public face of the notary.

    GET /notary                  what it is, and a box to check a digest
    GET /notary/{digest}         was this recorded here, and when
    GET /notary/{digest}.json    the same answer, machine-readable

## Why the check is public

An attestation that only its submitter can look up is not an attestation,
it is a favour. The entire value of lodging a digest with an independent
party is that a third person -- a customer, an auditor, a court, the other
side of an argument -- can verify it without needing an account here, a
relationship with the operator, or anybody's permission. So this page
answers anybody, and it answers the same way every time.

That is also why the answer for an unknown digest is a plain "not
recorded here" rather than a 404: a checker asking about a digest they
were given deserves a sentence explaining that absence proves nothing,
not a browser error page they will read as a fault.

## The file is hashed in the browser and never uploaded

The page carries a drop box that computes a sha256 with SubtleCrypto,
locally, and fills the digest in. Nothing is sent. This is the one feature
here that is not strictly necessary, and it earns its place by making the
central claim visible rather than merely stated: a visitor can watch their
own file produce a digest without a request leaving the machine, and then
decide for themselves whether notarizing something confidential is safe.
A page that only ASSERTS "we never see your content" is asking to be
believed; one that demonstrates it is not.

## What it will not say

Every answer renders object_notary.attestation() -- what the record
proves, what it does not, and what it rests on -- immediately beside the
result rather than in a footnote. The failure mode of a notary is not
technical. It is a reader taking "notarized" to mean verified, owned or
approved, and then relying on it. The wording lives in object_notary so
this page, the JSON and the submission receipt cannot drift into three
different strengths of the same promise.
"""

import html
import json
import os

import object_notary
import object_records

COLLECTION = "notarizations"
SETTINGS_COLLECTION = "app_settings"
PUBLIC_SUBMISSION_KEY = "notary.public_submission"

_STYLE = """
.nt { max-width: 46rem; }
.nt h2 { font-size: 1.05rem; margin: 1.8rem 0 .4rem; }
.nt p, .nt li { line-height: 1.55; }
.nt .digest { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              font-size: .82rem; word-break: break-all; }
.nt .card { border: 1px solid var(--line, #38384a); border-radius: 8px;
            padding: .9rem 1.1rem; margin: 1rem 0; }
.nt .hit { border-color: var(--accent, #b5713a); }
.nt .when { font-size: 1.35rem; font-weight: 600; margin: .3rem 0 .1rem; }
.nt .note { border-left: 3px solid var(--line, #55556a); padding: .1rem 0 .1rem .7rem;
            margin: .6rem 0 1.2rem; font-size: .88rem; opacity: .85; }
.nt .cols { display: grid; gap: 1rem; grid-template-columns: 1fr 1fr; }
@media (max-width: 40rem) { .nt .cols { grid-template-columns: 1fr; } }
.nt .cols h3 { font-size: .9rem; margin: 0 0 .4rem; text-transform: uppercase;
               letter-spacing: .04em; opacity: .75; }
.nt .cols ul { margin: 0; padding-left: 1.1rem; font-size: .9rem; }
.nt input[type=text] { width: 100%; font-family: ui-monospace, monospace;
                       font-size: .85rem; padding: .5rem .6rem; box-sizing: border-box; }
.nt .drop { border: 1px dashed var(--line, #55556a); border-radius: 8px;
            padding: 1.1rem; text-align: center; font-size: .9rem; margin: .6rem 0; }
.nt .drop.over { border-color: var(--accent, #b5713a); }
"""


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _rows(base):
    try:
        return object_records.read_collection_records(COLLECTION, base_dir=base)
    except Exception:
        return []


def _public_submission_allowed(base):
    try:
        rows = object_records.read_collection_records(
            SETTINGS_COLLECTION, base_dir=base)
    except Exception:
        return False
    for row in rows:
        if str(row.get("key") or "").strip() == PUBLIC_SUBMISSION_KEY:
            return str(row.get("value") or "").strip().lower() in {
                "1", "true", "yes", "on"}
    return False


def lookup(digest, *, base=None, algorithm=None):
    """The answer, as a plain dict. One fold, three surfaces.

    Tries every supported algorithm when none is named, because a checker
    handed a bare hex string knows what it is only by its length -- and
    refusing to look because they did not say sha512 would be pedantry
    standing between somebody and the fact they came for.
    """
    base = _base_dir() if base is None else base
    rows = _rows(base)
    candidates = ([algorithm] if algorithm
                  else sorted(object_notary.ALGORITHMS))
    for name in candidates:
        normalized = object_notary.normalize_digest(digest, name)
        if not normalized:
            continue
        found = object_notary.first_seen(normalized, rows, name)
        if found is not None:
            return object_notary.receipt(found)
    return object_notary.receipt(None, found=False)


# --- rendering ---------------------------------------------------------------

def _attestation_html(attestation):
    proves = "".join(f"<li>{_esc(line)}</li>"
                     for line in attestation.get("proves") or [])
    denies = "".join(f"<li>{_esc(line)}</li>"
                     for line in attestation.get("does_not_prove") or [])
    rests = "".join(f"<p class=\"note\">{_esc(line)}</p>"
                    for line in attestation.get("rests_on") or [])
    proves_block = (f"<div><h3>What this proves</h3><ul>{proves}</ul></div>"
                    if proves else "")
    return f"""
<div class="cols">
{proves_block}
<div><h3>What it does not prove</h3><ul>{denies}</ul></div>
</div>
{rests}
"""


def _result_html(digest, answer):
    shown = _esc(object_notary.normalize_digest(digest, "sha512")
                 or object_notary.normalize_digest(digest, "sha256")
                 or digest)
    if not answer["found"]:
        return f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/notary">Notary</a>
 / Check</div>
<h1>Not recorded here</h1>
<p class="digest">{shown}</p>
<div class="card">
<p>This server has <strong>no record of that digest</strong>.</p>
<p>That is not evidence of anything. A digest is absent because nobody
lodged it here — which may mean it was lodged with a different notary,
lodged under a different algorithm, or never lodged at all. Absence from
this log proves nothing about the data, its age or its author.</p>
</div>
<p><a href="/notary">Check another, or lodge one</a></p>
"""

    return f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/notary">Notary</a>
 / Check</div>
<h1>Recorded</h1>
<p class="digest">{_esc(answer['digest'])}</p>
<div class="card hit">
<p class="muted">First recorded by this server at</p>
<p class="when"><time datetime="{_esc(answer['first_seen_at'])}">
{_esc(answer['first_seen_at'])}</time></p>
<p class="muted">{_esc(answer['first_seen_at'])} UTC &mdash; the canonical
value, shown because an attestation is evidence and evidence should not
change shape with who is reading it. The line above is the same instant in
your own timezone.</p>
<p class="muted">{_esc(answer['algorithm'])}{
    ' &middot; ' + _esc(answer['label']) if answer.get('label') else ''}</p>
</div>
{_attestation_html(answer['attestation'])}
<p><a href="/notary/{_esc(answer['digest'])}.json">This answer as JSON</a>
 &middot; <a href="/notary">check another</a></p>
"""


def _index_html(base, count):
    open_submission = _public_submission_allowed(base)
    submit_line = (
        "<p>Anyone may lodge a digest with this server.</p>"
        if open_submission else
        "<p>Lodging a digest here requires an account or a service key. "
        "Checking one does not, and never will.</p>")
    return f"""
<div class="breadcrumb"><a href="/">Home</a> / Notary</div>
<h1>Notary</h1>
<p>This server keeps an <strong>append-only list of digests and the moment
each was first seen</strong>. Lodge one now, and at any point in the future
anybody can confirm that the data it was computed from already existed by
then.</p>
<p class="note">It holds <strong>{count:,}</strong> digest{
    '' if count == 1 else 's'} and <strong>no content whatsoever</strong>.
There is no field for a file, no code path that would accept one, and
nothing here worth stealing. That is the design, not a stage it will grow
out of.</p>

<h2>Check a digest</h2>
<form id="check" onsubmit="return goCheck(event)">
<p><input type="text" id="digest" name="digest" autocomplete="off"
   spellcheck="false" placeholder="paste a sha256 or sha512 hex digest"></p>
<p><button type="submit">Check</button></p>
</form>

<div class="drop" id="drop">
<strong>Or drop a file here</strong> to hash it.<br>
<span class="muted">It is hashed by your browser and never uploaded — no
request leaves this machine, and this server never learns anything about
it.</span>
<div><input type="file" id="file"></div>
</div>

<h2>Lodging one</h2>
{submit_line}
<pre><code>POST /objects/action_notarize
{{"digest": "&lt;hex&gt;", "algorithm": "sha256", "label": "optional note"}}</code></pre>
<p>Submitting the same digest twice returns the <strong>original</strong>
record and its original time. A later submission cannot make something
earlier, so nothing here can walk a timestamp forward.</p>

<h2>What this is for</h2>
<ul>
<li>Anchoring the head of a ledger somewhere its own server cannot reach,
so rewriting history means also rewriting a record held elsewhere.</li>
<li>A contract, a photograph before an insurance claim, source code at a
release, experimental data before it is analysed.</li>
<li>Anywhere somebody may later need to say <em>this existed then, and here
is an independent party that saw it</em>.</li>
</ul>

{_attestation_html(object_notary.attestation())}

<script>
function goCheck(event) {{
  event.preventDefault();
  var value = document.getElementById('digest').value.trim();
  if (value) location.href = '/notary/' + encodeURIComponent(value);
  return false;
}}
(function () {{
  var drop = document.getElementById('drop');
  var input = document.getElementById('file');
  var box = document.getElementById('digest');
  if (!drop || !window.crypto || !window.crypto.subtle) {{
    if (drop) drop.innerHTML = '<span class="muted">Your browser cannot ' +
      'hash files locally here (it needs a secure context), so paste a ' +
      'digest above instead. Nothing is uploaded either way.</span>';
    return;
  }}
  function hash(file) {{
    if (!file) return;
    drop.classList.remove('over');
    var reader = new FileReader();
    reader.onload = function () {{
      crypto.subtle.digest('SHA-256', reader.result).then(function (buf) {{
        var hex = Array.prototype.map.call(new Uint8Array(buf), function (b) {{
          return ('00' + b.toString(16)).slice(-2);
        }}).join('');
        box.value = hex;
        box.focus();
      }});
    }};
    reader.readAsArrayBuffer(file);
  }}
  input.addEventListener('change', function () {{ hash(input.files[0]); }});
  ['dragenter', 'dragover'].forEach(function (name) {{
    drop.addEventListener(name, function (e) {{
      e.preventDefault(); drop.classList.add('over');
    }});
  }});
  drop.addEventListener('dragleave', function () {{
    drop.classList.remove('over');
  }});
  drop.addEventListener('drop', function (e) {{
    e.preventDefault();
    hash(e.dataTransfer.files && e.dataTransfer.files[0]);
  }});
}})();
</script>
"""


def _page(title, body):
    return {
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "body": f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap nt">
<header class="app"><h1><a href="/">DBBASIC</a></h1></header>
{body}
</div>
<script src="/nav"></script>
</body>
</html>""",
    }


def GET(request):
    request = request or {}
    base = _base_dir()
    digest = str(request.get("digest") or "").strip()

    wants_json = digest.lower().endswith(".json")
    if wants_json:
        digest = digest[:-len(".json")]

    if not digest:
        return _page("Notary", _index_html(base, len(_rows(base))))

    answer = lookup(digest, base=base)
    if wants_json:
        # 200 either way, including for a digest that is not here. "Not
        # recorded" is a complete and correct answer to the question asked,
        # and a monitor that has to distinguish a 404-meaning-absent from a
        # 404-meaning-broken will eventually get it wrong.
        return {
            "status": 200,
            "content_type": "application/json; charset=utf-8",
            "body": json.dumps(answer, indent=2, sort_keys=True),
        }
    return _page("Notary", _result_html(digest, answer))


def POST(request):
    # Checking a digest is a read however it is spelled. Lodging one is
    # action_notarize, deliberately a different object: a page that both
    # answered questions and wrote rows would be one refactor away from
    # recording a digest somebody only meant to look up.
    return GET(request)
