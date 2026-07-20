"""Single-topic permalink: a topic plus its replies, threaded by parent_id.

Served through a site route like /forum/topics/{topic_id:uuid} — documented
in dbbasic-package.json, not seeded, same precedent as app-notes'
/notes/{note_id:uuid}. The forum is public read (see permissions/rules.json),
so anonymous visitors see the topic and every reply; only signed-in users
get the reply form and, on their own topic/replies, owner-tools (edit,
delete, and the pin/lock/solved/is_solution toggles).

Replies come back flat from /collections/forum_replies/records (same
"fetch the whole collection, filter client-side" pattern app-invoices'
invoice_view.py uses for invoice_lines) and are nested into a tree by
parent_id in this page's own JS — the "small client-side nesting" the
package brief calls for, not a generator feature.

is_pinned/is_locked/is_solved are moderation flags carried faithfully from
the source model, which had no separate moderator queue: the flags *are*
the moderation state. is_locked is enforced here only at the UI layer (the
reply form is hidden on a locked topic) — there is no server-side rule
blocking a reply create against a locked topic's flag, because a
row_filter checks the record being written, not a different collection's
row. Closing that gap is part of the same cross-user-moderation deferral
noted in dbbasic-package.json.
"""

import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_STYLE = """
h1#title { font-size: 1.6rem; margin: 0 0 0.25rem; }
#meta { color: var(--muted); font-size: 0.8rem; margin-bottom: 1.25rem; }
#content { white-space: pre-wrap; word-break: break-word; margin-bottom: 1rem; }
.owner-tools { margin: 1rem 0; display: none; gap: 0.5rem; flex-wrap: wrap; }
textarea.edit, input.edit-title { display: none; margin-top: 0.75rem; }
textarea.edit { min-height: 10rem; }
.replies { margin-top: 1.5rem; }
.reply { border-top: 1px solid var(--line, #38384a); padding: 0.75rem 0; }
.reply .body { white-space: pre-wrap; word-break: break-word; }
.reply .meta { color: var(--muted); font-size: 0.75rem; margin-top: 0.25rem; }
.reply .solution { color: var(--accent, #4caf50); font-weight: 600; }
.reply-children { margin-left: 1.5rem; }
.reply-actions { margin-top: 0.25rem; display: flex; gap: 0.5rem; }
.reply-actions button { font-size: 0.75rem; }
#replyformpanel { margin-top: 1.5rem; display: none; }
#locked-note { margin-top: 1.5rem; color: var(--muted); }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
let topic = null;
let replies = [];

function renderTopic() {
  el("title").textContent = topic.title;
  const bits = [];
  if (topic.is_pinned === "true") bits.push("pinned");
  if (topic.is_locked === "true") bits.push("locked");
  if (topic.is_solved === "true") bits.push("solved");
  bits.push((topic.views || "0") + " views");
  el("meta").textContent = bits.join(" \\u00b7 ");
  el("content").textContent = topic.content;
  const mine = VIEWER_ID && topic.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "flex" : "none";
  if (mine) {
    el("pin-btn").textContent = topic.is_pinned === "true" ? "Unpin" : "Pin";
    el("lock-btn").textContent = topic.is_locked === "true" ? "Unlock" : "Lock";
    el("solve-btn").textContent = topic.is_solved === "true" ? "Mark Unsolved" : "Mark Solved";
  }
  document.title = topic.title;
  const canReply = VIEWER_ID && topic.is_locked !== "true";
  el("replyformpanel").style.display = canReply ? "block" : "none";
  el("locked-note").style.display = (VIEWER_ID && topic.is_locked === "true") ? "block" : "none";
}

function replyNode(r) {
  const children = replies.filter((x) => x.parent_id === r.id);
  const mineReply = VIEWER_ID && r.owner_id === VIEWER_ID;
  const solutionTag = r.is_solution === "true" ? '<span class="solution">&#10003; solution</span> ' : "";
  const solveBtn = mineReply
    ? '<button class="btn" data-act="toggle-solution" data-id="' + esc(r.id) + '">'
      + (r.is_solution === "true" ? "Unmark Solution" : "Mark Solution") + '</button>'
    : "";
  const replyBtn = (VIEWER_ID && topic.is_locked !== "true")
    ? '<button class="btn" data-act="reply-to" data-id="' + esc(r.id) + '">Reply</button>'
    : "";
  const deleteBtn = mineReply
    ? '<button class="btn danger" data-act="delete-reply" data-id="' + esc(r.id) + '">Delete</button>'
    : "";
  return '<div class="reply">'
    + '<div class="body">' + solutionTag + esc(r.content) + '</div>'
    + '<div class="meta">' + esc(r.created_at || "") + '</div>'
    + '<div class="reply-actions">' + replyBtn + solveBtn + deleteBtn + '</div>'
    + (children.length ? '<div class="reply-children">' + children.map(replyNode).join("") + '</div>' : "")
    + '</div>';
}

function renderReplies() {
  const top = replies.filter((r) => !r.parent_id);
  el("replies").innerHTML = top.length
    ? top.map(replyNode).join("")
    : '<p class="hint">No replies yet.</p>';
}

async function loadTopic() {
  const res = await fetch(`/collections/forum_topics/records/${TOPIC_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("body").innerHTML = '<p class="hint">This topic does not exist.</p>';
    el("replies-section").style.display = "none";
    return;
  }
  const body = await res.json();
  topic = body.record || body;
  renderTopic();
  loadReplies();
}

async function loadReplies() {
  const res = await fetch("/collections/forum_replies/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  replies = (body.records || []).filter((r) => r.topic_id === TOPIC_ID);
  renderReplies();
}

async function saveTopic(changes) {
  const res = await fetch(`/collections/forum_topics/records/${TOPIC_ID}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(changes),
  });
  const body = await res.json();
  el("page-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { topic = body.record || topic; renderTopic(); }
  return res.ok;
}

el("edit-btn").addEventListener("click", () => {
  el("edit-title").style.display = "block";
  el("edit-title").value = topic.title;
  el("edit-box").style.display = "block";
  el("edit-box").value = topic.content;
  el("save-btn").style.display = "inline-block";
});
el("save-btn").addEventListener("click", async () => {
  if (await saveTopic({title: el("edit-title").value, content: el("edit-box").value})) {
    el("edit-title").style.display = "none";
    el("edit-box").style.display = "none";
    el("save-btn").style.display = "none";
  }
});
el("pin-btn").addEventListener("click", () => saveTopic({is_pinned: topic.is_pinned === "true" ? "false" : "true"}));
el("lock-btn").addEventListener("click", () => saveTopic({is_locked: topic.is_locked === "true" ? "false" : "true"}));
el("solve-btn").addEventListener("click", () => saveTopic({is_solved: topic.is_solved === "true" ? "false" : "true"}));
el("delete-btn").addEventListener("click", async () => {
  if (!confirm("Delete this topic?")) return;
  const res = await fetch(`/collections/forum_topics/records/${TOPIC_ID}`,
                          {method: "DELETE", credentials: "same-origin",
                           headers: {accept: "application/json"}});
  if (res.ok) window.location = "/forum";
  else el("page-error").textContent = "Delete failed";
});

el("replies").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.act === "reply-to") {
    replyParentId = id;
    el("reply-parent-note").textContent = "Replying to a comment";
    el("reply-parent-note").style.display = "block";
    el("replyformpanel").scrollIntoView({behavior: "smooth"});
  } else if (btn.dataset.act === "toggle-solution") {
    const r = replies.find((x) => x.id === id);
    if (!r) return;
    await fetch(`/collections/forum_replies/records/${id}`, {
      method: "PUT", credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: JSON.stringify({is_solution: r.is_solution === "true" ? "false" : "true"}),
    });
    loadReplies();
  } else if (btn.dataset.act === "delete-reply") {
    if (!confirm("Delete this reply?")) return;
    await fetch(`/collections/forum_replies/records/${id}`,
      {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
    loadReplies();
  }
});

let replyParentId = "";
el("reply-cancel-parent").addEventListener("click", () => {
  replyParentId = "";
  el("reply-parent-note").style.display = "none";
});

async function initReplyForm() {
  await window.dbbasicForm("forum_replies", {
    mount: "#replyformmount", owner: VIEWER_ID,
    onSaved: () => {
      const box = document.querySelector('#replyformmount textarea[name="content"]');
      if (box) box.value = "";
      replyParentId = "";
      el("reply-parent-note").style.display = "none";
      loadReplies();
    },
  });
  // topic_id is a required relation field on forum_replies; the generic
  // form generator has no "prefill and lock a field to the page context"
  // hook, so this page sets and hides it after render instead of
  // building a bespoke create form for two fields -- same trade-off
  // app-invoices' invoice_view.py makes for invoice_lines.invoice_id.
  const topicField = document.querySelector('#replyformmount select[name="topic_id"]');
  if (topicField) {
    topicField.value = TOPIC_ID;
    const wrapper = topicField.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
  const parentField = document.querySelector('#replyformmount select[name="parent_id"]');
  if (parentField) {
    const wrapper = parentField.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
  const form = document.querySelector("#replyformmount form");
  if (form) {
    form.addEventListener("submit", () => {
      if (parentField) parentField.value = replyParentId;
    }, true);
  }
}

loadTopic();
if (VIEWER_ID) initReplyForm();

// Realtime: auto-refresh when either collection changes (another tab,
// user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadTopic, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("forum_topics", reload);
      window.dbbasicSubscribe("forum_replies", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    topic_id = str(request.get("topic_id") or "").strip()
    if topic_id and not _RECORD_ID_RE.fullmatch(topic_id):
        topic_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_forum_topic served", topic_id=topic_id or "missing",
                 user_id=user_id or "anonymous")

    if not topic_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Topic not found. <a href='/forum'>Back to forum</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/forum/topics/{topic_id}">sign in</a>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Topic</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><a href="/forum">Forum</a><div class="who">{who}</div></header>
<div id="body">
<h1 id="title">loading&hellip;</h1>
<div class="meta" id="meta"></div>
<div id="content"></div>
</div>
<input class="edit-title" id="edit-title">
<textarea class="edit" id="edit-box"></textarea>
<div class="owner-tools" id="owner-tools">
<button id="edit-btn" class="btn">Edit</button>
<button id="save-btn" class="btn primary" style="display:none">Save</button>
<button id="pin-btn" class="btn">Pin</button>
<button id="lock-btn" class="btn">Lock</button>
<button id="solve-btn" class="btn">Mark Solved</button>
<button id="delete-btn" class="btn danger">Delete</button>
</div>
<div class="error" id="page-error"></div>
<div class="replies" id="replies-section">
<h3>Replies</h3>
<div id="replies"><div class="state">loading&hellip;</div></div>
<p id="locked-note" style="display:none">This topic is locked; no new replies can be posted.</p>
<div id="replyformpanel">
<h3>Add Reply</h3>
<p id="reply-parent-note" style="display:none">Replying to a comment
  <button class="btn" id="reply-cancel-parent" type="button">(reply to topic instead)</button>
</p>
<div id="replyformmount"></div>
</div>
</div>
</div>
<script>const TOPIC_ID = {topic_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
