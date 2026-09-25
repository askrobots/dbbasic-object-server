"""Files as a folder: the pure half of the WebDAV surface at ``/dav/``.

A person's files (the ``files`` collection, bytes under ``user_files/``) are
records, not a directory tree: each has an id, a ``filename`` that need not
be unique, and no parent folder. WebDAV lets every desktop mount them as a
folder anyway -- Linux (davfs2, the file manager's "Connect to Server"),
macOS Finder, Windows -- without a client of our own. The server side lives
in ``object_server._handle_webdav``; this module is what it needs that does
no I/O: naming, paths, the XML, ranges.

Decisions, and what was rejected:

- **Flat, by name.** ``/dav/files/`` holds the caller's own files, one
  entry per record. Folders were rejected for now: the records have none,
  and inventing a tree here would be a second, drifting notion of where a
  file "is". MKCOL is refused (405), not faked.
- **Duplicate names get a suffix, oldest keeps the plain name.** Two uploads
  of ``notes.txt`` show as ``notes.txt`` and ``notes (2).txt``, ordered by
  creation then id, so the mapping is stable while both exist. The records
  are not renamed: the suffix is only how the folder shows them.
- **No LOCK.** A lock that is not enforced is a stub that pretends, which
  this codebase refuses (AGENTS.md section 4). Without it the server is DAV
  class 1: davfs2 needs ``use_locks 0``, and macOS Finder mounts read-only.
  Real locks (enforced on PUT/DELETE/MOVE) can come later.
- **Names from a record are made safe for a path, never trusted.** A
  filename is data typed by a person; ``/`` and control characters would
  split or corrupt the path a desktop sees.
"""

from __future__ import annotations

import mimetypes
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

DAV_ROOT = "/dav"
FILES_FOLDER = "files"
DAV_NS = "DAV:"
MAX_NAME_LENGTH = 255
MAX_PROPFIND_BODY = 65_536

# The live properties this server answers; anything else asked for is
# reported missing (404 in its own propstat), as RFC 4918 requires.
LIVE_PROPS = (
    "displayname",
    "resourcetype",
    "getcontentlength",
    "getcontenttype",
    "getlastmodified",
    "creationdate",
    "getetag",
)

# Answered only when asked for by name (RFC 4331 says quota properties
# should stay out of allprop): what a desktop shows as the folder's free space.
ON_REQUEST_PROPS = frozenset({"quota-used-bytes", "quota-available-bytes"})

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class DavRequestError(ValueError):
    """A request this surface cannot honour; carries the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DavFile:
    name: str  # as the folder shows it: safe and unique
    record: dict[str, Any]
    size: int
    modified: float  # unix seconds
    created: float

    @property
    def record_id(self) -> str:
        return str(self.record.get("id") or "")

    @property
    def content_type(self) -> str:
        return str(self.record.get("content_type") or "") or guess_type(self.name)

    @property
    def etag(self) -> str:
        return f'"{self.record_id}-{self.size}-{int(self.modified * 1000)}"'


def safe_name(filename: str) -> str:
    """A record's filename as one path segment: no slashes, no control
    characters, not empty, not ``.`` or ``..``, at most 255 characters."""
    name = _CONTROL.sub("", str(filename or "")).replace("/", "_").replace("\\", "_").strip()
    if name in {"", ".", ".."}:
        name = "unnamed"
    return name[:MAX_NAME_LENGTH]


def check_new_name(name: str) -> str:
    """A name a client asks to create or rename to; refused, not repaired,
    because silently storing something other than what was asked for would
    leave the client looking for a file that is not there."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name or _CONTROL.search(name):
        raise DavRequestError(400, "Not a usable file name.")
    if len(name) > MAX_NAME_LENGTH:
        raise DavRequestError(400, f"File names are limited to {MAX_NAME_LENGTH} characters.")
    return name


def _with_suffix(name: str, n: int) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:  # no extension, or a dotfile like ".bashrc"
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


