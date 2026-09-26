"""system_mail_sync against a fake IMAP server.

The fake is injected where the real one would be -- imaplib.IMAP4_SSL and
imaplib.IMAP4, plus socket.getaddrinfo for the host check -- the way the
email outbox tests swap object_email.sender_for. It records every command,
so the claims that matter can be asserted as facts about the conversation
rather than inferred from the rows: the folder was EXAMINEd, every body was
fetched with PEEK, nothing was ever STOREd, APPENDed or EXPUNGEd.

The lessons the predecessor taught are each a test here, named for the
claim: mail is never marked read; the same Message-ID in two people's
mailboxes is two rows; a reply never joins another person's thread; a
failure is written on the account and counted, never swallowed; a link in
a header is stored and never fetched; and a pass that dies half way loses
nothing and duplicates nothing.

An OPTIONAL test at the bottom talks to a real IMAP server, and is skipped
unless DBBASIC_TEST_IMAP=host:port:user:password names one.
"""

import imaplib
import json
import os
import shutil
import socket
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
from conftest import stage_collection

import object_execution
import object_mail
import object_records
import object_service_keys
import python_object_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "packages" / "app-mail-sync"
SOURCE = PACKAGE / "objects" / "system" / "mail_sync.py"
RUNTIME = python_object_runtime.PythonObjectRuntime()

NOW = datetime.now(timezone.utc)
PASSWORD = "correct horse battery"


# --- the fake server ------------------------------------------------------------

def internaldate(moment):
    return object_mail.imap_date(moment) + moment.strftime(" %H:%M:%S +0000")


class FakeServer:
    """Mailboxes by (user, folder). Each message: uid, raw, flags, date."""

    def __init__(self):
        self.users = {}
        self.boxes = {}
        self.uidvalidity = {}
        self.commands = []
        self.timeouts = []
        self.connections = 0
        self.fail_fetch_uid = None
        self.sent_folder = "Sent"
        self.send_uidnext = True

    def user(self, name, password=PASSWORD):
        self.users[name] = password
        self.boxes.setdefault((name, "INBOX"), [])
        self.boxes.setdefault((name, self.sent_folder), [])
        self.uidvalidity.setdefault((name, "INBOX"), 1)
        self.uidvalidity.setdefault((name, self.sent_folder), 1)

    def deliver(self, user, raw, *, folder="INBOX", flags=(), when=None, size=None):
        box = self.boxes.setdefault((user, folder), [])
        self.uidvalidity.setdefault((user, folder), 1)
        uid = (max((m["uid"] for m in box), default=0)) + 1
        box.append({"uid": uid, "raw": raw, "flags": list(flags),
                    "when": when or NOW - timedelta(hours=1), "size": size})
        return uid

    def connect(self, host, port, timeout=None, **_):
        self.connections += 1
        self.timeouts.append(timeout)
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, server):
        self.server = server
        self.user = None
        self.box = None
        self.readonly = None
        self.pending = {}

    def _log(self, *parts):
        self.server.commands.append(" ".join(str(p) for p in parts))

    def starttls(self, ssl_context=None):
        self._log("STARTTLS")

    def login(self, user, password):
        self._log("LOGIN", user)
        if self.server.users.get(user) != password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        self.user = user
        return "OK", [b"logged in"]

    def logout(self):
        self._log("LOGOUT")
        return "BYE", [b""]

    def list(self, directory='""', pattern="*"):
        self._log("LIST")
        lines = []
        for (user, folder) in self.server.boxes:
            if user != self.user:
                continue
            attrs = "\\HasNoChildren \\Sent" if folder == self.server.sent_folder else "\\HasNoChildren"
            lines.append(f'({attrs}) "/" "{folder}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        name = mailbox[1:-1] if mailbox.startswith('"') else mailbox
        self._log("EXAMINE" if readonly else "SELECT", name)
        key = (self.user, name)
        if key not in self.server.boxes:
            return "NO", [b"Mailbox does not exist"]
        self.box = key
        self.readonly = readonly
        messages = self.server.boxes[key]
        self.pending = {"UIDVALIDITY": [str(self.server.uidvalidity[key]).encode()]}
        if self.server.send_uidnext:
            self.pending["UIDNEXT"] = [
                str(max((m["uid"] for m in messages), default=0) + 1).encode()]
        return "OK", [str(len(messages)).encode()]

    def response(self, code):
        return code, self.pending.pop(code, [None])

    def _messages(self):
        return self.server.boxes[self.box]

    def uid(self, command, *args):
        self._log("UID", command, *args)
        command = command.upper()
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self._search(args)).encode()]
        if command == "FETCH":
            return self._fetch(args[0], args[1])
        raise AssertionError(f"the sync issued UID {command}, which it must never do")

    def _search(self, args):
        messages = self._messages()
        if args[0] == "ALL":
            return [m["uid"] for m in messages]
        if args[0] == "SINCE":
            day, month, year = args[1].split("-")
            since = datetime(int(year), object_mail._MONTHS.index(month) + 1, int(day),
                             tzinfo=timezone.utc)
            return [m["uid"] for m in messages if m["when"] >= since]
        if args[0] == "UID":
            low = int(args[1].split(":")[0])
            found = [m["uid"] for m in messages if m["uid"] >= low]
            # Real servers answer n:* with the highest message even when n
            # is past it. The sync must not import that message again.
            if not found and messages:
                found = [messages[-1]["uid"]]
            return found
        raise AssertionError(f"unexpected SEARCH {args}")

    def _fetch(self, uid_set, items):
        wanted = {int(u) for u in uid_set.split(",")}
        data = []
        for m in self._messages():
            if m["uid"] not in wanted:
                continue
            if "BODY.PEEK[" in items:
                if self.server.fail_fetch_uid == m["uid"]:
                    raise OSError("connection reset by peer")
                raw = m["raw"]
                if "HEADER" in items:
                    raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                data.append((f"{m['uid']} (UID {m['uid']} BODY[] {{{len(raw)}}}".encode(), raw))
                data.append(b")")
            elif "BODY[" in items or "RFC822" in items.replace("RFC822.SIZE", ""):
                # A non-PEEK body fetch sets \Seen on a real server.
                if "\\Seen" not in m["flags"]:
                    m["flags"].append("\\Seen")
                raise AssertionError("the sync fetched a body without PEEK")
            else:
                size = m["size"] if m["size"] is not None else len(m["raw"])
                flags = " ".join(m["flags"])
                data.append(
                    f'{m["uid"]} (UID {m["uid"]} FLAGS ({flags}) RFC822.SIZE {size} '
                    f'INTERNALDATE "{internaldate(m["when"])}")'.encode())
        return "OK", data


