"""Object-served HTML is no-store, so "did you deploy?" has one answer.

These pages are rendered per request and their inline script IS the
application, yet they shipped with no cache headers at all -- leaving the
browser's heuristic cache in charge of whether a deploy arrived. Safari's
heuristics bit for real: a /talk fix was deployed and verified live with
curl while an iPad kept reproducing the old bug, because the iPad was
running the old page and there was no way to know.

Deliberately HTML-only: the generative widgets (/style, /list, /form) are
separate objects with their own content types, and turning off caching
for every asset on every view is a heavier performance decision than a
stale-page bug justifies.
"""

import object_server


def normalize(payload):
    return object_server._normalize_object_response(payload)


def header(headers, name):
    return [v for k, v in headers if k.lower() == name]


def test_an_html_page_object_is_no_store():
    _, headers, _ = normalize({"content_type": "text/html; charset=utf-8",
                               "body": "<h1>page</h1>"})
    assert header(headers, "cache-control") == ["no-store"]


def test_a_bare_string_page_is_no_store():
    _, headers, _ = normalize("bare html")
    assert header(headers, "cache-control") == ["no-store"]


def test_json_and_other_content_types_are_untouched():
    for payload in ({"content_type": "application/json", "body": "{}"},
                    {"content_type": "text/css", "body": "body{}"},
                    {"content_type": "application/javascript", "body": ";"},
                    {"ok": True}):
        _, headers, _ = normalize(payload)
        assert header(headers, "cache-control") == [], payload


def test_the_tuple_form_keeps_full_header_control():
    """An object that writes its own headers is respected -- including
    choosing to cache."""
    _, headers, _ = normalize((200, [("content-type", "text/html"),
                                     ("cache-control", "max-age=60")], b"x"))
    assert header(headers, "cache-control") == ["max-age=60"]


def test_content_type_parameters_do_not_dodge_the_rule():
    _, headers, _ = normalize({"content_type": "TEXT/HTML; charset=utf-8",
                               "body": "x"})
    assert header(headers, "cache-control") == ["no-store"]
