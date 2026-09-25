"""WebDAV (/dav/files/): a person's files as a folder a desktop can mount.

What these guard, and why it would matter:

- Only the key's owner's files are listed, and only an API key opens the
  folder: a mounted folder is a standing connection, so the wrong files or a
  browser cookie riding along would be a quiet, durable leak.
- Writes go through the same quota, permission and record paths as
  /api/files: a second way in that skipped one would be a hole with a
  convenient shape.
- Refusals are refusals (MKCOL, LOCK): a desktop told a folder or a lock
  exists when it does not loses work later instead of failing now.
"""

import base64
import json

import pytest
from test_object_server import create_identity_session, raw_request, request
from test_object_user_files import files_env, multipart_body

import object_api_keys
import object_server
import object_user_files
import object_webdav


def dav_env(tmp_path, monkeypatch):
    data_dir = files_env(tmp_path, monkeypatch)
    monkeypatch.setenv(object_server.WEBDAV_ENABLED_ENV, "true")
    # enforce the files policy for real (as the desk does), so these tests
    # exercise the owner row filter rather than the admin-token fallback
    monkeypatch.setenv(object_server.PERMISSION_UNREADY_ENFORCEMENT_ENV, "true")
    return data_dir


def key_for(data_dir, user_id="dan"):
    _, token = object_api_keys.create_api_key(user_id, "desk", base_dir=data_dir)
    return token


def basic(token, user="anyone"):
    raw = base64.b64encode(f"{user}:{token}".encode()).decode()
    return [("authorization", f"Basic {raw}")]


def dav(path, token, method="GET", body=b"", headers=()):
    return raw_request(path, method=method, body=body, headers=basic(token) + list(headers))


def listing(token, path="/dav/files/", depth="1"):
    status, _, body = dav(path, token, method="PROPFIND", headers=[("depth", depth)])
    assert status == 207, body
    return body.decode()


def names_in(xml):
    import re

    return re.findall(r"<D:displayname>([^<]*)</D:displayname>", xml)


def records(data_dir):
    lines = (data_dir / "collections" / "files" / "records.tsv").read_text().splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, line.split("\t"), strict=False)) for line in lines[1:] if line.strip()]


# --- the pure half ---------------------------------------------------------


def test_a_filename_is_made_safe_for_a_path_not_trusted():
    assert object_webdav.safe_name("a/b\\c.txt") == "a_b_c.txt"
    assert object_webdav.safe_name("bad\x00\x1fname") == "badname"
    assert object_webdav.safe_name("..") == "unnamed"
    assert object_webdav.safe_name("") == "unnamed"
    assert len(object_webdav.safe_name("x" * 400)) == 255


def test_duplicate_names_get_a_suffix_and_the_oldest_keeps_the_plain_name():
    folder = object_webdav.folder_names(
        [
            {"id": "b", "filename": "notes.txt", "created_at": "2026-01-02T00:00:00Z"},
            {"id": "a", "filename": "notes.txt", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "c", "filename": "NOTES.txt", "created_at": "2026-01-03T00:00:00Z"},
            {"id": "d", "filename": "README", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "e", "filename": "README", "created_at": "2026-01-02T00:00:00Z"},
        ]
    )
    assert folder["notes.txt"]["id"] == "a"
    assert folder["notes (2).txt"]["id"] == "b"
    assert folder["NOTES (3).txt"]["id"] == "c"  # case-insensitive, like the desktops
    assert folder["README (2)"]["id"] == "e"


def test_paths_outside_the_two_level_tree_do_not_exist():
    assert object_webdav.split_path("/dav") == ("root", None)
    assert object_webdav.split_path("/dav/files/") == ("files", None)
    assert object_webdav.split_path("/dav/files/a b.txt") == ("file", "a b.txt")
    for path in ("/dav/other/", "/dav/files/a/b", "/davx"):
        with pytest.raises(object_webdav.DavRequestError):
            object_webdav.split_path(path)


def test_ranges_serve_one_slice_and_refuse_what_cannot_be_served():
    assert object_webdav.parse_range("bytes=0-3", 10) == (0, 3)
    assert object_webdav.parse_range("bytes=5-", 10) == (5, 9)
    assert object_webdav.parse_range("bytes=-4", 10) == (6, 9)
    assert object_webdav.parse_range("bytes=2-99", 10) == (2, 9)
    assert object_webdav.parse_range("bytes=0-1,4-5", 10) is None  # whole file
    with pytest.raises(object_webdav.DavRequestError):
        object_webdav.parse_range("bytes=10-", 10)


