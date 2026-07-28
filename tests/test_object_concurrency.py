"""Tests for 63 -- optimistic concurrency (If-Match / expected_rev
compare-and-set on record update). See plan/vocabulary/63-concurrency-spec.md.

Covers the primitive at the record layer (compute_record_rev's properties and
update_collection_record's expected_rev precondition, including the claim race
the spec's worked example describes) plus the MCP bridge that carries the
precondition as a reserved body key.
"""

import json
from pathlib import Path

import pytest

import object_mcp
import object_records


def write_records(data_dir: Path, collection: str, content: str) -> Path:
    path = data_dir / "collections" / collection / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_schema(data_dir: Path, collection: str, fields: list[dict]) -> Path:
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fields": fields}))
    return path


# ---------------------------------------------------------------------------
# compute_record_rev -- the content fingerprint
# ---------------------------------------------------------------------------


def test_rev_is_deterministic_and_key_order_independent():
    a = {"id": "t1", "status": "open", "assigned_to": ""}
    b = {"assigned_to": "", "id": "t1", "status": "open"}  # same content, reordered
    assert object_records.compute_record_rev(a) == object_records.compute_record_rev(b)


def test_rev_changes_when_any_field_changes():
    base = {"id": "t1", "status": "open", "assigned_to": ""}
    changed = {"id": "t1", "status": "assigned", "assigned_to": "agent-A"}
    assert object_records.compute_record_rev(base) != object_records.compute_record_rev(changed)


def test_rev_excludes_the_rev_field_itself():
    """A caller round-tripping a full record it read (which carries `_rev`)
    must fingerprint to the same value as the stored row without it -- else
    echoing a read's token back would never match."""
    stored = {"id": "t1", "status": "open"}
    round_tripped = {"id": "t1", "status": "open", object_records.REV_FIELD: "whatever"}
    assert object_records.compute_record_rev(stored) == object_records.compute_record_rev(round_tripped)


def test_rev_handles_mixed_type_extra_values():
    # Surfaced extra-blob values can be non-string (ints, lists); the hash
    # must still be stable and not raise.
    rec = {"id": "o1", "total_cents": 1299, "tags": ["a", "b"], "meta": {"k": 1}}
    first = object_records.compute_record_rev(rec)
    assert first == object_records.compute_record_rev(dict(rec))


# ---------------------------------------------------------------------------
# update_collection_record -- the precondition
# ---------------------------------------------------------------------------


def _seed_task(data_dir: Path) -> str:
    write_records(
        data_dir,
        "tasks",
        "id\tstatus\tassigned_to\nt1\topen\t\n",
    )
    rev = object_records.compute_record_rev(
        object_records.get_collection_record("tasks", "t1", base_dir=data_dir, roots=[])
    )
    return rev


def test_matching_expected_rev_allows_the_write(tmp_path):
    data_dir = tmp_path / "data"
    rev = _seed_task(data_dir)

    updated = object_records.update_collection_record(
        "tasks",
        "t1",
        {"status": "assigned", "assigned_to": "agent-A"},
        base_dir=data_dir,
        roots=[],
        expected_rev=rev,
    )
    assert updated["assigned_to"] == "agent-A"


def test_mismatched_expected_rev_raises_and_writes_nothing(tmp_path):
    data_dir = tmp_path / "data"
    _seed_task(data_dir)

    with pytest.raises(object_records.VersionConflictError):
        object_records.update_collection_record(
            "tasks",
            "t1",
            {"status": "assigned", "assigned_to": "agent-B"},
            base_dir=data_dir,
            roots=[],
            expected_rev="stale-token-that-does-not-match",
        )
    # No write happened: the row is untouched.
    row = object_records.get_collection_record("tasks", "t1", base_dir=data_dir, roots=[])
    assert row == {"id": "t1", "status": "open", "assigned_to": ""}


