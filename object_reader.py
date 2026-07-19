"""The reader: fetch a URL server-side, strip HTML to readable text.

The lynx/w3m move for an AI + voice consumer (see
plan/vocabulary/57-reader-spec.md): a page becomes {title, text, links}
where links are numbered in document order so they double as speakable
navigation targets ("open link three"). stdlib only -- urllib for the
fetch, html.parser for the strip. No regex-only HTML scraping: regex
reliably misses attribute quoting, comments, and malformed markup in ways
that would either corrupt the text or -- worse -- leak script/style
content into what's supposed to be the readable body.

THE SSRF GATE is the point of this module, not an afterthought. A server
that will fetch any URL a caller names is a standard pivot into internal
infrastructure: loopback, RFC1918 ranges, and especially link-local
169.254.169.254 (cloud metadata, credentials for the asking). Every
hostname -- the original and every redirect hop -- is resolved and
checked BEFORE the socket opens. Redirects are followed manually (never
by urllib's own follower) precisely so each hop gets its own resolve+check
pass instead of trusting the first check to cover wherever the chain
ends up (DNS rebinding: a name can resolve to something public at check
time and something private moments later). Any resolution error or
ambiguity refuses the fetch -- fail closed, never fail open.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

#: Redirect hops allowed after the initial fetch. Kept small on purpose --
#: a legitimate page does not need a long chain, and every hop is another
#: SSRF re-check surface.
MAX_REDIRECTS = 5

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Tags whose content is never readable body text: markup, chrome, and
# machine-only payloads, not prose. `head` is deliberately NOT here --
# `<title>` lives inside it and is captured separately; head's other
# children (meta, link) carry no text data, so nothing else leaks through.
_SKIP_TEXT_TAGS = frozenset({"script", "style", "nav", "noscript", "template"})
# Tags that mark a paragraph/line boundary -- their start inserts a break so
# the collapsed-whitespace pass below still keeps paragraphs apart.
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article", "table", "header", "footer",
})
_LINK_LABEL_MAX = 80
_READ_CHUNK = 65536
_USER_AGENT = "dbbasic-reader/1.0 (+text mode)"


class ReaderError(RuntimeError):
    """Any refusal or fetch failure. Callers render the message, not a traceback."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urllib's own redirect-following.

    Returning None from redirect_request tells urllib "don't build a
    follow-up request" -- urlopen then raises HTTPError for the 3xx status
    instead of silently chasing it. That HTTPError's Location header is
    read manually by read_page, which re-resolves and re-checks the next
    hop before ever opening a connection to it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def read_page(url: str, *, timeout: float = 10, max_bytes: int = 2_000_000) -> dict[str, Any]:
    """Fetch one URL and return {title, text, links, final_url, truncated}.

    ``links`` is a list of {n, label, href} in document order, deduped by
    href, resolved to absolute URLs against the page that carried them.

    Raises ReaderError for a disallowed scheme, an SSRF-refused host, too
    many redirects, a non-HTML response, or any network failure/timeout.
    Never raises anything else -- callers (the MCP tool, the view block,
    the HTTP endpoint) map ReaderError to a visible error, not a 500.
    """
    opener = urllib.request.build_opener(_NoRedirectHandler)
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        parsed = _check_url(current)
        request = urllib.request.Request(
            urllib.parse.urlunsplit(parsed), headers={"User-Agent": _USER_AGENT}
        )

        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in _REDIRECT_CODES:
                current = _next_redirect_url(current, exc)
                continue
            raise ReaderError(f"Fetch failed: HTTP {exc.code}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ReaderError(f"Fetch timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ReaderError(f"Fetch timed out after {timeout}s") from exc
            raise ReaderError(f"Fetch failed: {exc.reason}") from exc

        try:
            with response:
                content_type_header = response.headers.get("Content-Type") or ""
                content_type = content_type_header.split(";", 1)[0].strip().lower()
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    raise ReaderError(
                        f"Refusing non-HTML content type: {content_type or 'unknown'}"
                    )
                final_url = response.geturl()
                body, truncated = _read_capped(response, max_bytes=max_bytes)
        except (socket.timeout, TimeoutError) as exc:
            raise ReaderError(f"Fetch timed out after {timeout}s") from exc
        except OSError as exc:
            raise ReaderError(f"Fetch failed while reading response: {exc}") from exc

        charset = _charset_from_content_type(content_type_header)
        try:
            html_text = body.decode(charset, errors="replace")
        except LookupError:
            html_text = body.decode("utf-8", errors="replace")

        page = _strip_html(html_text, base_url=final_url)
        page["final_url"] = final_url
        page["truncated"] = truncated
        return page

    raise ReaderError(f"Too many redirects (> {MAX_REDIRECTS})")


def _next_redirect_url(current: str, exc: urllib.error.HTTPError) -> str:
    location = exc.headers.get("Location") if exc.headers is not None else None
    if not location:
        raise ReaderError(f"Redirect ({exc.code}) from {current} carried no Location") from exc
    next_url = urllib.parse.urljoin(current, location)
    next_scheme = urllib.parse.urlsplit(next_url).scheme
    if next_scheme not in _ALLOWED_SCHEMES:
        raise ReaderError(
            f"Refusing redirect to non-http(s) scheme: {next_scheme or '(none)'}"
        ) from exc
    return next_url


def _check_url(url: str) -> urllib.parse.SplitResult:
    """Parse `url`, enforce http/https, and run the SSRF host check.

    Called on the original URL and again on every redirect hop -- the
    re-resolution on each hop is the DNS-rebinding defense the spec asks
    for; a single check up front would not cover a hop that lands
    somewhere different than the first hostname resolved to.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ReaderError(f"Refusing non-http(s) scheme: {parsed.scheme or '(none)'}")
    hostname = parsed.hostname
    if not hostname:
        raise ReaderError("URL has no host")
    _refuse_internal_host(hostname)
    return parsed


def _refuse_internal_host(hostname: str) -> None:
    """Resolve `hostname` and refuse if ANY resolved address is internal.

    Fails closed: a DNS error, an empty resolution, or an address
    ipaddress can't parse all refuse the fetch rather than let it through
    on ambiguity -- the same posture as 02-public-write-hardening.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise ReaderError(f"Could not resolve host {hostname!r}: {exc}") from exc

    if not infos:
        raise ReaderError(f"Host {hostname!r} resolved to no addresses")

    for info in infos:
        raw_addr = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError as exc:
            raise ReaderError(
                f"Unparseable resolved address for {hostname!r}: {raw_addr!r}"
            ) from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise ReaderError(
                f"Refusing to fetch {hostname!r}: resolves to internal address {addr}"
            )


def _read_capped(response, *, max_bytes: int) -> tuple[bytes, bool]:
    """Stream the response body, aborting past `max_bytes`."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            keep = max_bytes - (total - len(chunk))
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


def _charset_from_content_type(header: str) -> str:
    for part in header.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"').strip("'")
            if charset:
                return charset
    return "utf-8"


class _Stripper(HTMLParser):
    """Reduce one HTML document to (title, body text, numbered links).

    script/style/nav/head content is dropped entirely. Block-level tags
    insert a paragraph break; everything else collapses to plain text.
    Anchor text becomes both the link's label and part of the flowing
    body text, same as how the text would read in a browser.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._links: list[dict[str, Any]] = []
        self._seen_hrefs: set[str] = set()
        self._current_href: str | None = None
        self._current_label_parts: list[str] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())

    @property
    def links(self) -> list[dict[str, Any]]:
        return self._links

    def text(self) -> str:
        paragraphs = [
            collapsed
            for chunk in "".join(self._text_parts).split("\n")
            if (collapsed := " ".join(chunk.split()))
        ]
        return "\n\n".join(paragraphs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            self._open_anchor(dict(attrs))
            return
        if tag in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _SKIP_TEXT_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TEXT_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a" and self._current_href is not None:
            self._close_anchor()
        elif tag in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._current_href is not None:
            self._current_label_parts.append(data)
        self._text_parts.append(data)

    def _open_anchor(self, attrs: dict[str, str | None]) -> None:
        href = (attrs.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            return
        self._current_href = urllib.parse.urljoin(self._base_url, href)
        self._current_label_parts = []

    def _close_anchor(self) -> None:
        href = self._current_href
        self._current_href = None
        label = " ".join("".join(self._current_label_parts).split())
        self._current_label_parts = []
        if href is None or href in self._seen_hrefs:
            return
        self._seen_hrefs.add(href)
        if not label:
            label = href
        if len(label) > _LINK_LABEL_MAX:
            label = label[: _LINK_LABEL_MAX - 1].rstrip() + "…"
        self._links.append({"n": len(self._links) + 1, "label": label, "href": href})


def _strip_html(html_text: str, *, base_url: str) -> dict[str, Any]:
    stripper = _Stripper(base_url)
    stripper.feed(html_text)
    stripper.close()
    return {"title": stripper.title, "text": stripper.text(), "links": stripper.links}
