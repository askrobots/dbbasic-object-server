"""Mail Accounts page -- the signed-in person's IMAP mailboxes, how each
one's sync is going, and whether its password is set.

The password is the one field the generic form cannot show, because it is
not a field: it lives in the owner's write-only identity vault as
``imap-<account id>`` (docs/secrets-and-credentials.md). So this page does
the one thing the generic renderer cannot: it asks the vault which services
are SET (never their values) and renders "set" / "not set", with an input
that PUTs straight to /identity/users/{you}/service-keys. The value goes
from the browser to the vault and never passes through a record, this
page's HTML, or a response.

Saving a password also zeroes the account's sync_attempts, so an account
that was waiting after failed logins tries again on the next pass instead
of after its backoff -- the person who just fixed the password is the one
person certain the next try is worth making.

Everything else (add, edit) is the generic form over mail_accounts, and the
page reads only rows whose owner_id is the signed-in user.
"""

import html
import json
import os

import object_feature_flags
import object_mail
import object_records
import object_service_keys

_SCRIPT = """
const panel = document.getElementById("accountpanel");
function openForm(record) {
  document.getElementById("paneltitle").textContent = record ? "Edit account" : "Add account";
  panel.style.display = "block";
  window.dbbasicForm("mail_accounts", {
    mount: "#formmount", owner: OWNER_ID, record: record || undefined,
    onSaved: () => { location.reload(); },
    onCancel: () => { panel.style.display = "none"; },
  });
}
document.getElementById("addaccount").addEventListener("click", () => openForm(null));
document.querySelectorAll("[data-edit]").forEach((btn) => btn.addEventListener("click", () =>
  openForm(ACCOUNTS.find((a) => a.id === btn.dataset.edit))));
document.querySelectorAll("form[data-password]").forEach((form) => form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = form.dataset.password;
  const input = form.querySelector("input[type=password]");
  const note = form.querySelector("[data-note]");
  if (!input.value) { note.textContent = "Type the password first."; return; }
  const res = await fetch("/identity/users/" + encodeURIComponent(OWNER_ID) + "/service-keys", {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({service: "imap-" + id, key: input.value})});
  input.value = "";
  if (!res.ok) { note.textContent = "Not saved (" + res.status + ")."; return; }
  await fetch("/collections/mail_accounts/records/" + encodeURIComponent(id), {
    method: "PUT", credentials: "same-origin",
    headers: {"content-type": "application/json", accept: "application/json"},
    body: JSON.stringify({sync_attempts: "0"})});
  note.textContent = "Saved. The next sync uses it.";
  const state = form.closest(".account").querySelector("[data-pwstate]");
  if (state) state.textContent = "set";
}));
"""


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _js(value):
    """JSON for inside a <script> element: "</" escaped so no value can
    close the element early."""
    return json.dumps(value).replace("</", "<\\/")


def _status(account):
    """One honest sentence about where this account's sync stands."""
    if _text(account.get("enabled")).lower() in {"false", "0", "no", "off"}:
        return "Not syncing: turned off for this account."
    attempts = _text(account.get("sync_attempts")) or "0"
    error = _text(account.get("sync_error"))
    if error and attempts not in {"", "0"}:
        return f"Failing ({attempts} tries in a row): {error}"
    last = _text(account.get("last_synced_at"))
    return f"Up to date as of {last}." if last else "Not synced yet."


def _card(account, password_set):
    e = html.escape
    account_id = e(account["id"])
    title = e(_text(account.get("label")) or _text(account.get("email_address")))
    detail = e(f"{_text(account.get('email_address'))} on {_text(account.get('host'))} "
               f"({_text(account.get('security')) or 'ssl'})")
    folders = e(_text(account.get("folders")) or "INBOX and Sent, found on the first sync")
    return f"""
<div class="account card" style="margin-bottom:1rem">
  <div class="pagehead"><h2 style="font-size:1rem;margin:0">{title}</h2>
    <button class="btn" data-edit="{account_id}">Edit</button></div>
  <p class="hint">{detail}. Folders: {folders}.</p>
  <p>{e(_status(account))}</p>
  <form class="stack" data-password="{account_id}">
    <label>Password: <strong data-pwstate>{"set" if password_set else "not set"}</strong></label>
    <input type="password" autocomplete="new-password" placeholder="{"Replace the password" if password_set else "The mailbox password"}">
    <div><button class="btn primary" type="submit">Save password</button> <span class="hint" data-note></span></div>
  </form>
</div>"""


def GET(request):
    identity = request.get("_identity", {}) or {}
    user_id = identity.get("user_id")
    if not user_id:
        body = ('<p class="hint"><a href="/login?next=/mail/accounts">Sign in</a> '
                'to manage your mail accounts.</p>')
        script = ""
    else:
        base = _base_dir()
        try:
            rows = object_records.read_collection_records("mail_accounts", base_dir=base)
        except (LookupError, OSError, ValueError):
            rows = None
        if rows is None:
            body = '<p class="hint">Mail sync is not installed on this server.</p>'
            script = ""
        else:
            accounts = [row for row in rows if _text(row.get("owner_id")) == str(user_id)]
            try:
                services = {entry["service"] for entry in
                            object_service_keys.list_service_key_status(str(user_id), base_dir=base)}
            except (object_service_keys.InvalidServiceKeyError, OSError):
                services = set()
            off = object_feature_flags.refusal(
                env_name=object_mail.ENABLE_ENV, flag=object_mail.KILL_SWITCH_FLAG, base_dir=base)
            notice = (f'<p class="error">Mail sync is {html.escape(off)}. Accounts can be set up, '
                      f'but nothing is copied until it is on.</p>') if off else ""
            cards = "".join(_card(account, object_mail.password_service(account["id"]) in services)
                            for account in accounts)
            body = f"""
<div class="breadcrumb"><a href="/">Home</a> / <a href="/inbox">Inbox</a> / Mail accounts</div>
<div class="pagehead"><h1>Mail accounts</h1><button class="btn primary" id="addaccount">+ Add account</button></div>
<p class="hint">New mail in these mailboxes is copied into your <a href="/inbox">inbox</a> every few
minutes. Nothing is sent, deleted or marked read on the mail server. Passwords are kept where no page or
record can read them back; this page only knows whether one is set.</p>
{notice}
<div id="accountpanel" style="display:none; margin-bottom:1rem">
  <h2 id="paneltitle" style="font-size:1rem; margin:0 0 0.5rem">Add account</h2>
  <div id="formmount"></div>
</div>
{cards or '<p class="hint">No mail accounts yet.</p>'}
"""
            # The rows hold no secret: the password is not a field.
            script = (
                f"<script>const OWNER_ID = {_js(str(user_id))};"
                f"const ACCOUNTS = {_js(accounts)};</script>"
                '<script src="/form"></script>'
                f"<script>{_SCRIPT}</script>"
            )

    who = (f"signed in as <strong>{html.escape(str(user_id))}</strong>" if user_id
           else '<a href="/login?next=/mail/accounts">sign in</a>')
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mail accounts</title>
<link rel="stylesheet" href="/style">
</head>
<body>
<div class="wrap">
<header class="app"><h1><a href="/">DBBASIC</a></h1><div class="who">{who}</div></header>
{body}
</div>
{script}
<script src="/nav"></script>
</body>
</html>"""
    return {"content_type": "text/html; charset=utf-8", "body": page}
