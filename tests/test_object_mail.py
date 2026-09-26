"""object_mail: the pure half of inbound mail -- parsing, dedupe, threading,
cursor planning -- tested with no server, no clock and no data directory.

The object-level claims (never marks read, never crosses owners, never
loses a message to a crash) are in tests/test_mail_sync.py; these are the
decisions underneath them, one at a time.
"""

from datetime import datetime, timedelta, timezone

import object_mail

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def raw(*headers, body="Hello.", content_type="text/plain; charset=utf-8"):
    head = "\r\n".join(headers + ("MIME-Version: 1.0", f"Content-Type: {content_type}"))
    return (head + "\r\n\r\n" + body + "\r\n").encode("utf-8")


# --- parsing -----------------------------------------------------------------------

def test_encoded_headers_are_decoded_and_dates_use_the_server_spelling():
    parsed = object_mail.parse_message(raw(
        "From: =?utf-8?q?J=C3=B6rg?= <jorg@example.org>",
        'To: me@dove.test, "Doe, Jane" <jane@example.org>',
        "Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe?=",
        "Date: Fri, 25 Sep 2026 10:00:00 +0200",
        "Message-ID: <abc@example.org>"), internaldate="25-Sep-2026 08:00:01 +0000")
    assert parsed["subject"] == "Grüße"
    assert parsed["from_name"] == "Jörg" and parsed["from_address"] == "jorg@example.org"
    assert parsed["to"] == ["me@dove.test", "jane@example.org"]
    assert parsed["date"] == "2026-09-25T08:00:00Z"
    assert parsed["internaldate"] == "2026-09-25T08:00:01Z"
    assert parsed["message_id"] == "<abc@example.org>"


def test_plain_text_is_preferred_and_html_is_turned_into_text_when_alone():
    alternative = (
        "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain words\r\n"
        "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Html words</p>\r\n--b--")
    parsed = object_mail.parse_message(raw(
        "From: a@example.org", body=alternative,
        content_type="multipart/alternative; boundary=b"))
    assert parsed["body_text"] == "Plain words"

    only_html = object_mail.parse_message(raw(
        "From: a@example.org",
        body="<html><head><style>p{color:red}</style></head><body><p>One&nbsp;<b>two</b></p>"
             "<script>alert(1)</script><ul><li>three</li></ul></body></html>",
        content_type="text/html; charset=utf-8"))
    assert only_html["body_text"] == "One two\n\n- three"


def test_attachments_are_named_not_copied():
    body = ("--b\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
            "--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
            "filename=invoice.pdf\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--b--")
    parsed = object_mail.parse_message(raw("From: a@example.org", body=body,
                                           content_type="multipart/mixed; boundary=b"))
    assert parsed["body_text"] == "See attached.\n\n[Attachments not copied: invoice.pdf]"


def test_broken_bytes_become_storable_text_instead_of_a_failed_write():
    """A raw 8-bit subject, an unknown charset and a NUL must not stop the
    message from being stored; the substrate refuses NUL and lone surrogates."""
    message = (b"From: a@example.org\r\nSubject: caf\xe9 \x00menu\r\n"
               b"Content-Type: text/plain; charset=x-unknown\r\n\r\nbody \xff\x00 text\r\n")
    parsed = object_mail.parse_message(message)
    for value in (parsed["subject"], parsed["body_text"]):
        value.encode("utf-8")
        assert "\x00" not in value
    assert parsed["subject"].startswith("caf")
    assert "text" in parsed["body_text"]


def test_a_broken_date_is_left_blank_not_guessed():
    parsed = object_mail.parse_message(raw("From: a@example.org", "Date: yesterday-ish"))
    assert parsed["date"] == ""
    assert object_mail.parse_internaldate("not a date") == ""
    assert object_mail.parse_internaldate(" 1-Jan-2026 00:00:00 -0500") == "2026-01-01T05:00:00Z"


def test_a_long_body_is_cut_and_says_so():
    parsed = object_mail.parse_message(raw("From: a@example.org",
                                           body="x" * (object_mail.MAX_BODY_CHARS + 50)))
    assert len(parsed["body_text"]) < object_mail.MAX_BODY_CHARS + 200
    assert "the whole message is on the mail server" in parsed["body_text"]


def test_a_header_only_copy_says_how_big_the_message_was():
    parsed = object_mail.parse_message(raw("From: a@example.org"),
                                       header_only_size=12 * 1024 * 1024)
    assert "12.0 MB" in parsed["body_text"] and "only its headers" in parsed["body_text"]


