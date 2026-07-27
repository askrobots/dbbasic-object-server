"""Agents: liveness, capabilities, and the spend an agent has committed.

This package exists because of an audit, not an idea. Most of an "agent
runtime" was already on the box — an agent is a user, what it did is the
change log, the coordination feed is `feed_posts` with `claim` and
`release` already in its kinds, and race-free claiming is `expected_rev`.
So the collection had to pass a test before it was allowed to exist:
**every field must be something not derivable from what is already kept.**

Three things passed, and this file is mostly about the first:

LIVENESS IS NOT A LAST WRITE. An agent reasoning for ten minutes writes
nothing and is alive; a crashed one looks alive until its last write ages
out. No fold over the change log answers "is it still there", which is
the whole justification for a heartbeat column.

CAPABILITIES ARE OPT-IN. An agent advertising nothing is routed nothing,
because nobody's machine joins a compute pool by accident.

SPEND IS A FOLD, NEVER A COUNTER. The cap reads holds and debits out of
the ledger — the same argument hook_wallet_entries makes about
wallets.balance_minor.
"""

import json
import pathlib

import pytest
from conftest import stage_collection

import object_agents
import object_execution
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
AGENT_OBJECTS = PACKAGES / "app-agents" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

NOW = "2026-07-27T12:00:00Z"


def agent(**fields):
    row = {"agent_id": "server-agent", "label": "Server Agent",
           "status": "active", "heartbeat_at": "2026-07-27T11:58:00Z"}
    row.update({k: str(v) for k, v in fields.items()})
    return row


# --- the pure module -----------------------------------------------------------

def test_liveness_has_three_states_because_up_or_down_is_not_the_question():
    """The useful distinction is 'should I be worried yet'. A missed beat
    is ordinary, several is a question, a long silence is an answer."""
    assert object_agents.liveness(agent(), now=NOW) == "live"
    assert object_agents.liveness(
        agent(heartbeat_at="2026-07-27T11:30:00Z"), now=NOW) == "stale"
    assert object_agents.liveness(
        agent(heartbeat_at="2026-07-27T09:00:00Z"), now=NOW) == "lost"


def test_registered_but_never_spoken_is_its_own_answer():
    """'Registered and silent' is usually a misconfiguration; 'was here
    and stopped' is an incident. Folding them together loses the
    distinction an operator acts on."""
    assert object_agents.liveness(agent(heartbeat_at=""), now=NOW) == "never"


def test_a_paused_agent_reports_its_status_rather_than_looking_dead():
    """Rendering an operator's own decision as an alarm is how a board
    teaches people to ignore it."""
    for status in ("paused", "suspended", "retired"):
        stale = agent(status=status, heartbeat_at="2026-06-01T00:00:00Z")
        assert object_agents.liveness(stale, now=NOW) == status


def test_capabilities_are_opt_in_and_empty_advertises_nothing():
    """Nobody's machine joins a compute pool by accident. An agent that
    advertised nothing is not volunteering for general duty either."""
    assert object_agents.capabilities(agent(capabilities="")) == []
    assert object_agents.can_serve(agent(capabilities=""), "") is False
    assert object_agents.can_serve(agent(capabilities=""), "gpu") is False

    worker = agent(capabilities="GPU, triposr , gpu")
    assert object_agents.capabilities(worker) == ["gpu", "triposr"]
    assert object_agents.can_serve(worker, "gpu") is True
    assert object_agents.can_serve(worker, "gpu,triposr") is True
    assert object_agents.can_serve(worker, "whisper") is False
    # A blank requirement is ordinary work: anyone who opted in may take it.
    assert object_agents.can_serve(worker, "") is True


def test_committed_spend_is_a_fold_over_holds_and_debits():
    """Holds count, because a cap that ignored them would let an agent
    queue a hundred runs and only notice at settlement -- exactly the
    failure the hold model exists to prevent."""
    entries = [
        {"owner_id": "server-agent", "kind": "hold", "amount_minor": "-500"},
        {"owner_id": "server-agent", "kind": "debit", "amount_minor": "-250"},
        {"owner_id": "server-agent", "kind": "release", "amount_minor": "500"},
        {"owner_id": "server-agent", "kind": "topup", "amount_minor": "10000"},
        {"owner_id": "someone-else", "kind": "debit", "amount_minor": "-9999"},
    ]
    assert object_agents.committed_minor("server-agent", entries) == 750


def test_a_zero_cap_means_no_cap_not_no_spending():
    """The opposite reading would silently suspend every agent the moment
    the field shipped -- the worst possible default for a column added to
    existing rows. Matches billing.wallet.overdraft_minor's convention."""
    assert object_agents.over_cap(agent(spend_cap_minor="0"), []) is None

    entries = [{"owner_id": "server-agent", "kind": "hold",
                "amount_minor": "-900"}]
    capped = object_agents.over_cap(agent(spend_cap_minor="500"), entries)
    assert capped["over"] is True
    assert capped["committed_minor"] == 900
    assert capped["remaining_minor"] == 0


