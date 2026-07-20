"""Public creator profile: bio/skills/experience + follow button + moderated
guestbook -- a faithful port of the source system's public profile page.

Served through a site route like /u/{username} -> site_profile -- documented
here, not seeded, same precedent as app-notes' /notes/{note_id:uuid} and
app-forum's /forum/topics/{topic_id:uuid}. One difference from those: the
pattern segment here is plain {username}, NOT {username:uuid}. profiles.id is
the id of the account the profile belongs to (schemas/profiles.json: "one
profile per user"), and account ids in this codebase are not guaranteed to be
UUID-shaped (docs/permissions-model.md's own examples use plain ids like
"7") -- unlike most record ids here, which default to UUIDv4. So the route
pattern must accept any single path segment, not just a UUID one. There is
no separate "username" field or accounts collection in this codebase; the
path segment IS the account/profile id. Calling the route param "username"
matches the source system's URL shape even though what it captures is the
account id.

The page is public read (see permissions/rules.json): anonymous visitors see
the profile and its approved guestbook comments. Only a signed-in visitor
gets the follow button, and only a signed-in visitor who is not the profile
owner gets one that does anything (following your own profile is not a
concept this package builds). The add-comment form is signed-in v1 only --
see schemas/profile_comments.json and dbbasic-package.json for why anonymous
guestbook posting is deferred.

Aggregation of the profile owner's other public content (articles, links,
notes, files, templates in the source) is a nice-to-have per the package
brief, not the core of this port. This page includes ONE such aggregation --
recent public articles by this user, fetched opportunistically from
app-articles' collection -- and nothing else. It is opportunistic, not a
dependency: this package declares no dependency on app-articles in
dbbasic-package.json, and the fetch below fails silently (the section is
simply omitted) if that collection doesn't exist in this install. Notes,
links, files, and templates aggregation are left for a later slice -- see
dbbasic-package.json's deferred list.
"""

import re

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_STYLE = """
h1#name { font-size: 1.6rem; margin: 0 0 0.25rem; }
#meta { color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; }
.field-row { margin: 0.35rem 0; }
.field-row .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }
.field-row .value { white-space: pre-wrap; word-break: break-word; }
#social a { display: inline-block; margin: 0.15rem 0.5rem 0.15rem 0; }
.owner-tools { margin: 1rem 0; display: none; gap: 0.5rem; flex-wrap: wrap; }
#follow-area { margin: 1rem 0; }
.guestbook { margin-top: 2rem; }
.comment { border-top: 1px solid var(--line, #38384a); padding: 0.75rem 0; }
.comment .body { white-space: pre-wrap; word-break: break-word; }
.comment .meta { color: var(--muted); font-size: 0.75rem; margin-top: 0.25rem; }
.comment .pending-note { color: var(--muted); font-size: 0.75rem; font-style: italic; }
#commentformpanel { margin-top: 1rem; }
.articles-section { margin-top: 2rem; }
.articles-section ul { padding-left: 1.25rem; }
"""

