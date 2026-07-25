"""POST /webhooks/{name}: raw bytes for signature verification, and the
safelist that keeps a webhook object from becoming a credential tap.

Provider signatures are HMACs over the exact bytes on the wire; every
other route parses the body before an object runs. This endpoint is the
one deliberate exception, and because it is unauthenticated BY NATURE
(the caller is a payment processor, not a user), the tests here are
mostly about what it refuses to do.
"""

import asyncio
import json
import pathlib

import pytest

import object_credentials
import object_identity
import object_server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    objects_dir = tmp_path / "objects"
    (objects_dir / "webhook").mkdir(parents=True)
    (objects_dir / "webhook" / "echo.py").write_text(
        "def POST(request):\n"
        "    return {'ok': True,\n"
        "            'raw': request.get('_raw_body'),\n"
        "            'headers': request.get('_headers'),\n"
        "            'query': {k: v for k, v in request.items()\n"
        "                      if not k.startswith('_') and k not in ('ok',)}}\n")
    data_dir = tmp_path / "data"
    (data_dir / "permissions").mkdir(parents=True)
    (data_dir / "permissions" / "policy.json").write_text(json.dumps({
        "access_mode": "role_based",
        "rules": [{"effect": "allow", "principal": "public", "actions": ["execute"],
                   "object_id": "webhook_echo",
                   "reason": "test webhook"}],
    }))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_dir))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DBBASIC_ENABLE_PERMISSION_ENFORCEMENT", "true")
    monkeypatch.setenv("DBBASIC_ADMIN_TOKEN", "test-admin-token")
    # Enforcement stays in SHADOW MODE unless readiness passes, which
    # requires a non-admin identity path -- the deliberate lockout guard
    # that stops a half-configured server from locking its owner out.
    # Password login plus one active user satisfies it, same as the e2e
    # fixture; without this, the no-permission test below would pass
    # vacuously in shadow mode and prove nothing.
    monkeypatch.setenv("DBBASIC_ENABLE_PASSWORD_LOGIN", "true")
    object_identity.create_user(
        {"user_id": "wh-user", "email": "wh@test.local"}, base_dir=data_dir)
    object_credentials.set_password(
        "wh-user", "a-long-enough-password-123", base_dir=data_dir)
    return objects_dir, data_dir


def call(path, *, method="POST", body=b"", headers=None):
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
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
            elif message["type"] == "http.response.body":
                sent.setdefault("body", b"")
                sent["body"] += message.get("body", b"")

        await object_server.app(scope, receive, send)

    asyncio.run(run())
    payload = {}
    try:
        payload = json.loads(sent.get("body", b"{}") or b"{}")
    except ValueError:
        pass
    return sent.get("status"), payload


def test_raw_bytes_reach_the_object_unparsed(env):
    # NOT valid JSON on purpose: any parse step would have destroyed it.
    raw = b'{"amount": 100,   "spacing-matters": true}  \n trailing'
    status, payload = call("/webhooks/echo", body=raw,
                           headers={"stripe-signature": "t=1,v1=abc",
                                    "content-type": "application/json"})
    assert status == 200
    assert payload["raw"] == raw.decode()          # byte-exact, whitespace intact
    assert payload["headers"]["stripe-signature"] == "t=1,v1=abc"


def test_secret_bearing_headers_never_reach_the_object(env):
    status, payload = call("/webhooks/echo", body=b"x", headers={
        "authorization": "Bearer super-secret-user-token",
        "cookie": "dbbasic_session=super-secret-session",
        "stripe-signature": "t=1,v1=abc",
    })
    assert status == 200
    assert "authorization" not in payload["headers"]
    assert "cookie" not in payload["headers"]
    assert "super-secret" not in json.dumps(payload)


def test_unknown_webhook_is_404_not_a_probe_oracle(env):
    status, payload = call("/webhooks/nonexistent", body=b"x")
    assert status == 404
    status2, _ = call("/webhooks/../../etc/passwd", body=b"x")
    assert status2 == 404
    status3, _ = call("/webhooks/", body=b"x")
    assert status3 in (404, 405)


def test_get_is_refused(env):
    status, _ = call("/webhooks/echo", method="GET")
    assert status == 405


def test_no_permission_rule_means_no_execution(env, tmp_path):
    objects_dir, data_dir = env
    (objects_dir / "webhook" / "locked.py").write_text(
        "def POST(request):\n    return {'ok': True}\n")
    status, _ = call("/webhooks/locked", body=b"x")
    # The object exists but nobody granted public execute: refused. A
    # webhook endpoint must be an explicit opt-in per hook, never a blanket
    # exposure of every webhook_* object anyone installs.
    assert status in (401, 403)


def test_object_status_reaches_the_status_line(env):
    """An object answering {"status": 4xx} must produce that HTTP status.

    This was broken: only content_type responses honored `status`, so a
    webhook's signature failure -- and every action's refusal -- was
    delivered as HTTP 200 with the rejection hidden in the body. Providers
    retry on status alone, so a forgery answered 200 is a forgery
    permanently accepted.
    """
    objects_dir, _ = env
    (objects_dir / "webhook" / "refuser.py").write_text(
        "def POST(request):\n"
        "    return {'status': 400, 'error': 'Signature verification failed'}\n")
    (objects_dir.parent / "data" / "permissions" / "policy.json").write_text(json.dumps({
        "access_mode": "role_based",
        "rules": [{"effect": "allow", "principal": "public", "actions": ["execute"],
                   "object_id": "webhook_refuser", "reason": "test"}],
    }))
    status, payload = call("/webhooks/refuser", body=b"{}")
    assert status == 400
    assert payload["error"] == "Signature verification failed"


def test_a_records_own_status_field_is_not_a_status_line(env):
    """Collections carry a `status` of their own ("active", "posted"), and
    an object echoing a record back must not have it read as HTTP."""
    objects_dir, _ = env
    (objects_dir / "webhook" / "record.py").write_text(
        "def POST(request):\n"
        "    return {'ok': True, 'record': {'id': 'x'}, 'status': 'active'}\n")
    (objects_dir.parent / "data" / "permissions" / "policy.json").write_text(json.dumps({
        "access_mode": "role_based",
        "rules": [{"effect": "allow", "principal": "public", "actions": ["execute"],
                   "object_id": "webhook_record", "reason": "test"}],
    }))
    status, payload = call("/webhooks/record", body=b"{}")
    assert status == 200
    assert payload["status"] == "active"
