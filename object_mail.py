"""Inbound mail, the pure half: bytes and IMAP answers in, app-messaging rows
and cursor decisions out. No network, no clock, no data directory.

The I/O lives in packages/app-mail-sync (``system_mail_sync``), which opens
the IMAP connection, asks the questions this module plans, and writes the
rows this module shapes. Everything that can be wrong about mail without a
server being involved is decided here, where a test can reach it.

WHAT THE PREDECESSOR GOT WRONG, and where each is answered in this file.
The mail sync this replaces taught four lessons, each paid for:

- **It marked mail read.** It searched UNSEEN and fetched RFC822, and a
  plain FETCH of RFC822 sets \\Seen. So syncing made every message look
  read in the person's own mail program, and anything they had already
  read elsewhere was never synced at all because it was no longer unseen.
  Here the cursor is by UID, not by flag (``next_uids``), the fetch is
  BODY.PEEK (the object's job), and ``is_read`` is read off the server's
  FLAGS rather than decided by us.
- **It leaked across tenants.** Dedupe and threading were keyed on the
  Message-ID alone, so the same message in two people's mailboxes became
  one row, and a reply could join a thread owned by somebody else.
  ``stable_id`` folds the owner and the account into every id this module
  mints, and ``ThreadIndex`` refuses rows that belong to anyone but the
  owner it was built for.
- **It joined threads by subject.** Two strangers both writing "Invoice"
  ended up in one conversation. ``match_thread`` uses In-Reply-To and
  References only; a message with neither starts its own thread.
- **It swallowed failures.** ``except: pass`` around the fetch meant a box
  whose password had changed looked exactly like a quiet inbox. Nothing
  here catches and hides; the object records every failure on the account
  (sync_attempts, sync_error) and the attention count shows it.

And one thing it never did and this must never start: fetch a URL found in
a message. List-Unsubscribe is PARSED and STORED so a person (or a later
package they choose) can act on it; nothing in this module or the object
opens it. A server that fetches links out of mail is a server any sender
can make issue requests from inside the network.

Dates are written in the server's own timestamp spelling
(``2026-09-26T10:00:00Z``: UTC, ``Z``, microseconds only when present),
because two spellings of one instant do not sort together (AGENTS.md).
"""

from __future__ import annotations

import email
import email.header
import email.policy
import email.utils
import hashlib
import html
import html.parser
import ipaddress
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

# --- the gate's names (read by the object; named here so docs and tests share them)

ENABLE_ENV = "DBBASIC_ENABLE_MAIL_SYNC"
ALLOW_PRIVATE_HOSTS_ENV = "DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS"
KILL_SWITCH_FLAG = "mail_sync_enabled"
PASSWORD_SERVICE_PREFIX = "imap-"
# Failures in a row before an account is counted on the home page. Shared
# by the sync (which counts them) and its attention provider (which reads
# them), so the two cannot disagree about what "keeps failing" means.
ATTENTION_AFTER = 3

# --- field bounds (the app-messaging schema's own, plus this package's) -------

MAX_SUBJECT = 300
MAX_ADDRESS = 254
MAX_MESSAGE_ID = 998
MAX_SUMMARY = 300
MAX_REFERENCES = 8000
MAX_HEADER_VALUE = 2000
MAX_BODY_CHARS = 200_000

NO_SENDER = "(no sender)"

# A fixed namespace for uuid5 ids. Changing it would re-import every mailbox
# as new rows beside the old ones, so it never changes.
_ID_NAMESPACE = uuid.UUID("6f1d4b0e-3c1a-5b8e-9a61-0d5c3e2f7a90")

_MSGID_RE = re.compile(r"<[^<>\s]+>")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- small text helpers --------------------------------------------------------

def clean_text(value: Any) -> str:
    """Text that the TSV substrate will store: valid UTF-8, no NUL or other
    control characters except tab and newline, newlines as \\n.

    Headers with raw 8-bit bytes come out of the email package carrying
    surrogate escapes, which cannot be encoded as UTF-8 at all; they are
    turned back into bytes and decoded with replacement, so a badly encoded
    subject shows a replacement character instead of failing the write.
    """
    text = "" if value is None else str(value)
    text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub("", text)