def test_is_read_is_the_servers_seen_flag():
    assert object_mail.parse_message(raw("From: a@x.org"), flags="\\Seen \\Flagged")["is_read"]
    assert not object_mail.parse_message(raw("From: a@x.org"), flags=["\\Flagged"])["is_read"]


def test_list_headers_are_kept_as_text():
    parsed = object_mail.parse_message(raw(
        "From: a@example.org", "List-Unsubscribe: <https://example.org/u>, <mailto:u@example.org>",
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click", "List-Id: News <news.example.org>",
        "Precedence: bulk"))
    assert parsed["list_unsubscribe"] == "<https://example.org/u>, <mailto:u@example.org>"
    assert parsed["list_unsubscribe_post"] == "List-Unsubscribe=One-Click"
    assert parsed["list_id"] == "News <news.example.org>"
    assert parsed["precedence"] == "bulk"


# --- dedupe and ids -------------------------------------------------------------------

def test_the_dedupe_key_is_the_message_id_or_a_hash_without_one():
    with_id = object_mail.parse_message(raw("From: a@x.org", "Message-ID: <k@x>"))
    assert object_mail.dedupe_key(with_id) == "mid:<k@x>"
    without = object_mail.parse_message(raw("From: a@x.org", "Subject: s", "Date: Fri, 25 Sep 2026 10:00:00 +0000"))
    key = object_mail.dedupe_key(without)
    assert key.startswith("sha256:") and key == object_mail.dedupe_key(dict(without))
    other = object_mail.parse_message(raw("From: b@x.org", "Subject: s", "Date: Fri, 25 Sep 2026 10:00:00 +0000"))
    assert object_mail.dedupe_key(other) != key


def test_ids_are_the_same_for_the_same_facts_and_differ_by_owner():
    """The replay safety and the tenant boundary are the same property."""
    assert object_mail.stable_id("message", "u1", "a1", "mid:<k@x>") == \
        object_mail.stable_id("message", "u1", "a1", "mid:<k@x>")
    assert object_mail.stable_id("message", "u1", "a1", "mid:<k@x>") != \
        object_mail.stable_id("message", "u2", "a1", "mid:<k@x>")


# --- rows --------------------------------------------------------------------------------

def test_message_rows_fit_the_app_messaging_schema():
    parsed = object_mail.parse_message(raw(
        "From: a@example.org", "To: me@dove.test, ME@dove.test", "Cc: c@example.org",
        "Subject: " + "s" * 400, "Message-ID: <k@x>"), internaldate="25-Sep-2026 08:00:00 +0000")
    row, recipients = object_mail.message_rows(parsed, owner_id="u1", message_id="m1",
                                               thread_id="t1", direction="inbound")
    assert len(row["subject"]) == object_mail.MAX_SUBJECT
    assert row["is_read"] == "false" and row["received_at"] == "2026-09-25T08:00:00Z"
    assert "sent_at" not in row
    assert [(r["kind"], r["address"]) for r in recipients] == [("to", "me@dove.test"),
                                                               ("cc", "c@example.org")]
    assert {r["owner_id"] for r in recipients} == {"u1"}

    outbound, _ = object_mail.message_rows(parsed, owner_id="u1", message_id="m1",
                                           thread_id="t1", direction="outbound")
    assert outbound["is_read"] == "true" and "sent_at" in outbound


def test_a_message_with_no_from_says_so():
    parsed = object_mail.parse_message(raw("Subject: who"))
    row, _ = object_mail.message_rows(parsed, owner_id="u1", message_id="m", thread_id="t",
                                      direction="inbound")
    assert row["from_address"] == object_mail.NO_SENDER


def test_direction_is_the_sent_folder_or_the_accounts_own_address():
    parsed = {"from_address": "Me@Dove.test"}
    assert object_mail.direction_for(parsed, account_address="me@dove.test", folder_role="inbox") == "outbound"
    assert object_mail.direction_for({"from_address": "x@y"}, account_address="me@dove.test",
                                     folder_role="sent") == "outbound"
    assert object_mail.direction_for({"from_address": "x@y"}, account_address="me@dove.test",
                                     folder_role="inbox") == "inbound"


