"""system_mail_sync -- copy new mail from each person's IMAP mailboxes into
their app-messaging inbox, without touching the mailbox.

EVENT/POST: the scheduled pass declared in this package's `schedules`,
every two minutes. (The home-page count of failing accounts is its own
object, system_mail_sync_attention, as every attention provider is.)
Everything that can be decided without a server -- parsing,
dedupe, threading, where the cursor goes, what to download -- is
object_mail's, which is pure and tested on its own. This object is the I/O:
it opens the connection, asks, and writes rows.

READ-ONLY TOWARDS THE MAILBOX. Folders are opened with EXAMINE (imaplib's
``select(readonly=True)``) and messages fetched with BODY.PEEK[], so the
sync can never set \\Seen, and ``is_read`` on each copied message is the
server's own \\Seen flag at the moment of copying. The predecessor searched
UNSEEN and fetched RFC822, which marked every synced message read in the
person's own mail program and never copied anything they had read
elsewhere first. Messages are chosen by UID, never by flag. The sync sends
nothing, stores no link it does not open, and opens none.

BOUNDED, because it runs inside the daemon's loop, between the other
passes: at most BATCH_LIMIT messages and TICK_BYTE_BUDGET bytes per account
per pass, socket timeouts on every connection, messages over
MAX_MESSAGE_BYTES copied as headers only (the body then says so), a first
sync that takes the newest FIRST_WINDOW messages of the last
FIRST_WINDOW_DAYS days rather than the whole history, and no new account
started after TICK_SECONDS. A backlog drains over several passes.

AT LEAST ONCE, MADE SAFE. The folder cursor (mail_sync_state.last_uid) moves
only after the rows for those messages are written. A pass that dies
between the two repeats that work next time, and the repeat writes nothing
new: every row id is object_mail.stable_id(owner, account, dedupe key), so
the second create of the same message is refused as a duplicate.

QUIET WHEN NOTHING HAPPENS. An account is checked read-only first (EXAMINE
answers UIDNEXT); only when there is something to copy does the pass claim
the account -- a compare-and-set on its row with expected_rev, so a manual
run and the scheduled one cannot both write -- and only then are rows
written. An idle account refreshes last_synced_at at most hourly. Without
that, two account writes every two minutes would grow mail_accounts' change
log by ~1,400 entries a day per account for nothing (AGENTS.md: a writer
says how it is bounded).

FAILURES ARE RECORDED, NEVER SWALLOWED. Two kinds of silence are told
apart, the way system_tracking_poll does:

  not set up          the feature is off, the account has no password in
                      the vault, no owner, or an id that cannot name a
                      vault entry. Nothing is counted against the account;
                      the pass says why in its report.
  this account failed a refused login, an unreachable or refused host, a
                      missing folder, a message that would not store.
                      sync_attempts goes up, sync_error says what happened,
                      and the account waits longer between tries
                      (object_mail.account_due) so a wrong password is not
                      tried every two minutes until the provider locks the
                      mailbox. At object_mail.ATTENTION_AFTER failures
                      in a row it is
                      counted on the home page.

The password is read from the owner's identity vault under
``imap-<account id>``, resolved by the ACCOUNT ROW's owner_id and never by
anything a caller passed, used for the login, and never written, logged or
returned; error text from the server is scrubbed of it before it is stored.
"""

import imaplib
import os
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone

import object_feature_flags
import object_mail
import object_records
import object_schemas
import object_service_keys

ACTOR = "system_mail_sync"

ACCOUNTS = "mail_accounts"
STATE = "mail_sync_state"
SYNCED = "mail_synced_messages"
MESSAGES = "messages"
THREADS = "message_threads"
RECIPIENTS = "message_recipients"

BATCH_LIMIT = 50
FIRST_WINDOW = 200
FIRST_WINDOW_DAYS = 30
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
TICK_BYTE_BUDGET = 25 * 1024 * 1024
SOCKET_TIMEOUT_SECONDS = 30
TICK_SECONDS = 60
CLAIM_TTL_SECONDS = 15 * 60
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 30 * 60
HEARTBEAT_SECONDS = 60 * 60
MAX_ERROR = 500

_DEFAULT_PORTS = {"ssl": 993, "starttls": 143, "plain": 143}


class SyncError(Exception):
    """A failure this account's owner needs to read; the text is stored."""


def _base_dir():
    return os.environ.get("DBBASIC_DATA_DIR", "data")


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value, default=0):
    try:
        return int(_text(value) or default)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return object_mail.format_timestamp(moment)