def test_omitted_expected_rev_is_last_write_wins(tmp_path):
    data_dir = tmp_path / "data"
    _seed_task(data_dir)
    # No precondition supplied -> behaves exactly as before, no conflict.
    updated = object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[]
    )
    assert updated["status"] == "assigned"


def test_claim_race_second_writer_conflicts(tmp_path):
    """The spec's worked example: two agents read the same open task at the
    same `_rev`, both try to claim. First write wins; the second's precondition
    now compares against the changed row and fails -- no silent clobber."""
    data_dir = tmp_path / "data"
    rev = _seed_task(data_dir)  # both A and B read this same token

    # A claims first, with the shared rev -> succeeds.
    object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned", "assigned_to": "agent-A"},
        base_dir=data_dir, roots=[], expected_rev=rev,
    )

    # B claims with the SAME (now stale) rev -> conflict, no overwrite.
    with pytest.raises(object_records.VersionConflictError):
        object_records.update_collection_record(
            "tasks", "t1", {"status": "assigned", "assigned_to": "agent-B"},
            base_dir=data_dir, roots=[], expected_rev=rev,
        )

    row = object_records.get_collection_record("tasks", "t1", base_dir=data_dir, roots=[])
    assert row["assigned_to"] == "agent-A"  # A holds it; B never clobbered


def test_new_rev_after_write_is_usable_for_next_update(tmp_path):
    """A read-modify-write loop can chain: the rev of the just-written row is
    the precondition for the next write."""
    data_dir = tmp_path / "data"
    rev1 = _seed_task(data_dir)
    r1 = object_records.update_collection_record(
        "tasks", "t1", {"status": "assigned"}, base_dir=data_dir, roots=[], expected_rev=rev1
    )
    rev2 = object_records.compute_record_rev(r1)
    assert rev2 != rev1
    # The chained write succeeds against the fresh token.
    object_records.update_collection_record(
        "tasks", "t1", {"assigned_to": "agent-A"}, base_dir=data_dir, roots=[], expected_rev=rev2
    )
    # But the original token is now stale.
    with pytest.raises(object_records.VersionConflictError):
        object_records.update_collection_record(
            "tasks", "t1", {"status": "done"}, base_dir=data_dir, roots=[], expected_rev=rev1
        )


# ---------------------------------------------------------------------------
# MCP bridge -- precondition rides a reserved body key (no header channel)
# ---------------------------------------------------------------------------


def test_mcp_update_record_without_expected_rev_sends_plain_changes():
    method, path, query, body = object_mcp.tool_route(
        "update_record",
        {"collection": "tasks", "record_id": "t1", "changes": {"status": "assigned"}},
    )
    assert method == "PUT"
    assert json.loads(body) == {"status": "assigned"}  # no expected_rev key injected


def test_mcp_update_record_threads_expected_rev_into_body():
    method, path, query, body = object_mcp.tool_route(
        "update_record",
        {
            "collection": "tasks",
            "record_id": "t1",
            "changes": {"status": "assigned"},
            "expected_rev": "abc123",
        },
    )
    payload = json.loads(body)
    assert payload["status"] == "assigned"
    assert payload["expected_rev"] == "abc123"


def test_mcp_update_record_rejects_non_string_expected_rev():
    with pytest.raises(ValueError):
        object_mcp.tool_route(
            "update_record",
            {"collection": "tasks", "record_id": "t1", "changes": {}, "expected_rev": 5},
        )


# ---------------------------------------------------------------------------
# `revs` on the LIST read -- 63's missing half
#
# Single-record reads have always returned `_rev`. List reads did not, so a
# claiming agent had to GET each candidate individually just to obtain a rev
# before it could claim safely with If-Match: an extra round trip per attempt,
# on the exact path 63 exists for ("claim becomes race-safe"). A list that
# hands back rows you cannot act on without re-reading them made the caller
# do the work twice.

import sys as _sys                                                  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_object_server import (                                    # noqa: E402
    auth_headers, enable_admin_token, request, write_records, write_source,
)


