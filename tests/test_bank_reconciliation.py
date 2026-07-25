"""Bank reconciliation (plan/bank-import-reconciliation-spec.md, slice 2):
the matcher, evidence immutability, and the resolution verbs.

The control being tested is not "does it match things" but "can anyone make
the evidence agree with the books by hand" -- the answer must be no, and
every act of reconciliation must carry a name.
"""

import json
import pathlib

import object_execution
import object_records
import python_object_runtime
from conftest import stage_collection

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BANKING_OBJECTS = PACKAGES / "app-banking" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

ACCOUNT, ACCOUNT_2 = "bank-1", "bank-2"
CASH, CASH_2 = "acct-cash", "acct-savings"
FEES, INTEREST, AR, REV = "acct-fees", "acct-interest", "acct-ar", "acct-rev"
DAN = {"user_id": "dan"}


def setup_env(tmp_path, monkeypatch, *, settings=()):
    data_dir = tmp_path / "data"
    wanted = [("app-banking", "value_accounts"), ("app-banking", "bank_import_profiles"),
              ("app-banking", "bank_statement_imports"), ("app-banking", "bank_lines"),
              ("app-finance", "fin_accounts"), ("app-finance", "fin_journals"),
              ("app-finance", "fin_journal_lines"),
              ("app-payments", "payments"), ("app-payments", "refunds"),
              ("app-invoices", "invoices")]
    for pkg, name in wanted:
        stage_collection(data_dir, pkg, name)

    rows = ""
    for i, (k, v) in enumerate(settings):
        rows += f"s{i}\t{k}\t{v}\t\n"
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))

    for acct, kind in ((CASH, "asset"), (CASH_2, "asset"), (FEES, "expense"),
                       (INTEREST, "income"), (AR, "asset"), (REV, "income")):
        object_records.create_collection_record(
            "fin_accounts", {"id": acct, "name": acct, "account_type": kind,
                             "owner_id": "dan"}, base_dir=data_dir)
    for bank, cash, name in ((ACCOUNT, CASH, "Checking"), (ACCOUNT_2, CASH_2, "Savings")):
        object_records.create_collection_record(
            "value_accounts", {"id": bank, "name": name, "fin_account_id": cash,
                              "owner_id": "dan"}, base_dir=data_dir)
    return data_dir


def make_line(data_dir, lid, *, amount, on="2026-07-02", desc="", account=ACCOUNT, **extra):
    rec = {"id": lid, "bank_account_id": account, "posted_on": on,
           "amount_cents": str(amount), "description": desc, "raw": f"raw:{desc}",
           "line_hash": f"h-{lid}", "match_status": "unmatched", "owner_id": "dan"}
    rec.update(extra)
    return object_records.create_collection_record("bank_lines", rec, base_dir=data_dir)


def make_payment(data_dir, pid, *, cents, on="2026-07-02", reference="", invoice="inv1"):
    return object_records.create_collection_record(
        "payments",
        {"id": pid, "invoice_id": invoice, "amount_cents": str(cents), "method": "card",
         "received_on": on, "reference": reference, "status": "received", "owner_id": "dan"},
        base_dir=data_dir)


def make_invoice(data_dir, iid="inv1"):
    return object_records.create_collection_record(
        "invoices", {"id": iid, "number": "N-1", "customer_name": "Acme",
                     "status": "sent", "total_cents": "500000", "owner_id": "dan"},
        base_dir=data_dir)


def run_object(object_id, payload, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method=method, payload={"_identity": DAN, **payload}),
        roots=[BANKING_OBJECTS]).result


def hook(action, record=None, existing=None, changes=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_bank_lines", method="BEFORE_WRITE",
            payload={"action": action, "collection": "bank_lines",
                     "record": record or {}, "existing": existing or {},
                     "changes": changes or {}}),
        roots=[BANKING_OBJECTS]).result


def line(data_dir, lid):
    return object_records.get_collection_record("bank_lines", lid, base_dir=data_dir)


def journals(data_dir):
    return object_records.read_collection_records("fin_journals", base_dir=data_dir)


def lines_for(data_dir, journal_id):
    return {l["account_id"]: l for l in
            object_records.read_collection_records("fin_journal_lines", base_dir=data_dir)
            if l["journal_id"] == journal_id}


# --- the matcher --------------------------------------------------------------

def test_reference_plus_amount_auto_matches_at_tier_1(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, reference="INV-1001")
    make_line(data_dir, "L1", amount=150000, desc="ACME CORP PAYMENT INV-1001")

    result = run_object("system_bank_matcher", {})
    assert result["auto_matched"] == 1
    row = line(data_dir, "L1")
    assert row["match_status"] == "matched"
    assert row["matched_to"] == "payments/p1"
    assert json.loads(row["suggestions"])[0]["tier"] == 1