_SCRIPT = """
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const el = (id) => document.getElementById(id);
let profile = null;

function socialLinksHtml(raw) {
  if (!raw) return "";
  let urls = [];
  const trimmed = String(raw).trim();
  if (trimmed.startsWith("[")) {
    try { urls = JSON.parse(trimmed); } catch (e) { urls = []; }
  }
  if (!urls.length) urls = trimmed.split(/\\r?\\n/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) return "";
  return urls.map((u) => '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' + esc(u) + '</a>').join("");
}

function renderProfile() {
  el("name").textContent = profile.display_name || PROFILE_ID;
  document.title = profile.display_name || PROFILE_ID;
  el("bio").textContent = profile.bio || "";
  el("bio-row").style.display = profile.bio ? "block" : "none";

  const rows = [
    ["skills", "Skills"], ["experience", "Experience"], ["location", "Location"],
    ["education", "Education"], ["website", "Website"],
  ];
  for (const [field, label] of rows) {
    const row = el("row-" + field);
    if (!row) continue;
    const value = profile[field] || "";
    row.style.display = value ? "block" : "none";
    const valueEl = row.querySelector(".value");
    if (valueEl) valueEl.textContent = value;
  }

  const social = socialLinksHtml(profile.social_links);
  el("social-row").style.display = social ? "block" : "none";
  el("social").innerHTML = social;

  const mine = VIEWER_ID && profile.owner_id === VIEWER_ID;
  el("owner-tools").style.display = mine ? "flex" : "none";
}

async function loadProfile() {
  const res = await fetch(`/collections/profiles/records/${encodeURIComponent(PROFILE_ID)}`,
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) {
    el("body").innerHTML = '<p class="hint">No profile at this address.</p>';
    el("follow-area").style.display = "none";
    el("guestbook").style.display = "none";
    el("articles-section").style.display = "none";
    return;
  }
  const body = await res.json();
  profile = body.record || body;
  renderProfile();
  loadFollowState();
  loadComments();
  loadArticles();
}

// --- Follow / unfollow -----------------------------------------------
let myFollowEdgeId = null;

async function loadFollowState() {
  if (!VIEWER_ID || VIEWER_ID === PROFILE_ID) { el("follow-area").style.display = "none"; return; }
  const res = await fetch("/collections/follows/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const mine = (body.records || []).find(
    (r) => r.follower_id === VIEWER_ID && r.following_id === PROFILE_ID);
  myFollowEdgeId = mine ? mine.id : null;
  renderFollowButton();
}

function renderFollowButton() {
  el("follow-area").style.display = "block";
  el("follow-btn").textContent = myFollowEdgeId ? "Unfollow" : "Follow";
}

el("follow-btn").addEventListener("click", async () => {
  if (myFollowEdgeId) {
    await fetch(`/collections/follows/records/${encodeURIComponent(myFollowEdgeId)}`,
      {method: "DELETE", credentials: "same-origin", headers: {accept: "application/json"}});
    myFollowEdgeId = null;
  } else {
    const res = await fetch("/collections/follows/records", {
      method: "POST", credentials: "same-origin",
      headers: {"content-type": "application/json", accept: "application/json"},
      body: JSON.stringify({follower_id: VIEWER_ID, following_id: PROFILE_ID}),
    });
    const body = await res.json();
    if (res.ok) myFollowEdgeId = (body.record || body).id;
  }
  renderFollowButton();
});

// --- Guestbook ----------------------------------------------------------
function commentNode(c) {
  const mine = VIEWER_ID && c.owner_id === VIEWER_ID;
  const pendingNote = (mine && c.status !== "approved")
    ? '<div class="pending-note">' + esc(c.status) + " -- only you can see this until it is approved</div>"
    : "";
  return '<div class="comment">'
    + '<div class="body">' + esc(c.content) + '</div>'
    + '<div class="meta">' + esc(c.author_name || (mine ? "you" : "")) + " &middot; " + esc(c.created_at || "") + '</div>'
    + pendingNote
    + '</div>';
}

async function loadComments() {
  const res = await fetch("/collections/profile_comments/records?limit=500",
                          {credentials: "same-origin", headers: {accept: "application/json"}});
  if (!res.ok) return;
  const body = await res.json();
  const mine = (body.records || []).filter((c) => c.profile_id === PROFILE_ID);
  const visible = mine.filter((c) => c.status === "approved" || (VIEWER_ID && c.owner_id === VIEWER_ID));
  el("comments").innerHTML = visible.length
    ? visible.map(commentNode).join("")
    : '<p class="hint">No guestbook comments yet.</p>';
}

async function initCommentForm() {
  await window.dbbasicForm("profile_comments", {
    mount: "#commentformmount", owner: VIEWER_ID,
    onSaved: () => {
      const box = document.querySelector('#commentformmount textarea[name="content"]');
      if (box) box.value = "";
      loadComments();
    },
  });
  // profile_id is a required relation field on profile_comments; the generic
  // form generator has no "prefill and lock a field to the page context"
  // hook, so this page sets and hides it after render instead of building a
  // bespoke create form for one field -- same trade-off app-forum's
  // forum_topic.py makes for forum_replies.topic_id.
  const profileField = document.querySelector('#commentformmount select[name="profile_id"]');
  if (profileField) {
    profileField.value = PROFILE_ID;
    const wrapper = profileField.closest(".field");
    if (wrapper) wrapper.dataset.hidden = "true";
  }
}

// --- Recent public articles (opportunistic, no hard dependency) --------
async function loadArticles() {
  let res;
  try {
    res = await fetch("/collections/articles/records?limit=500",
                      {credentials: "same-origin", headers: {accept: "application/json"}});
  } catch (e) { el("articles-section").style.display = "none"; return; }
  if (!res.ok) { el("articles-section").style.display = "none"; return; }
  const body = await res.json();
  const mine = (body.records || []).filter(
    (a) => a.owner_id === PROFILE_ID && a.is_public === "true");
  if (!mine.length) { el("articles-section").style.display = "none"; return; }
  el("articles-section").style.display = "block";
  el("articles-list").innerHTML = mine.slice(0, 10)
    .map((a) => '<li><a href="/articles/' + esc(a.id) + '">' + esc(a.title) + '</a></li>')
    .join("");
}

loadProfile();
if (VIEWER_ID) initCommentForm();

// Realtime: auto-refresh when any of these collections change.
(function () {
  let _lt = null;
  const reload = () => { clearTimeout(_lt); _lt = setTimeout(loadProfile, 150); };
  (function wait() {
    if (window.dbbasicSubscribe) {
      window.dbbasicSubscribe("profiles", reload);
      window.dbbasicSubscribe("follows", reload);
      window.dbbasicSubscribe("profile_comments", reload);
    } else setTimeout(wait, 300);
  })();
})();
"""


