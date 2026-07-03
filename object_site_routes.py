"""Site routing: clean public URLs resolved to DBBASIC objects.

Design (see docs/site-routing.md for the full rationale):

- Convention first: `/about` maps to object `site_about`, `/docs/install` to
  `site_docs_install`, and `/` to `site_home`. Creating a page object creates
  its URL, like filesystem routing, with hyphens translated to underscores.
- Data overrides second: a `site_routes` records collection maps URL patterns
  to objects for what conventions cannot express — parameterized routes like
  `/articles/{article_id:uuid}` — with schema validation, change history, and
  rollback from the existing records machinery.
- Routing only maps URLs. Authorization stays in the permission policy: the
  resolved object runs through the normal execution path, so enforcement,
  audit, timeouts, and correlation ids all apply.

Pattern segments: literal text, `{name}` (any single segment), or
`{name:uuid}` (segment must be UUID-shaped). Captured params are merged into
the object's request payload.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

SITE_ROUTES_COLLECTION = "site_routes"
ROOT_OBJECT_ID = "site_home"
NOT_FOUND_OBJECT_ID = "site_404"
SITE_OBJECT_PREFIX = "site_"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PARAM_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)(?::(uuid))?\}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def convention_object_id(path: str) -> str | None:
    """Map one URL path to its conventional site object id, or None."""
    if path == "/":
        return ROOT_OBJECT_ID

    segments = _path_segments(path)
    if segments is None or not segments:
        return None

    parts = []
    for segment in segments:
        if not _SEGMENT_RE.fullmatch(segment):
            return None
        parts.append(segment.replace("-", "_"))
    return SITE_OBJECT_PREFIX + "_".join(parts)


def match_records(
    path: str,
    records: Iterable[Mapping[str, Any]],
) -> tuple[str, dict[str, str]] | None:
    """Match one path against site_routes records; most specific pattern wins."""
    segments = _path_segments(path)
    if segments is None:
        return None

    candidates = []
    for record in records:
        pattern = record.get("pattern")
        object_id = record.get("object_id")
        if not isinstance(pattern, str) or not isinstance(object_id, str):
            continue
        if not pattern.startswith("/") or not object_id.strip():
            continue
        parsed = _parse_pattern(pattern)
        if parsed is None:
            continue
        params = _match_segments(parsed, segments)
        if params is None:
            continue
        literal_count = sum(1 for kind, _, _ in parsed if kind == "literal")
        priority = record.get("priority")
        try:
            priority_value = int(priority) if priority not in (None, "") else 100
        except (TypeError, ValueError):
            priority_value = 100
        candidates.append((priority_value, -literal_count, object_id.strip(), params))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, object_id, params = candidates[0]
    return object_id, params


def _path_segments(path: str) -> list[str] | None:
    if not path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        return None
    return segments


def _parse_pattern(pattern: str) -> list[tuple[str, str, str]] | None:
    parsed: list[tuple[str, str, str]] = []
    for raw in pattern.split("/"):
        if not raw:
            continue
        param = _PARAM_RE.fullmatch(raw)
        if param is not None:
            parsed.append(("param", param.group(1), param.group(2) or ""))
        else:
            parsed.append(("literal", raw, ""))
    if not parsed:
        return None
    return parsed


def _match_segments(
    parsed: list[tuple[str, str, str]],
    segments: list[str],
) -> dict[str, str] | None:
    if len(parsed) != len(segments):
        return None

    params: dict[str, str] = {}
    for (kind, value, constraint), segment in zip(parsed, segments):
        if kind == "literal":
            if value != segment:
                return None
            continue
        if constraint == "uuid" and not _UUID_RE.fullmatch(segment):
            return None
        params[value] = segment
    return params
