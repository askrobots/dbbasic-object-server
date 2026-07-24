"""Pre-write hooks (plan/pre-write-hook-spec.md): a collection declares
`hooks: {"before_write": "<object_id>"}` and that object runs synchronously
inside the generic HTTP write path -- after permission checks, before persist.
It can reject (4xx with its own message) or transform the record. Fail CLOSED:
a declared hook that is missing, raises, or returns a non-contract shape
rejects the write. Trusted server-side writes (object_records called directly)
bypass hooks deliberately.

This is the escape hatch for logic the schema can't express (cross-field /
cross-collection validation) that keeps the generative form working -- the
form still POSTs to /collections/{c}/records; the hook lives server-side.
"""

import json

import object_records
import object_server
from test_object_server import (
    TEST_ADMIN_TOKEN,
    enable_admin_token,
    request,
    write_records,
)

AUTH = [("authorization", f"Token {TEST_ADMIN_TOKEN}")]

SCHEMA = {
    "name": "gadgets",
    "version": 1,
    "fields": [
        {"name": "id"},
        {"name": "name", "type": "text", "required": True},
        {"name": "qty", "type": "integer"},
        {"name": "created_at", "type": "datetime", "read_only": True},
        {"name": "owner_id", "type": "text"},
    ],
}


def setup_env(tmp_path, monkeypatch, hook_source=None, declare_hook=True):
    data_dir = tmp_path / "data"
    objects_root = tmp_path / "objects"
    objects_root.mkdir(parents=True, exist_ok=True)
    write_records(data_dir, "gadgets", "id\tname\tqty\tcreated_at\towner_id\n")
    schema = dict(SCHEMA)
    if declare_hook:
        schema["hooks"] = {"before_write": "hook_gadgets"}
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "gadgets.json").write_text(json.dumps(schema))
    if hook_source is not None:
        hook_dir = objects_root / "hook"
        hook_dir.mkdir(parents=True, exist_ok=True)
        (hook_dir / "gadgets.py").write_text(hook_source)
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(objects_root))
    enable_admin_token(monkeypatch)
    return data_dir


def create_gadget(record):
    return request(
        "/collections/gadgets/records",
        method="POST",
        body=json.dumps(record).encode("utf-8"),
        headers=AUTH,
    )


def list_gadgets():
    status, _, payload = request("/collections/gadgets/records", headers=AUTH)
    assert status == 200
    return payload["records"]


def test_collection_without_hook_writes_unchanged(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, declare_hook=False)
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "3"})
    assert status in (200, 201), payload
    assert [r["id"] for r in list_gadgets()] == ["g1"]


def test_hook_rejects_with_its_own_message_and_status(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            "    if int(request['record'].get('qty') or 0) > 100:\n"
            "        return {'error': 'Quantity over 100 needs approval', 'status': 422}\n"
            "    return None\n"
        ),
    )
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "500"})
    assert status == 422
    assert payload["code"] == "hook_rejected"
    assert payload["error"] == "Quantity over 100 needs approval"
    assert list_gadgets() == []

    # Under the limit passes through the same hook.
    status, _, _ = create_gadget({"id": "g2", "name": "widget", "qty": "5"})
    assert status in (200, 201)
    assert [r["id"] for r in list_gadgets()] == ["g2"]


def test_hook_reject_status_is_clamped_to_4xx(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            "    return {'error': 'no', 'status': 200}\n"
        ),
    )
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "1"})
    assert status == 400
    assert payload["code"] == "hook_rejected"


def test_hook_transform_persists_and_still_schema_validates(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            "    record = dict(request['record'])\n"
            "    record['name'] = record.get('name', '').upper()\n"
            "    if record.get('qty') == '13':\n"
            "        record['qty'] = 'thirteen'  # invalid for integer field\n"
            "    return {'record': record}\n"
        ),
    )
    status, _, _ = create_gadget({"id": "g1", "name": "widget", "qty": "2"})
    assert status in (200, 201)
    assert list_gadgets()[0]["name"] == "WIDGET"

    # A transform that violates the schema is still caught by validation.
    status, _, payload = create_gadget({"id": "g2", "name": "widget", "qty": "13"})
    assert status == 400
    assert "qty" in payload["error"]


def test_hook_cannot_touch_id_owner_or_read_only_fields(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            "    record = dict(request['record'])\n"
            "    record['id'] = 'hijacked'\n"
            "    record['owner_id'] = 'attacker'\n"
            "    record['created_at'] = '1999-01-01T00:00:00Z'\n"
            "    return {'record': record}\n"
        ),
    )
    status, _, _ = create_gadget({"id": "g1", "name": "widget", "qty": "1", "owner_id": "dan"})
    assert status in (200, 201)
    rows = list_gadgets()
    assert [r["id"] for r in rows] == ["g1"]
    assert rows[0]["owner_id"] == "dan"
    assert rows[0]["created_at"] != "1999-01-01T00:00:00Z"


def test_declared_but_missing_or_raising_hook_fails_closed(tmp_path, monkeypatch):
    # Declared, object file absent -> reject, nothing persisted.
    setup_env(tmp_path, monkeypatch, hook_source=None)
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "1"})
    assert status == 500
    assert payload["code"] == "hook_failed"
    assert list_gadgets() == []


def test_raising_hook_fails_closed(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source="def BEFORE_WRITE(request):\n    raise RuntimeError('boom')\n",
    )
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "1"})
    assert status == 500
    assert payload["code"] == "hook_failed"
    assert list_gadgets() == []


def test_non_contract_return_fails_closed(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source="def BEFORE_WRITE(request):\n    return 'nope'\n",
    )
    status, _, payload = create_gadget({"id": "g1", "name": "widget", "qty": "1"})
    assert status == 500
    assert payload["code"] == "hook_failed"


