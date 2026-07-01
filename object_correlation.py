"""Request correlation IDs for DBBASIC runtime actions."""

from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import UUID, uuid4


CORRELATION_ID_HEADER = "x-dbbasic-correlation-id"

_current_correlation_id: ContextVar[str | None] = ContextVar(
    "dbbasic_correlation_id",
    default=None,
)


def new_correlation_id() -> str:
    """Return a new UUIDv4 correlation ID."""
    return str(uuid4())


def normalize_correlation_id(value: str | None) -> str | None:
    """Return a canonical UUID string, or ``None`` for unusable input."""
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


def ensure_correlation_id(value: str | None = None) -> str:
    """Normalize an incoming ID or create a new UUIDv4 ID."""
    return normalize_correlation_id(value) or new_correlation_id()


def current_correlation_id() -> str | None:
    """Return the current request/action correlation ID, if one is set."""
    return _current_correlation_id.get()


def set_current_correlation_id(correlation_id: str | None) -> Token[str | None]:
    """Set the current correlation ID and return a reset token."""
    return _current_correlation_id.set(normalize_correlation_id(correlation_id))


def reset_current_correlation_id(token: Token[str | None]) -> None:
    """Reset the current correlation ID to a previous context value."""
    _current_correlation_id.reset(token)
