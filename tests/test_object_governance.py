"""Governance introspection: the placement table live, and the workflow
diagram compiled from declarations.

The claim under test is the one that separates this from every
hand-maintained workflow picture: because each kind of rule has exactly
one declared home, the diagram is COMPILED from the same declarations the
server enforces -- install a handler or add a transition and the next
render shows it, with no drawing to forget to update.
"""

import pathlib

from conftest import stage_collection

import object_governance

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
ROOTS = [PACKAGES / "app-payments" / "objects", PACKAGES / "app-invoices" / "objects"]


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for pkg, name in (("app-invoices", "invoices"), ("app-payments", "payments")):
        stage_collection(data_dir, pkg, name)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


def test_governs_answers_the_seven_layers_in_one_call(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    info = object_governance.governs("payments", base_dir=data_dir, roots=ROOTS)

    # gate: the schema names its hook, and the report says it fails closed
    assert info["gates"][0]["object_id"] == "hook_payments"
    assert "fails closed" in info["gates"][0]["note"]
    # reactions: parsed from HANDLES source, never by importing the objects
    reacting = {r["object_id"] for r in info["reactions"]}
    assert {"system_books", "system_invoice_status"} <= reacting
    # declarations: enums/relations/transitions from the schema itself
    assert "invoice_id" in info["declarations"]["relations"]
    assert info["declarations"]["relations"]["invoice_id"] == "invoices"


def test_governs_reads_transitions_with_their_guards(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    info = object_governance.governs("invoices", base_dir=data_dir, roots=ROOTS)
    moves = info["declarations"]["transitions"]["status"]
    targets = {t["to"] for t in moves["draft"]}
    assert targets == {"sent", "void"}
    # guards travel with the edge -- who may make the move is part of the map
    assert moves["draft"][0]["when"] == {"owner_id": "$user_id"}


def test_handles_parser_accepts_the_real_source_shape(tmp_path, monkeypatch):
    """books.py's HANDLES has a trailing comma -- legal Python, illegal
    JSON, and exactly the kind of gap that silently empties an index."""
    setup_env(tmp_path, monkeypatch)
    handlers = object_governance._handler_index(ROOTS)
    by_id = {h["object_id"]: h for h in handlers}
    assert "system_books" in by_id
    events = {(e["collection"], e["action"]) for e in by_id["system_books"]["events"]}
    assert ("invoices", "created") in events
    assert ("payments", "updated") in events


def test_mermaid_compiles_states_guards_gates_and_reactions(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    source = object_governance.workflow_mermaid("payments", base_dir=data_dir, roots=ROOTS)
    assert source.startswith("flowchart LR")
    assert 'gate: hook_payments' in source
    assert '-->|"on created"| system_books' in source
    # a reaction node is declared once even when it fires on several actions
    assert source.count('system_books[/"system_books"/]') == 1

    invoices = object_governance.workflow_mermaid("invoices", base_dir=data_dir, roots=ROOTS)
    assert 'invoices_draft -->|"owner_id=$user_id"| invoices_sent' in invoices
    assert "system_invoice_portal_link" in invoices


def test_missing_layers_report_as_empty_not_as_errors(tmp_path, monkeypatch):
    """A collection with no hooks simply has no gate rows -- that IS the
    honest report, and a governance tool that raised on a plain collection
    would never get used."""
    data_dir = tmp_path / "data"
    stage_collection(data_dir, "app-links", "links")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    info = object_governance.governs("links", base_dir=data_dir, roots=ROOTS)
    assert info["gates"] == []
    assert info["reactions"] == []
    assert info["declarations"]["transitions"] == {}
    source = object_governance.workflow_mermaid("links", base_dir=data_dir, roots=ROOTS)
    assert "links" in source          # renders a plain node, not a traceback


def test_unknown_collection_is_a_plain_empty_report(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    info = object_governance.governs("nothing_here", base_dir=data_dir, roots=ROOTS)
    assert info["collection"] == "nothing_here"
    assert info["gates"] == [] and info["views"] == []