def mail(message_id=None, *, sender="alice@example.org", to="me@dove.test", subject="Hello",
         body="Hi there.", in_reply_to=None, references=None, extra=""):
    lines = [f"From: Alice <{sender}>", f"To: {to}", f"Subject: {subject}",
             f"Date: {format_datetime(NOW - timedelta(hours=2))}"]
    if message_id:
        lines.append(f"Message-ID: {message_id}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    if extra:
        lines.append(extra)
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "", body, ""]
    return "\r\n".join(lines).encode("utf-8")


# --- the box -------------------------------------------------------------------------

@pytest.fixture
def box(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in ("message_threads", "messages", "message_recipients"):
        stage_collection(data_dir, "app-messaging", name)
    for name in ("mail_accounts", "mail_sync_state", "mail_synced_messages"):
        stage_collection(data_dir, "app-mail-sync", name)
    objects_root = tmp_path / "objects"
    shutil.copytree(PACKAGE / "objects", objects_root)

    server = FakeServer()

    def connect(*args, **kwargs):
        return server.connect(*args, **kwargs)

    # The object names imaplib.IMAP4.error / .abort; the stand-in keeps them.
    connect.error, connect.abort = imaplib.IMAP4.error, imaplib.IMAP4.abort
    monkeypatch.setattr(imaplib, "IMAP4_SSL", connect)
    monkeypatch.setattr(imaplib, "IMAP4", connect)
    addresses = {"host": "1.1.1.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses["host"], port))])
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_MAIL_SYNC", "true")
    monkeypatch.delenv("DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS", raising=False)

    class Box:
        pass

    b = Box()
    b.data, b.objects, b.server, b.addresses = data_dir, objects_root, server, addresses
    return b


def account(box, account_id="acct-1", *, owner="u1", user="me@dove.test", password=PASSWORD,
            **fields):
    row = {"id": account_id, "label": "Work", "email_address": user,
           "host": "imap.example.test", "security": "ssl", "username": user,
           "owner_id": owner}
    row.update(fields)
    object_records.create_collection_record("mail_accounts", row, base_dir=box.data, actor="test")
    if password is not None:
        object_service_keys.set_service_key(owner, f"imap-{account_id}", password,
                                            base_dir=box.data)
    return row


def run(box, method="POST", object_id="system_mail_sync"):
    outcome = object_execution.execute_object(
        RUNTIME, object_execution.ObjectExecutionRequest(object_id, method=method, payload={}),
        roots=[box.objects])
    assert outcome.ok, outcome.error.to_dict() if outcome.error else outcome
    return outcome.result


def rows(box, collection):
    return object_records.read_collection_records(collection, base_dir=box.data)


def acct(box, account_id="acct-1"):
    return object_records.get_collection_record("mail_accounts", account_id, base_dir=box.data)


def retry_now(box, account_id="acct-1"):
    """What the owner does after fixing something: zero the failure count."""
    object_records.update_collection_record("mail_accounts", account_id,
                                            {"sync_attempts": "0"}, base_dir=box.data,
                                            actor="u1")


def change_count(box, collection):
    path = box.data / "record_changes" / collection / "changes.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


# --- off unless the operator says so -----------------------------------------------------

def test_the_pass_is_off_until_the_operator_opts_in_and_says_which_variable(box, monkeypatch):
    monkeypatch.delenv("DBBASIC_ENABLE_MAIL_SYNC")
    account(box)
    result = run(box)
    assert "DBBASIC_ENABLE_MAIL_SYNC" in result["skipped"]
    assert box.server.connections == 0


def test_the_kill_switch_stops_it_without_a_redeploy(box):
    stage_collection(box.data, "app-settings", "feature_flags")
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "mail_sync_enabled", "value": "off"},
        base_dir=box.data, actor="test")
    account(box)
    result = run(box)
    assert "mail_sync_enabled" in result["skipped"]
    assert box.server.connections == 0


