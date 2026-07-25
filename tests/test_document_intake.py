"""Document intake: a machine reads, a person decides.

The property this whole package exists to hold is that nothing an
extractor produces ever reaches the books on its own. Everything else --
dedup, engine choice, confidence -- is in service of that, and most of
these tests are checking that a BAD reading is merely inconvenient rather
than wrong.
"""

import base64
import json
import pathlib

from conftest import stage_collection

import object_execution
import object_intake
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
INTAKE_OBJECTS = PACKAGES / "app-intake" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

RECEIPT = """BLUE BOTTLE COFFEE
1234 Market St
06/15/2026

Latte              4.50
Croissant          3.75
Subtotal           8.25
Tax                0.74
TOTAL              8.99

Suggested tip 20%  1.80
"""


def setup_env(tmp_path, monkeypatch, *, settings=()):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-intake", "scans"), ("app-finance", "expenses"),
                      ("app-projects", "projects")):
        stage_collection(data_dir, pkg, name)
    rows = "".join(f"s{i}\t{k}\t{v}\t\n" for i, (k, v) in enumerate(settings))
    stage_collection(data_dir, "app-settings", "app_settings", rows=rows)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def run(object_id, payload=None, *, roots=None):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            object_id, method="POST", payload=payload or {}),
        roots=roots or [INTAKE_OBJECTS]).result


def ingest(text=RECEIPT, **fields):
    payload = {"text": text, "owner_id": "dana", "filename": "coffee.txt"}
    payload.update(fields)
    return run("action_scan_ingest", payload)


def ingest_bytes(content=b"\x89PNG fake image", **fields):
    payload = {"content_base64": base64.b64encode(content).decode(),
               "owner_id": "dana", "filename": "receipt.png"}
    payload.update(fields)
    return run("action_scan_ingest", payload)


def scan(data_dir, scan_id):
    return object_records.get_collection_record("scans", scan_id, base_dir=data_dir)


def expenses(data_dir):
    return object_records.read_collection_records("expenses", base_dir=data_dir)


def hook(record, *, existing=None, changes=None, action="create"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(
            "hook_scans", method="BEFORE_WRITE",
            payload={"action": action, "collection": "scans", "record": record,
                     "existing": existing,
                     "changes": changes if changes is not None else dict(record),
                     "subject": {"user_id": "dana", "roles": []}}),
        roots=[INTAKE_OBJECTS]).result


# --- reading, with no model and no key -------------------------------------

def test_the_total_is_chosen_by_its_label_not_its_size():
    """Taking the biggest number on the page is how an intake system
    bills a client for the suggested-tip line."""
    assert object_intake.find_total_cents(RECEIPT) == 899


def test_a_date_and_a_vendor_come_off_the_page():
    assert object_intake.find_date(RECEIPT) == "2026-06-15"
    assert object_intake.find_vendor(RECEIPT) == "BLUE BOTTLE COFFEE"


def test_confidence_reflects_what_was_found_not_what_was_claimed():
    full = object_intake.guess_from_text(RECEIPT)
    empty = object_intake.guess_from_text("")
    assert full["confidence"] > empty["confidence"] == 0.0
    assert full["total_cents"] == 899


def test_an_unreadable_page_yields_an_empty_suggestion_not_a_crash():
    """Nothing found has to be an answer, because it is the answer a free
    extractor gives most often."""
    guess = object_intake.guess_from_text("~~~ 8||| ~~~")
    assert guess["total_cents"] == 0 and guess["confidence"] == 0.0


def test_a_models_loose_json_is_clamped_into_the_same_shape():
    normalized = object_intake.normalize_extraction(
        '{"kind": "RECEIPT", "vendor": "Blue Bottle", "total": "$8.99",'
        ' "confidence": 87, "line_items": [{"description": "Latte",'
        ' "amount": "4.50"}]}', engine="ai_vision")
    assert normalized["kind"] == "receipt"
    assert normalized["total_cents"] == 899
    assert normalized["confidence"] == 0.87          # percent understood as a ratio
    assert normalized["line_items"][0]["amount_cents"] == 450