def _collection_exists(base, name):
    if object_records.collection_has_records(name, base_dir=base):
        return True
    try:
        object_schemas.get_schema(name, base_dir=base)
        return True
    except (object_schemas.SchemaNotFoundError, OSError, ValueError):
        return False


def _get(base, collection, record_id):
    try:
        return object_records.get_collection_record(collection, record_id, base_dir=base)
    except object_records.RecordNotFoundError:
        return None


def _create(base, collection, row):
    """True when written, False when a row with this id already exists --
    the replay of a batch a previous pass did not finish."""
    try:
        object_records.create_collection_record(collection, row, base_dir=base, actor=ACTOR)
        return True
    except object_records.DuplicateRecordIdError:
        return False


def _scrub(text, secrets):
    text = _text(text)
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return text[:MAX_ERROR]


# --- the connection ------------------------------------------------------------

def _connect(account, password):
    host = _text(account.get("host"))
    security = _text(account.get("security")) or "ssl"
    if security not in _DEFAULT_PORTS:
        raise SyncError(f"security must be ssl, starttls or plain, not {security!r}")
    port = _int(account.get("port")) or _DEFAULT_PORTS[security]
    username = _text(account.get("username")) or _text(account.get("email_address"))
    if not host:
        raise SyncError("the account has no IMAP server")
    if any(char in username for char in "\r\n\x00"):
        # imaplib quotes a login but does not refuse a line break, which
        # would end the command and start another on the mailbox.
        raise SyncError("the user name cannot contain a line break")

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SyncError(f"could not find the server {host!r}: {exc}") from exc
    refusal = object_mail.host_refusal(
        host, sorted({info[4][0] for info in infos}), security=security,
        allow_private=object_feature_flags.env_opt_in(object_mail.ALLOW_PRIVATE_HOSTS_ENV))
    if refusal:
        raise SyncError(refusal)

    conn = None
    try:
        if security == "ssl":
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(),
                                     timeout=SOCKET_TIMEOUT_SECONDS)
        else:
            conn = imaplib.IMAP4(host, port, timeout=SOCKET_TIMEOUT_SECONDS)
            if security == "starttls":
                conn.starttls(ssl_context=ssl.create_default_context())
    except (OSError, imaplib.IMAP4.error, ssl.SSLError) as exc:
        if conn is not None:
            _logout(conn)
        raise SyncError(f"could not connect to {host}:{port} ({security}): {exc}") from exc

    try:
        conn.login(username, password)
    except UnicodeError as exc:
        _logout(conn)
        raise SyncError("the user name or password has characters an IMAP login "
                        "cannot send (only plain ASCII is allowed)") from exc
    except imaplib.IMAP4.error as exc:
        _logout(conn)
        raise SyncError(f"the server refused the login: {_scrub(exc, [password])}") from exc
    return conn


def _logout(conn):
    try:
        conn.logout()
    except Exception:  # noqa: BLE001 -- the work is done or already failed; closing is best effort
        return


def _ok(typ, data, what):
    if typ != "OK":
        detail = b" ".join(d for d in (data or []) if isinstance(d, bytes)).decode("utf-8", "replace")
        raise SyncError(f"{what} failed: {detail or typ}")
    return data


def _examine(conn, folder):
    """Open a folder read-only; (uidvalidity, uidnext or 0)."""
    try:
        quoted = object_mail.quote_mailbox(folder)
    except ValueError as exc:
        raise SyncError(f"the folder name {folder!r} cannot be used: {exc}") from exc
    typ, data = conn.select(quoted, readonly=True)
    _ok(typ, data, f"opening the folder {folder!r}")
    uidvalidity = object_mail.parse_search(conn.response("UIDVALIDITY")[1])
    uidnext = object_mail.parse_search(conn.response("UIDNEXT")[1])
    if not uidvalidity:
        raise SyncError(f"the server gave no UIDVALIDITY for {folder!r}")
    return str(uidvalidity[0]), (uidnext[0] if uidnext else 0)


# --- one account -----------------------------------------------------------------

