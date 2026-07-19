"""Tests for object_reader: the SSRF gate and the HTML-to-text strip.

No real network calls -- socket.getaddrinfo and the urllib opener are
monkeypatched, same convention as test_object_tts.py swapping out
subprocess.run. The SSRF gate is the important half: see the spec at
plan/vocabulary/57-reader-spec.md, "THE SECURITY GATE".
"""

from __future__ import annotations

import io
import socket

import pytest

import object_reader as reader


class FakeHeaders(dict):
    """Case-insensitive enough for the .get("Location") / .get("Content-Type") calls."""

    def get(self, key, default=None):
        for existing in self:
            if existing.lower() == key.lower():
                return self[existing]
        return default


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str, headers: dict):
        super().__init__(data)
        self._url = url
        self.headers = FakeHeaders(headers)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def addrinfo_for(mapping: dict[str, str]):
    """Build a fake socket.getaddrinfo that answers from a host->ip map."""

    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host not in mapping:
            raise socket.gaierror(f"no mapping for {host!r} in this test")
        ip = mapping[host]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    return fake_getaddrinfo


SIMPLE_HTML = (
    b"<html><head><title>Hello   World</title><style>.x{color:red}</style></head>"
    b"<body>\n"
    b"<nav>site nav, not content</nav>\n"
    b"<h1>Big Heading</h1>\n"
    b"<p>First   paragraph with a <a href=\"/about\">link about us</a> and more text.</p>\n"
    b"<p>Second paragraph. <a href=\"https://example.com/x\">External</a></p>\n"
    b"<script>var hostile = 1;</script>\n"
    b"<a href=\"/about\">duplicate href, different label</a>\n"
    b"</body></html>"
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private (RFC1918)
        "169.254.169.254",  # link-local -- cloud metadata
        "::1",  # loopback, IPv6
    ],
)
def test_refuses_when_resolved_address_is_internal(monkeypatch, address):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"internal.example": address}))

    with pytest.raises(reader.ReaderError, match="internal address"):
        reader.read_page("http://internal.example/secret")


def test_refuses_when_dns_resolution_fails(monkeypatch):
    def raising_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(reader.socket, "getaddrinfo", raising_getaddrinfo)

    with pytest.raises(reader.ReaderError, match="Could not resolve"):
        reader.read_page("http://nowhere.invalid/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"])
def test_refuses_non_http_schemes(url):
    with pytest.raises(reader.ReaderError, match="non-http"):
        reader.read_page(url)


def test_redirect_to_a_private_address_is_refused_on_the_hop(monkeypatch):
    """DNS-rebinding defense: the SSRF check reruns on every redirect hop,
    not just the first URL -- a page can be public but redirect somewhere
    internal."""
    monkeypatch.setattr(
        reader.socket,
        "getaddrinfo",
        addrinfo_for({"public.example": "93.184.216.34", "internal.example": "10.0.0.9"}),
    )

    def fake_open(_self, request, timeout=None):
        if "public.example" in request.full_url:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                FakeHeaders({"Location": "http://internal.example/secret"}),
                None,
            )
        raise AssertionError(f"should never reach {request.full_url}")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(reader.ReaderError, match="internal address"):
        reader.read_page("http://public.example/start")


def test_redirect_follows_to_a_public_host_and_re_resolves(monkeypatch):
    monkeypatch.setattr(
        reader.socket,
        "getaddrinfo",
        addrinfo_for({"a.example": "93.184.216.34", "b.example": "93.184.216.35"}),
    )

    def fake_open(_self, request, timeout=None):
        if "a.example" in request.full_url:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", FakeHeaders({"Location": "https://b.example/next"}), None
            )
        return FakeResponse(SIMPLE_HTML, request.full_url, {"Content-Type": "text/html; charset=utf-8"})

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    result = reader.read_page("https://a.example/start")

    assert result["final_url"] == "https://b.example/next"
    assert result["title"] == "Hello World"


def test_too_many_redirects_are_refused(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"loop.example": "93.184.216.34"}))

    def fake_open(_self, request, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(
            request.full_url, 302, "Found", FakeHeaders({"Location": "https://loop.example/again"}), None
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(reader.ReaderError, match="Too many redirects"):
        reader.read_page("https://loop.example/start")


def test_refuses_non_html_content_type(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"api.example": "93.184.216.34"}))

    def fake_open(_self, request, timeout=None):
        return FakeResponse(b'{"ok": true}', request.full_url, {"Content-Type": "application/json"})

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(reader.ReaderError, match="non-HTML"):
        reader.read_page("https://api.example/data.json")


def test_enforces_timeout(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"slow.example": "93.184.216.34"}))

    def fake_open(_self, request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    with pytest.raises(reader.ReaderError, match="timed out"):
        reader.read_page("https://slow.example/", timeout=1)


def test_enforces_max_bytes_and_sets_truncated(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"big.example": "93.184.216.34"}))
    big_html = b"<html><title>Big</title><body>" + b"word " * 100_000 + b"</body></html>"

    def fake_open(_self, request, timeout=None):
        return FakeResponse(big_html, request.full_url, {"Content-Type": "text/html"})

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    result = reader.read_page("https://big.example/", max_bytes=1000)

    assert result["truncated"] is True


def test_happy_path_returns_stripped_text_and_numbered_deduped_links(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", addrinfo_for({"example.com": "93.184.216.34"}))

    def fake_open(_self, request, timeout=None):
        return FakeResponse(
            SIMPLE_HTML, "https://example.com/page", {"Content-Type": "text/html; charset=utf-8"}
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    result = reader.read_page("https://example.com/page")

    assert result["title"] == "Hello World"
    # script/style/nav content never appears in the body text.
    assert "hostile" not in result["text"]
    assert "site nav" not in result["text"]
    assert "Big Heading" in result["text"]
    assert "First paragraph with a link about us and more text." in result["text"]
    # Links are numbered in document order, deduped by href, absolute.
    assert result["links"] == [
        {"n": 1, "label": "link about us", "href": "https://example.com/about"},
        {"n": 2, "label": "External", "href": "https://example.com/x"},
    ]
    assert result["final_url"] == "https://example.com/page"
    assert result["truncated"] is False