def test_an_extractor_having_a_bad_day_returns_nothing_usable(tmp_path):
    """It must leave a scan waiting for a human, never break the pass
    reading a hundred other documents."""
    for junk in ("not json at all", None, 42, "[1,2,3]"):
        normalized = object_intake.normalize_extraction(junk)
        assert normalized["total_cents"] == 0 and normalized["confidence"] == 0.0


# --- ingest -------------------------------------------------------------------

def test_ingest_stores_and_returns_without_reading_anything(tmp_path, monkeypatch):
    """Capture happens at a till with a phone; anything slower is a
    receipt that ends up in a coat pocket."""
    data_dir = setup_env(tmp_path, monkeypatch)
    result = ingest()
    assert result["ok"] and result["status_of_scan"] == "pending"

    row = scan(data_dir, result["scan_id"])
    assert row["status"] == "pending"
    assert row["extracted"] == ""          # nothing read yet, deliberately
    assert row["content_sha256"]


def test_the_same_receipt_photographed_twice_is_one_scan(tmp_path, monkeypatch):
    """The second photo of a crumpled receipt is the normal case."""
    data_dir = setup_env(tmp_path, monkeypatch)
    first = ingest()
    again = ingest()
    assert again["duplicate"] is True
    assert again["scan_id"] == first["scan_id"]
    assert len(object_records.read_collection_records("scans", base_dir=data_dir)) == 1