def one_line(value: Any) -> str:
    """A header value as one line: folded whitespace collapsed."""
    return " ".join(clean_text(value).split())


def cap(value: str, limit: int) -> str:
    """At most ``limit`` characters, marking a cut with an ellipsis."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def format_timestamp(moment: datetime) -> str:
    """The server's timestamp spelling. A naive datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    """A stored timestamp back to an aware datetime, or None. Compare
    these, never the strings (AGENTS.md: two spellings do not sort)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_date_header(value: str) -> str:
    """An RFC 5322 Date header as a server timestamp, or "" when it cannot
    be read. A sender's clock is not evidence of much, so a missing or
    broken Date is left blank rather than guessed."""
    raw = one_line(value)
    if not raw:
        return ""
    try:
        moment = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    if moment is None:
        return ""
    try:
        return format_timestamp(moment)
    except (OverflowError, ValueError):
        return ""


def parse_internaldate(value: str) -> str:
    """An IMAP INTERNALDATE (``26-Sep-2026 10:00:00 +0000``) as a server
    timestamp, or "". Parsed by hand rather than with strptime's %b, which
    follows the process locale."""
    match = re.fullmatch(
        r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})\s*",
        str(value or ""))
    if not match:
        return ""
    day, month, year, hour, minute, second, sign, off_h, off_m = match.groups()
    try:
        month_number = [m.lower() for m in _MONTHS].index(month.lower()) + 1
        offset = timedelta(hours=int(off_h), minutes=int(off_m))
        zone = timezone(offset if sign == "+" else -offset)
        moment = datetime(int(year), month_number, int(day),
                          int(hour), int(minute), int(second), tzinfo=zone)
    except ValueError:
        return ""
    return format_timestamp(moment)


def imap_date(moment: datetime) -> str:
    """A date as IMAP SEARCH wants it (``26-Sep-2026``), locale-free."""
    return f"{moment.day}-{_MONTHS[moment.month - 1]}-{moment.year}"


def extract_message_ids(value: str) -> list[str]:
    """Every ``<id>`` in a header, in order, without repeats."""
    seen: list[str] = []
    for found in _MSGID_RE.findall(clean_text(value)):
        if found not in seen and len(found) <= MAX_MESSAGE_ID:
            seen.append(found)
    return seen


def stable_id(*parts: Any) -> str:
    """A deterministic record id for the same facts.

    Every id the sync writes is minted here from the owner, the account and
    the message's dedupe key. That is what makes a replayed batch safe: the
    second attempt at the same message produces the same id, the create is
    refused as a duplicate, and nothing is written twice. It is also the
    tenant boundary for ids -- the same Message-ID in two people's
    mailboxes yields two different ids, because the owner is part of the
    name.
    """
    name = "\x1f".join(str(part) for part in parts)
    return str(uuid.uuid5(_ID_NAMESPACE, name))


# --- HTML to text ---------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style", "head", "title", "template", "noscript"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "ul", "ol", "hr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Readable text from an HTML body: scripts and styles dropped, blocks
    as line breaks, entities decoded, runs of blank lines collapsed. No
    link is followed and no image is loaded; this only reads the string."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 -- html.parser is lenient; a crash still leaves text
        return clean_text(re.sub(r"<[^>]*>", " ", html.unescape(markup)))
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    text = "\n".join(lines).strip()
    return _BLANK_LINES_RE.sub("\n\n", text)


# --- parsing one message ---------------------------------------------------------

def _raw_header(message, name: str) -> list[str]:
    """Header values the slow, forgiving way: the raw text, decoded with
    email.header. Used when the policy's structured parse raises."""
    values = []
    for key, value in message.raw_items():
        if key.lower() != name.lower():
            continue
        try:
            values.append(str(email.header.make_header(email.header.decode_header(str(value)))))
        except Exception:  # noqa: BLE001 -- keep the undecoded text rather than nothing
            values.append(str(value))
    return values


