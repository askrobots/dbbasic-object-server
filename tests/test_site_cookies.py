"""Cookies across the object boundary: what goes in, and what comes back.

A page object with no cookies cannot recognise a returning visitor at all
-- that is a whole class of feature (baskets, preferences, anything
remembered) that simply does not work. So cookies must cross. The
interesting question is which ones, and the answer is deliberately
lopsided: every app cookie goes IN, the identity session never does, and
exactly one header comes back OUT.

None of this is testable in-process: `_cookies` is put on the payload by
the server's routing layer and `set_cookie` is read by its response
layer, so a test that calls the object directly proves nothing about
either. These drive the ASGI app, same as the webhook tests.
"""

import asyncio
import json

import pytest

import object_server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    objects_dir = tmp_path / "objects"
    (objects_dir / "site").mkdir(parents=True)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
    # Site routing is the surface under test: `/probe` -> `site_probe` by
    # convention, no site_routes record needed.
    monkeypatch.setenv("DBBASIC_ENABLE_SITE_ROUTES", "true")
    return objects_dir


def stage(objects_dir, name, source):
    (objects_dir / "site" / f"{name}.py").write_text(source)


def call(path, *, method="GET", headers=None):
    sent = {}

    async def run():
        scope = {
            "type": "http", "method": method, "path": path, "query_string": b"",
            "headers": [(k.lower().encode(), v.encode())
                        for k, v in (headers or {}).items()],
        }
        received = {"done": False}

        async def receive():
            if received["done"]:
                return {"type": "http.disconnect"}
            received["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = [(k.decode("latin-1"), v.decode("latin-1"))
                                   for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                sent.setdefault("body", b"")
                sent["body"] += message.get("body", b"")

        await object_server.app(scope, receive, send)

    asyncio.run(run())
    return sent


def header_values(sent, name):
    return [value for key, value in sent.get("headers", []) if key.lower() == name]


def test_app_cookies_reach_the_object_and_the_session_cookie_does_not(env):
    """The whole point, and the whole limit, in one request.

    Without the first half every visit mints a new basket, because the
    `cart` cookie the browser sends never reaches the page. Without the
    second half every public page any package installs becomes a place a
    signed-in user's session can be read and sent somewhere else -- the
    session cookie is not data, it IS the authentication, and it is the
    same reasoning that keeps `cookie` out of the webhook safelist.
    """
    stage(env, "probe",
          "import json\n"
          "def GET(request):\n"
          "    return {'status': 200, 'content_type': 'application/json',\n"
          "            'body': json.dumps(request.get('_cookies'))}\n")
    sent = call("/probe", headers={
        "cookie": "cart=abc; dbbasic_session=SECRET",
    })
    assert sent["status"] == 200
    assert json.loads(sent["body"]) == {"cart": "abc"}
    assert "SECRET" not in sent["body"].decode("utf-8")


def test_an_object_can_set_one_cookie_on_the_response(env):
    """A basket token that is minted but never sent back is no token at
    all: the shopper gets a new empty basket on every page."""
    stage(env, "setter",
          "def GET(request):\n"
          "    return {'status': 200, 'content_type': 'text/html',\n"
          "            'body': 'ok',\n"
          "            'set_cookie': 'cart=xyz; Path=/; HttpOnly; SameSite=Lax'}\n")
    sent = call("/setter")
    assert sent["status"] == 200
    assert header_values(sent, "set-cookie") == [
        "cart=xyz; Path=/; HttpOnly; SameSite=Lax"]


def test_a_cookie_carrying_a_newline_is_not_emitted(env):
    """Response splitting: a newline in the value would let an object append
    headers of its own -- a redirect, a CORS grant -- to the wire. Dropped,
    not raised: the page still renders, it just gets no cookie."""
    stage(env, "injector",
          "def GET(request):\n"
          "    return {'status': 200, 'content_type': 'text/html',\n"
          "            'body': 'ok',\n"
          "            'set_cookie': 'a=b\\r\\nlocation: http://evil.test/'}\n")
    sent = call("/injector")
    assert sent["status"] == 200
    assert header_values(sent, "set-cookie") == []
    assert header_values(sent, "location") == []


def test_no_cookie_header_is_an_empty_dict_not_a_missing_key(env):
    """First-ever visitor. `_cookies` is always present so an object can
    read it without guarding, and always a dict so `.get` is safe."""
    stage(env, "probe",
          "import json\n"
          "def GET(request):\n"
          "    return {'status': 200, 'content_type': 'application/json',\n"
          "            'body': json.dumps(request.get('_cookies'))}\n")
    sent = call("/probe")
    assert json.loads(sent["body"]) == {}


def test_objects_cannot_set_headers_other_than_the_cookie(env):
    """A safelist of ONE. `location`, CORS grants and cache directives are
    the SERVER's security posture; a general headers passthrough would hand
    every installed package the ability to rewrite it."""
    stage(env, "greedy",
          "def GET(request):\n"
          "    return {'status': 200, 'content_type': 'text/html',\n"
          "            'body': 'ok',\n"
          "            'headers': {'location': 'http://evil.test/',\n"
          "                        'access-control-allow-origin': '*'}}\n")
    sent = call("/greedy")
    assert header_values(sent, "location") == []
    assert header_values(sent, "access-control-allow-origin") == []
