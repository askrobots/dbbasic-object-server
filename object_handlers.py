"""Event handler objects (HANDLES) — Phase 5a of the upgrade system.

See docs/event-hooks-decisions.md and docs/upgrade-and-customization.md
(Rule 4) for the design and why it looks like this. Summary: an
operator/package object may declare a module-level ``HANDLES = [...]`` list
of event names it wants to run on. When a record write commits, the server
dispatches the matching event to each handler object, post-commit and
best-effort — a handler failure never breaks or rolls back the write.

This module owns three small, independent pieces:

- Static extraction of ``HANDLES`` from source text (AST only; never exec).
- A cached event -> handler-object-id index built from system object
  sources only (Decision 2: user-authored handlers are deferred).
- A reentry guard so a handler's own writes cannot cause unbounded
  recursive dispatch.

The whole feature is gated behind ``DBBASIC_ENABLE_EVENT_HANDLERS``; when
unset (the default) every entry point here is inert and object_server's
dispatch call is a no-op.
"""
from __future__ import annotations

import ast
import contextlib
import os
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable, Iterator

from object_namespace import iter_object_sources

HANDLERS_ENABLED_ENV = "DBBASIC_ENABLE_EVENT_HANDLERS"

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Present-tense past-participle event names matching the action:
# create -> created, update -> updated, delete -> deleted.
_ACTION_EVENT_SUFFIXES = {
    "create": "created",
    "update": "updated",
    "delete": "deleted",
}

MAX_DISPATCH_DEPTH = 4

_dispatch_depth: ContextVar[int] = ContextVar("dbbasic_event_dispatch_depth", default=0)

_INDEX: dict[str, list[str]] | None = None
_INDEX_LOCK = threading.Lock()


def handlers_enabled() -> bool:
    """Return True when event handler dispatch is enabled.

    Reads os.environ directly (standalone, mirrors the shape of
    object_server's other boolean env checks without depending on it).
    """
    value = os.environ.get(HANDLERS_ENABLED_ENV, "")
    return value.strip().lower() in _TRUE_VALUES


def event_name(collection: str, action: str) -> str | None:
    """Return "<collection>.record.<created|updated|deleted>", or None.

    None is returned for an unknown action or an empty collection name.
    """
    suffix = _ACTION_EVENT_SUFFIXES.get(action)
    if suffix is None or not collection:
        return None
    return f"{collection}.record.{suffix}"


def extract_handles(source_text: str) -> list[str]:
    """Statically extract a module-level ``HANDLES = [...]`` string list.

    Pure AST parse — the source is never executed. Mirrors the safety
    posture of object_source.source_method_report. Returns [] whenever the
    source has a syntax error, has no ``HANDLES`` assignment, or the value
    is not a list/tuple literal. Non-string entries inside the literal are
    ignored rather than raising.
    """
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "HANDLES"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return []
        return [
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]

    return []


def build_index(roots: Iterable[Path] | None = None) -> dict[str, list[str]]:
    """Scan system object sources and map each declared event to object IDs.

    System objects only (source.kind == "system") — user and override
    sources are skipped, per Decision 2 in docs/event-hooks-decisions.md:
    only operator-installed objects may declare handlers in this phase.
    """
    index: dict[str, set[str]] = {}
    for source in iter_object_sources(roots):
        if source.kind != "system":
            continue
        try:
            text = source.path.read_text()
        except OSError:
            continue
        for event in extract_handles(text):
            index.setdefault(event, set()).add(source.object_id)

    return {event: sorted(object_ids) for event, object_ids in index.items()}


def get_handlers(event: str, roots: Iterable[Path] | None = None) -> list[str]:
    """Return handler object IDs for an event, building/caching the index."""
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = build_index(roots)
        return list(_INDEX.get(event, []))


def invalidate() -> None:
    """Drop the cached index so the next get_handlers() call rebuilds it.

    Call this after any object source create/update/delete so a
    newly-added or edited handler is picked up.
    """
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None


def current_depth() -> int:
    """Return the current event-dispatch reentry depth."""
    return _dispatch_depth.get()


@contextlib.contextmanager
def dispatch_guard() -> Iterator[None]:
    """Track reentrant dispatch depth for the duration of one handler call.

    Belt-and-suspenders: in practice a handler's writes go through
    object_records, which does not itself re-fire dispatch synchronously
    into a growing call stack the way this guard assumes, but the guard
    costs nothing and caps any future path that could recurse.
    """
    token = _dispatch_depth.set(_dispatch_depth.get() + 1)
    try:
        yield
    finally:
        _dispatch_depth.reset(token)
