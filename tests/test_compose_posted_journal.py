"""object_finance.compose_posted_journal: the shared composer every
generated journal goes through (extracted at the doctrine-#4 threshold --
plan/inventory-adjustments-spec.md section 5). Callers own policy; these
tests pin the mechanics: idempotency by provenance, draft -> lines ->
re-read -> verify -> post, and the fail-soft shapes."""

import json
import pathlib

import object_finance
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"


def _header_from_schema(pkg, name):
    schema = json.loads((PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for pkg, name in (("app-finance", "fin_journals"),
                      ("app-finance", "fin_journal_lines"),
                      ("app-finance", "fin_accounts"),
                      ("app-entities", "entities")):
        (schema_dir / f"{name}.json").write_text(
            (PACKAGES / pkg / "schemas" / f"{name}.json").read_text())
        d = data_dir / "collections" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "records.tsv").write_text(_header_from_schema(pkg, name))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    for acct in ("a-dr", "a-cr", "a", "b"):
        object_records.create_collection_record(
            "fin_accounts",
            {"id": acct, "name": acct, "account_type": "asset", "owner_id": "dan"},
            base_dir=data_dir)
    for ent in ("ent-1", "ent-2"):
        object_records.create_collection_record(
            "entities", {"id": ent, "name": ent, "owner_id": "dan"},
            base_dir=data_dir)
    return data_dir


def compose(data_dir, **overrides):
    kwargs = dict(
        generated_from="test/1",
        date="2026-07-24",
        description="Test entry",
        lines=[
            {"account_id": "a-dr", "debit_cents": 500, "credit_cents": 0},
            {"account_id": "a-cr", "debit_cents": 0, "credit_cents": 500},
        ],
        owner_id="dan",
    )
    kwargs.update(overrides)
    return object_finance.compose_posted_journal(data_dir, **kwargs)


def journals(data_dir):
    return object_records.read_collection_records("fin_journals", base_dir=data_dir)


def lines_for(data_dir, journal_id):
    return [l for l in object_records.read_collection_records(
                "fin_journal_lines", base_dir=data_dir)
            if l["journal_id"] == journal_id]


def test_balanced_entry_is_created_and_posted_with_provenance(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = compose(data_dir)
    assert result["ok"] and result["posted"]
    journal = journals(data_dir)[0]
    assert journal["status"] == "posted"
    assert journal["generated_from"] == "test/1"
    assert journal["kind"] == "standard"
    landed = lines_for(data_dir, result["journal_id"])
    assert sorted((l["debit_cents"], l["credit_cents"]) for l in landed) == \
        [("0", "500"), ("500", "0")]


def test_replay_is_idempotent_by_provenance(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    first = compose(data_dir)
    again = compose(data_dir)
    assert "already composed" in again["skipped"]
    assert again["journal_id"] == first["journal_id"]
    assert again["posted"] is True
    assert len(journals(data_dir)) == 1


def test_unbalanced_entry_stays_draft_with_note(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = compose(data_dir, lines=[
        {"account_id": "a-dr", "debit_cents": 500, "credit_cents": 0},
        {"account_id": "a-cr", "debit_cents": 0, "credit_cents": 300},
    ])
    assert result["posted"] is False
    assert "did not balance" in result["note"]
    assert journals(data_dir)[0]["status"] == "draft"


def test_post_false_composes_but_leaves_draft(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = compose(data_dir, post=False)
    assert result["posted"] is False
    assert "post not requested" in result["note"]
    assert journals(data_dir)[0]["status"] == "draft"


def test_zero_or_empty_lines_compose_nothing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    assert compose(data_dir, lines=[])["skipped"] == "zero amount"
    assert compose(data_dir, lines=[
        {"account_id": "a", "debit_cents": 0, "credit_cents": 0},
    ])["skipped"] == "zero amount"
    assert journals(data_dir) == []


def test_unusable_amounts_error_without_writing(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    bad = compose(data_dir, lines=[{"account_id": "a", "debit_cents": "abc"}])
    assert bad["ok"] is False
    negative = compose(data_dir, lines=[
        {"account_id": "a", "debit_cents": -500, "credit_cents": 0},
        {"account_id": "b", "debit_cents": 0, "credit_cents": -500},
    ])
    assert negative["ok"] is False
    assert journals(data_dir) == []


def test_memo_kind_and_per_line_entity_land(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = compose(data_dir, kind="adjusting", entity_id="ent-1", lines=[
        {"account_id": "a-dr", "debit_cents": 200, "credit_cents": 0,
         "memo": "note", "entity_id": "ent-2"},
        {"account_id": "a-cr", "debit_cents": 0, "credit_cents": 200},
    ])
    journal = journals(data_dir)[0]
    assert journal["kind"] == "adjusting"
    assert journal["entity_id"] == "ent-1"
    by_account = {l["account_id"]: l for l in lines_for(data_dir, result["journal_id"])}
    assert by_account["a-dr"]["memo"] == "note"
    assert by_account["a-dr"]["entity_id"] == "ent-2"     # per-line override
    assert by_account["a-cr"]["entity_id"] == "ent-1"     # journal default


def test_find_journal_by_provenance(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    assert object_finance.find_journal_by_provenance(data_dir, "test/1") is None
    created = compose(data_dir)
    found = object_finance.find_journal_by_provenance(data_dir, "test/1")
    assert found["id"] == created["journal_id"]
