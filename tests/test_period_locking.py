"""Closed periods (the q9-parity control this system lacked): settled
books stay settled.

The scenario every test here guards: the year is filed, and months later
someone -- helpful, hurried, or dishonest -- edits last March. Without
this gate the filed statements and the live books drift apart with nobody
deciding they should; with it, the only paths are a correcting journal in
the CURRENT period, or a deliberate, attributed reopening.
"""

import pathlib

from conftest import stage_collection

import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
FINANCE_OBJECTS = PACKAGES / "app-finance" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in ("fin_journals", "fin_journal_lines", "fin_accounts",
                 "fin_closed_periods"):
        stage_collection(data_dir, "app-finance", name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "fin_accounts", {"id": "acct-cash", "name": "Cash", "account_type": "asset",
                         "owner_id": "dan"}, base_dir=data_dir)
    return data_dir


def close_period(data_dir, *, start="2026-01-01", end="2026-03-31", owner="dan",
                 entity="", reason="Q1 filed", pid="cp1"):
    return object_records.create_collection_record(
        "fin_closed_periods",
        {"id": pid, "start_date": start, "end_date": end, "reason": reason,
         "owner_id": owner, **({"entity_id": entity} if entity else {})},
        base_dir=data_dir)


def make_journal(data_dir, jid, *, date, owner="dan", status="draft"):
    return object_records.create_collection_record(
        "fin_journals",
        {"id": jid, "date": date, "description": f"J {jid}", "status": status,
         "kind": "standard", "owner_id": owner},
        base_dir=data_dir)


def hook(object_id, payload):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method="BEFORE_WRITE", payload=payload),
        roots=[FINANCE_OBJECTS]).result


def journal_hook(action, *, record=None, existing=None, changes=None):
    return hook("hook_fin_journals", {
        "action": action, "collection": "fin_journals",
        "record": record or {}, "existing": existing or {}, "changes": changes or {}})


def line_hook(action, *, record=None, existing=None, changes=None):
    return hook("hook_fin_journal_lines", {
        "action": action, "collection": "fin_journal_lines",
        "record": record or {}, "existing": existing or {}, "changes": changes or {}})


# --- the journal-side gate ----------------------------------------------------

def test_a_journal_cannot_be_created_inside_a_closed_period(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir)
    verdict = journal_hook("create", record={
        "id": "j1", "date": "2026-02-14", "owner_id": "dan"})
    assert verdict["status"] == 409
    assert "are closed" in verdict["error"]
    assert "Q1 filed" in verdict["error"]          # the reason travels
    # And the honest paths are named in the message itself.
    assert "current period" in verdict["error"]
    assert "reopen" in verdict["error"]


def test_dates_outside_the_period_pass_including_the_boundaries(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir)                          # Jan 1 .. Mar 31 inclusive
    ok = journal_hook("create", record={"id": "j1", "date": "2026-04-01",
                                        "owner_id": "dan"})
    assert ok is None
    on_end = journal_hook("create", record={"id": "j2", "date": "2026-03-31",
                                            "owner_id": "dan"})
    assert on_end["status"] == 409                  # end_date is INSIDE


def test_a_journal_dated_inside_cannot_be_edited_at_all(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir)
    make_journal(data_dir, "j-old", date="2026-02-01")
    verdict = journal_hook("update",
                           existing={"id": "j-old", "date": "2026-02-01",
                                     "owner_id": "dan"},
                           changes={"description": "just fixing a typo"})
    assert verdict["status"] == 409                 # settled means settled


def test_a_date_cannot_be_moved_into_a_closed_period(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir)
    verdict = journal_hook("update",
                           existing={"id": "j-new", "date": "2026-05-01",
                                     "owner_id": "dan"},
                           changes={"date": "2026-02-01"})
    assert verdict["status"] == 409                 # backdating is the classic move


def test_other_owners_and_other_entities_are_untouched(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir, owner="dan")
    other = journal_hook("create", record={"id": "j1", "date": "2026-02-14",
                                           "owner_id": "pat"})
    assert other is None                            # dan's filing binds only dan

    stage_collection(data_dir, "app-entities", "entities")
    object_records.create_collection_record(
        "entities", {"id": "ent-2", "name": "Second Books", "owner_id": "dan"},
        base_dir=data_dir)
    scoped = journal_hook("create", record={"id": "j2", "date": "2026-02-14",
                                            "owner_id": "dan", "entity_id": "ent-2"})
    assert scoped is None                           # entity-blank period != ent-2 books


def test_reopening_is_deleting_the_row_and_then_writes_flow_again(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir, pid="cp-q1")
    blocked = journal_hook("create", record={"id": "j1", "date": "2026-02-14",
                                             "owner_id": "dan"})
    assert blocked["status"] == 409
    object_records.delete_collection_record("fin_closed_periods", "cp-q1",
                                            base_dir=data_dir, actor="dan")
    reopened = journal_hook("create", record={"id": "j1", "date": "2026-02-14",
                                              "owner_id": "dan"})
    assert reopened is None                         # deliberate, attributed, effective


# --- the line-side gate (the amounts live in the lines) ------------------------

def test_lines_on_a_closed_period_journal_are_frozen(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_journal(data_dir, "j-feb", date="2026-02-01", status="posted")
    close_period(data_dir)
    add = line_hook("create", record={"id": "l1", "journal_id": "j-feb",
                                      "account_id": "acct-cash",
                                      "debit_cents": "100", "credit_cents": "0"})
    assert add["status"] == 409
    assert "new journal in the current period" in add["error"]
    edit = line_hook("update",
                     existing={"id": "l0", "journal_id": "j-feb"},
                     changes={"debit_cents": "999"})
    assert edit["status"] == 409


def test_lines_on_open_period_journals_flow_normally(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    close_period(data_dir)
    make_journal(data_dir, "j-may", date="2026-05-01")
    ok = line_hook("create", record={"id": "l1", "journal_id": "j-may",
                                     "account_id": "acct-cash",
                                     "debit_cents": "100", "credit_cents": "0"})
    assert ok is None


# --- the two gates coexist ------------------------------------------------------

def test_balance_gate_still_holds_after_the_period_gate_joined_it(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_journal(data_dir, "j-open", date="2026-06-01")
    object_records.create_collection_record(
        "fin_journal_lines",
        {"id": "l1", "journal_id": "j-open", "account_id": "acct-cash",
         "debit_cents": "500", "credit_cents": "0", "owner_id": "dan"},
        base_dir=data_dir)
    verdict = journal_hook("update",
                           existing={"id": "j-open", "date": "2026-06-01",
                                     "status": "draft", "owner_id": "dan"},
                           changes={"status": "posted"})
    assert verdict["status"] == 409
    assert "must balance" in verdict["error"]
