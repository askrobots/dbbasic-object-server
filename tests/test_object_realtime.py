"""Tests for realtime push: the hub, permission-filtered publishing, and the
websocket transport end to end."""

import asyncio
import json

import object_permissions
import object_realtime
import object_server

from test_object_server import (
    TEST_ADMIN_TOKEN,
    asgi_request,
    enable_admin_token,
    save_permission_policy,
    write_records,
)


def _subject(user_id, *roles):
    return object_permissions.PermissionSubject(user_id=user_id, roles=tuple(roles))


# --- Hub (transport-only) ----------------------------------------------------

def test_hub_membership_and_wants():
    async def body():
        hub = object_realtime.RealtimeHub()
        sub = object_realtime.Subscriber(subject=_subject("7"), queue=asyncio.Queue())
        assert hub.count() == 0
        hub.add(sub)
        assert hub.count() == 1
        sub.collections.add("notes")
        assert sub.wants("notes") and not sub.wants("tasks")
        assert hub.wanting("notes") == [sub] and hub.wanting("tasks") == []
        sub.collections.add(object_realtime.ALL)
        assert sub.wants("anything")
        hub.remove(sub)
        assert hub.count() == 0

    asyncio.run(body())


def test_deliver_and_queue_full_is_not_fatal():
    async def body():
        sub = object_realtime.Subscriber(subject=None, queue=asyncio.Queue(maxsize=1))
        assert sub.deliver({"n": 1}) is True
        assert sub.deliver({"n": 2}) is False  # full -> dropped, not raised
        assert sub.queue.qsize() == 1

    asyncio.run(body())


# --- Permission-filtered publishing (the security-critical path) -------------

def test_realtime_publish_respects_row_filters(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    save_permission_policy(
        data_dir,
        {
            "access_mode": "role_based",
            "rules": [
                {
                    "effect": "allow",
                    "principal": "registered",
                    "actions": ["read"],
                    "collection": "notes",
                    "row_filter": {"owner_id": "$user_id"},
                    "reason": "own notes",
                }
            ],
        },
    )
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(object_server.PERMISSION_ENFORCEMENT_ENV, "true")
    monkeypatch.setenv(object_server.PERMISSION_TRUST_HEADERS_ENV, "true")
    enable_admin_token(monkeypatch)
    assert object_server._permission_enforcement_enabled()

    async def body():
        owner = object_realtime.Subscriber(
            subject=_subject("7", "member"), queue=asyncio.Queue(), collections={"notes"}
        )
        other = object_realtime.Subscriber(
            subject=_subject("8", "member"), queue=asyncio.Queue(), collections={"notes"}
        )
        object_server._realtime_hub.add(owner)
        object_server._realtime_hub.add(other)
        try:
            object_server._realtime_publish(
                "notes", "n1", "create", {"id": "n1", "owner_id": "7"}
            )
            # only the owner (user 7) may see a record owned by 7
            assert owner.queue.qsize() == 1
            assert other.queue.qsize() == 0
            event = owner.queue.get_nowait()
            assert event == {
                "type": "record",
                "collection": "notes",
                "record_id": "n1",
                "action": "create",
            }
        finally:
            object_server._realtime_hub.remove(owner)
            object_server._realtime_hub.remove(other)

    asyncio.run(body())


def test_realtime_publish_noop_without_subscribers_or_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path))

    async def body():
        # no subscribers -> nothing raised
        object_server._realtime_publish("notes", "n1", "create", {"id": "n1"})

        sub = object_realtime.Subscriber(
            subject=_subject("7"), queue=asyncio.Queue(), collections={"notes"}
        )
        object_server._realtime_hub.add(sub)
        try:
            monkeypatch.setenv(object_server.REALTIME_ENABLED_ENV, "false")
            object_server._realtime_publish("notes", "n1", "create", {"id": "n1"})
            assert sub.queue.qsize() == 0  # disabled -> not delivered
        finally:
            object_server._realtime_hub.remove(sub)

    asyncio.run(body())


# --- Websocket transport -----------------------------------------------------

