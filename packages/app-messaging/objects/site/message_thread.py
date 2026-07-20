"""Single-thread detail page: a thread plus its messages, oldest first.

Served through a site route like /inbox/{thread_id:uuid} — documented in
dbbasic-package.json, not seeded, same precedent as app-invoices'
/invoices/{invoice_id:uuid}. A mailbox is private (see
permissions/rules.json: owner-scoped CRUD, no public read on any
collection in this package) — unlike app-forum's topic permalink, an
anonymous visitor here never sees thread or message content; the
row-filtered collection API returns nothing for someone who is not the
owner, so this page only ever renders real data for the signed-in owner.

Messages come back flat from /collections/messages/records and are
filtered to this thread client-side — the same "fetch the whole
collection, filter client-side" pattern app-invoices' invoice_view.py uses
for invoice_lines and app-forum's forum_topic.py uses for forum_replies.
Unlike forum_topic's replies, messages are NOT self-threaded (no
parent_id/nesting) — the source model's messages were a flat chronological
list within a thread, not a nested reply tree, so this page renders them
in one list ordered oldest first.

v1 is read-only viewing plus mark-read: star/archive/trash toggles and a
per-message read/unread toggle, all via direct PUTs, same button-toggle
style as forum_topic.py's pin/lock/solved. Composing/sending a real
message is deferred (no IMAP/SMTP transport — see dbbasic-package.json);
the "Save Draft" panel creates a message_drafts row scoped to this thread
(thread_id preset and hidden, same trade-off app-invoices' invoice_view.py
makes for invoice_lines.invoice_id) and makes explicit that nothing is
sent.
"""