def test_a_destination_is_read_as_a_path_whatever_host_it_names():
    assert object_webdav.destination_path("http://proxy:9/dav/files/a%20b.txt") == "/dav/files/a b.txt"
    assert object_webdav.destination_path("/dav/files/x") == "/dav/files/x"


# --- the surface -----------------------------------------------------------


def test_the_folder_is_off_unless_enabled(tmp_path, monkeypatch):
    data_dir = files_env(tmp_path, monkeypatch)
    status, _, _ = dav("/dav/files/", key_for(data_dir), method="PROPFIND")
    assert status == 404


def test_without_an_api_key_the_folder_asks_for_one(tmp_path, monkeypatch):
    """A password or a session cookie must not open it: a password here would
    be a login without the login's rate limits, and a cookie would let any
    page the person visits write to their files."""
    dav_env(tmp_path, monkeypatch)
    status, headers, _ = raw_request("/dav/files/", method="PROPFIND")
    assert status == 401
    assert headers[b"www-authenticate"].startswith(b"Basic")

    status, _, _ = dav("/dav/files/", "not-a-key", method="PROPFIND")
    assert status == 401

    session, _ = create_identity_session({"user_id": "dan"})
    status, _, _ = raw_request(
        "/dav/files/", method="PROPFIND", headers=[("cookie", f"dbbasic_session={session}")]
    )
    assert status == 401
    status, _, _ = raw_request(
        "/dav/files/", method="PROPFIND", headers=[("authorization", f"Bearer {session}")]
    )
    assert status == 401


def test_options_says_class_1_without_asking_for_a_key(tmp_path, monkeypatch):
    dav_env(tmp_path, monkeypatch)
    status, headers, _ = raw_request("/dav/files/", method="OPTIONS")
    assert status == 200
    assert headers[b"dav"] == b"1"
    assert b"LOCK" not in headers[b"allow"]


def test_put_creates_a_file_that_the_files_api_also_serves(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    status, _, _ = dav("/dav/files/hello.txt", token, method="PUT", body=b"hi there")
    assert status == 201

    [record] = records(data_dir)
    assert record["filename"] == "hello.txt"
    assert record["owner_id"] == "dan"
    assert record["size"] == "8"
    assert record["content_type"] == "text/plain"

    status, _, body = raw_request(f"/api/files/{record['id']}", headers=[("authorization", f"Bearer {token}")])
    assert status == 200 and body == b"hi there"
    assert "hello.txt" in names_in(listing(token))


def test_only_the_key_owners_files_are_in_the_folder(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    dan, eve = key_for(data_dir, "dan"), key_for(data_dir, "eve")
    dav("/dav/files/dans.txt", dan, method="PUT", body=b"d")
    dav("/dav/files/eves.txt", eve, method="PUT", body=b"e")

    assert names_in(listing(dan)) == ["files", "dans.txt"]
    status, _, _ = dav("/dav/files/eves.txt", dan)
    assert status == 404
    status, _, _ = dav("/dav/files/eves.txt", dan, method="DELETE")
    assert status == 404
    assert len(records(data_dir)) == 2


def test_a_put_to_an_existing_name_replaces_the_bytes_and_keeps_the_record(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"one")
    [before] = records(data_dir)
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"second version")
    assert status == 204
    [after] = records(data_dir)
    assert after["id"] == before["id"]
    assert after["size"] == "14"
    status, _, body = dav("/dav/files/a.txt", token)
    assert body == b"second version"


def test_conditional_writes_refuse_to_clobber(tmp_path, monkeypatch):
    """If-None-Match: * is "create only"; a stale If-Match is someone else's
    newer copy. Without these a client can silently overwrite."""
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"one")
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"x", headers=[("if-none-match", "*")])
    assert status == 412
    status, headers, _ = dav("/dav/files/a.txt", token)
    etag = headers[b"etag"].decode()
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"two", headers=[("if-match", '"stale"')])
    assert status == 412
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"two", headers=[("if-match", etag)])
    assert status == 204


