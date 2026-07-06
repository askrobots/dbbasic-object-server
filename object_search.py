"""Cross-collection record search driven by schema ``search`` metadata.

A collection opts into global search by declaring which fields are
searchable in its schema:

    {"search": {"fields": ["title", "content"]}}

Search semantics deliberately match the previous Django implementation so
existing clients keep their expectations: the query is split on
whitespace, every term must match at least one searchable field
(case-insensitive substring), and a short hex-ish query also matches
record ids by prefix. Results come back per collection with no ranking.

This module is pure matching and configuration. The server owns IO,
permission checks, and row filtering — search must only ever see records
the caller is already allowed to read.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

DEFAULT_COLLECTION_LIMIT = 10
MAX_COLLECTION_LIMIT = 100

_ID_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]+$")
_MIN_ID_PREFIX_HEX = 4


class InvalidSearchConfigError(ValueError):
    """Raised when a schema declares an unusable ``search`` section."""


def search_config(schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the normalized search config for one schema, or None.

    Returns None when the schema does not declare ``search``. Raises
    InvalidSearchConfigError when a declared config is malformed, so the
    misconfiguration surfaces as a warning instead of silently dropping
    the collection from search results.
    """
    section = schema.get("search")
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise InvalidSearchConfigError("Schema search section must be an object")

    fields = section.get("fields")
    if not isinstance(fields, list) or not fields:
        raise InvalidSearchConfigError("Schema search.fields must be a non-empty list")
    if not all(isinstance(name, str) and name for name in fields):
        raise InvalidSearchConfigError("Schema search.fields must contain field names")

    result_fields = section.get("result_fields", None)
    if result_fields is None:
        result_fields = _default_result_fields(schema, fields)
    elif not isinstance(result_fields, list) or not all(
        isinstance(name, str) and name for name in result_fields
    ):
        raise InvalidSearchConfigError("Schema search.result_fields must be a list of field names")

    return {"fields": list(fields), "result_fields": list(result_fields)}


def split_terms(query: str) -> list[str]:
    """Return the whitespace-separated search terms of a query."""
    return [term for term in query.split() if term]


def looks_like_id_prefix(query: str) -> bool:
    """Return True when a query should also match record ids by prefix."""
    text = query.strip()
    if not _ID_PREFIX_RE.fullmatch(text):
        return False
    return len(text.replace("-", "")) >= _MIN_ID_PREFIX_HEX


def record_matches(record: Mapping[str, Any], query: str, fields: list[str]) -> bool:
    """Return True when a record matches every term of a query.

    Each term must appear in at least one searchable field. As a
    fallback, an id-prefix-looking query matches the record id directly.
    """
    terms = split_terms(query)
    if terms and all(_term_in_fields(record, term, fields) for term in terms):
        return True
    if looks_like_id_prefix(query):
        record_id = str(record.get("id") or "")
        return record_id.lower().startswith(query.strip().lower())
    return False


def search_records(
    records: list[dict[str, str]],
    query: str,
    config: Mapping[str, Any],
    *,
    limit: int = DEFAULT_COLLECTION_LIMIT,
) -> list[dict[str, str]]:
    """Return matching records trimmed to the config's result fields."""
    if limit < 1:
        raise ValueError("Search limit must be positive")

    matches: list[dict[str, str]] = []
    for record in records:
        if not record_matches(record, query, config["fields"]):
            continue
        matches.append(_result_record(record, config["result_fields"]))
        if len(matches) >= limit:
            break
    return matches


def _term_in_fields(record: Mapping[str, Any], term: str, fields: list[str]) -> bool:
    needle = term.lower()
    for field in fields:
        value = record.get(field)
        if value is not None and needle in str(value).lower():
            return True
    return False


def _result_record(record: dict[str, str], result_fields: list[str]) -> dict[str, str]:
    return {field: record[field] for field in result_fields if field in record}


def _default_result_fields(schema: Mapping[str, Any], fields: list[str]) -> list[str]:
    views = schema.get("views")
    list_fields: list[str] = []
    if isinstance(views, Mapping):
        candidate = views.get("list_fields")
        if isinstance(candidate, list):
            list_fields = [name for name in candidate if isinstance(name, str)]

    ordered: list[str] = ["id"]
    for name in list_fields or fields:
        if name not in ordered:
            ordered.append(name)
    return ordered