def _contacts_box(tmp_path, monkeypatch, rows="id\tname\nc1\tAda\nc2\tGrace\n"):
    root, data_dir = tmp_path / "objects", tmp_path / "data"
    write_source(root / "contacts" / "directory.py", "def GET(request):\n    return {}\n")
    write_records(data_dir, "contacts", rows)
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    enable_admin_token(monkeypatch)
    return data_dir


def test_a_list_read_hands_back_a_rev_for_every_row(tmp_path, monkeypatch):
    _contacts_box(tmp_path, monkeypatch)
    status, _, payload = request("/admin/collections/contacts/records",
                                 headers=auth_headers())
    assert status == 200
    assert set(payload["revs"]) == {"c1", "c2"}
    assert all(len(rev) == 64 for rev in payload["revs"].values())


def test_the_list_rev_is_the_same_token_the_single_read_gives(tmp_path, monkeypatch):
    """If these two ever disagreed, an If-Match built from a list would 409
    forever while the identical one from a detail read succeeded -- the
    worst kind of bug, because it looks like a race that is not there."""
    _contacts_box(tmp_path, monkeypatch)
    _, _, listed = request("/admin/collections/contacts/records",
                           headers=auth_headers())
    _, _, single = request("/admin/collections/contacts/records/c1",
                           headers=auth_headers())
    assert listed["revs"]["c1"] == single["_rev"]


def test_a_rev_from_a_list_actually_claims(tmp_path, monkeypatch):
    """The whole point, end to end: list, claim with the listed rev, and a
    second claim holding the same now-stale rev loses."""
    _contacts_box(tmp_path, monkeypatch)
    _, _, listed = request("/admin/collections/contacts/records",
                           headers=auth_headers())
    rev = listed["revs"]["c1"]

    first = request("/admin/collections/contacts/records/c1", method="PUT",
                    body=json.dumps({"name": "Ada L"}).encode(),
                    headers=auth_headers() + [("if-match", rev)])
    assert first[0] == 200

    second = request("/admin/collections/contacts/records/c1", method="PUT",
                     body=json.dumps({"name": "Somebody Else"}).encode(),
                     headers=auth_headers() + [("if-match", rev)])
    assert second[0] == 409


def test_only_the_returned_window_is_hashed(tmp_path, monkeypatch):
    """One sha256 per RETURNED row, never per matching row, so a narrow
    page over a large collection stays cheap."""
    rows = "id\tname\n" + "".join(f"c{n}\tName{n}\n" for n in range(1, 51))
    _contacts_box(tmp_path, monkeypatch, rows=rows)
    _, _, payload = request("/admin/collections/contacts/records",
                            query_string="limit=5", headers=auth_headers())
    assert payload["count"] == 5
    assert payload["total"] == 50
    assert len(payload["revs"]) == 5
    assert set(payload["revs"]) == {row["id"] for row in payload["records"]}


def test_revs_are_a_sibling_map_never_merged_into_the_rows(tmp_path, monkeypatch):
    """The record shape is a contract: the single-record read documents
    `_rev` as sibling metadata that must never collide with a schema field
    or appear on write-back. A list is the one place records are most
    likely to be echoed straight into a write, so merging it there would
    break the rule where it matters most."""
    _contacts_box(tmp_path, monkeypatch)
    _, _, payload = request("/admin/collections/contacts/records",
                            headers=auth_headers())
    for row in payload["records"]:
        assert "_rev" not in row


def test_the_flag_governs_the_list_too(tmp_path, monkeypatch):
    """With the precondition ignored, shipping revs would hand callers a
    token that buys them nothing -- and every If-Match built from it would
    silently succeed against a changed row."""
    _contacts_box(tmp_path, monkeypatch)
    monkeypatch.setenv("DBBASIC_ENABLE_CONCURRENCY", "false")
    _, _, payload = request("/admin/collections/contacts/records",
                            headers=auth_headers())
    assert "revs" not in payload