class _Pass:
    """One account's sync: what to check, then (only if needed) the writes."""

    def __init__(self, base, account, password, now):
        self.base = base
        self.account = account
        self.password = password
        self.now = now
        self.account_id = account["id"]
        self.owner_id = _text(account.get("owner_id"))
        self.address = _text(account.get("email_address"))
        self.imported = 0
        self.duplicates = 0
        self.errors = []
        self.discovered = None
        self._seen = None
        self._index = None

    # -- read-only check -------------------------------------------------------

    def check(self, conn):
        """Folders with something to do, found without writing anything."""
        names = object_mail.split_folders(self.account.get("folders"))
        entries = None
        if not names:
            entries = object_mail.parse_list(_ok(*conn.list(), "LIST"))
            chosen = object_mail.choose_folders(entries)
            self.discovered = ", ".join(name for name, _ in chosen)
            names = [name for name, _ in chosen]

        work = []
        for name in names:
            state_id = object_mail.stable_id("folder", self.account_id, name)
            state = _get(self.base, STATE, state_id)
            try:
                uidvalidity, uidnext = _examine(conn, name)
            except SyncError as exc:
                self.errors.append(str(exc))
                continue
            decision = object_mail.plan_folder(state, uidvalidity=uidvalidity)
            last_uid = _int((state or {}).get("last_uid"))
            if decision == "continue" and uidnext and uidnext - 1 <= last_uid:
                continue
            role = (state or {}).get("role")
            if not role:
                if entries is None:
                    entries = object_mail.parse_list(_ok(*conn.list(), "LIST"))
                role = object_mail.folder_role(name, entries)
            work.append({"folder": name, "state_id": state_id, "state": state,
                         "decision": decision, "uidvalidity": uidvalidity,
                         "uidnext": uidnext, "role": role})
        return work

    # -- the writes --------------------------------------------------------------

    def run(self, conn, work):
        budget = BATCH_LIMIT
        bytes_left = TICK_BYTE_BUDGET
        for item in work:
            if budget <= 0 or bytes_left <= 0:
                break
            try:
                done, spent = self._folder(conn, item, budget, bytes_left)
            except SyncError as exc:
                self.errors.append(str(exc))
                continue
            except (imaplib.IMAP4.error, OSError) as exc:
                # The connection itself failed; later folders cannot work.
                self.errors.append(f"{item['folder']}: {_scrub(exc, [self.password])}")
                break
            budget -= done
            bytes_left -= spent

    def _folder(self, conn, item, budget, bytes_left):
        folder = item["folder"]
        uidvalidity, uidnext = _examine(conn, folder)
        if uidvalidity != item["uidvalidity"]:
            raise SyncError(f"{folder!r} changed UIDVALIDITY while it was being read; "
                            f"the next pass starts it again")
        state = item["state"]
        last_uid = _int((state or {}).get("last_uid"))

        if item["decision"] in ("fresh", "reset"):
            since = object_mail.imap_date(self.now - timedelta(days=FIRST_WINDOW_DAYS))
            window = object_mail.parse_search(_ok(*conn.uid("SEARCH", "SINCE", since), "SEARCH"))
            highest = uidnext - 1 if uidnext else max(
                object_mail.parse_search(_ok(*conn.uid("SEARCH", "ALL"), "SEARCH")) or [0])
            last_uid = object_mail.window_baseline(window, first_window=FIRST_WINDOW,
                                                   highest_uid=highest)
            row = {"account_id": self.account_id, "folder": folder, "role": item["role"],
                   "uidvalidity": uidvalidity, "last_uid": str(last_uid),
                   "owner_id": self.owner_id}
            if state is None:
                _create(self.base, STATE, {"id": item["state_id"], **row})
            else:
                object_records.update_collection_record(
                    STATE, item["state_id"], row, base_dir=self.base, actor=ACTOR)

        found = object_mail.parse_search(
            _ok(*conn.uid("SEARCH", "UID", f"{last_uid + 1}:*"), "SEARCH"))
        uids = object_mail.next_uids(found, last_uid=last_uid, limit=budget)
        if not uids:
            return 0, 0
        metas = object_mail.parse_fetch_meta(_ok(*conn.uid(
            "FETCH", ",".join(str(uid) for uid in uids),
            "(UID FLAGS RFC822.SIZE INTERNALDATE)"), "FETCH"))
        by_uid = {meta["uid"]: meta for meta in metas}
        plan = object_mail.plan_downloads(metas, max_message_bytes=MAX_MESSAGE_BYTES,
                                          byte_budget=bytes_left)

        advanced = last_uid
        done = spent = 0
        try:
            for uid, mode in plan:
                meta = by_uid[uid]
                part = "BODY.PEEK[]" if mode == "full" else "BODY.PEEK[HEADER]"
                raw = object_mail.fetch_literal(_ok(*conn.uid("FETCH", str(uid), f"(UID {part})"),
                                                    "FETCH"))
                if raw is None:
                    raise SyncError(f"{folder!r}: the server sent no message for UID {uid}")
                parsed = object_mail.parse_message(
                    raw, flags=meta["flags"], internaldate=meta["internaldate"],
                    header_only_size=meta["size"] if mode == "header" else None)
                try:
                    self._store(parsed, folder=folder, uid=uid, uidvalidity=uidvalidity,
                                role=item["role"])
                except (object_records.InvalidRecordPayloadError, ValueError, LookupError) as exc:
                    raise SyncError(f"{folder!r}: UID {uid} could not be stored: "
                                    f"{str(exc)[:200]}") from exc
                advanced = uid
                done += 1
                spent += meta["size"] if mode == "full" else 0
        finally:
            # The cursor moves to the last message whose rows are written,
            # and no further, whatever stopped the loop.
            if advanced > last_uid:
                object_records.update_collection_record(
                    STATE, item["state_id"], {"last_uid": str(advanced)},
                    base_dir=self.base, actor=ACTOR)
        return done, spent

    def _prepare(self):
        """The dedupe keys and thread index, built once per pass and only
        when there is something to import -- they are the expensive reads."""
        if self._seen is not None:
            return
        synced = object_records.read_collection_records(SYNCED, base_dir=self.base)
        own = [row for row in synced if row.get("owner_id") == self.owner_id]
        self._seen = {row.get("dedupe_key") for row in own
                      if row.get("account_id") == self.account_id}
        messages = object_records.read_collection_records(MESSAGES, base_dir=self.base)
        self._index = object_mail.ThreadIndex.from_rows(self.owner_id, messages, own)

    def _store(self, parsed, *, folder, uid, uidvalidity, role):
        self._prepare()
        key = object_mail.dedupe_key(parsed)
        if key in self._seen:
            self.duplicates += 1
            return
        owner, account_id = self.owner_id, self.account_id
        direction = object_mail.direction_for(parsed, account_address=self.address,
                                              folder_role=role)
        message_id = object_mail.stable_id("message", owner, account_id, key)
        thread_id = object_mail.match_thread(parsed, self._index)
        joined = thread_id is not None
        if not joined:
            thread_id = object_mail.stable_id("thread", owner, account_id, key)
            _create(self.base, THREADS, object_mail.new_thread_row(
                parsed, owner_id=owner, thread_id=thread_id, direction=direction,
                account_address=self.address))

        row, recipients = object_mail.message_rows(
            parsed, owner_id=owner, message_id=message_id, thread_id=thread_id,
            direction=direction)
        created = _create(self.base, MESSAGES, row)
        for recipient in recipients:
            _create(self.base, RECIPIENTS, recipient)
        _create(self.base, SYNCED, object_mail.synced_row(
            parsed, row_id=object_mail.stable_id("synced", owner, account_id, key),
            owner_id=owner, account_id=account_id, folder=folder, uid=uid,
            uidvalidity=uidvalidity, message_id=message_id, key=key))
        if created and joined:
            thread = _get(self.base, THREADS, thread_id)
            if thread is not None:
                changes = object_mail.thread_changes(thread, parsed, direction=direction,
                                                     account_address=self.address)
                object_records.update_collection_record(
                    THREADS, thread_id, changes, base_dir=self.base, actor=ACTOR)
        self._index.add(parsed, thread_id)
        self._seen.add(key)
        if created:
            self.imported += 1
        else:
            self.duplicates += 1