def folder_names(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Name -> record for one folder: the oldest record keeps a name, later
    ones get " (2)", " (3)"... Case-insensitive, because the desktops that
    mount this (macOS, Windows) treat "A.txt" and "a.txt" as one file."""
    ordered = sorted(records, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    taken: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for record in ordered:
        base = safe_name(record.get("filename"))
        name, n = base, 1
        while name.lower() in taken:
            n += 1
            name = _with_suffix(base, n)
        taken.add(name.lower())
        out[name] = record
    return out


def lookup(names: dict[str, Any], name: str) -> str | None:
    """The folder's own spelling of ``name``, matched without case."""
    if name in names:
        return name
    lowered = name.lower()
    for existing in names:
        if existing.lower() == lowered:
            return existing
    return None


def split_path(path: str) -> tuple[str, str | None]:
    """Classify a decoded request path under /dav.

    Returns ("root", None), ("files", None) or ("file", name). Anything else
    raises DavRequestError(404): there is nothing else in this tree.
    """
    if path == DAV_ROOT or path == DAV_ROOT + "/":
        return "root", None
    rest = path[len(DAV_ROOT) + 1:] if path.startswith(DAV_ROOT + "/") else None
    if rest is None:
        raise DavRequestError(404, "Not under /dav.")
    parts = [p for p in rest.split("/")]
    if parts and parts[-1] == "":
        parts = parts[:-1]  # a trailing slash
    if parts == [FILES_FOLDER]:
        return "files", None
    if len(parts) == 2 and parts[0] == FILES_FOLDER and parts[1]:
        return "file", parts[1]
    raise DavRequestError(404, "No such folder or file.")


def destination_path(header: str | None) -> str:
    """The decoded path of a MOVE/COPY Destination header (absolute URL or
    absolute path). The host is not compared: a client behind a proxy may
    name any host, and the path alone decides what is touched."""
    if not header:
        raise DavRequestError(400, "Destination header required.")
    path = urlsplit(header.strip()).path
    if not path.startswith("/"):
        raise DavRequestError(400, "Destination must be an absolute URL or path.")
    return unquote(path)


def parse_depth(header: str | None, *, default: str = "infinity") -> str:
    value = (header or default).strip().lower()
    if value not in {"0", "1", "infinity"}:
        raise DavRequestError(400, "Depth must be 0, 1 or infinity.")
    return value


def parse_overwrite(header: str | None) -> bool:
    value = (header or "T").strip().upper()
    if value not in {"T", "F"}:
        raise DavRequestError(400, "Overwrite must be T or F.")
    return value == "T"


def _parse_xml(body: bytes, what: str) -> ET.Element:
    if len(body) > MAX_PROPFIND_BODY:
        raise DavRequestError(413, f"{what} body too large.")
    # No document type declarations at all: they are how entity expansion
    # attacks get in, and no DAV client needs one.
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise DavRequestError(400, "Document type declarations are not accepted.")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise DavRequestError(400, f"Malformed {what} body.") from exc


def parse_propfind(body: bytes) -> list[tuple[str, str]] | None:
    """Which properties a PROPFIND asks for: None means all of them (an
    empty body, <allprop/>), otherwise a list of (namespace, name).
    <propname/> is answered like <allprop/>: values alongside names are a
    superset no client has been seen to mind."""
    if not body.strip():
        return None
    root = _parse_xml(body, "PROPFIND")
    if root.tag != f"{{{DAV_NS}}}propfind":
        raise DavRequestError(400, "Expected a DAV:propfind element.")
    prop = root.find(f"{{{DAV_NS}}}prop")
    if prop is None:
        return None
    wanted = []
    for child in prop:
        ns, _, name = child.tag[1:].partition("}") if child.tag.startswith("{") else ("", "", child.tag)
        wanted.append((ns, name))
    return wanted


def parse_proppatch(body: bytes) -> list[tuple[str, str]]:
    """The (namespace, name) of every property a PROPPATCH tries to set or
    remove. None of them are writable here; the caller answers 403 for each."""
    root = _parse_xml(body, "PROPPATCH")
    names = []
    for prop in root.iter(f"{{{DAV_NS}}}prop"):
        for child in prop:
            ns, _, name = child.tag[1:].partition("}") if child.tag.startswith("{") else ("", "", child.tag)
            names.append((ns, name))
    return names


def http_date(ts: float) -> str:
    return formatdate(ts, usegmt=True)


def iso_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Types older Pythons' mimetypes lacks, so a file's type does not depend on
# which Python the server runs.
_EXTRA_TYPES = {".md": "text/markdown", ".markdown": "text/markdown"}


def guess_type(name: str) -> str:
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    _, dot, ext = name.rpartition(".")
    return _EXTRA_TYPES.get(f".{ext.lower()}" if dot else "", "application/octet-stream")


def href(*segments: str, folder: bool = False) -> str:
    path = DAV_ROOT + "/" + "/".join(quote(s, safe="") for s in segments if s)
    if folder and not path.endswith("/"):
        path += "/"
    return path


def folder_props(
    name: str, modified: float, *, used: int | None = None, available: int | None = None
) -> dict[str, str | None]:
    props: dict[str, str | None] = {
        "displayname": escape(name),
        "resourcetype": "<D:collection/>",
        "getlastmodified": http_date(modified),
        "creationdate": iso_date(modified),
        "getcontenttype": "httpd/unix-directory",
    }
    if used is not None and available is not None:
        props["quota-used-bytes"] = str(used)
        props["quota-available-bytes"] = str(max(0, available))
    return props


def file_props(f: DavFile) -> dict[str, str | None]:
    return {
        "displayname": escape(f.name),
        "resourcetype": "",
        "getcontentlength": str(f.size),
        "getcontenttype": escape(f.content_type),
        "getlastmodified": http_date(f.modified),
        "creationdate": iso_date(f.created),
        "getetag": escape(f.etag),
    }


def multistatus(
    responses: Iterable[tuple[str, dict[str, str | None]]],
    wanted: list[tuple[str, str]] | None,
) -> bytes:
    """A 207 body. ``responses`` is (href, {live prop name: XML value});
    values are already escaped. With ``wanted`` set, only those are
    returned and the ones this resource lacks go in a 404 propstat."""
    out = ['<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">']
    for target, props in responses:
        out.append(f"<D:response><D:href>{escape(target)}</D:href>")
        if wanted is None:
            found = [(DAV_NS, k) for k in props if k not in ON_REQUEST_PROPS]
            missing: list[tuple[str, str]] = []
        else:
            found = [w for w in wanted if w[0] == DAV_NS and w[1] in props]
            missing = [w for w in wanted if w not in found]
        if found:
            out.append("<D:propstat><D:prop>")
            for _, name in found:
                out.append(f"<D:{name}>{props[name] or ''}</D:{name}>")
            out.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>")
        if missing:
            out.append(_propstat_status(missing, "404 Not Found"))
        out.append("</D:response>")
    out.append("</D:multistatus>\n")
    return "".join(out).encode("utf-8")


def proppatch_refused(target: str, names: list[tuple[str, str]]) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
        f"<D:response><D:href>{escape(target)}</D:href>"
        + _propstat_status(names, "403 Forbidden")
        + "</D:response></D:multistatus>\n"
    ).encode("utf-8")