def _header_values(message, name: str) -> list:
    """Every value of one header, parsed by the default policy (which is
    where a malformed header raises), else decoded from the raw text."""
    try:
        return list(message.get_all(name) or [])
    except Exception:  # noqa: BLE001 -- fall back to the raw text, never drop the header
        return _raw_header(message, name)


def header_text(message, name: str) -> str:
    values = _header_values(message, name)
    return one_line(values[0]) if values else ""


def header_addresses(message, name: str) -> list[tuple[str, str]]:
    """(display name, address) pairs from every instance of one header."""
    pairs: list[tuple[str, str]] = []
    for value in _header_values(message, name):
        found = None
        try:
            found = [(a.display_name, a.addr_spec) for a in value.addresses]
        except Exception:  # noqa: BLE001 -- not a structured header, or it would not parse
            found = None
        if found is None:
            found = email.utils.getaddresses([str(value)])
        for display, address in found:
            address = one_line(address).strip("<>")
            if "@" in address and len(address) <= MAX_ADDRESS:
                pairs.append((one_line(display), address))
    return pairs


def _decode_part(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError, ValueError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def _attachment_names(message) -> list[str]:
    names = []
    try:
        parts = list(message.iter_attachments()) if message.is_multipart() else []
    except Exception:  # noqa: BLE001 -- a malformed tree still has a body worth keeping
        parts = []
    for part in parts:
        try:
            name = part.get_filename() or part.get_content_type()
        except Exception:  # noqa: BLE001
            name = "attachment"
        names.append(one_line(name) or "attachment")
    return names


def body_text(message) -> str:
    """The message's words: the text/plain body, else its HTML body as text,
    with a closing line naming any attachments that were not copied."""
    text = ""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        part = None
    if part is not None:
        content = _decode_part(part)
        if part.get_content_type() == "text/html":
            content = html_to_text(content)
        text = clean_text(content).strip()
    names = _attachment_names(message)
    if names:
        note = "[Attachments not copied: " + ", ".join(names[:20])
        note += f" and {len(names) - 20} more]" if len(names) > 20 else "]"
        text = f"{text}\n\n{note}" if text else note
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + (
            f"\n\n[Cut at {MAX_BODY_CHARS} characters; the whole message is on the mail server.]")
    return text


def parse_flags(flags: Iterable[str] | str) -> list[str]:
    if isinstance(flags, str):
        flags = flags.split()
    return [str(flag) for flag in flags if str(flag)]


def parse_message(
    raw: bytes,
    *,
    flags: Iterable[str] | str = (),
    internaldate: str = "",
    header_only_size: int | None = None,
) -> dict[str, Any]:
    """RFC 5322 bytes to the facts the rows need.

    ``header_only_size`` says the object fetched only the header because
    the message was too big to download; the body then says exactly that,
    with the size, rather than looking empty.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    senders = header_addresses(message, "From")
    from_name, from_address = senders[0] if senders else ("", "")

    message_ids = extract_message_ids(header_text(message, "Message-ID"))
    message_id = message_ids[0] if message_ids else ""
    if not message_id:
        bare = header_text(message, "Message-ID").strip()
        if bare and " " not in bare and len(bare) + 2 <= MAX_MESSAGE_ID:
            message_id = f"<{bare}>"
    replies = extract_message_ids(header_text(message, "In-Reply-To"))
    references = extract_message_ids(" ".join(
        one_line(value) for value in _header_values(message, "References")))

    if header_only_size is not None:
        megabytes = header_only_size / (1024 * 1024)
        body = (f"[This message is {megabytes:.1f} MB, larger than this server "
                f"downloads, so only its headers were copied. Open it in your "
                f"mail program to read it.]")
    else:
        body = body_text(message)

    flag_list = parse_flags(flags)
    date_raw = header_text(message, "Date")
    return {
        "message_id": message_id,
        "in_reply_to": replies[0] if replies else "",
        "references": references,
        "subject": header_text(message, "Subject"),
        "from_name": from_name,
        "from_address": from_address,
        "to": [address for _, address in header_addresses(message, "To")],
        "cc": [address for _, address in header_addresses(message, "Cc")],
        "bcc": [address for _, address in header_addresses(message, "Bcc")],
        "date_raw": date_raw,
        "date": parse_date_header(date_raw),
        "internaldate": parse_internaldate(internaldate),
        "body_text": body,
        "flags": flag_list,
        "is_read": any(flag.lower() == "\\seen" for flag in flag_list),
        "list_unsubscribe": cap(header_text(message, "List-Unsubscribe"), MAX_HEADER_VALUE),
        "list_unsubscribe_post": cap(header_text(message, "List-Unsubscribe-Post"), MAX_HEADER_VALUE),
        "list_id": cap(header_text(message, "List-Id"), MAX_HEADER_VALUE),
        "precedence": cap(header_text(message, "Precedence"), 100),
    }


def dedupe_key(parsed: Mapping[str, Any]) -> str:
    """What makes two copies the same message: its Message-ID, or, for the
    rare message without one, a hash of who sent it, when they said, the
    subject and the start of the body. The key is compared only inside one
    owner's one account (the object scopes the lookup, ``stable_id`` scopes
    the ids), so it never needs to be globally unique."""
    if parsed.get("message_id"):
        return "mid:" + str(parsed["message_id"])
    digest = hashlib.sha256("\x1f".join([
        str(parsed.get("from_address") or ""),
        str(parsed.get("date_raw") or ""),
        str(parsed.get("subject") or ""),
        str(parsed.get("body_text") or "")[:4096],
    ]).encode("utf-8", "replace")).hexdigest()
    return "sha256:" + digest


def direction_for(parsed: Mapping[str, Any], *, account_address: str, folder_role: str) -> str:
    """outbound for what this mailbox sent (the Sent folder, or From is the
    account's own address), inbound for everything else."""
    if folder_role == "sent":
        return "outbound"
    sender = str(parsed.get("from_address") or "").casefold()
    if sender and sender == str(account_address or "").casefold():
        return "outbound"
    return "inbound"


# --- rows ----------------------------------------------------------------------

def _participants(parsed: Mapping[str, Any], account_address: str) -> list[str]:
    own = str(account_address or "").casefold()
    seen: list[str] = []
    for address in [parsed.get("from_address") or ""] + list(parsed.get("to") or []) + list(parsed.get("cc") or []):
        if address and address.casefold() != own and address.casefold() not in {s.casefold() for s in seen}:
            seen.append(address)
    return seen


def participant_summary(existing: str, parsed: Mapping[str, Any], account_address: str) -> str:
    names = [name.strip() for name in str(existing or "").split(",") if name.strip()]
    for address in _participants(parsed, account_address):
        if address.casefold() not in {name.casefold() for name in names}:
            names.append(address)
    return cap(", ".join(names), MAX_SUMMARY)


def message_rows(
    parsed: Mapping[str, Any],
    *,
    owner_id: str,
    message_id: str,
    thread_id: str,
    direction: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """The ``messages`` row and its ``message_recipients`` rows."""
    row = {
        "id": message_id,
        "thread_id": thread_id,
        "direction": direction,
        "from_address": parsed.get("from_address") or NO_SENDER,
        "subject": cap(str(parsed.get("subject") or ""), MAX_SUBJECT),
        "body_text": str(parsed.get("body_text") or ""),
        "is_read": "true" if direction == "outbound" or parsed.get("is_read") else "false",
        "in_reply_to": str(parsed.get("in_reply_to") or ""),
        "external_message_id": str(parsed.get("message_id") or ""),
        "owner_id": owner_id,
    }
    if direction == "outbound":
        stamp = parsed.get("date") or parsed.get("internaldate")
        if stamp:
            row["sent_at"] = stamp
    else:
        stamp = parsed.get("internaldate") or parsed.get("date")
        if stamp:
            row["received_at"] = stamp

    recipients = []
    for kind in ("to", "cc", "bcc"):
        seen: set[str] = set()
        for address in parsed.get(kind) or []:
            if address.casefold() in seen:
                continue
            seen.add(address.casefold())
            recipients.append({
                "id": stable_id("recipient", message_id, kind, address.casefold()),
                "message_id": message_id,
                "kind": kind,
                "address": address,
                "owner_id": owner_id,
            })
    return row, recipients


def message_time(parsed: Mapping[str, Any], direction: str) -> str:
    if direction == "outbound":
        return parsed.get("date") or parsed.get("internaldate") or ""
    return parsed.get("internaldate") or parsed.get("date") or ""


def new_thread_row(
    parsed: Mapping[str, Any], *, owner_id: str, thread_id: str,
    direction: str, account_address: str,
) -> dict[str, str]:
    row = {
        "id": thread_id,
        "thread_type": "email",
        "subject": cap(str(parsed.get("subject") or ""), MAX_SUBJECT),
        "participant_summary": participant_summary("", parsed, account_address),
        "message_count": "1",
        "is_read": "true" if direction == "outbound" or parsed.get("is_read") else "false",
        "owner_id": owner_id,
    }
    stamp = message_time(parsed, direction)
    if stamp:
        row["last_message_at"] = stamp
    return row


def thread_changes(
    thread: Mapping[str, Any], parsed: Mapping[str, Any], *,
    direction: str, account_address: str,
) -> dict[str, str]:
    """What one more message does to the thread it joined: the count goes up,
    the latest time moves forward only (an old message arriving late does
    not make a thread look fresh), an unread arrival makes the thread
    unread, and new participants are added."""
    try:
        count = int(float(str(thread.get("message_count") or "0")))
    except ValueError:
        count = 0
    changes = {"message_count": str(count + 1)}
    stamp = message_time(parsed, direction)
    current = parse_timestamp(thread.get("last_message_at"))
    incoming = parse_timestamp(stamp)
    if incoming is not None and (current is None or incoming > current):
        changes["last_message_at"] = stamp
    if direction != "outbound" and not parsed.get("is_read"):
        changes["is_read"] = "false"
    summary = participant_summary(str(thread.get("participant_summary") or ""), parsed, account_address)
    if summary != str(thread.get("participant_summary") or ""):
        changes["participant_summary"] = summary
    return changes


def synced_row(
    parsed: Mapping[str, Any], *, row_id: str, owner_id: str, account_id: str,
    folder: str, uid: int, uidvalidity: str, message_id: str, key: str,
) -> dict[str, str]:
    """The transport facts app-messaging deliberately does not carry."""
    return {
        "id": row_id,
        "account_id": account_id,
        "folder": folder,
        "uid": str(uid),
        "uidvalidity": str(uidvalidity),
        "message_id": message_id,
        "dedupe_key": key,
        "references": cap(" ".join(parsed.get("references") or []), MAX_REFERENCES),
        "list_unsubscribe": str(parsed.get("list_unsubscribe") or ""),
        "list_unsubscribe_post": str(parsed.get("list_unsubscribe_post") or ""),
        "list_id": str(parsed.get("list_id") or ""),
        "precedence": str(parsed.get("precedence") or ""),
        "owner_id": owner_id,
    }


# --- threading -----------------------------------------------------------------

class ThreadIndex:
    """Which thread a Message-ID belongs to, for ONE owner.

    Built from that owner's rows only: a row whose owner_id is anyone
    else's is skipped even if the caller passed it in, so a caller that
    forgets to filter cannot make a thread leak across mailboxes.
    """

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self.by_message_id: dict[str, str] = {}
        self.by_parent: dict[str, str] = {}

    @classmethod
    def from_rows(
        cls, owner_id: str, messages: Iterable[Mapping[str, Any]],
        synced: Iterable[Mapping[str, Any]] = (),
    ) -> "ThreadIndex":
        index = cls(owner_id)
        thread_of_message: dict[str, str] = {}
        for row in messages:
            if str(row.get("owner_id") or "") != owner_id or not row.get("thread_id"):
                continue
            thread_id = str(row["thread_id"])
            thread_of_message[str(row.get("id") or "")] = thread_id
            if row.get("external_message_id"):
                index.by_message_id.setdefault(str(row["external_message_id"]), thread_id)
            if row.get("in_reply_to"):
                index.by_parent.setdefault(str(row["in_reply_to"]), thread_id)
        for row in synced:
            if str(row.get("owner_id") or "") != owner_id:
                continue
            thread_id = thread_of_message.get(str(row.get("message_id") or ""))
            if not thread_id:
                continue
            for reference in str(row.get("references") or "").split():
                index.by_parent.setdefault(reference, thread_id)
        return index

    def add(self, parsed: Mapping[str, Any], thread_id: str) -> None:
        if parsed.get("message_id"):
            self.by_message_id.setdefault(str(parsed["message_id"]), thread_id)
        for parent in [parsed.get("in_reply_to")] + list(parsed.get("references") or []):
            if parent:
                self.by_parent.setdefault(str(parent), thread_id)


def match_thread(parsed: Mapping[str, Any], index: ThreadIndex) -> str | None:
    """The owner's existing thread this message belongs to, or None.

    In order: the message it says it replies to; then its References, the
    nearest ancestor first; then a reply to it that arrived first (Sent
    and INBOX are separate folders, so a reply can be synced before its
    parent). Never the subject: two people writing "Invoice" are not one
    conversation.
    """
    candidates = [parsed.get("in_reply_to")] + list(reversed(parsed.get("references") or []))
    for candidate in candidates:
        if candidate and candidate in index.by_message_id:
            return index.by_message_id[candidate]
    own = parsed.get("message_id")
    if own and own in index.by_parent:
        return index.by_parent[own]
    return None


# --- cursor planning -------------------------------------------------------------

def plan_folder(state: Mapping[str, Any] | None, *, uidvalidity: str) -> str:
    """``fresh`` (never synced), ``reset`` (the server renumbered the folder:
    UIDVALIDITY changed, so every stored UID is meaningless) or
    ``continue``. A fresh or reset folder starts from a recent window, and
    dedupe makes re-meeting old messages harmless."""
    stored = str((state or {}).get("uidvalidity") or "").strip()
    if not stored:
        return "fresh"
    if stored != str(uidvalidity).strip():
        return "reset"
    return "continue"


def window_baseline(window_uids: Iterable[int], *, first_window: int, highest_uid: int) -> int:
    """Where a fresh cursor starts: just below the oldest of the newest
    ``first_window`` recent messages, so the first sync takes a window and
    not the whole history. Nothing recent means start at the top: the next
    message to arrive is the first one synced."""
    recent = sorted({int(uid) for uid in window_uids})[-max(1, first_window):]
    if not recent:
        return max(0, int(highest_uid))
    return recent[0] - 1


def next_uids(found: Iterable[int], *, last_uid: int, limit: int) -> list[int]:
    """The next UIDs to import, oldest first, at most ``limit``.

    Filters ``> last_uid`` itself because ``UID SEARCH UID n:*`` answers
    with the highest message even when n is beyond it -- a classic way for
    a sync to import its newest message forever.
    """
    return sorted({int(uid) for uid in found if int(uid) > int(last_uid)})[: max(0, limit)]


def plan_downloads(
    metas: Iterable[Mapping[str, Any]], *, max_message_bytes: int, byte_budget: int,
) -> list[tuple[int, str]]:
    """(uid, "full" | "header") for this tick, oldest first.

    A message over ``max_message_bytes`` gets its header only (the body
    then says so). The byte budget bounds one tick's download because the
    sync runs inside the daemon's loop; it always admits the first message,
    so a big message cannot stall the folder, and it stops before the one
    that would overrun, which the next tick picks up.
    """
    plan: list[tuple[int, str]] = []
    spent = 0
    for meta in sorted(metas, key=lambda m: int(m["uid"])):
        size = int(meta.get("size") or 0)
        mode = "full" if size <= max_message_bytes else "header"
        cost = size if mode == "full" else 0
        if plan and spent + cost > byte_budget:
            break
        plan.append((int(meta["uid"]), mode))
        spent += cost
    return plan


# --- account scheduling -----------------------------------------------------------

def account_due(
    account: Mapping[str, Any], now: datetime, *,
    claim_ttl_seconds: int, retry_base_seconds: int, retry_max_seconds: int,
) -> tuple[bool, str]:
    """(due, reason-not). Disabled accounts, accounts another pass is
    working on, and failing accounts inside their backoff are not due.

    Backoff because a wrong password tried every two minutes forever is
    how a mail provider locks an account. It doubles from
    ``retry_base_seconds`` and stops growing at ``retry_max_seconds``; the
    owner can zero sync_attempts to try again at once.
    """
    if str(account.get("enabled") or "true").strip().lower() in {"false", "0", "no", "off"}:
        return False, "disabled"
    started = parse_timestamp(account.get("sync_started_at"))
    finished = parse_timestamp(account.get("sync_finished_at"))
    if started is not None and (finished is None or started > finished):
        if (now - started).total_seconds() < claim_ttl_seconds:
            return False, "another pass is syncing it"
    try:
        attempts = int(str(account.get("sync_attempts") or "0"))
    except ValueError:
        attempts = 0
    if attempts > 0 and finished is not None:
        try:
            wait = retry_base_seconds * (2 ** (attempts - 1))
        except OverflowError:
            wait = retry_max_seconds
        wait = min(wait, retry_max_seconds)
        resume = finished + timedelta(seconds=wait)
        if now < resume:
            return False, f"waiting after {attempts} failed tries, until {format_timestamp(resume)}"
    return True, ""


def password_service(account_id: str) -> str:
    """The identity-vault service name holding an account's password."""
    return PASSWORD_SERVICE_PREFIX + str(account_id)


def host_refusal(
    host: str, addresses: Iterable[str], *, security: str, allow_private: bool,
) -> str | None:
    """Why the server must not connect to this host, or None.

    The account's host is typed by a user, and the server is the one that
    connects, so without this check any member could make the server knock
    on addresses inside its own network (a database port on localhost, a
    metadata service) and read reachability out of the error. Private,
    loopback and link-local addresses are refused unless the operator set
    DBBASIC_MAIL_SYNC_ALLOW_PRIVATE_HOSTS (a desk with its own mail server).
    ``plain`` (no TLS) is allowed only to this machine itself: a password
    sent in the clear over any real network is a password given away.
    """
    resolved = []
    for address in addresses:
        try:
            resolved.append(ipaddress.ip_address(str(address).split("%", 1)[0]))
        except ValueError:
            return f"{host!r} resolved to {address!r}, which is not an address"
    if not resolved:
        return f"{host!r} did not resolve to any address"
    if security == "plain" and not all(ip.is_loopback for ip in resolved):
        return ("security 'plain' sends the password unencrypted, so it is only "
                "allowed to this machine itself (127.0.0.1 or ::1); use ssl or starttls")
    if not allow_private:
        for ip in resolved:
            if not ip.is_global:
                return (f"{host!r} is a private or local address ({ip}); set "
                        f"{ALLOW_PRIVATE_HOSTS_ENV}=true if this server is meant to "
                        f"reach mail servers on its own network")
    return None


# --- reading IMAP answers ----------------------------------------------------------

def _as_text(item: Any) -> str:
    if isinstance(item, tuple):
        item = item[0]
    if isinstance(item, bytes):
        return item.decode("utf-8", "replace")
    return str(item or "")


def parse_search(data: Iterable[Any]) -> list[int]:
    """UIDs from a UID SEARCH answer."""
    found: list[int] = []
    for item in data or []:
        for token in _as_text(item).split():
            if token.isdigit():
                found.append(int(token))
    return found


def parse_fetch_meta(data: Iterable[Any]) -> list[dict[str, Any]]:
    """uid, flags, size and internaldate from a ``UID FETCH (UID FLAGS
    RFC822.SIZE INTERNALDATE)`` answer, one dict per message."""
    metas = []
    for item in data or []:
        text = _as_text(item)
        uid = re.search(r"\bUID (\d+)", text)
        if not uid:
            continue
        size = re.search(r"\bRFC822\.SIZE (\d+)", text)
        flags = re.search(r"\bFLAGS \(([^)]*)\)", text)
        internal = re.search(r'\bINTERNALDATE "([^"]+)"', text)
        metas.append({
            "uid": int(uid.group(1)),
            "size": int(size.group(1)) if size else 0,
            "flags": flags.group(1).split() if flags else [],
            "internaldate": internal.group(1) if internal else "",
        })
    return metas


def fetch_literal(data: Iterable[Any]) -> bytes | None:
    """The message bytes from a ``UID FETCH (BODY.PEEK[...])`` answer."""
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and "BODY[" in _as_text(item[0]).upper():
            payload = item[1]
            return payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    return None


_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_LIST_LITERAL_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+\{\d+\}$')


def parse_list(data: Iterable[Any]) -> list[tuple[list[str], str]]:
    """(attributes, mailbox name) from a LIST answer. Names stay in the
    server's own (modified UTF-7) spelling, which is what SELECT needs."""
    entries = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            # A name sent as a literal: the attributes line, then its bytes.
            match = _LIST_LITERAL_RE.match(_as_text(item[0]).strip())
            if match and len(item) >= 2:
                entries.append((match.group("flags").split(), _as_text(item[1])))
            continue
        match = _LIST_RE.match(_as_text(item).strip())
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = re.sub(r'\\(.)', r'\1', name[1:-1])
        entries.append((match.group("flags").split(), name))
    return entries


_SENT_NAMES = ("sent", "sent items", "sent messages", "sent mail", "inbox.sent",
               "[gmail]/sent mail")


def choose_folders(entries: Iterable[tuple[list[str], str]]) -> list[tuple[str, str]]:
    """(name, role) for a new account: INBOX, and the Sent folder when the
    server has one -- by its \\Sent attribute (RFC 6154) first, then by the
    names servers commonly use."""
    entries = list(entries)
    chosen = [("INBOX", "inbox")]
    sent = next((name for attrs, name in entries
                 if any(a.lower() == "\\sent" for a in attrs)), None)
    if sent is None:
        sent = next((name for attrs, name in entries
                     if name.lower() in _SENT_NAMES
                     and not any(a.lower() == "\\noselect" for a in attrs)), None)
    if sent and sent.upper() != "INBOX":
        chosen.append((sent, "sent"))
    return chosen


def folder_role(name: str, entries: Iterable[tuple[list[str], str]]) -> str:
    """inbox, sent or other, for a folder the owner named themselves."""
    if str(name).upper() == "INBOX":
        return "inbox"
    for attrs, listed in entries:
        if listed == name and any(a.lower() == "\\sent" for a in attrs):
            return "sent"
    return "sent" if str(name).lower() in _SENT_NAMES else "other"


def split_folders(value: str) -> list[str]:
    """The account's folders field: names separated by commas."""
    names = []
    for name in str(value or "").split(","):
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def quote_mailbox(name: str) -> str:
    """A mailbox name as an IMAP quoted string (imaplib does not quote).

    A line break inside a quoted string would end the command and let the
    rest of the name run as a second one on the person's mailbox (a
    DELETE, an EXPUNGE), so a name carrying one is refused, not quoted.
    """
    name = str(name)
    if "\r" in name or "\n" in name or "\x00" in name:
        raise ValueError("a mailbox name cannot contain a line break")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