def test_the_board_puts_the_silent_ones_first():
    fold = object_agents.board(
        [agent(agent_id="a", label="Alive"),
         agent(agent_id="b", label="Gone", heartbeat_at="2026-07-01T00:00:00Z")],
        [], now=NOW)
    assert [row["label"] for row in fold["agents"]] == ["Gone", "Alive"]
    assert fold["live"] == 1 and fold["total"] == 2


# --- through the objects --------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    base = tmp_path / "data"
    stage_collection(base, "app-agents", "agent_registry")
    stage_collection(base, "app-collab", "feed_posts")
    stage_collection(base, "app-billing", "wallet_entries")
    stage_collection(base, "app-settings", "app_settings")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(base))
    return base


def run(object_id, payload=None, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload or {}),
        roots=[AGENT_OBJECTS]).result


def beat(**payload):
    payload.setdefault("_identity", {"user_id": "server-agent"})
    return run("action_agent_heartbeat", payload)


def registry(data_dir):
    return object_records.read_collection_records("agent_registry",
                                                   base_dir=data_dir)


def test_the_first_heartbeat_registers_and_later_ones_only_beat(data_dir):
    """One verb, not two: a separate register call is a step somebody
    forgets and then debugs, and there is nothing a registration knows
    that the first heartbeat does not."""
    first = beat(label="Server Agent", capabilities="gpu, triposr")
    assert first["registered"] is True
    assert first["capabilities"] == ["gpu", "triposr"]

    again = beat()
    assert again["registered"] is False
    assert len(registry(data_dir)) == 1
    assert again["heartbeat_at"] >= first["heartbeat_at"]


def test_a_bare_keepalive_does_not_wipe_capabilities(data_dir):
    """Absent leaves alone; present-but-empty withdraws. Without that
    distinction a heartbeat on a timer would silently de-register the
    agent from every capability it advertised."""
    beat(label="Worker", capabilities="gpu,whisper")
    assert beat()["capabilities"] == ["gpu", "whisper"]

    withdrawn = beat(capabilities="")
    assert withdrawn["capabilities"] == []


def test_an_agent_cannot_beat_on_behalf_of_another(data_dir):
    """A heartbeat is the evidence something is alive, and evidence one
    party can manufacture about another is not evidence."""
    beat(agent_id="somebody-else", label="Impostor")
    rows = registry(data_dir)
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "server-agent"      # from identity, not body


def test_an_anonymous_heartbeat_is_refused(data_dir):
    refused = run("action_agent_heartbeat", {"label": "nobody"})
    assert refused["status"] == 401
    assert not registry(data_dir)


def test_the_board_states_liveness_with_the_heartbeat_it_came_from(data_dir):
    """'lost' without 'last seen four hours ago' is a claim. Same reason
    site_ledger_integrity states its method beside its verdict."""
    beat(label="Server Agent", capabilities="gpu")
    body = " ".join(run("site_agents", {}, method="GET")["body"].split())
    assert "Server Agent" in body
    assert "live" in body
    assert "last beat" in body
    assert "gpu" in body


def test_the_board_says_so_when_nothing_has_registered(data_dir):
    body = " ".join(run("site_agents", {}, method="GET")["body"].split())
    assert "No agents are registered" in body
    assert "action_agent_heartbeat" in body


def test_the_page_and_the_json_are_the_same_fold(data_dir):
    beat(label="Server Agent", capabilities="gpu")
    fold = json.loads(run("site_agents", {"_path": "/agents.json"},
                          method="GET")["body"])
    assert fold["total"] == 1
    assert fold["capabilities"] == ["gpu"]
    assert fold["agents"][0]["liveness"] == "live"


def test_the_board_renders_the_existing_feed_rather_than_a_new_one(data_dir):
    """feed_posts (app-collab) has carried `claim` and `release` in its
    kinds since before this package existed. A second coordination
    channel would have been the mistake this whole package was audited to
    avoid."""
    object_records.create_collection_record(
        "feed_posts", {"kind": "claim", "body": "taking the shipping bug",
                       "owner_id": "server-agent"}, base_dir=data_dir)
    body = " ".join(run("site_agents", {}, method="GET")["body"].split())
    assert "taking the shipping bug" in body
    assert "claim" in body


def test_this_package_declares_no_feed_or_task_collection_of_its_own():
    """The audit's conclusion, pinned: the coordination feed and the work
    queue already existed. A package that quietly added its own would
    reintroduce exactly the drift this one was written to avoid."""
    manifest = json.loads(
        (PACKAGES / "app-agents" / "dbbasic-package.json").read_text())
    collections = {entry["collection"] for entry in manifest["schemas"]}
    assert collections == {"agent_registry"}
    assert "app-collab" in manifest["dependencies"]


def test_the_registry_has_no_field_that_duplicates_the_change_log():
    """Every column had to be something not derivable from what the box
    already keeps. A count of work done, or a last-action stamp, would be
    a fold pretending to be a fact."""
    schema = json.loads((PACKAGES / "app-agents" / "schemas"
                         / "agent_registry.json").read_text())
    names = {field["name"] for field in schema["fields"]}
    assert not names & {"last_action", "last_action_at", "tasks_completed",
                        "runs_completed", "actions", "activity", "spend_minor",
                        "committed_minor"}
    assert "heartbeat_at" in names          # the one thing that is NOT derivable