def test_an_account_without_a_password_is_skipped_not_failed(box):
    """Not set up is not a failure: nothing counts against the account, so
    setting the password later just works."""
    account(box, password=None)
    result = run(box)
    assert result["declined"] == 1
    assert "imap-acct-1" in result["results"][0]["declined"]
    assert box.server.connections == 0
    assert acct(box)["sync_attempts"] in ("", "0")


# --- copying without touching the mailbox -------------------------------------------------

def test_mail_is_copied_and_never_marked_read(box):
    """The predecessor's worst habit: syncing marked everything read, and
    mail read elsewhere was never synced. Here the folder is EXAMINEd, the
    body is PEEKed, and is_read is whatever the server said."""
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>", subject="Unread one"))
    box.server.deliver("me@dove.test", mail("<m2@x>", subject="Read elsewhere"), flags=["\\Seen"])
    account(box)

    result = run(box)
    assert result["imported"] == 2, result

    messages = {m["subject"]: m for m in rows(box, "messages")}
    assert messages["Unread one"]["is_read"] == "false"
    assert messages["Read elsewhere"]["is_read"] == "true"
    assert all(m["owner_id"] == "u1" and m["direction"] == "inbound" for m in messages.values())
    flags = [m["flags"] for m in box.server.boxes[("me@dove.test", "INBOX")]]
    assert flags == [[], ["\\Seen"]]
    assert not any(c.startswith("SELECT") for c in box.server.commands)
    fetches = [c for c in box.server.commands if c.startswith("UID FETCH") and "BODY" in c]
    assert fetches and all("BODY.PEEK[" in c for c in fetches)
    for verb in ("STORE", "APPEND", "EXPUNGE", "COPY", "MOVE"):
        assert not any(verb in c.split()[:2] for c in box.server.commands), verb