def _propstat_status(names: list[tuple[str, str]], status: str) -> str:
    parts = ["<D:propstat><D:prop>"]
    for i, (ns, name) in enumerate(names):
        if ns == DAV_NS:
            parts.append(f"<D:{name}/>")
        else:
            parts.append(f'<x{i}:{name} xmlns:x{i}="{escape(ns, {chr(34): "&quot;"})}"/>')
    parts.append(f"</D:prop><D:status>HTTP/1.1 {status}</D:status></D:propstat>")
    return "".join(parts)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """One byte range as (start, end inclusive), or None for the whole file.

    Only a single range is served; a multi-range request gets the whole file
    (allowed by RFC 9110, and what players that seek actually send is one
    range). An unsatisfiable range raises DavRequestError(416).
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[6:]
    if "," in spec:
        return None
    first, _, last = spec.strip().partition("-")
    try:
        if first == "":
            n = int(last)
            if n <= 0:
                raise DavRequestError(416, "Range not satisfiable.")
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start < 0 or end < start:
        raise DavRequestError(416, "Range not satisfiable.")
    return start, min(end, size - 1)


def etag_matches(header: str | None, etag: str | None) -> bool:
    """If-Match / If-None-Match: does any listed tag (or *) match?"""
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*":
            return etag is not None
        if tag.startswith("W/"):
            tag = tag[2:]
        if etag is not None and tag == etag:
            return True
    return False


def folder_html(title: str, entries: Iterable[tuple[str, str]]) -> bytes:
    """A plain index for a browser that opens a folder URL: (href, label)."""
    rows = "".join(f'<li><a href="{escape(h, {chr(34): "&quot;"})}">{escape(label)}</a></li>' for h, label in entries)
    return (
        f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
        f"<h1>{escape(title)}</h1><ul>{rows}</ul>"
    ).encode("utf-8")