def test_move_renames_and_will_not_replace_without_overwrite(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"aaa")
    dav("/dav/files/b.md", token, method="PUT", body=b"bbb")

    status, _, _ = dav(
        "/dav/files/a.txt", token, method="MOVE",
        headers=[("destination", "http://desk/dav/files/b.md"), ("overwrite", "F")],
    )
    assert status == 412

    status, _, _ = dav(
        "/dav/files/a.txt", token, method="MOVE", headers=[("destination", "/dav/files/Renamed%20a.md")]
    )
    assert status == 201
    names = sorted(r["filename"] for r in records(data_dir))
    assert names == ["Renamed a.md", "b.md"]
    renamed = next(r for r in records(data_dir) if r["filename"] == "Renamed a.md")
    assert renamed["content_type"] == "text/markdown"

    status, _, _ = dav(
        "/dav/files/Renamed a.md", token, method="MOVE", headers=[("destination", "/dav/files/b.md")]
    )
    assert status == 204
    [left] = records(data_dir)
    assert left["filename"] == "b.md" and left["id"] == renamed["id"]
    status, _, body = dav("/dav/files/b.md", token)
    assert body == b"aaa"


def test_a_rename_that_only_changes_case_is_a_rename(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/readme.txt", token, method="PUT", body=b"r")
    status, _, _ = dav("/dav/files/readme.txt", token, method="MOVE", headers=[("destination", "/dav/files/README.txt")])
    assert status == 201
    assert [r["filename"] for r in records(data_dir)] == ["README.txt"]


def test_copy_makes_a_second_file(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"same")
    status, _, _ = dav("/dav/files/a.txt", token, method="COPY", headers=[("destination", "/dav/files/c.txt")])
    assert status == 201
    assert sorted(r["filename"] for r in records(data_dir)) == ["a.txt", "c.txt"]
    status, _, body = dav("/dav/files/c.txt", token)
    assert body == b"same"


def test_delete_removes_the_record_and_the_bytes(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"gone soon")
    [record] = records(data_dir)
    status, _, _ = dav("/dav/files/a.txt", token, method="DELETE")
    assert status == 204
    assert records(data_dir) == []
    assert not object_user_files.file_path("dan", record["id"], base_dir=data_dir).exists()


def test_a_range_is_served_as_206(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/v.bin", token, method="PUT", body=b"0123456789")
    status, headers, body = dav("/dav/files/v.bin", token, headers=[("range", "bytes=2-5")])
    assert status == 206
    assert body == b"2345"
    assert headers[b"content-range"] == b"bytes 2-5/10"
    status, headers, _ = dav("/dav/files/v.bin", token, headers=[("range", "bytes=50-")])
    assert status == 416


def test_an_uploaded_page_is_served_sandboxed_as_an_attachment(tmp_path, monkeypatch):
    """Served from the server's own origin: a stored HTML file must not run
    as a page there, or uploading it would be stored XSS."""
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/x.html", token, method="PUT", body=b"<script>alert(1)</script>")
    status, headers, _ = dav("/dav/files/x.html", token)
    assert status == 200
    assert headers[b"content-security-policy"] == b"sandbox"
    assert headers[b"content-disposition"] == b"attachment"
    assert headers[b"x-content-type-options"] == b"nosniff"


def test_propfind_answers_what_was_asked_and_names_what_is_missing(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"abc")
    body = (
        b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:Z="urn:x">'
        b"<D:prop><D:getcontentlength/><Z:quota/></D:prop></D:propfind>"
    )
    status, _, xml = dav("/dav/files/a.txt", token, method="PROPFIND", body=body, headers=[("depth", "0")])
    assert status == 207
    xml = xml.decode()
    assert "<D:getcontentlength>3</D:getcontentlength>" in xml
    assert "404 Not Found" in xml and "quota" in xml
    assert "getlastmodified" not in xml

    root = listing(token, "/dav/", depth="infinity")
    assert names_in(root) == ["dav", "files", "a.txt"]


def test_a_propfind_with_a_doctype_is_refused(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    body = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><D:propfind xmlns:D="DAV:"/>'
    status, _, _ = dav("/dav/files/", key_for(data_dir), method="PROPFIND", body=body)
    assert status == 400


def test_folders_and_locks_are_refused_not_faked(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    for method, path in (("MKCOL", "/dav/files/new/"), ("LOCK", "/dav/files/a.txt"), ("UNLOCK", "/dav/files/a.txt")):
        status, headers, _ = dav(path, token, method=method)
        assert status == 405, method
        assert b"PROPFIND" in headers[b"allow"]


def test_proppatch_refuses_each_property_with_403(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a.txt", token, method="PUT", body=b"a")
    body = (
        b'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:W="urn:schemas-microsoft-com:">'
        b"<D:set><D:prop><W:Win32LastModifiedTime>x</W:Win32LastModifiedTime></D:prop></D:set>"
        b"</D:propertyupdate>"
    )
    status, _, xml = dav("/dav/files/a.txt", token, method="PROPPATCH", body=body)
    assert status == 207
    assert b"403 Forbidden" in xml and b"Win32LastModifiedTime" in xml


def test_the_quota_holds_for_new_files_and_for_replacements(tmp_path, monkeypatch):
    data_dir = dav_env(tmp_path, monkeypatch)
    monkeypatch.setenv(object_server.USER_FILES_QUOTA_ENV, "10")
    token = key_for(data_dir)
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"12345678")
    assert status == 201
    status, _, _ = dav("/dav/files/b.txt", token, method="PUT", body=b"123")
    assert status == 413
    # replacing a file frees its old bytes first: 8 -> 10 fits, 8 -> 11 does not
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"1234567890")
    assert status == 204
    status, _, _ = dav("/dav/files/a.txt", token, method="PUT", body=b"12345678901")
    assert status == 507


def test_a_put_may_exceed_the_general_request_cap_up_to_its_own(tmp_path, monkeypatch):
    """A file is bigger than an API call; the PUT cap is its own and still a
    cap, because the body is read into memory whole."""
    data_dir = dav_env(tmp_path, monkeypatch)
    monkeypatch.setenv(object_server.MAX_REQUEST_BYTES_ENV, "16")
    monkeypatch.setenv(object_server.WEBDAV_MAX_FILE_BYTES_ENV, "64")
    token = key_for(data_dir)
    status, _, _ = dav("/dav/files/a.bin", token, method="PUT", body=b"x" * 40)
    assert status == 201
    status, _, _ = dav("/dav/files/b.bin", token, method="PUT", body=b"x" * 65)
    assert status == 413
    # everything else keeps the general cap
    body, content_type = multipart_body("file", "c.txt", b"x" * 40)
    status, _, _ = request(
        "/api/files", method="POST", body=body,
        headers=[("authorization", f"Bearer {token}"), ("content-type", content_type)],
    )
    assert status == 413


def test_the_files_folder_listing_is_valid_xml(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    data_dir = dav_env(tmp_path, monkeypatch)
    token = key_for(data_dir)
    dav("/dav/files/a <&> b.txt", token, method="PUT", body=b"odd name")
    root = ET.fromstring(listing(token))
    hrefs = [e.text for e in root.iter("{DAV:}href")]
    assert "/dav/files/a%20%3C%26%3E%20b.txt" in hrefs
    assert json.dumps(names_in(listing(token)))  # escaped, still readable


def test_someone_elses_public_file_is_not_in_your_folder(tmp_path, monkeypatch):
    """The policy lets everyone read a public file, but it is still the
    owner's: in your folder it would be yours to rename or overwrite."""
    data_dir = dav_env(tmp_path, monkeypatch)
    dan, eve = key_for(data_dir, "dan"), key_for(data_dir, "eve")
    body, content_type = multipart_body("file", "flyer.pdf", b"%PDF", extra=[("is_public", "true")])
    eve_session, _ = create_identity_session({"user_id": "eve"})
    status, _, uploaded = request(
        "/api/files", method="POST", body=body,
        headers=[("authorization", f"Bearer {eve_session}"), ("content-type", content_type)],
    )
    assert status == 201 and uploaded["file"]["is_public"] == "true"
    status, _, _ = raw_request(f"/api/files/{uploaded['file']['id']}", headers=[("authorization", f"Bearer {dan}")])
    assert status == 200  # dan may read it...
    assert names_in(listing(dan)) == ["files"]  # ...but it is not in his folder
    assert "flyer.pdf" in names_in(listing(eve))
