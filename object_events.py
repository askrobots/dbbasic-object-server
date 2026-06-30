"""Daemon-compatible event and subscription state.

The old private prototype stored event trigger state in the `events` object
state file. Keep that shape here so the public HTTP server, Scroll, and the
daemon do not grow separate event systems.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import object_state
from object_versions import DEFAULT_DATA_DIR

EVENTS_OBJECT_ID = "events"
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 1000
DEFAULT_EVENT_KEEP_COUNT = 1000
DEFAULT_EVENT_KEEP_SECONDS = 7 * 24 * 60 * 60
MAX_EVENT_KEEP_COUNT = 1_000_000
VALID_CALLBACK_SCHEMES = {"http", "https"}

_EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SUBSCRIBER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InvalidEventTypeError(ValueError):
    """Raised when an event type is not safe for state keys or routing."""


class InvalidSubscriberIdError(ValueError):
    """Raised when a subscriber id is not safe for state keys or routing."""


class SubscriptionNotFoundError(LookupError):
    """Raised when an event subscription cannot be found."""


def validate_event_type(event_type: str) -> bool:
    """Return True when an event type is route and state-key safe."""
    if not isinstance(event_type, str):
        return False
    return bool(_EVENT_TYPE_RE.fullmatch(event_type))


def validate_subscriber_id(subscriber_id: str) -> bool:
    """Return True when a subscriber id is route and state-key safe."""
    if not isinstance(subscriber_id, str):
        return False
    return bool(_SUBSCRIBER_ID_RE.fullmatch(subscriber_id))


def publish_event(
    event_type: str,
    *,
    payload: Any | None = None,
    source: str = "api",
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
    keep_count: int | None = None,
    keep_seconds: int | None = None,
) -> dict[str, Any]:
    """Publish one event into daemon-compatible trigger state."""
    clean_event_type = _clean_event_type(event_type)
    clean_keep_count = _clean_keep_count(keep_count)
    clean_keep_seconds = _clean_keep_seconds(keep_seconds)
    timestamp = int(time.time())
    event = {
        "id": f"evt_{uuid4().hex[:16]}",
        "event_type": clean_event_type,
        "payload": _json_safe_value({} if payload is None else payload),
        "source": _clean_text(source, default="api"),
        "actor": _clean_text(actor, default="api"),
        "timestamp": timestamp,
        "created_at": _utc_timestamp(),
    }

    manager = _state_manager(base_dir)
    manager.set(_event_state_key(event), _json_dumps(event))
    if _should_prune_after_publish(
        manager.get_all(),
        keep_count=clean_keep_count,
        keep_seconds=clean_keep_seconds,
        now=timestamp,
    ):
        prune_events(
            base_dir=base_dir,
            keep_count=clean_keep_count,
            keep_seconds=clean_keep_seconds,
            now=timestamp,
        )
    return event


def list_events(
    *,
    event_type: str | None = None,
    since: int | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_EVENT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first events from daemon-compatible trigger state."""
    if event_type is not None:
        event_type = _clean_event_type(event_type)
    if since is not None and since < 0:
        raise ValueError("since must be at least 0")
    _validate_page(limit=limit, offset=offset)

    events = []
    for key, value in _state_manager(base_dir).get_all().items():
        if not key.startswith("event_"):
            continue
        event = _load_json_object(value)
        if event is None:
            continue
        if event_type is not None and event.get("event_type") != event_type:
            continue
        if since is not None and _coerce_int(event.get("timestamp")) < since:
            continue
        events.append(event)

    events.sort(
        key=lambda item: (_coerce_int(item.get("timestamp")), str(item.get("id", ""))),
        reverse=True,
    )
    total = len(events)
    window = events[offset:offset + limit]
    payload: dict[str, Any] = {
        "events": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }
    if event_type is not None:
        payload["event_type"] = event_type
    if since is not None:
        payload["since"] = since
    return payload


