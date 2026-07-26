"""The notary: an independent party's record that a digest already existed.

Two claims carry this file, and everything else here is in service of one
or the other.

THE TIMESTAMP CANNOT BE WALKED FORWARD. A notarization claims "this
existed BY then", so a second submission of the same digest must return
the FIRST record rather than making a new one. Everywhere else in this
building idempotency exists to avoid a duplicate row; here it exists
because the only direction an attacker would want to move a timestamp is
later, and there must be no way to do it.

THE SERVICE NEVER LEARNS WHAT IT ATTESTS TO. There is no field for
content and no code path that would accept it, which is what makes
notarizing something confidential safe and what makes this collection not
worth stealing. And because the record proves so much less than the word
"notarized" suggests, every surface has to say so -- the failure mode of
a notary is not a bug, it is a reader taking it for verification.
"""

import pathlib

import pytest
from conftest import stage_collection

import object_execution
import object_notary
import object_records
import python_object_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
NOTARY_OBJECTS = PACKAGES / "app-notary" / "objects"
RUNTIME = python_object_runtime.PythonObjectRuntime()

# sha256 of b"the quick brown fox", computed by hashlib in the fixture
# rather than pasted, so the constant cannot rot away from the algorithm.
SHA256_A = object_notary.digest_of(b"the quick brown fox")
SHA256_B = object_notary.digest_of(b"a different thing entirely")
SHA512_A = object_notary.digest_of(b"the quick brown fox", "sha512")


# --- the pure module, with no data directory in sight -------------------------

def test_a_digest_is_normalised_rather_than_refused_over_its_spelling():
    """A hex digest is the same value upper or lower cased, and tools hand
    them over with '0x' and 'sha256:' prefixes all the time. Losing a
    lookup to a capital letter would be a policy nobody chose."""
    assert object_notary.normalize_digest(SHA256_A.upper()) == SHA256_A
    assert object_notary.normalize_digest("  " + SHA256_A + "  ") == SHA256_A
    assert object_notary.normalize_digest("sha256:" + SHA256_A) == SHA256_A
    assert object_notary.normalize_digest("0x" + SHA256_A) == SHA256_A


def test_weak_algorithms_are_refused_and_the_refusal_says_which_are_not():
    """md5 and sha1 are not merely unimplemented. A digest somebody can
    collide lets them notarize one document today and produce a different
    one with the same hash tomorrow -- which is the entire attack this
    exists to make hard, so accepting one would be storing nothing while
    appearing to store something."""
    problems = object_notary.problems("d41d8cd98f00b204e9800998ecf8427e", "md5")
    assert problems
    joined = " ".join(problems)
    assert "md5" in joined
    assert "sha256" in joined and "sha512" in joined
    assert "collided" in joined or "collision" in joined
    assert object_notary.normalize_algorithm("sha1") == ""


def test_a_wrong_length_digest_is_told_what_length_it_should_be():
    """'Invalid digest' is the sentence whose commonest cause is somebody
    having hashed with sha1 and having no way to find that out."""
    problems = object_notary.problems("abc123", "sha256")
    assert len(problems) == 1
    assert "64" in problems[0] and "6" in problems[0]

    not_hex = object_notary.problems("z" * 64, "sha256")
    assert "not hex" in " ".join(not_hex).lower()


def test_the_earliest_record_wins_and_a_later_one_cannot_replace_it():
    """THE load-bearing rule. A notarization says the data existed BY the
    stamped moment; a later submission cannot make it earlier, so the
    first record is the only one that carries information. If the latest
    won, anybody could quietly discard the single fact the log holds by
    submitting again."""
    rows = [
        {"digest": SHA256_A, "algorithm": "sha256",
         "created_at": "2026-07-26T10:00:00"},
        {"digest": SHA256_A, "algorithm": "sha256",
         "created_at": "2026-01-02T09:00:00"},
        {"digest": SHA256_A, "algorithm": "sha256",
         "created_at": "2026-12-31T23:59:59"},
    ]
    found = object_notary.first_seen(SHA256_A, rows)
    assert found["created_at"] == "2026-01-02T09:00:00"


def test_the_same_hex_under_a_different_algorithm_is_a_different_record():
    """A sha256 and a sha512 digest cannot collide by length, but the
    lookup still keys on both -- otherwise a row's attestation would be
    about an algorithm nobody checked."""
    rows = [{"digest": SHA512_A, "algorithm": "sha512",
             "created_at": "2026-01-01T00:00:00"}]
    assert object_notary.first_seen(SHA512_A, rows, "sha512") is not None
    assert object_notary.first_seen(SHA512_A, rows, "sha256") is None


def test_the_attestation_names_what_it_does_not_prove():
    """Overstating this is the only way a service this simple can do harm.
    The wording lives in one function so the page, the JSON and the
    submission receipt cannot drift into three different strengths of the
    same promise."""
    said = object_notary.attestation({"created_at": "2026-07-26T10:00:00"})
    denied = " ".join(said["does_not_prove"]).lower()
    assert "who created" in denied
    assert "owned" in denied
    assert "earlier" in denied            # it is an upper bound on age
    assert "content" in denied
    assert "clock" in " ".join(said["rests_on"]).lower()