import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_STYLE = """
h1#subject { font-size: 1.6rem; margin: 0 0 0.25rem; }
#meta { color: var(--muted); font-size: 0.8rem; margin-bottom: 1.25rem; }
.owner-tools { margin: 1rem 0; display: flex; gap: 0.5rem; flex-wrap: wrap; }
.messages { margin-top: 1.5rem; }
.msg { border-top: 1px solid var(--line, #38384a); padding: 0.75rem 0; }
.msg .head { display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.msg .from { font-weight: 600; }
.msg .dir { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }
.msg .when { color: var(--muted); font-size: 0.75rem; }
.msg .subject { color: var(--muted); font-size: 0.85rem; margin-top: 0.15rem; }
.msg .body { white-space: pre-wrap; word-break: break-word; margin-top: 0.5rem; }
.msg.unread { border-left: 3px solid var(--accent, #4caf50); padding-left: 0.5rem; }
.msg-actions { margin-top: 0.4rem; display: flex; gap: 0.5rem; }
.msg-actions button { font-size: 0.75rem; }
#draftformpanel { margin-top: 1.5rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
let thread = null;
let messages = [];

function renderThread() {
  el("subject").textContent = thread.subject || "(no subject)";
  const bits = [thread.thread_type];
  if (thread.participant_summary) bits.push(thread.participant_summary);
  if (thread.is_starred === "true") bits.push("starred");
  if (thread.is_archived === "true") bits.push("archived");
  if (thread.is_trashed === "true") bits.push("trashed");
  bits.push((thread.message_count || "0") + " messages");
  el("meta").textContent = bits.filter(Boolean).join(" \\u00b7 ");
  const mine = VIEWER_ID && thread.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "flex" : "none";
  if (mine) {
    el("read-btn").textContent = thread.is_read === "true" ? "Mark Unread" : "Mark Read";
    el("star-btn").textContent = thread.is_starred === "true" ? "Unstar" : "Star";
    el("archive-btn").textContent = thread.is_archived === "true" ? "Unarchive" : "Archive";
    el("trash-btn").textContent = thread.is_trashed === "true" ? "Restore" : "Move to Trash";
  }
  document.title = thread.subject || "Thread";
}

function msgNode(m) {
  const mine = VIEWER_ID && m.owner_id === VIEWER_ID;
  const unread = m.is_read !== "true";
  const readBtn = mine
    ? '<button class="btn" data-act="toggle-read" data-id="' + esc(m.id) + '">'
      + (unread ? "Mark Read" : "Mark Unread") + '</button>'
    : "";
  const when = m.received_at || m.sent_at || m.created_at || "";
  return '<div class="msg' + (unread ? " unread" : "") + '">'
    + '<div class="head"><span class="from">' + esc(m.from_address || "") + '</span>'
    + '<span class="dir">' + esc(m.direction || "") + '</span>'
    + '<span class="when">' + esc(when) + '</span></div>'
    + (m.subject ? '<div class="subject">' + esc(m.subject) + '</div>' : "")
    + '<div class="body">' + esc(m.body_text || "") + '</div>'
    + '<div class="msg-actions">' + readBtn + '</div>'
    + '</div>';
}

function renderMessages() {
  const sorted = messages.slice().sort((a, b) =>
    String(a.created_at || "").localeCompare(String(b.created_at || "")));
  el("messages").innerHTML = sorted.length
    ? sorted.map(msgNode).join("")
    : '<p class="hint">No messages in this thread yet.</p>';
}

async function loadThread() {
  const res = await fetch(`/collections/message_threads/records/${THREAD_ID}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("body").innerHTML = VIEWER_ID
      ? '<p class="hint">This thread does not exist or is not yours.</p>'
      : `<p class="hint"><a href="/login?next=/inbox/${THREAD_ID}">Sign in</a> to view this thread.</p>`;
    el("messages-section").style.display = "none";
    return;
  }
  const body = await res.json();
  thread = body.record || body;
  renderThread();
  loadMessages();
}

async function loadMessages() {
  const res = await fetch("/collections/messages/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  messages = (body.records || []).filter((m) => m.thread_id === THREAD_ID);
  renderMessages();
}

async function saveThread(changes) {
  const res = await fetch(`/collections/message_threads/records/${THREAD_ID}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify(changes),
  });
  const body = await res.json();
  el("page-error").textContent = res.ok ? "" : (body.error || "Save failed");
  if (res.ok) { thread = body.record || thread; renderThread(); }
  return res.ok;
}

el("read-btn").addEventListener("click", () => saveThread({is_read: thread.is_read === "true" ? "false" : "true"}));
el("star-btn").addEventListener("click", () => saveThread({is_starred: thread.is_starred === "true" ? "false" : "true"}));
el("archive-btn").addEventListener("click", () => saveThread({is_archived: thread.is_archived === "true" ? "false" : "true"}));
el("trash-btn").addEventListener("click", () => saveThread({is_trashed: thread.is_trashed === "true" ? "false" : "true"}));

el("messages").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn || btn.dataset.act !== "toggle-read") return;
  const id = btn.dataset.id;
  const m = messages.find((x) => x.id === id);
  if (!m) return;
  await fetch(`/collections/messages/records/${id}`, {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({is_read: m.is_read === "true" ? "false" : "true"}),
  });
  loadMessages();
});

async function initDraftForm() {
  await window.dbbasicForm("message_drafts", {
    mount: "#draftformmount", owner: VIEWER_ID,
    onSaved: () => {
      const box = document.querySelector('#draftformmount textarea[name="body_text"]');
      if (box) box.value = "";
    },
  });
  // thread_id is a relation field on message_drafts; the generic form
  // generator has no "prefill and lock a field to the page context" hook,
  // so this page sets and hides it after render instead of building a
  // bespoke create form for one field -- same trade-off app-forum's
  // forum_topic.py makes for forum_replies.topic_id.
  const field = document.querySelector('#draftformmount select[name="thread_id"]');
  if (field) {
    field.value = THREAD_ID;
    const wrapper = field.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
}

loadThread();
if (VIEWER_ID) initDraftForm();

// Realtime: auto-refresh when either collection changes (another tab,
// user, or agent).
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadThread, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("message_threads", reload);
      window.dbbasicSubscribe("messages", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    thread_id = str(request.get("thread_id") or "").strip()
    if thread_id and not _RECORD_ID_RE.fullmatch(thread_id):
        thread_id = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_message_thread served", thread_id=thread_id or "missing",
                 user_id=user_id or "anonymous")

    if not thread_id:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Thread not found. <a href='/inbox'>Back to inbox</a></p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/inbox/{thread_id}">sign in</a>'
    )
    draft_form_html = (
        '<div id="draftformmount"></div>'
        if user_id
        else f'<p class="hint"><a href="/login?next=/inbox/{thread_id}">Sign in</a> to save a draft reply.</p>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thread</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><a href="/inbox">Inbox</a><div class="who">{who}</div></header>
<div id="body">
<h1 id="subject">loading&hellip;</h1>
<div class="meta" id="meta"></div>
<div class="owner-tools" id="owner-tools" style="display:none">
<button id="read-btn" class="btn">Mark Read</button>
<button id="star-btn" class="btn">Star</button>
<button id="archive-btn" class="btn">Archive</button>
<button id="trash-btn" class="btn danger">Move to Trash</button>
</div>
<div class="error" id="page-error"></div>
<div class="messages" id="messages-section">
<h3>Messages</h3>
<div id="messages"><div class="state">loading&hellip;</div></div>
<div id="draftformpanel">
<h3>Save Draft</h3>
<p class="hint">Saves a draft only -- nothing is sent. Composing/sending needs mail
transport (IMAP/SMTP), which this package does not build.</p>
{draft_form_html}
</div>
</div>
</div>
</div>
<script>const THREAD_ID = {thread_id!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