def prune_events(
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    keep_count: int | None = DEFAULT_EVENT_KEEP_COUNT,
    keep_seconds: int | None = DEFAULT_EVENT_KEEP_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    """Prune old event queue rows while preserving subscriptions.

    Events are a delivery queue. Durable audit history lives in record change
    logs, source versions, and object logs, so this helper removes old event
    rows before the shared events state file grows without bound.
    """
    clean_keep_count = _clean_keep_count(keep_count)
    clean_keep_seconds = _clean_keep_seconds(keep_seconds)
    current_time = int(time.time() if now is None else now)

    manager = _state_manager(base_dir)
    state = manager.get_all()
    protected_ids = _subscription_last_event_ids(state)
    events: list[tuple[str, dict[str, Any], int, bool]] = []
    event_keys: set[str] = set()
    keys_to_delete: set[str] = set()
    corrupt_deleted = 0

    for key, value in state.items():
        if not key.startswith("event_"):
            continue
        event_keys.add(key)
        event = _load_json_object(value)
        if event is None:
            keys_to_delete.add(key)
            corrupt_deleted += 1
            continue

        event_id = str(event.get("id", ""))
        events.append(
            (
                key,
                event,
                _coerce_int(event.get("timestamp")),
                event_id in protected_ids,
            )
        )

    if clean_keep_seconds is not None:
        cutoff = current_time - clean_keep_seconds
        for key, _event, timestamp, protected in events:
            if not protected and timestamp < cutoff:
                keys_to_delete.add(key)

    remaining = [row for row in events if row[0] not in keys_to_delete]
    if clean_keep_count is not None:
        unprotected = [row for row in remaining if not row[3]]
        unprotected.sort(
            key=lambda row: (_coerce_int(row[1].get("timestamp")), str(row[1].get("id", ""))),
            reverse=True,
        )
        for key, _event, _timestamp, _protected in unprotected[clean_keep_count:]:
            keys_to_delete.add(key)

    deleted = manager.delete_many(sorted(keys_to_delete)) if keys_to_delete else 0
    valid_deleted = len(keys_to_delete.intersection(event_keys)) - corrupt_deleted

    return {
        "deleted": deleted,
        "kept": len(events) - valid_deleted,
        "scanned": len(event_keys),
        "protected": sum(1 for _key, _event, _timestamp, protected in events if protected),
        "corrupt_deleted": corrupt_deleted,
        "keep_count": clean_keep_count,
        "keep_seconds": clean_keep_seconds,
    }


def subscribe_event(
    event_type: str,
    *,
    subscriber_id: str | None = None,
    callback_url: str = "",
    actor: str = "api",
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Create or replace one daemon-compatible event subscription."""
    clean_event_type = _clean_event_type(event_type)
    clean_subscriber_id = _clean_subscriber_id(subscriber_id or f"sub_{uuid4().hex[:16]}")
    clean_callback_url = _clean_callback_url(callback_url)
    timestamp = int(time.time())
    subscription = {
        "id": clean_subscriber_id,
        "event_type": clean_event_type,
        "callback_url": clean_callback_url,
        "created_at": timestamp,
        "created_at_iso": _utc_timestamp(),
        "created_by": _clean_text(actor, default="api"),
        "last_event_id": None,
    }

    manager = _state_manager(base_dir)
    manager.set(_subscription_state_key(clean_event_type, clean_subscriber_id), _json_dumps(subscription))
    return subscription


def list_subscriptions(
    *,
    event_type: str | None = None,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    limit: int = DEFAULT_EVENT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return newest-first event subscriptions."""
    if event_type is not None:
        event_type = _clean_event_type(event_type)
    _validate_page(limit=limit, offset=offset)

    subscriptions = []
    for key, value in _state_manager(base_dir).get_all().items():
        if not key.startswith("sub_"):
            continue
        subscription = _load_json_object(value)
        if subscription is None:
            continue
        if event_type is not None and subscription.get("event_type") != event_type:
            continue
        subscriptions.append(subscription)

    subscriptions.sort(
        key=lambda item: (_coerce_int(item.get("created_at")), str(item.get("id", ""))),
        reverse=True,
    )
    total = len(subscriptions)
    window = subscriptions[offset:offset + limit]
    payload: dict[str, Any] = {
        "subscriptions": window,
        "count": len(window),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(window) < total,
    }
    if event_type is not None:
        payload["event_type"] = event_type
    return payload


def delete_subscription(
    event_type: str,
    subscriber_id: str,
    *,
    base_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Delete one event subscription and return the removed subscription."""
    clean_event_type = _clean_event_type(event_type)
    clean_subscriber_id = _clean_subscriber_id(subscriber_id)
    key = _subscription_state_key(clean_event_type, clean_subscriber_id)
    manager = _state_manager(base_dir)
    current = _load_json_object(manager.get(key))
    if current is None:
        raise SubscriptionNotFoundError(
            f"Subscription not found: {clean_event_type}/{clean_subscriber_id}"
        )

    manager.delete(key)
    return current


def _state_manager(base_dir: Path | str) -> object_state.ObjectStateManager:
    return object_state.ObjectStateManager(EVENTS_OBJECT_ID, base_dir=base_dir)


def _event_state_key(event: dict[str, Any]) -> str:
    return f"event_{event['timestamp']}_{event['id']}"


def _subscription_state_key(event_type: str, subscriber_id: str) -> str:
    return f"sub_{event_type}_{subscriber_id}"


def _clean_event_type(event_type: str) -> str:
    if not isinstance(event_type, str):
        raise InvalidEventTypeError(f"Invalid event type: {event_type}")
    clean = event_type.strip()
    if not validate_event_type(clean):
        raise InvalidEventTypeError(f"Invalid event type: {event_type}")
    return clean


def _clean_subscriber_id(subscriber_id: str) -> str:
    if not isinstance(subscriber_id, str):
        raise InvalidSubscriberIdError(f"Invalid subscriber id: {subscriber_id}")
    clean = subscriber_id.strip()
    if not validate_subscriber_id(clean):
        raise InvalidSubscriberIdError(f"Invalid subscriber id: {subscriber_id}")
    return clean


def _clean_callback_url(callback_url: str) -> str:
    clean = str(callback_url or "").strip()
    if not clean:
        return ""

    parsed = urlparse(clean)
    if parsed.scheme not in VALID_CALLBACK_SCHEMES or not parsed.netloc:
        raise ValueError("callback_url must be an absolute http or https URL")
    return clean


def _validate_page(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > MAX_EVENT_LIMIT:
        raise ValueError(f"limit must be at most {MAX_EVENT_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be at least 0")


def _clean_keep_count(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("keep_count must be an integer")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("keep_count must be an integer") from exc
    if count < 0:
        raise ValueError("keep_count must be at least 0")
    if count > MAX_EVENT_KEEP_COUNT:
        raise ValueError(f"keep_count must be at most {MAX_EVENT_KEEP_COUNT}")
    return count or None


def _clean_keep_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("keep_seconds must be an integer")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("keep_seconds must be an integer") from exc
    if seconds < 0:
        raise ValueError("keep_seconds must be at least 0")
    return seconds or None


def _subscription_last_event_ids(state: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    for key, value in state.items():
        if not key.startswith("sub_"):
            continue
        subscription = _load_json_object(value)
        if subscription is None:
            continue
        last_event_id = subscription.get("last_event_id")
        if isinstance(last_event_id, str) and last_event_id:
            protected.add(last_event_id)
    return protected


def _should_prune_after_publish(
    state: dict[str, Any],
    *,
    keep_count: int | None,
    keep_seconds: int | None,
    now: int,
) -> bool:
    if keep_count is None and keep_seconds is None:
        return False

    protected_ids = _subscription_last_event_ids(state)
    cutoff = None if keep_seconds is None else now - keep_seconds
    unprotected_count = 0
    for key, value in state.items():
        if not key.startswith("event_"):
            continue
        event = _load_json_object(value)
        if event is None:
            return True
        event_id = str(event.get("id", ""))
        if event_id in protected_ids:
            continue
        unprotected_count += 1
        if cutoff is not None and _coerce_int(event.get("timestamp")) < cutoff:
            return True

    return keep_count is not None and unprotected_count > keep_count


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    return str(value)


def _load_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _clean_text(value: str, *, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
