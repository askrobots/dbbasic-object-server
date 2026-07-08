"""In-process live pub/sub for realtime push over websockets.

The durable event log (``object_events``) remains the source of truth and
the poll-based fallback. This hub is the live overlay: connected
websocket subscribers receive record-change signals the instant a write
happens — already filtered, by the caller, to what each subscriber is
permitted to read.

The hub is deliberately transport-only. It holds subscribers and their
asyncio queues; it does not know about permissions. The server, which
owns the policy engine, decides what a subscriber may receive and calls
``deliver`` only for allowed events. Keeping the security decision in one
place (the server) and the transport dumb (here) makes both easy to test.

Single-process assumption: ``deliver`` is called on the server's event
loop, and with one uvicorn worker every write and every socket share that
loop, so ``put_nowait`` is safe. Across multiple workers each process has
its own hub and clients fall back to polling the durable event log — see
docs/asgi-realtime-direction.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

ALL = "*"
DEFAULT_QUEUE_MAX = 1000


@dataclass(eq=False)
class Subscriber:
    """One connected websocket, its identity, and its outbound queue.

    ``subject`` is the caller's permission subject, opaque to the hub —
    the server reads it when deciding what to deliver. ``collections`` is
    the set the client asked to follow (``"*"`` means every collection).
    """

    subject: Any
    queue: "asyncio.Queue[dict[str, Any]]"
    collections: set[str] = field(default_factory=set)

    def wants(self, collection: str) -> bool:
        return ALL in self.collections or collection in self.collections

    def deliver(self, event: dict[str, Any]) -> bool:
        """Enqueue one event; return False if the queue is full (dropped).

        A dropped event is not fatal: the client refetches on reconnect
        and the durable event log still holds it for polling.
        """
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False


class RealtimeHub:
    """A registry of live subscribers. No IO, no policy — just membership."""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()

    def add(self, subscriber: Subscriber) -> None:
        self._subscribers.add(subscriber)

    def remove(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def count(self) -> int:
        return len(self._subscribers)

    def subscribers(self) -> list[Subscriber]:
        return list(self._subscribers)

    def wanting(self, collection: str) -> list[Subscriber]:
        """Return subscribers following ``collection`` (or all collections)."""
        return [s for s in self._subscribers if s.wants(collection)]