# --- the pass ------------------------------------------------------------------------

def _finish(base, account, changes, *, expected_rev=None):
    try:
        object_records.update_collection_record(
            ACCOUNTS, account["id"], changes, base_dir=base, actor=ACTOR,
            preserve_read_only=True, expected_rev=expected_rev)
        return True
    except object_records.VersionConflictError:
        return False


def _failed(base, account, message, now):
    attempts = _int(account.get("sync_attempts")) + 1
    _finish(base, account, {"sync_attempts": str(attempts), "sync_error": message[:MAX_ERROR],
                            "sync_finished_at": _stamp(now)})
    return attempts


def _sync_one(base, account, now):
    """One account's outcome, as a report entry."""
    entry = {"account": account["id"]}
    owner_id = _text(account.get("owner_id"))
    if not owner_id:
        entry["declined"] = "the account has no owner, so there is no vault to read a password from"
        return entry
    service = object_mail.password_service(account["id"])
    if not object_service_keys.validate_service_name(service):
        entry["declined"] = (f"the account id cannot name a vault entry ({service!r} is not a "
                             f"valid service name); recreate the account from /mail/accounts")
        return entry
    password = object_service_keys.get_service_key(owner_id, service, base_dir=base)
    if not password:
        entry["declined"] = f"no password set (the owner's vault has no {service!r})"
        return entry

    rev = object_records.compute_record_rev(account)
    conn = None
    try:
        conn = _connect(account, password)
        sync = _Pass(base, account, password, now)
        work = sync.check(conn)
        if not work and sync.discovered is None:
            # Nothing to copy. Write only what changed: a recovery clears the
            # failure count; otherwise last_synced_at is refreshed hourly.
            if sync.errors:
                raise SyncError("; ".join(sync.errors))
            last = object_mail.parse_timestamp(account.get("last_synced_at"))
            recovering = _int(account.get("sync_attempts")) or _text(account.get("sync_error"))
            if recovering or last is None or (now - last).total_seconds() >= HEARTBEAT_SECONDS:
                _finish(base, account, {"last_synced_at": _stamp(now), "sync_finished_at": _stamp(now),
                                        "sync_attempts": "0", "sync_error": ""}, expected_rev=rev)
            entry["up_to_date"] = True
            return entry

        # Something to write: claim the account first. expected_rev is the
        # row as this pass read it, so a second pass (or an edit) that got
        # there first makes this one stand down instead of writing beside it.
        if not _finish(base, account, {"sync_started_at": _stamp(now)}, expected_rev=rev):
            entry["declined"] = "the account changed while this pass read it; the next pass will try"
            return entry
        try:
            sync.run(conn, work)
        except Exception as exc:  # noqa: BLE001 -- recorded on the account below, not hidden
            sync.errors.append(_scrub(f"{type(exc).__name__}: {exc}", [password]))
        entry["imported"] = sync.imported
        if sync.duplicates:
            entry["already_copied"] = sync.duplicates
        changes = {"sync_finished_at": _stamp(_now())}
        if sync.discovered is not None:
            changes["folders"] = sync.discovered
        if sync.errors:
            message = _scrub("; ".join(sync.errors), [password])
            changes.update({"sync_attempts": str(_int(account.get("sync_attempts")) + 1),
                            "sync_error": message})
            entry["error"] = message
        else:
            changes.update({"last_synced_at": _stamp(now), "sync_attempts": "0", "sync_error": ""})
        _finish(base, account, changes)
        return entry
    except SyncError as exc:
        message = _scrub(exc, [password])
        entry["error"] = message
        entry["attempts"] = _failed(base, account, message, now)
        return entry
    except Exception as exc:  # noqa: BLE001 -- written on the account below, never hidden
        # Anything else (an answer imaplib could not parse, a bug here) is
        # still this account's failure: recorded like the rest, and the
        # other accounts still get their turn this pass.
        message = _scrub(f"{type(exc).__name__}: {exc}", [password])
        entry["error"] = message
        entry["attempts"] = _failed(base, account, message, now)
        return entry
    finally:
        if conn is not None:
            _logout(conn)