def GET(request):
    username = str(request.get("username") or "").strip()
    if username and not _RECORD_ID_RE.fullmatch(username):
        username = ""
    identity = request.get("_identity", {})
    user_id = identity.get("user_id")
    _logger.info("site_profile served", username=username or "missing",
                 user_id=user_id or "anonymous")

    if not username:
        return {
            "content_type": "text/html; charset=utf-8",
            "body": "<p>Profile not found.</p>",
            "status": 404,
        }

    who = (
        f"signed in as <strong>{user_id}</strong>"
        if user_id
        else f'<a href="/login?next=/u/{username}">sign in</a>'
    )
    comment_form_html = (
        '<div id="commentformmount"></div>'
        if user_id
        else f'<p class="hint"><a href="/login?next=/u/{username}">Sign in</a> to leave a guestbook comment.</p>'
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile</title>
<link rel="stylesheet" href="/style">
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap narrow">
<header class="app"><a href="/">DBBASIC</a><div class="who">{who}</div></header>
<div id="body">
<h1 id="name">loading&hellip;</h1>
<div class="owner-tools" id="owner-tools">
<a class="btn" href="/profile/edit">Edit Profile</a>
</div>
<div class="field-row" id="bio-row" style="display:none">
<div id="bio"></div>
</div>
<div class="field-row" id="row-skills" style="display:none"><div class="label">Skills</div><div class="value"></div></div>
<div class="field-row" id="row-experience" style="display:none"><div class="label">Experience</div><div class="value"></div></div>
<div class="field-row" id="row-location" style="display:none"><div class="label">Location</div><div class="value"></div></div>
<div class="field-row" id="row-education" style="display:none"><div class="label">Education</div><div class="value"></div></div>
<div class="field-row" id="row-website" style="display:none"><div class="label">Website</div><div class="value"></div></div>
<div class="field-row" id="social-row" style="display:none"><div class="label">Links</div><div id="social"></div></div>
</div>
<div id="follow-area" style="display:none">
<button class="btn primary" id="follow-btn">Follow</button>
</div>
<div class="articles-section" id="articles-section" style="display:none">
<h3>Recent Public Articles</h3>
<ul id="articles-list"></ul>
</div>
<div class="guestbook" id="guestbook">
<h3>Guestbook</h3>
<div id="comments"><div class="state">loading&hellip;</div></div>
<div id="commentformpanel">
{comment_form_html}
</div>
</div>
</div>
<script>const PROFILE_ID = {username!r}; const VIEWER_ID = {(user_id or "")!r};</script>
<script src="/form"></script>
<script>{_SCRIPT}</script>
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": html}