def test_a_late_old_message_does_not_make_a_thread_look_fresh():
    thread = {"message_count": "3", "last_message_at": "2026-09-25T10:00:00Z",
              "participant_summary": "a@example.org"}
    older = {"internaldate": "2026-09-01T10:00:00Z", "is_read": False,
             "from_address": "b@example.org"}
    changes = object_mail.thread_changes(thread, older, direction="inbound",
                                         account_address="me@dove.test")
    assert changes["message_count"] == "4"
    assert "last_message_at" not in changes
    assert changes["is_read"] == "false"
    assert changes["participant_summary"] == "a@example.org, b@example.org"
    newer = dict(older, internaldate="2026-09-25T10:00:01+00:00", is_read=True)
    assert object_mail.thread_changes(thread, newer, direction="inbound",
                                      account_address="me@dove.test")["last_message_at"]


# --- threading ------------------------------------------------------------------------------

def _index(owner="u1"):
    messages = [
        {"id": "m1", "owner_id": "u1", "thread_id": "t1", "external_message_id": "<root@x>"},
        {"id": "m2", "owner_id": "u2", "thread_id": "t9", "external_message_id": "<theirs@x>"},
        {"id": "m3", "owner_id": "u1", "thread_id": "t3", "external_message_id": "<kid@x>",
         "in_reply_to": "<late-parent@x>"},
    ]
    synced = [{"owner_id": "u1", "message_id": "m1", "references": "<ancient@x>"}]
    return object_mail.ThreadIndex.from_rows(owner, messages, synced)


def test_threading_follows_in_reply_to_then_references_then_a_reply_that_came_first():
    index = _index()
    assert object_mail.match_thread({"in_reply_to": "<root@x>"}, index) == "t1"
    assert object_mail.match_thread({"references": ["<nope@x>", "<root@x>"]}, index) == "t1"
    assert object_mail.match_thread({"message_id": "<late-parent@x>"}, index) == "t3"
    assert object_mail.match_thread({"message_id": "<ancient@x>"}, index) == "t1"


def test_threading_never_crosses_owners_even_when_handed_their_rows():
    assert object_mail.match_thread({"in_reply_to": "<theirs@x>"}, _index("u1")) is None
    assert object_mail.match_thread({"in_reply_to": "<root@x>"}, _index("u2")) is None


def test_the_subject_alone_joins_nothing():
    index = _index()
    index.by_message_id["<x@x>"] = "t1"
    assert object_mail.match_thread({"subject": "Re: Invoice", "message_id": "<new@x>"}, index) is None


# --- cursor planning ------------------------------------------------------------------------

def test_plan_folder_resets_when_uidvalidity_changes():
    assert object_mail.plan_folder(None, uidvalidity="5") == "fresh"
    assert object_mail.plan_folder({"uidvalidity": "5"}, uidvalidity="5") == "continue"
    assert object_mail.plan_folder({"uidvalidity": "5"}, uidvalidity="6") == "reset"


def test_the_first_window_is_the_newest_n_and_an_empty_window_starts_at_the_top():
    assert object_mail.window_baseline(range(1, 501), first_window=200, highest_uid=500) == 300
    assert object_mail.window_baseline([], first_window=200, highest_uid=42) == 42
    assert object_mail.window_baseline([7], first_window=200, highest_uid=42) == 6


def test_next_uids_drops_the_highest_message_n_star_hands_back():
    assert object_mail.next_uids([10], last_uid=10, limit=50) == []
    assert object_mail.next_uids([13, 11, 12, 11], last_uid=10, limit=2) == [11, 12]


def test_downloads_are_bounded_and_a_big_message_never_stalls_a_folder():
    metas = [{"uid": 1, "size": 30}, {"uid": 2, "size": 500}, {"uid": 3, "size": 30},
             {"uid": 4, "size": 30}]
    assert object_mail.plan_downloads(metas, max_message_bytes=100, byte_budget=70) == [
        (1, "full"), (2, "header"), (3, "full")]
    assert object_mail.plan_downloads([{"uid": 9, "size": 90}], max_message_bytes=100,
                                      byte_budget=10) == [(9, "full")]


def test_imap_dates_do_not_depend_on_the_locale():
    assert object_mail.imap_date(datetime(2026, 9, 6)) == "6-Sep-2026"


# --- when an account is due -------------------------------------------------------------------

def _due(account, now=NOW):
    return object_mail.account_due(account, now, claim_ttl_seconds=900,
                                   retry_base_seconds=120, retry_max_seconds=1800)