def test_recipients_threads_and_transport_facts_land_in_their_own_places(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail(
        "<m1@x>", to="me@dove.test, Bob <bob@example.org>",
        extra="Cc: carol@example.org\r\nList-Id: <news.example.org>\r\n"
              "List-Unsubscribe: <https://example.org/unsub?u=1>"))
    account(box)
    run(box)

    message = rows(box, "messages")[0]
    recipients = sorted((r["kind"], r["address"]) for r in rows(box, "message_recipients"))
    assert recipients == [("cc", "carol@example.org"), ("to", "bob@example.org"),
                          ("to", "me@dove.test")]
    thread = rows(box, "message_threads")[0]
    assert thread["id"] == message["thread_id"] and thread["message_count"] == "1"
    assert thread["is_read"] == "false" and thread["owner_id"] == "u1"
    synced = rows(box, "mail_synced_messages")[0]
    assert synced["message_id"] == message["id"] and synced["folder"] == "INBOX"
    assert synced["list_unsubscribe"] == "<https://example.org/unsub?u=1>"
    assert synced["list_id"] == "<news.example.org>"


def test_the_first_sync_takes_a_recent_window_not_the_whole_history(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<old@x>", subject="Old"), when=NOW - timedelta(days=90))
    box.server.deliver("me@dove.test", mail("<new@x>", subject="New"))
    account(box)
    run(box)
    assert [m["subject"] for m in rows(box, "messages")] == ["New"]


def test_folders_are_found_on_the_first_sync_and_written_back(box):
    box.server.user("me@dove.test")
    account(box)
    run(box)
    assert acct(box)["folders"] == "INBOX, Sent"
    roles = {s["folder"]: s["role"] for s in rows(box, "mail_sync_state")}
    assert roles == {"INBOX": "inbox", "Sent": "sent"}


def test_a_pass_copies_at_most_fifty_messages_and_the_next_one_continues(box):
    """The pass runs inside the daemon's loop: a backlog drains over
    several passes instead of one pass holding everything else up."""
    box.server.user("me@dove.test")
    for n in range(60):
        box.server.deliver("me@dove.test", mail(f"<m{n}@x>", subject=f"n{n}"))
    account(box)
    assert run(box)["imported"] == 50
    assert run(box)["imported"] == 10
    assert len(rows(box, "messages")) == 60


def test_a_huge_message_arrives_as_headers_and_says_so(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<big@x>", subject="Big", body="body text"),
                       size=40 * 1024 * 1024)
    account(box)
    run(box)
    message = rows(box, "messages")[0]
    assert message["subject"] == "Big"
    assert "only its headers were copied" in message["body_text"]
    assert any("BODY.PEEK[HEADER]" in c for c in box.server.commands)


def test_every_connection_has_a_timeout(box):
    box.server.user("me@dove.test")
    account(box)
    run(box)
    assert box.server.timeouts and all(t and t > 0 for t in box.server.timeouts)


# --- dedupe and at-least-once -------------------------------------------------------------

def test_running_twice_copies_nothing_twice_and_an_idle_pass_writes_nothing(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>"))
    account(box)
    run(box)
    before = {c: change_count(box, c) for c in ("mail_accounts", "messages", "mail_sync_state")}
    second = run(box)
    assert second["imported"] == 0 and second["results"][0].get("up_to_date") is True
    after = {c: change_count(box, c) for c in ("mail_accounts", "messages", "mail_sync_state")}
    assert after == before, "an account with nothing new must not rewrite its rows"
    assert len(rows(box, "messages")) == 1


def test_a_server_without_uidnext_does_not_hand_back_its_newest_message_forever(box):
    """UID SEARCH UID n:* answers with the highest message even when n is
    past it. Without the filter every pass would download that message
    again (dedupe would drop it, but the download is the cost)."""
    box.server.send_uidnext = False
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>"))
    account(box)
    run(box)
    box.server.commands.clear()
    run(box)
    assert not [c for c in box.server.commands if "BODY.PEEK" in c]
    assert len(rows(box, "messages")) == 1


def test_a_pass_that_dies_half_way_loses_nothing_and_duplicates_nothing(box):
    """The cursor moves only past rows that are written; the retry
    re-meets nothing it already wrote because every id is derived from the
    message."""
    box.server.user("me@dove.test")
    for n in range(3):
        box.server.deliver("me@dove.test", mail(f"<m{n}@x>", subject=f"n{n}"))
    account(box)
    box.server.fail_fetch_uid = 2
    first = run(box)
    assert first["failed"] == 1
    assert [m["subject"] for m in rows(box, "messages")] == ["n0"]
    inbox = next(s for s in rows(box, "mail_sync_state") if s["folder"] == "INBOX")
    assert inbox["last_uid"] == "1"
    assert acct(box)["sync_attempts"] == "1"

    box.server.fail_fetch_uid = None
    retry_now(box)
    run(box)
    assert sorted(m["subject"] for m in rows(box, "messages")) == ["n0", "n1", "n2"]
    assert acct(box)["sync_attempts"] == "0" and acct(box)["sync_error"] == ""


def test_replaying_a_batch_whose_cursor_never_moved_writes_nothing_new(box):
    """The worst crash: every row written, the cursor not. Same ids, so the
    replay is refused row by row and nothing is doubled."""
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>"))
    box.server.deliver("me@dove.test", mail("<m2@x>", in_reply_to="<m1@x>"))
    account(box)
    run(box)
    inbox = next(s for s in rows(box, "mail_sync_state") if s["folder"] == "INBOX")
    object_records.update_collection_record("mail_sync_state", inbox["id"], {"last_uid": "0"},
                                            base_dir=box.data, actor="test")
    counts = {c: len(rows(box, c)) for c in ("messages", "message_threads",
                                             "message_recipients", "mail_synced_messages")}
    run(box)
    assert {c: len(rows(box, c)) for c in counts} == counts
    assert rows(box, "message_threads")[0]["message_count"] == "2"


def test_a_uidvalidity_change_restarts_the_folder_without_duplicates(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>"))
    account(box)
    run(box)
    box.server.uidvalidity[("me@dove.test", "INBOX")] = 99
    box.server.deliver("me@dove.test", mail("<m2@x>"))
    run(box)
    assert sorted(m["external_message_id"] for m in rows(box, "messages")) == ["<m1@x>", "<m2@x>"]
    inbox = next(s for s in rows(box, "mail_sync_state") if s["folder"] == "INBOX")
    assert inbox["uidvalidity"] == "99"


# --- the tenant boundary ---------------------------------------------------------------------

def test_the_same_message_in_two_peoples_mailboxes_is_two_rows(box):
    """The predecessor deduped on Message-ID alone and made one row of it."""
    for user in ("me@dove.test", "alice@dove.test"):
        box.server.user(user)
        box.server.deliver(user, mail("<shared@x>", to="me@dove.test, alice@dove.test"))
    account(box, "acct-1", owner="u1", user="me@dove.test")
    account(box, "acct-2", owner="u2", user="alice@dove.test")
    run(box)
    messages = rows(box, "messages")
    assert sorted(m["owner_id"] for m in messages) == ["u1", "u2"]
    assert len({m["thread_id"] for m in messages}) == 2
    threads = {t["id"]: t["owner_id"] for t in rows(box, "message_threads")}
    assert all(threads[m["thread_id"]] == m["owner_id"] for m in messages)
    recipients = rows(box, "message_recipients")
    owner_of = {m["id"]: m["owner_id"] for m in messages}
    assert all(r["owner_id"] == owner_of[r["message_id"]] for r in recipients)


def test_a_reply_never_joins_another_persons_thread(box):
    """u1 holds the original; u2 receives only a reply to it. u2's reply
    must start u2's own thread."""
    box.server.user("me@dove.test")
    box.server.user("alice@dove.test")
    box.server.deliver("me@dove.test", mail("<orig@x>", subject="Plans"))
    account(box, "acct-1", owner="u1", user="me@dove.test")
    account(box, "acct-2", owner="u2", user="alice@dove.test")
    run(box)
    box.server.deliver("alice@dove.test", mail("<reply@x>", subject="Re: Plans",
                                               in_reply_to="<orig@x>", references="<orig@x>"))
    retry_now(box, "acct-2")
    run(box)
    by_owner = {m["owner_id"]: m for m in rows(box, "messages")}
    assert by_owner["u1"]["thread_id"] != by_owner["u2"]["thread_id"]
    thread = object_records.get_collection_record("message_threads", by_owner["u2"]["thread_id"],
                                                  base_dir=box.data)
    assert thread["owner_id"] == "u2"


# --- threading ----------------------------------------------------------------------------------

def test_a_reply_joins_the_sent_message_it_answers_and_a_subject_joins_nothing(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<out@x>", sender="me@dove.test", to="bob@example.org",
                                            subject="Invoice"), folder="Sent")
    box.server.deliver("me@dove.test", mail("<back@x>", sender="bob@example.org",
                                            subject="Re: Invoice", in_reply_to="<out@x>"))
    box.server.deliver("me@dove.test", mail("<other@x>", sender="eve@example.org",
                                            subject="Invoice"))
    account(box)
    run(box)
    messages = {m["external_message_id"]: m for m in rows(box, "messages")}
    assert messages["<out@x>"]["direction"] == "outbound"
    assert messages["<out@x>"]["is_read"] == "true"
    assert messages["<back@x>"]["thread_id"] == messages["<out@x>"]["thread_id"]
    assert messages["<other@x>"]["thread_id"] != messages["<out@x>"]["thread_id"]
    thread = object_records.get_collection_record(
        "message_threads", messages["<out@x>"]["thread_id"], base_dir=box.data)
    assert thread["message_count"] == "2"
    assert thread["is_read"] == "false", "an unread reply makes its thread unread"


# --- failures are written down --------------------------------------------------------------

def test_a_refused_login_is_recorded_without_the_password_and_backs_off(box):
    box.server.user("me@dove.test", password="something else")
    account(box)
    result = run(box)
    assert result["failed"] == 1
    row = acct(box)
    assert row["sync_attempts"] == "1"
    assert "refused the login" in row["sync_error"]
    assert PASSWORD not in json.dumps(result) and PASSWORD not in row["sync_error"]

    connections = box.server.connections
    again = run(box)
    assert box.server.connections == connections, "a failing account waits before the next try"
    assert "waiting" in again["results"][0]


def test_three_failures_in_a_row_are_counted_on_the_home_page(box):
    box.server.user("me@dove.test", password="something else")
    account(box)
    assert run(box, "COUNT", "system_mail_sync_attention") == {"count": 0}
    for _ in range(3):
        retry_now_keep = acct(box)["sync_attempts"]
        object_records.update_collection_record(
            "mail_accounts", "acct-1", {"sync_finished_at": "2000-01-01T00:00:00Z"},
            base_dir=box.data, actor="test", preserve_read_only=True)
        run(box)
        assert int(acct(box)["sync_attempts"]) == int(retry_now_keep or 0) + 1
    counted = run(box, "COUNT", "system_mail_sync_attention")
    assert counted["count"] == 1
    assert "me@dove.test" not in json.dumps(counted), "the home page count names nobody"


def test_a_missing_folder_is_an_error_on_the_account_not_a_silent_skip(box):
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail("<m1@x>"))
    account(box, folders="INBOX, Archive")
    run(box)
    row = acct(box)
    assert row["sync_attempts"] == "1" and "Archive" in row["sync_error"]
    assert len(rows(box, "messages")) == 1, "the folder that works still syncs"


def test_a_private_address_is_refused_unless_the_operator_allows_it(box, monkeypatch):
    """The host is typed by a member and the server is the one connecting:
    without this, anyone could make it knock on its own network."""
    box.server.user("me@dove.test")
    box.addresses["host"] = "10.0.0.5"
    account(box)
    run(box)
    assert box.server.connections == 0
    assert "DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS" in acct(box)["sync_error"]

    monkeypatch.setenv("DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS", "true")
    retry_now(box)
    run(box)
    assert box.server.connections == 1
    assert acct(box)["sync_error"] == ""


# --- what it never does ------------------------------------------------------------------------

def test_it_never_fetches_a_url_from_a_header_and_never_sends(box, monkeypatch):
    """List-Unsubscribe is stored for a person to act on; the server opening
    it would let any sender make it issue requests."""
    import http.client
    import smtplib
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("the mail sync opened a network connection it must not")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    box.server.user("me@dove.test")
    box.server.deliver("me@dove.test", mail(
        "<m1@x>", extra="List-Unsubscribe: <https://example.org/u>\r\nList-Unsubscribe-Post: "
                        "List-Unsubscribe=One-Click"))
    account(box)
    assert run(box)["imported"] == 1
    source = SOURCE.read_text() + (REPO_ROOT / "object_mail.py").read_text()
    for module in ("urllib", "http.client", "smtplib", "requests"):
        assert f"import {module}" not in source


def test_the_scheduled_entry_point_is_event_with_post_as_its_alias():
    """AGENTS.md: the dispatcher calls EVENT; an object with only POST never
    runs and never complains."""
    source = SOURCE.read_text()
    assert "def EVENT(request)" in source and "POST = EVENT" in source


# --- the page ---------------------------------------------------------------------------------

def page(box, user_id=None):
    payload = {"_identity": {"user_id": user_id}} if user_id else {}
    outcome = object_execution.execute_object(
        RUNTIME, object_execution.ObjectExecutionRequest("site_mail_accounts", method="GET",
                                                         payload=payload),
        roots=[box.objects])
    assert outcome.ok, outcome.error.to_dict() if outcome.error else outcome
    return outcome.result["body"]


def test_the_page_shows_whether_a_password_is_set_and_never_the_password(box):
    account(box, "acct-1", owner="u1", label="Work")
    account(box, "acct-2", owner="u1", label="Home", password=None)
    account(box, "acct-3", owner="u2", label="Someone else's")
    body = page(box, "u1")
    assert "Work" in body and "Home" in body
    assert "Someone else" not in body, "only the signed-in person's accounts"
    assert body.count("<strong data-pwstate>set</strong>") == 1
    assert body.count("<strong data-pwstate>not set</strong>") == 1
    assert PASSWORD not in body
    assert "/service-keys" in body and '"imap-" + id' in body


def test_the_page_says_when_the_operator_has_not_turned_sync_on(box, monkeypatch):
    monkeypatch.delenv("DBBASIC_ENABLE_MAIL_SYNC")
    account(box)
    assert "DBBASIC_ENABLE_MAIL_SYNC" in page(box, "u1")


def test_a_visitor_gets_a_sign_in_prompt(box):
    account(box)
    body = page(box)
    assert "Sign in" in body and "Work" not in body


# --- a real server, when one is named -------------------------------------------------------------

REAL = os.environ.get("DBBASIC_TEST_IMAP", "")


@pytest.mark.skipif(not REAL, reason="set DBBASIC_TEST_IMAP=host:port:user:password to run")
def test_against_a_real_imap_server(tmp_path, monkeypatch):
    """Plain IMAP on this machine (a GreenMail or Dovecot test server): the
    sync copies whatever the window holds and leaves every flag as it was."""
    host, port, user, password = REAL.split(":", 3)
    data_dir = tmp_path / "data"
    for name in ("message_threads", "messages", "message_recipients"):
        stage_collection(data_dir, "app-messaging", name)
    for name in ("mail_accounts", "mail_sync_state", "mail_synced_messages"):
        stage_collection(data_dir, "app-mail-sync", name)
    objects_root = tmp_path / "objects"
    shutil.copytree(PACKAGE / "objects", objects_root)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_MAIL_SYNC", "true")
    monkeypatch.setenv("DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS", "true")

    def flags():
        conn = imaplib.IMAP4(host, int(port), timeout=10)
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.fetch("1:*", "(UID FLAGS)")
        conn.logout()
        return sorted(object_mail._as_text(d) for d in data if d)

    before = flags()
    object_records.create_collection_record("mail_accounts", {
        "id": "real-1", "email_address": user if "@" in user else f"{user}@localhost",
        "host": host, "port": port, "security": "plain", "username": user,
        "owner_id": "u1"}, base_dir=data_dir, actor="test")
    object_service_keys.set_service_key("u1", "imap-real-1", password, base_dir=data_dir)
    result = object_execution.execute_object(
        RUNTIME, object_execution.ObjectExecutionRequest("system_mail_sync", method="POST",
                                                         payload={}),
        roots=[objects_root]).result
    assert result["failed"] == 0, result
    assert flags() == before, "the sync changed a flag on the server"
    assert len(object_records.read_collection_records("messages", base_dir=data_dir)) >= 1