def test_amount_only_is_suggested_never_auto_matched(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, on="2026-07-01", reference="INV-9999")
    make_line(data_dir, "L1", amount=150000, on="2026-07-02", desc="DEPOSIT")

    result = run_object("system_bank_matcher", {})
    assert result["auto_matched"] == 0 and result["suggested"] == 1
    row = line(data_dir, "L1")
    assert row["match_status"] == "suggested"
    assert row["matched_to"] == ""          # a suggestion is not a decision
    proposal = json.loads(row["suggestions"])[0]
    assert proposal["tier"] == 2 and proposal["refs"] == ["payments/p1"]


def test_batched_deposit_is_proposed_as_a_combination(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=60000)
    make_payment(data_dir, "p2", cents=90000)
    make_line(data_dir, "L1", amount=150000, desc="BATCH DEPOSIT")

    run_object("system_bank_matcher", {})
    proposals = json.loads(line(data_dir, "L1")["suggestions"])
    combo = [p for p in proposals if p["tier"] == 3]
    assert combo and sorted(combo[0]["refs"]) == ["payments/p1", "payments/p2"]
    assert line(data_dir, "L1")["match_status"] == "suggested"


def test_out_of_window_and_claimed_records_are_not_proposed(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.date_window_days", "2"),))
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, on="2026-06-01")   # far outside the window
    make_line(data_dir, "L1", amount=150000, on="2026-07-02", desc="DEPOSIT")
    assert run_object("system_bank_matcher", {})["suggested"] == 0

    make_payment(data_dir, "p2", cents=150000, on="2026-07-02", reference="INV-1001")
    make_line(data_dir, "L2", amount=150000, on="2026-07-02", desc="PAYMENT INV-1001")
    make_line(data_dir, "L3", amount=150000, on="2026-07-02", desc="PAYMENT INV-1001")
    run_object("system_bank_matcher", {})
    # One book record cannot satisfy two bank lines.
    matched = [l for l in object_records.read_collection_records("bank_lines", base_dir=data_dir)
               if l.get("matched_to") == "payments/p2"]
    assert len(matched) == 1


def test_auto_match_can_be_switched_off_entirely(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.auto_match_tier", "0"),))
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, reference="INV-1001")
    make_line(data_dir, "L1", amount=150000, desc="PAYMENT INV-1001")
    result = run_object("system_bank_matcher", {})
    assert result["auto_matched"] == 0 and result["suggested"] == 1
    assert line(data_dir, "L1")["match_status"] == "suggested"


