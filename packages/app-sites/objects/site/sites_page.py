"""A published page, GET /s/{site} and /s/{site}/{page} (app-sites).

Webmaster (or any site builder) publishes each page as a site_pages record;
this serves it. The HTML is whatever the author exported, scripts included,
so it is never trusted:

- Served with `Content-Security-Policy: sandbox allow-scripts allow-forms
  allow-popups allow-popups-to-escape-sandbox` -- deliberately WITHOUT
  allow-same-origin. The page runs in an opaque origin: its scripts cannot
  read this server's cookies (the visitor's session) or call its API as the
  visitor, which is the whole danger of serving user HTML from the app's own
  origin. A dedicated hostname per site (site_hosts) is the stronger shape
  later; the sandbox is what makes the shared origin safe now.
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- Shown only when the record is public or the visitor owns it. Anything
  else, including a page that does not exist, is the same plain 404: a
  private page's existence is not revealed.

Links between pages: a builder exports "about.html"; published under
/s/{site}/ the page slug is "about", and /s/{site}/about.html is accepted
too (the ".html" is dropped), so relative links keep working.
"""

import html as html_lib
import os
import re

import object_records

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
COLLECTION = "site_pages"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"


def _data_dir():
    return os.environ.get(DATA_DIR_ENV, object_records.DEFAULT_DATA_DIR)


def _headers(content_type="text/html; charset=utf-8"):
    return {
        "content-type": content_type,
        "content-security-policy": SANDBOX,
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cache-control": "no-cache",
    }


def _not_found():
    body = "<!doctype html><meta charset=utf-8><title>Not found</title><h1>Not found</h1>"
    return (404, _headers(), body)


def find_page(site, slug, user_id, rows):
    """The record for site/slug if the visitor may see it, else None."""
    for row in rows:
        if row.get("site") != site or row.get("slug") != slug:
            continue
        if str(row.get("is_public") or "").lower() == "true":
            return row
        if user_id and row.get("owner_id") == user_id:
            return row
        return None
    return None


def GET(request):
    site = str(request.get("site") or "").strip().lower()
    page = str(request.get("page") or "index").strip().lower()
    if page.endswith(".html"):
        page = page[: -len(".html")]
    if not _NAME_RE.fullmatch(site) or not _NAME_RE.fullmatch(page):
        return _not_found()
    identity = request.get("_identity") or {}
    try:
        rows = object_records.read_collection_records(COLLECTION, base_dir=_data_dir())
    except Exception:   # the collection is not installed yet: nothing is published
        return _not_found()
    row = find_page(site, page, identity.get("user_id"), rows)
    if row is None:
        return _not_found()
    body = row.get("html") or ""
    if not body.strip():
        title = html_lib.escape(row.get("title") or page)
        body = f"<!doctype html><meta charset=utf-8><title>{title}</title><h1>{title}</h1>"
    return (200, _headers(), body)