def EVENT(request):
    base = _base_dir()
    refusal = object_feature_flags.refusal(
        env_name=object_mail.ENABLE_ENV, flag=object_mail.KILL_SWITCH_FLAG, base_dir=base)
    if refusal:
        return {"ok": True, "skipped": f"mail sync is {refusal}"}
    if not _collection_exists(base, ACCOUNTS):
        return {"ok": True, "skipped": "app-mail-sync is not installed (no mail_accounts)"}
    missing = [name for name in (MESSAGES, THREADS, RECIPIENTS, STATE, SYNCED)
               if not _collection_exists(base, name)]
    if missing:
        return {"ok": True, "skipped": f"missing collections {missing}: install app-messaging "
                                       f"and app-mail-sync"}

    accounts = object_records.read_collection_records(ACCOUNTS, base_dir=base)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    accounts.sort(key=lambda row: object_mail.parse_timestamp(row.get("last_synced_at")) or epoch)

    report = {"ok": True, "accounts": 0, "imported": 0, "failed": 0, "declined": 0,
              "waiting": 0, "results": []}
    deadline = time.monotonic() + TICK_SECONDS
    for account in accounts:
        if not _text(account.get("id")):
            continue
        now = _now()
        due, why = object_mail.account_due(
            account, now, claim_ttl_seconds=CLAIM_TTL_SECONDS,
            retry_base_seconds=RETRY_BASE_SECONDS, retry_max_seconds=RETRY_MAX_SECONDS)
        if not due:
            if why != "disabled":
                report["waiting"] += 1
                report["results"].append({"account": account["id"], "waiting": why})
            continue
        if time.monotonic() > deadline:
            report["results"].append({"account": account["id"],
                                      "waiting": "this pass ran out of time; the next one starts here"})
            report["waiting"] += 1
            continue
        report["accounts"] += 1
        entry = _sync_one(base, account, now)
        report["imported"] += entry.get("imported", 0)
        if "error" in entry:
            report["failed"] += 1
        if "declined" in entry:
            report["declined"] += 1
        report["results"].append(entry)
    return report


POST = EVENT