def test_the_hook_refuses_a_duplicate_written_any_other_way(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    result = ingest()
    digest = scan(data_dir, result["scan_id"])["content_sha256"]
    verdict = hook({"content_sha256": digest, "owner_id": "dana"})
    assert verdict["status"] == 409 and "two expenses" in verdict["error"]


def test_two_businesses_may_receive_the_same_document(tmp_path, monkeypatch):
    """The same invoice PDF reaching two companies is two genuine facts."""
    setup_env(tmp_path, monkeypatch)
    ingest()
    other = ingest(owner_id="sam")
    assert other["ok"] and not other.get("duplicate")


def test_ingest_needs_an_owner_and_some_content(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert run("action_scan_ingest", {"text": "x"})["status"] == 401
    assert run("action_scan_ingest", {"owner_id": "dana"})["status"] == 400
    assert run("action_scan_ingest",
               {"owner_id": "dana", "content_base64": "!!not base64!!"})["status"] == 400


# --- the reading pass -----------------------------------------------------------

def test_the_free_engine_reads_text_that_arrived_as_text(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    result = run("system_scan_processor")
    assert result["extracted"] == 1 and result["engine"] == "text_rules"

    row = scan(data_dir, scan_id)
    assert row["status"] == "extracted"
    assert json.loads(row["extracted"])["total_cents"] == 899
    assert row["confirmed_record"] == ""       # read, not posted


def test_an_image_with_no_ocr_engine_still_becomes_confirmable(tmp_path, monkeypatch):
    """The honest free path: no text found, image attached, type the
    total. Never a wrong number."""
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest_bytes()["scan_id"]
    run("system_scan_processor")

    row = scan(data_dir, scan_id)
    assert row["status"] == "extracted"
    assert json.loads(row["extracted"])["total_cents"] == 0


def test_a_missing_host_engine_leaves_the_document_pending(tmp_path, monkeypatch):
    """A host that is not set up is an operator's problem to fix, not a
    document to give up on -- so it stays pending and the backlog drains
    the moment the engine appears."""
    data_dir = setup_env(tmp_path, monkeypatch,
                         settings=(("intake.ocr_engine", "ai_vision"),))
    scan_id = ingest_bytes()["scan_id"]
    result = run("system_scan_processor")

    row = scan(data_dir, scan_id)
    assert result["failed"] == 1
    assert row["status"] == "pending"          # retried when the host is fixed
    assert "ai_vision" in row["error"]


def test_a_document_that_keeps_failing_stops_being_retried(tmp_path, monkeypatch):
    """Churning a paid extractor forever against a corrupt file is how an
    intake queue silently spends money."""
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest_bytes()["scan_id"]
    object_records.update_collection_record(
        "scans", scan_id, {"status": "error", "attempts": "3"},
        base_dir=data_dir, actor="test")
    result = run("system_scan_processor")
    assert result["considered"] == 0
    assert scan_id in result["needing_a_human"]


def test_reading_twice_does_not_re_read(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    ingest()
    run("system_scan_processor")
    assert run("system_scan_processor")["extracted"] == 0


def test_intake_absent_is_a_graceful_skip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert run("system_scan_processor")["skipped"].startswith("intake not installed")


# --- confirmation ----------------------------------------------------------------

def test_confirming_creates_a_draft_expense_with_its_provenance(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    run("system_scan_processor")

    result = run("action_confirm_scan", {"scan_id": scan_id, "project_id": ""})
    assert result["ok"] and result["status_of_expense"] == "draft"

    row = expenses(data_dir)[0]
    assert row["description"] == "BLUE BOTTLE COFFEE"
    assert row["incurred_on"] == "2026-06-15"
    assert row["amount_cents"] == "899"
    assert row["receipt_ref"] == f"scans/{scan_id}"
    assert row["status"] == "draft"            # still faces the approval gate
    assert scan(data_dir, scan_id)["confirmed_record"] == f"expenses/{row['id']}"


def test_one_scan_one_record_however_many_clicks(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    run("system_scan_processor")
    run("action_confirm_scan", {"scan_id": scan_id})
    again = run("action_confirm_scan", {"scan_id": scan_id})
    assert again["duplicate"] is True
    assert len(expenses(data_dir)) == 1


def test_what_a_person_types_beats_what_the_machine_read(tmp_path, monkeypatch):
    """The point of confirmation is that somebody is looking at the image
    while they do it."""
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    run("system_scan_processor")
    run("action_confirm_scan", {"scan_id": scan_id, "amount_cents": "1079",
                                "description": "Coffee with the Acme team"})

    row = expenses(data_dir)[0]
    assert row["amount_cents"] == "1079"
    assert row["description"] == "Coffee with the Acme team"


def test_an_unread_document_asks_for_the_amount_rather_than_posting_zero(
        tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest_bytes()["scan_id"]
    run("system_scan_processor")

    result = run("action_confirm_scan", {"scan_id": scan_id})
    assert result["status"] == 400 and "type it in from the image" in result["error"]
    assert expenses(data_dir) == []


def test_preview_shows_the_draft_without_writing_it(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    run("system_scan_processor")

    result = run("action_confirm_scan", {"scan_id": scan_id, "preview": "true"})
    assert result["would_create"]["amount_cents"] == "899"
    assert expenses(data_dir) == []
    assert scan(data_dir, scan_id)["status"] == "extracted"


def test_confirmed_evidence_is_closed(tmp_path, monkeypatch):
    """Correct the expense, not the record of what arrived."""
    data_dir = setup_env(tmp_path, monkeypatch)
    scan_id = ingest()["scan_id"]
    run("system_scan_processor")
    run("action_confirm_scan", {"scan_id": scan_id})

    existing = scan(data_dir, scan_id)
    verdict = hook({"ocr_text": "something else"}, existing=existing,
                   changes={"ocr_text": "something else"}, action="update")
    assert verdict["status"] == 409 and "not the evidence" in verdict["error"]


def test_confirming_a_scan_that_does_not_exist_says_so(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    assert run("action_confirm_scan", {"scan_id": "nope"})["status"] == 404
    assert run("action_confirm_scan", {})["status"] == 400