def test_a_missing_digest_denies_rather_than_proves_absence():
    """A checker who is told '404' reads a fault. A checker who is told
    'not recorded here' has the right answer, and the record has to say
    that absence is not evidence."""
    answer = object_notary.receipt(None, found=False)
    assert answer["found"] is False
    assert not answer["attestation"]["proves"]
    assert "not evidence" in " ".join(
        answer["attestation"]["does_not_prove"]).lower()


def test_a_label_cannot_break_the_row_it_is_written_into():
    """A tab ends a field and a newline ends a record. Refusing the
    submission over whitespace would lose a notarization to a formatting
    detail, so the label is collapsed instead."""
    label = object_notary.normalize_label("payroll\thead\nfor July  2026 ")
    assert "\t" not in label and "\n" not in label
    assert label == "payroll head for July 2026"
    assert len(object_notary.normalize_label("x" * 500)) == object_notary.MAX_LABEL


# --- through the objects, on a real data directory ----------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    base = tmp_path / "data"
    stage_collection(base, "app-notary", "notarizations")
    stage_collection(base, "app-settings", "app_settings")
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(base))
    return base


def run(object_id, payload, *, method="POST"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest(object_id, method=method,
                                                payload=payload),
        roots=[NOTARY_OBJECTS]).result


def notarize(**payload):
    payload.setdefault("_identity", {"user_id": "dan"})
    return run("action_notarize", payload)


def setting(data_dir, key, value):
    return object_records.create_collection_record(
        "app_settings", {"key": key, "value": value}, base_dir=data_dir)


def test_lodging_a_digest_records_the_moment_and_not_the_content(data_dir):
    receipt = notarize(digest=SHA256_A, label="ledger head")
    assert receipt["found"] is True
    assert receipt["already_recorded"] is False
    assert receipt["digest"] == SHA256_A
    assert receipt["first_seen_at"]

    rows = object_records.read_collection_records("notarizations",
                                                  base_dir=data_dir)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["digest"] == SHA256_A
    assert stored["submitter"] == "dan"
    # The whole safety argument in one assertion: there is nowhere in this
    # row for the thing that was hashed, so a stolen copy of the log tells
    # a thief which digests exist and nothing else at all.
    assert "the quick brown fox" not in "\t".join(stored.values())
    assert set(stored) <= {"id", "digest", "algorithm", "label", "submitter",
                           "source", "created_at", "_op", "rev"}


def test_resubmitting_returns_the_original_record_and_writes_nothing(data_dir):
    """The property that makes the whole service worth anything. If a
    resubmission wrote a new row, the earliest fact would still be in the
    log -- but a checker reading the newest would be told a later time,
    and the attestation would have quietly weakened itself."""
    first = notarize(digest=SHA256_A, label="original")
    again = notarize(digest=SHA256_A, label="a different label entirely")

    assert again["already_recorded"] is True
    assert again["first_seen_at"] == first["first_seen_at"]
    assert again["label"] == "original"        # the later label is discarded
    assert "cannot move a timestamp forward" in again["note"]

    rows = object_records.read_collection_records("notarizations",
                                                  base_dir=data_dir)
    assert len(rows) == 1


def test_resubmitting_upper_cased_is_still_the_same_digest(data_dir):
    first = notarize(digest=SHA256_A)
    again = notarize(digest=SHA256_A.upper())
    assert again["already_recorded"] is True
    assert again["first_seen_at"] == first["first_seen_at"]
    assert len(object_records.read_collection_records(
        "notarizations", base_dir=data_dir)) == 1


def test_a_weak_algorithm_is_refused_at_the_door(data_dir):
    refused = notarize(digest="d41d8cd98f00b204e9800998ecf8427e",
                       algorithm="md5")
    assert refused["status"] == 400
    assert not object_records.read_collection_records("notarizations",
                                                      base_dir=data_dir)


def test_anonymous_submission_is_closed_until_the_operator_opens_it(data_dir):
    """A capability boundary in the house shape: absent by default, and a
    409 naming exactly what to configure rather than a stub that pretends
    to have worked. A notary run as a service for other people must accept
    strangers; one run for a single business's own ledger heads must not,
    and those two deployments are one setting apart."""
    refused = run("action_notarize", {"digest": SHA256_A})
    assert refused["status"] == 409
    assert refused["setting"] == "notary.public_submission"
    assert "notary.public_submission" in refused["error"]
    assert not object_records.read_collection_records("notarizations",
                                                      base_dir=data_dir)

    setting(data_dir, "notary.public_submission", "true")
    accepted = run("action_notarize", {"digest": SHA256_A})
    assert accepted["found"] is True
    assert accepted["digest"] == SHA256_A
    rows = object_records.read_collection_records("notarizations",
                                                  base_dir=data_dir)
    assert rows[0]["submitter"] == "public"