def test_denied_write_never_reaches_the_hook(tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran"
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            f"    open({str(marker)!r}, 'w').write('ran')\n"
            "    return None\n"
        ),
    )
    # No admin token header -> the write is denied before any hook runs.
    status, _, _ = request(
        "/collections/gadgets/records",
        method="POST",
        body=json.dumps({"id": "g1", "name": "widget"}).encode("utf-8"),
    )
    assert status in (401, 403)
    assert not marker.exists()


def test_trusted_server_side_writes_bypass_hooks(tmp_path, monkeypatch):
    marker = tmp_path / "hook-ran"
    data_dir = setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            f"    open({str(marker)!r}, 'w').write('ran')\n"
            "    return {'error': 'would reject'}\n"
        ),
    )
    stored = object_records.create_collection_record(
        "gadgets",
        {"id": "sys1", "name": "seeded", "qty": "1"},
        base_dir=data_dir,
    )
    assert stored["id"] == "sys1"
    assert not marker.exists()


def test_update_path_runs_hook_with_existing_and_action(tmp_path, monkeypatch):
    setup_env(
        tmp_path,
        monkeypatch,
        hook_source=(
            "def BEFORE_WRITE(request):\n"
            "    if request['action'] != 'update':\n"
            "        return None\n"
            "    before = int(request['existing'].get('qty') or 0)\n"
            "    after = int(request['record'].get('qty') or 0)\n"
            "    if after < before:\n"
            "        return {'error': 'Quantity can only grow', 'status': 409}\n"
            "    record = dict(request['record'])\n"
            "    record['name'] = record.get('name', '') + '-checked'\n"
            "    return {'record': record}\n"
        ),
    )
    status, _, _ = create_gadget({"id": "g1", "name": "widget", "qty": "5"})
    assert status in (200, 201)

    # Shrinking qty is rejected by the hook with its own status.
    status, _, payload = request(
        "/collections/gadgets/records/g1",
        method="PUT",
        body=json.dumps({"qty": "2"}).encode("utf-8"),
        headers=AUTH,
    )
    assert status == 409
    assert payload["code"] == "hook_rejected"
    assert list_gadgets()[0]["qty"] == "5"

    # Growing qty passes and the hook's transform lands on the stored row.
    status, _, _ = request(
        "/collections/gadgets/records/g1",
        method="PUT",
        body=json.dumps({"qty": "9"}).encode("utf-8"),
        headers=AUTH,
    )
    assert status == 200
    row = list_gadgets()[0]
    assert row["qty"] == "9"
    assert row["name"] == "widget-checked"


# ---------------------------------------------------------------------
# First adopter: fin_journals balance enforcement (packages/app-finance).
# Cross-collection validation the schema can't express -- the canonical
# reason this primitive exists.
# ---------------------------------------------------------------------

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FINANCE = REPO_ROOT / "packages" / "app-finance"


def setup_finance(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_records(
        data_dir,
        "fin_journals",
        "id\tdate\tdescription\tstatus\towner_id\tcreated_at\n"
        "j1\t2026-07-01\tOpening entry\tdraft\tadmin\t\n",
    )
    write_records(
        data_dir,
        "fin_journal_lines",
        "id\tjournal_id\taccount_id\tdebit_cents\tcredit_cents\towner_id\n",
    )
    # account_id is a validated relation -> the target record must exist.
    write_records(data_dir, "fin_accounts", "id\tname\na1\tCash\n")
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for name in ("fin_journals", "fin_journal_lines"):
        (schema_dir / f"{name}.json").write_text(
            (FINANCE / "schemas" / f"{name}.json").read_text()
        )
    monkeypatch.setenv(object_server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(FINANCE / "objects"))
    enable_admin_token(monkeypatch)
    return data_dir


def post_journal():
    return request(
        "/collections/fin_journals/records/j1",
        method="PUT",
        body=json.dumps({"status": "posted"}).encode("utf-8"),
        headers=AUTH,
    )


def add_line(data_dir, line_id, debit, credit):
    object_records.create_collection_record(
        "fin_journal_lines",
        {"id": line_id, "journal_id": "j1", "account_id": "a1",
         "debit_cents": str(debit), "credit_cents": str(credit)},
        base_dir=data_dir,
    )


def test_fin_journal_cannot_post_empty(tmp_path, monkeypatch):
    setup_finance(tmp_path, monkeypatch)
    status, _, payload = post_journal()
    assert status == 409
    assert payload["code"] == "hook_rejected"
    assert "empty journal" in payload["error"]


def test_fin_journal_cannot_post_unbalanced_but_posts_balanced(tmp_path, monkeypatch):
    data_dir = setup_finance(tmp_path, monkeypatch)
    add_line(data_dir, "l1", 1000, 0)
    add_line(data_dir, "l2", 0, 900)

    status, _, payload = post_journal()
    assert status == 409
    assert "debits 1000 != credits 900" in payload["error"]
    # still draft
    status, _, row = request("/collections/fin_journals/records/j1", headers=AUTH)
    assert row["record"]["status"] == "draft"

    add_line(data_dir, "l3", 0, 100)
    status, _, _ = post_journal()
    assert status == 200
    status, _, row = request("/collections/fin_journals/records/j1", headers=AUTH)
    assert row["record"]["status"] == "posted"


def test_fin_journal_draft_edits_pass_the_hook_untouched(tmp_path, monkeypatch):
    setup_finance(tmp_path, monkeypatch)
    status, _, _ = request(
        "/collections/fin_journals/records/j1",
        method="PUT",
        body=json.dumps({"description": "renamed"}).encode("utf-8"),
        headers=AUTH,
    )
    assert status == 200
