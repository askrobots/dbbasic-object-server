"""Shared ID helpers for DBBASIC-created resources."""

from __future__ import annotations

from uuid import UUID, uuid4


def new_uuid4() -> str:
    """Return a canonical UUIDv4 string."""
    return str(uuid4())


def normalize_uuid4(value: object) -> str | None:
    """Return a canonical UUIDv4 string, or ``None`` for non-UUIDv4 input."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = UUID(text)
    except (TypeError, ValueError, AttributeError):
        return None

    if parsed.version != 4:
        return None

    return str(parsed)


def is_uuid4(value: object) -> bool:
    """Return True when ``value`` is a UUIDv4 string."""
    return normalize_uuid4(value) is not None