def test_matcher_is_a_no_op_without_banking_data(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert run_object("system_bank_matcher", {})["scanned"] == 0


# --- evidence immutability ----------------------------------------------------

def test_the_bank_words_cannot_be_edited(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000, desc="ACME CORP PAYMENT")
    existing = line(data_dir, "L1")

    for field, value in (("amount_cents", "140000"), ("posted_on", "2026-07-03"),
                         ("description", "SOMETHING ELSE"), ("raw", "tampered"),
                         ("bank_account_id", ACCOUNT_2)):
        outcome = hook("update", existing=existing, changes={field: value})
        assert outcome["status"] == 409, field
        assert "cannot be edited" in outcome["error"]

    # Reconciliation work on the same record is allowed.
    assert hook("update", existing=existing,
                changes={"match_status": "matched", "matched_to": "payments/p1"}) is None
    assert hook("update", existing=existing,
                changes={"resolved_as": "fee", "match_status": "resolved"}) is None


def test_matched_requires_a_well_formed_target(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=150000)
    existing = line(data_dir, "L1")
    bare = hook("update", existing=existing, changes={"match_status": "matched"})
    assert bare["status"] == 400 and "matched_to" in bare["error"]
    malformed = hook("update", existing=existing,
                     changes={"match_status": "matched", "matched_to": "p1"})
    assert malformed["status"] == 400


def test_confirming_a_match_is_an_ordinary_attributed_update(tmp_path, monkeypatch):
    """Deliberately NOT an action object: a normal write already carries the
    actor into the change log, which is what makes reconciliation auditable."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000)
    make_line(data_dir, "L1", amount=150000, desc="DEPOSIT")
    run_object("system_bank_matcher", {})

    object_records.update_collection_record(
        "bank_lines", "L1", {"match_status": "matched", "matched_to": "payments/p1"},
        base_dir=data_dir, actor="dan")
    assert line(data_dir, "L1")["match_status"] == "matched"
    import object_record_changes
    entries = object_record_changes.list_record_changes(
        "bank_lines", record_id="L1", base_dir=data_dir)["changes"]
    confirmations = [e for e in entries
                     if "match_status" in (e.get("changed_fields") or [])]
    assert confirmations and confirmations[0]["actor"] == "dan"


# --- resolution verbs ---------------------------------------------------------

def test_fee_composes_a_posted_journal_and_is_idempotent(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.journal.fees_account", FEES),))
    make_line(data_dir, "L1", amount=-2500, desc="MONTHLY SERVICE FEE")

    result = run_object("action_resolve_bank_line", {"line_id": "L1", "kind": "fee"})
    assert result["status"] == 200 and result["journal"]["posted"] is True
    row = line(data_dir, "L1")
    assert row["match_status"] == "resolved" and row["resolved_as"] == "fee"
    booked = lines_for(data_dir, result["journal"]["journal_id"])
    assert booked[FEES]["debit_cents"] == "2500"
    assert booked[CASH]["credit_cents"] == "2500"

    again = run_object("action_resolve_bank_line", {"line_id": "L1", "kind": "fee"})
    assert again["status"] == 409           # already resolved
    assert len(journals(data_dir)) == 1


def test_interest_books_the_other_direction(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.journal.interest_account", INTEREST),))
    make_line(data_dir, "L1", amount=812, desc="INTEREST PAID")
    result = run_object("action_resolve_bank_line", {"line_id": "L1", "kind": "interest"})
    booked = lines_for(data_dir, result["journal"]["journal_id"])
    assert booked[CASH]["debit_cents"] == "812"
    assert booked[INTEREST]["credit_cents"] == "812"


def test_transfer_moves_between_two_own_accounts(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=-100000, desc="TRANSFER TO SAVINGS")
    result = run_object("action_resolve_bank_line",
                        {"line_id": "L1", "kind": "transfer",
                         "counterpart_bank_account_id": ACCOUNT_2})
    assert result["status"] == 200
    booked = lines_for(data_dir, result["journal"]["journal_id"])
    assert booked[CASH_2]["debit_cents"] == "100000"     # money into savings
    assert booked[CASH]["credit_cents"] == "100000"      # out of checking


def test_nsf_bounces_the_payment_and_reverses_its_journal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch, settings=(
        ("payments.accounting_basis", "cash"),
        ("payments.journal.cash_account", CASH),
        ("payments.journal.revenue_account", REV)))
    make_invoice(data_dir)
    make_payment(data_dir, "p1", cents=150000, reference="INV-1001")
    make_line(data_dir, "L1", amount=-150000, desc="RETURNED ITEM NSF")

    result = run_object("action_resolve_bank_line",
                        {"line_id": "L1", "kind": "nsf", "payment_id": "p1"})
    assert result["status"] == 200
    payment = object_records.get_collection_record("payments", "p1", base_dir=data_dir)
    assert payment["status"] == "bounced"
    reversal = [j for j in journals(data_dir)
                if j["generated_from"] == "payments/p1:bounced"]
    assert reversal and reversal[0]["kind"] == "reversing"
    booked = lines_for(data_dir, reversal[0]["id"])
    assert booked[REV]["debit_cents"] == "150000"    # revenue reversed
    assert booked[CASH]["credit_cents"] == "150000"  # cash taken back out
    assert line(data_dir, "L1")["matched_to"] == "payments/p1"


def test_timing_items_carry_forward_without_a_journal(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=50000, desc="DEPOSIT IN TRANSIT")
    result = run_object("action_resolve_bank_line", {"line_id": "L1", "kind": "timing"})
    assert result["status"] == 200 and result["journal"] is None
    assert line(data_dir, "L1")["resolved_as"] == "timing"
    assert journals(data_dir) == []


def test_resolution_refusals(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    make_line(data_dir, "L1", amount=-2500, desc="FEE")
    # Unconfigured expense account: refuse rather than post to nowhere.
    assert run_object("action_resolve_bank_line",
                      {"line_id": "L1", "kind": "fee"})["status"] == 409
    assert run_object("action_resolve_bank_line",
                      {"line_id": "L1", "kind": "nonsense"})["status"] == 400
    assert run_object("action_resolve_bank_line",
                      {"line_id": "missing", "kind": "fee"})["status"] == 404
    # A transfer with no counterpart named.
    assert run_object("action_resolve_bank_line",
                      {"line_id": "L1", "kind": "transfer"})["status"] == 400
    anon = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "action_resolve_bank_line", method="POST",
            payload={"line_id": "L1", "kind": "timing"}),
        roots=[BANKING_OBJECTS]).result
    assert anon["status"] == 403


def test_a_bank_account_without_a_book_account_cannot_post(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("reconcile.journal.fees_account", FEES),))
    object_records.create_collection_record(
        "value_accounts", {"id": "bank-3", "name": "Unlinked", "owner_id": "dan"},
        base_dir=data_dir)
    make_line(data_dir, "L9", amount=-2500, desc="FEE", account="bank-3")
    outcome = run_object("action_resolve_bank_line", {"line_id": "L9", "kind": "fee"})
    assert outcome["status"] == 409 and "fin_account_id" in outcome["error"]