def test_account_due_respects_disable_claims_and_backoff():
    assert _due({"enabled": "false"}) == (False, "disabled")
    assert _due({})[0] is True
    held = {"sync_started_at": "2026-09-26T11:55:00Z", "sync_finished_at": "2026-09-26T11:00:00Z"}
    assert _due(held)[0] is False
    stale = dict(held, sync_started_at="2026-09-26T11:00:30Z", sync_finished_at="2026-09-26T11:00:00Z")
    assert _due(stale)[0] is True, "a claim nobody finished expires"
    failing = {"sync_attempts": "3", "sync_finished_at": "2026-09-26T11:55:00Z"}
    assert _due(failing)[0] is False                      # waits 8 minutes after 3 failures
    assert _due(failing, NOW + timedelta(minutes=4))[0] is True
    hopeless = {"sync_attempts": "40", "sync_finished_at": "2026-09-26T11:00:00Z"}
    assert _due(hopeless)[0] is True, "the wait stops growing at the cap"


# --- where the server may connect -------------------------------------------------------------

def test_private_and_plaintext_hosts_are_refused_by_default():
    assert object_mail.host_refusal("imap.x", ["1.1.1.1"], security="ssl", allow_private=False) is None
    refused = object_mail.host_refusal("db", ["10.1.2.3"], security="ssl", allow_private=False)
    assert object_mail.ALLOW_PRIVATE_HOSTS_ENV in refused
    assert object_mail.host_refusal("db", ["10.1.2.3"], security="ssl", allow_private=True) is None
    assert object_mail.host_refusal("x", ["169.254.169.254"], security="ssl", allow_private=False)
    assert object_mail.host_refusal("x", ["1.1.1.1"], security="plain", allow_private=True)
    assert object_mail.host_refusal("localhost", ["127.0.0.1", "::1"], security="plain",
                                    allow_private=True) is None
    assert object_mail.host_refusal("x", [], security="ssl", allow_private=True)


# --- reading IMAP answers ----------------------------------------------------------------------

def test_fetch_answers_are_read_without_a_server():
    metas = object_mail.parse_fetch_meta([
        b'1 (UID 7 FLAGS (\\Seen \\Answered) RFC822.SIZE 1234 INTERNALDATE "25-Sep-2026 08:00:00 +0000")',
        b'2 (UID 8 FLAGS () RFC822.SIZE 99 INTERNALDATE " 5-Sep-2026 08:00:00 +0000")'])
    assert metas[0] == {"uid": 7, "size": 1234, "flags": ["\\Seen", "\\Answered"],
                        "internaldate": "25-Sep-2026 08:00:00 +0000"}
    assert metas[1]["flags"] == []
    assert object_mail.fetch_literal([(b"1 (UID 7 BODY[] {3}", b"abc"), b")"]) == b"abc"
    assert object_mail.fetch_literal([b"1 (UID 7)"]) is None
    assert object_mail.parse_search([b"3 4 5"]) == [3, 4, 5]
    assert object_mail.parse_search([None]) == []


def test_folders_are_found_by_the_sent_attribute_then_by_name():
    entries = object_mail.parse_list([
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
        b'(\\HasNoChildren) "/" Archive',
        (b'(\\HasNoChildren) "/" {10}', b'Quoted "x"'),
    ])
    assert ([], "Archive") not in entries
    assert (["\\HasNoChildren"], "Archive") in entries
    assert (["\\HasNoChildren"], 'Quoted "x"') in entries
    assert object_mail.choose_folders(entries) == [("INBOX", "inbox"), ("Sent Items", "sent")]
    by_name = object_mail.choose_folders([([], "INBOX"), ([], "Sent")])
    assert by_name == [("INBOX", "inbox"), ("Sent", "sent")]
    assert object_mail.choose_folders([([], "INBOX")]) == [("INBOX", "inbox")]
    assert object_mail.folder_role("Sent Items", entries) == "sent"
    assert object_mail.folder_role("Archive", entries) == "other"
    assert object_mail.quote_mailbox('a "b"') == '"a \\"b\\""'
    assert object_mail.split_folders("INBOX, Sent, ,INBOX") == ["INBOX", "Sent"]


def test_a_line_break_in_a_folder_name_is_refused_not_sent():
    """Inside a quoted string a CRLF ends the command and starts another:
    'INBOX\\r\\nA1 DELETE INBOX' would delete the person's inbox."""
    import pytest

    with pytest.raises(ValueError):
        object_mail.quote_mailbox("INBOX\r\nA1 DELETE INBOX")