class _WS:
    """Drive object_server.app as a websocket peer."""

    def __init__(self, headers):
        self.to_server: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        }

    async def receive(self):
        return await self.to_server.get()

    async def send(self, message):
        self.sent.append(message)

    def texts(self):
        return [json.loads(m["text"]) for m in self.sent if m["type"] == "websocket.send"]

    def kinds(self):
        return [m["type"] for m in self.sent]


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0.01)


def test_websocket_subscribe_and_receive_push(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(data_dir, "notifications", "id\tuser_id\tbody\tis_read\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    async def body():
        ws = _WS([("authorization", f"Token {TEST_ADMIN_TOKEN}")])
        task = asyncio.ensure_future(object_server.app(ws.scope, ws.receive, ws.send))
        await ws.to_server.put({"type": "websocket.connect"})
        await _settle()
        assert "websocket.accept" in ws.kinds()
        assert any(t.get("type") == "welcome" for t in ws.texts())

        await ws.to_server.put(
            {"type": "websocket.receive",
             "text": json.dumps({"action": "subscribe", "collections": ["notifications"]})}
        )
        await _settle()
        assert any(t.get("type") == "subscribed" and "notifications" in t["collections"]
                   for t in ws.texts())

        object_server._realtime_publish(
            "notifications", "n1", "create", {"id": "n1", "user_id": "admin"}
        )
        await _settle()
        record_events = [t for t in ws.texts() if t.get("type") == "record"]
        assert record_events and record_events[-1]["record_id"] == "n1"
        assert record_events[-1]["collection"] == "notifications"

        await ws.to_server.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, timeout=1)
        assert object_server._realtime_hub.count() == 0

    asyncio.run(body())


def test_websocket_rejects_anonymous(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path))
    enable_admin_token(monkeypatch)

    async def body():
        ws = _WS([])
        task = asyncio.ensure_future(object_server.app(ws.scope, ws.receive, ws.send))
        await ws.to_server.put({"type": "websocket.connect"})
        await asyncio.wait_for(task, timeout=1)
        assert "websocket.accept" not in ws.kinds()
        assert ws.sent[-1]["type"] == "websocket.close"

    asyncio.run(body())


def test_websocket_closed_when_realtime_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path))
    enable_admin_token(monkeypatch)
    monkeypatch.setenv(object_server.REALTIME_ENABLED_ENV, "false")

    async def body():
        ws = _WS([("authorization", f"Token {TEST_ADMIN_TOKEN}")])
        task = asyncio.ensure_future(object_server.app(ws.scope, ws.receive, ws.send))
        await ws.to_server.put({"type": "websocket.connect"})
        await asyncio.wait_for(task, timeout=1)
        assert "websocket.accept" not in ws.kinds()
        assert ws.sent[-1]["type"] == "websocket.close"

    asyncio.run(body())


def test_http_record_write_pushes_to_websocket(tmp_path, monkeypatch):
    """The capstone: a create through the HTTP API reaches a live subscriber."""
    data_dir = tmp_path / "data"
    write_records(data_dir, "notes", "id\tcontent\n")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)

    async def body():
        ws = _WS([("authorization", f"Token {TEST_ADMIN_TOKEN}")])
        task = asyncio.ensure_future(object_server.app(ws.scope, ws.receive, ws.send))
        await ws.to_server.put({"type": "websocket.connect"})
        await _settle()
        await ws.to_server.put(
            {"type": "websocket.receive",
             "text": json.dumps({"action": "subscribe", "collections": ["notes"]})}
        )
        await _settle()

        status, _, raw = await asgi_request(
            "/collections/notes/records",
            method="POST",
            body=json.dumps({"id": "n-live", "content": "pushed"}).encode(),
            headers=[("authorization", f"Token {TEST_ADMIN_TOKEN}"),
                     ("content-type", "application/json")],
        )
        assert status == 201
        await _settle()

        record_events = [t for t in ws.texts() if t.get("type") == "record"]
        assert any(e["record_id"] == "n-live" and e["action"] == "create"
                   for e in record_events)

        await ws.to_server.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(body())