def test_an_unreadable_log_refuses_rather_than_stamping_a_second_time(tmp_path,
                                                                      monkeypatch):
    """If the log cannot be read, a first-seen row cannot be found, so
    appending would risk a SECOND and later stamp on something already
    recorded -- exactly the failure this object exists to prevent. A
    submission refused gets retried; a timestamp silently reset is gone."""
    base = tmp_path / "empty"
    base.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(base))
    refused = run("action_notarize",
                  {"digest": SHA256_A, "_identity": {"user_id": "dan"}})
    assert refused["status"] == 409
    assert "second" in refused["error"] and "later stamp" in refused["error"]


# --- the public check ---------------------------------------------------------

def check(digest, *, method="GET"):
    return object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest("site_notary", method=method,
                                                payload={"digest": digest}),
        roots=[NOTARY_OBJECTS]).result


def test_the_check_is_public_and_answers_with_the_time_it_first_saw_it(data_dir):
    receipt = notarize(digest=SHA256_A, label="ledger head 26 July")
    page = check(SHA256_A)
    assert page["status"] == 200
    body = page["body"]
    assert SHA256_A in body
    assert receipt["first_seen_at"] in body
    assert "ledger head 26 July" in body
    # And the caveats travel with the answer rather than living in a
    # footnote somebody scrolls past.
    assert "does not prove" in body
    assert "Anybody can notarize anybody" in body


def test_the_public_answer_does_not_say_who_lodged_it(data_dir):
    """What a digest is worth as evidence does not depend on who filed it,
    and publishing who anchored what would leak the shape of somebody's
    records without adding anything to the attestation."""
    import json
    run("action_notarize", {"digest": SHA256_A, "label": "ledger head",
                            "_identity": {"user_id": "accounts-payable-clerk"}})

    body = check(SHA256_A)["body"]
    assert SHA256_A in body                      # the answer IS there
    assert "accounts-payable-clerk" not in body

    parsed = json.loads(check(SHA256_A + ".json")["body"])
    assert parsed["found"] is True
    assert "submitter" not in parsed
    assert "accounts-payable-clerk" not in json.dumps(parsed)

    # And the operator can still see it, because "not on the public page"
    # is not the same as "not recorded".
    stored = object_records.read_collection_records("notarizations",
                                                    base_dir=data_dir)
    assert stored[0]["submitter"] == "accounts-payable-clerk"


def test_an_unknown_digest_is_answered_rather_than_404ed(data_dir):
    page = check(SHA256_B)
    assert page["status"] == 200
    assert "Not recorded here" in page["body"]
    assert "not evidence of anything" in page["body"]

    answer = check(SHA256_B + ".json")
    assert answer["status"] == 200          # a complete answer, not a fault
    import json
    assert json.loads(answer["body"])["found"] is False


def test_the_json_and_the_page_are_the_same_fold(data_dir):
    import json
    notarize(digest=SHA256_A, label="head")
    parsed = json.loads(check(SHA256_A + ".json")["body"])
    assert parsed["found"] is True
    assert parsed["digest"] == SHA256_A
    assert parsed["first_seen_at"] in check(SHA256_A)["body"]


def test_the_index_says_it_holds_no_content_and_offers_a_local_hash(data_dir):
    """The drop box hashes in the browser with SubtleCrypto and uploads
    nothing. It earns its place by demonstrating the central claim instead
    of asking to be believed -- a visitor can watch a file produce a digest
    with no request leaving the machine."""
    page = object_execution.execute_object(
        RUNTIME,
        object_execution.ObjectExecutionRequest("site_notary", method="GET",
                                                payload={}),
        roots=[NOTARY_OBJECTS]).result
    body = page["body"]
    assert "no content whatsoever" in body
    assert "crypto.subtle" in body
    assert "never uploaded" in body
    # The submission door is described as closed, because it is.
    assert "requires an account or a service key" in body


def test_nothing_in_the_package_offers_a_way_to_delete_a_notarization():
    """A notary with an edit path is a notary whose operator can be leaned
    on. This is a grep rather than a behaviour test on purpose: the risk
    is not that today's code deletes a row, it is that somebody later adds
    a tidy-up that does, and a test naming the whole package is what
    stands in the way."""
    rules = (PACKAGES / "app-notary" / "permissions" / "rules.json").read_text()
    assert '"delete"' not in rules
    assert '"update"' not in rules

    for path in (PACKAGES / "app-notary").rglob("*.py"):
        source = path.read_text()
        assert "delete_collection_record" not in source, path
        assert "update_collection_record" not in source, path
        assert "prune_collection_records" not in source, path


def test_the_schema_stores_the_digest_append_only_and_holds_no_content_field():
    import json
    schema = json.loads(
        (PACKAGES / "app-notary" / "schemas" / "notarizations.json").read_text())
    assert schema["storage"] == "append"
    names = {field["name"] for field in schema["fields"]}
    assert names == {"id", "digest", "algorithm", "label", "submitter",
                     "source", "created_at"}
    # Named explicitly: if somebody adds one of these the assertion above
    # already fails, but the failure should read as a decision being
    # reversed rather than a list needing updating.
    assert not names & {"content", "body", "data", "file", "filename",
                        "bytes", "payload"}
