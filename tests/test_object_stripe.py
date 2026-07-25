"""Adversarial tests for the Stripe primitives.

The posture: this is the money path, so every test here is written as an
attempt to break object_stripe rather than to demonstrate it. The webhook
verifier gets tampered payloads, wrong secrets, replayed and future
timestamps, rotation headers, and a pile of malformed input that a prober
would send; the API layer gets a transport that never touches a socket and
is inspected for exactly what went on the wire.

No test in this file makes a network call. ``api_request`` takes an
injected ``transport``, and the one test that exercises the default
transport's URL rules stops before any connection is opened.
"""

import ast
import hashlib
import hmac
import inspect
import json
import urllib.parse
from decimal import Decimal

import pytest

import object_stripe


SECRET = "whsec_test_0123456789abcdef"
NOW = 1_700_000_000


def sign_header(payload: bytes, secret: str = SECRET, timestamp: int = NOW,
                scheme: str = "v1") -> str:
    """Build a Stripe-Signature header the way Stripe documents it."""
    digest = hmac.new(secret.encode("utf-8"),
                      str(timestamp).encode("ascii") + b"." + payload,
                      hashlib.sha256).hexdigest()
    return f"t={timestamp},{scheme}={digest}"


PAYLOAD = b'{"id":"evt_1Abc","type":"checkout.session.completed","data":{}}'


# --- signature: the fixed vector --------------------------------------------

def test_fixed_vector_pins_the_signed_string_format():
    """A hardcoded digest computed outside this module.

    The other signature tests build their header with the same HMAC recipe
    the implementation uses, so they would all still pass if the signed
    string quietly became, say, "{t}:{body}". This one would not: the
    digest below was computed once, by hand, over "1700000000." + body.
    """
    payload = b'{"id":"evt_fixed","type":"checkout.session.completed"}'
    header = ("t=1700000000,"
              "v1=b0ffffece04823710d9c27175b5093fa7c7416f9f6de5572499420eeaba0c6a8")
    result = object_stripe.verify_webhook_signature(
        payload, header, "whsec_fixed_vector_secret", now=1700000000)
    assert result == {"ok": True, "timestamp": 1700000000}


# --- signature: the happy path and the near misses --------------------------

def test_valid_signature_accepted():
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, sign_header(PAYLOAD), SECRET, now=NOW)
    assert result["ok"] is True
    assert result["timestamp"] == NOW


def test_one_flipped_byte_in_the_payload_is_rejected():
    header = sign_header(PAYLOAD)
    tampered = bytearray(PAYLOAD)
    tampered[10] ^= 0x01
    result = object_stripe.verify_webhook_signature(bytes(tampered), header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "no v1 signature matched" in result["reason"]


def test_appended_byte_is_rejected():
    result = object_stripe.verify_webhook_signature(
        PAYLOAD + b" ", sign_header(PAYLOAD), SECRET, now=NOW)
    assert result["ok"] is False


def test_wrong_secret_is_rejected():
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, sign_header(PAYLOAD), "whsec_a_different_secret_entirely", now=NOW)
    assert result["ok"] is False
    assert "no v1 signature matched" in result["reason"]


def test_secret_off_by_one_character_is_rejected():
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, sign_header(PAYLOAD), SECRET + "0", now=NOW)
    assert result["ok"] is False


def test_signature_for_a_different_timestamp_is_rejected():
    """The timestamp is inside the MAC, so swapping it invalidates."""
    header = sign_header(PAYLOAD, timestamp=NOW)
    digest = header.split("v1=", 1)[1]
    forged = f"t={NOW + 1},v1={digest}"
    result = object_stripe.verify_webhook_signature(PAYLOAD, forged, SECRET, now=NOW)
    assert result["ok"] is False
    assert "no v1 signature matched" in result["reason"]


# --- signature: the replay window -------------------------------------------

def test_stale_timestamp_rejected():
    header = sign_header(PAYLOAD, timestamp=NOW - 3600)
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "3600s old" in result["reason"]
    assert "outside the 300s tolerance" in result["reason"]


def test_future_timestamp_beyond_tolerance_rejected():
    header = sign_header(PAYLOAD, timestamp=NOW + 3600)
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "in the future" in result["reason"]


@pytest.mark.parametrize("offset", [-300, -299, 0, 299, 300])
def test_inside_or_exactly_at_tolerance_is_accepted(offset):
    """The window is closed at both ends: |now - t| == tolerance passes."""
    stamp = NOW + offset
    header = sign_header(PAYLOAD, timestamp=stamp)
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is True
    assert result["timestamp"] == stamp


@pytest.mark.parametrize("offset", [-301, 301])
def test_one_second_past_tolerance_is_rejected(offset):
    header = sign_header(PAYLOAD, timestamp=NOW + offset)
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "tolerance" in result["reason"]


def test_tolerance_is_configurable():
    header = sign_header(PAYLOAD, timestamp=NOW - 600)
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is False
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, tolerance_seconds=900, now=NOW)["ok"] is True


def test_default_now_uses_the_clock(monkeypatch):
    monkeypatch.setattr(object_stripe.time, "time", lambda: float(NOW))
    result = object_stripe.verify_webhook_signature(PAYLOAD, sign_header(PAYLOAD), SECRET)
    assert result["ok"] is True


def test_a_captured_valid_event_cannot_be_replayed_later():
    """The whole point of the window: a real, correctly-signed event
    sniffed off the wire is worthless an hour after it was sent."""
    header = sign_header(PAYLOAD, timestamp=NOW)
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW + 3601)["ok"] is False


# --- signature: secret rotation ---------------------------------------------

def test_multiple_v1_signatures_accepted_when_one_matches():
    good = sign_header(PAYLOAD).split("v1=", 1)[1]
    header = f"t={NOW},v1={'0' * 64},v1={good}"
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True


def test_multiple_v1_signatures_rejected_when_none_match():
    header = f"t={NOW},v1={'0' * 64},v1={'f' * 64}"
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "no v1 signature matched" in result["reason"]


def test_v0_only_is_rejected():
    """v0 is Stripe's test-mode scheme over a different payload. Treating
    it as a signature would accept anything the dashboard can send."""
    header = sign_header(PAYLOAD, scheme="v0")
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "no v1= signature" in result["reason"]


def test_v0_alongside_a_valid_v1_still_verifies():
    good = sign_header(PAYLOAD).split("v1=", 1)[1]
    header = f"t={NOW},v0={'a' * 64},v1={good}"
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True


def test_too_many_v1_values_is_refused():
    """A rotation uses two secrets, not sixty. A header stuffed with
    candidates is a resource-exhaustion probe, not a webhook."""
    # 17 candidates stays under the 2048-char header cap, so the
    # signature-count limit itself fires; 40 would trip the length cap
    # first, which the length test covers separately.
    header = "t=%d,%s" % (NOW, ",".join(f"v1={'0' * 64}" for _ in range(17)))
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "more than 16 v1 signatures" in result["reason"]


# --- signature: malformed headers -------------------------------------------

MALFORMED = [
    ("", "signature header is missing"),
    ("    ", "signature header is missing"),
    (None, "signature header is missing"),
    (12345, "signature header is missing"),
    (b"t=1,v1=abc", "signature header is missing"),
    ("v1=" + "a" * 64, "no t= timestamp"),
    (f"t={NOW}", "no v1= signature"),
    (f"t={NOW},v1=", "no v1= signature"),
    ("t=,v1=" + "a" * 64, "no t= timestamp"),
    ("t=abc,v1=" + "a" * 64, "not a plain unsigned integer"),
    ("t=-1,v1=" + "a" * 64, "not a plain unsigned integer"),
    ("t=1.5,v1=" + "a" * 64, "not a plain unsigned integer"),
    ("t=0x10,v1=" + "a" * 64, "not a plain unsigned integer"),
    ("t=١٧٠٠٠٠٠٠٠٠,v1=" + "a" * 64, "not a plain unsigned integer"),
    # 5000 nines would trip the header-length cap first; 100 exercises the
    # timestamp check itself (a digit-length cap on t=, refused as not a
    # plain unsigned integer rather than parsed into a bignum).
    ("t=" + "9" * 100 + ",v1=" + "a" * 64, "not a plain unsigned integer"),
    (f"t={NOW},t={NOW + 1},v1=" + "a" * 64, "more than one t= timestamp"),
    ("wat", "no t= timestamp"),
    (",,,,", "no t= timestamp"),
    ("=", "no t= timestamp"),
    ("t", "no t= timestamp"),
    ("\x00\x01\x02", "no t= timestamp"),
    ("t=" + str(NOW) + ",v1=" + "z" * 64, "no v1 signature matched"),
]


@pytest.mark.parametrize("header,expected", MALFORMED)
def test_malformed_headers_are_refused_never_raised(header, expected):
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert isinstance(result["reason"], str) and result["reason"]
    assert expected in result["reason"]


def test_oversized_header_is_refused():
    header = f"t={NOW},v1=" + "a" * 4000
    result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
    assert result["ok"] is False
    assert "longer than 2048 characters" in result["reason"]


def test_spaces_after_commas_are_tolerated():
    """Stripe emits none, but a reverse proxy that normalises headers is a
    realistic thing to sit behind. Whitespace cannot change what is
    verified -- the t token is constrained to ASCII digits and the digest
    is compared byte for byte -- so leniency here is free."""
    header = sign_header(PAYLOAD).replace(",", ", ")
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True


def test_leading_and_trailing_header_whitespace_tolerated():
    header = "  " + sign_header(PAYLOAD) + "  \r\n"
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True


def test_uppercase_hex_digest_is_refused():
    """A deliberate strictness: hex is case-insensitive in principle, but
    Stripe emits lowercase, so accepting mixed case would mean
    canonicalising attacker input immediately before a security
    comparison. If Stripe ever changes, every webhook fails loudly and
    visibly rather than one of them passing quietly."""
    header = sign_header(PAYLOAD)
    upper = header.split("v1=", 1)[1].upper()
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, f"t={NOW},v1={upper}", SECRET, now=NOW)
    assert result["ok"] is False


def test_non_ascii_signature_candidate_is_skipped_not_raised():
    """hmac.compare_digest raises TypeError on non-ASCII str -- exactly the
    500-on-garbage a public webhook URL must never do."""
    good = sign_header(PAYLOAD).split("v1=", 1)[1]
    header = f"t={NOW},v1=café{'a' * 60},v1={good}"
    assert object_stripe.verify_webhook_signature(
        PAYLOAD, header, SECRET, now=NOW)["ok"] is True

    only_bad = f"t={NOW},v1=café{'a' * 60}"
    result = object_stripe.verify_webhook_signature(PAYLOAD, only_bad, SECRET, now=NOW)
    assert result["ok"] is False


def test_timestamp_token_is_signed_verbatim_not_reparsed():
    """A token with a leading zero still verifies against what was signed.

    Stripe hashes the characters it sent. Re-serialising int(t) back into
    the signed string would break on any representation we did not
    anticipate; hashing the token as it arrived cannot.
    """
    token = "01700000000"
    digest = hmac.new(SECRET.encode(), token.encode() + b"." + PAYLOAD,
                      hashlib.sha256).hexdigest()
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, f"t={token},v1={digest}", SECRET, now=1700000000)
    assert result["ok"] is True
    assert result["timestamp"] == 1700000000


# --- signature: the empty-secret hole ---------------------------------------

def test_empty_webhook_secret_never_verifies():
    """The one that matters most.

    HMAC keyed with "" is a function anyone can compute, so an
    unconfigured deployment that verified against an empty secret would
    accept forged payment events from the entire internet. Here is the
    exact forgery, and it must be refused.
    """
    forged_digest = hmac.new(b"", str(NOW).encode() + b"." + PAYLOAD,
                             hashlib.sha256).hexdigest()
    header = f"t={NOW},v1={forged_digest}"
    for absent in ("", "   ", None, 0):
        result = object_stripe.verify_webhook_signature(PAYLOAD, header, absent, now=NOW)
        assert result["ok"] is False
        assert "webhook secret is not configured" in result["reason"]


def test_unconfigured_config_cannot_verify_anything():
    config = object_stripe.stripe_config_from_env({})
    assert config.configured is False
    result = object_stripe.verify_webhook_signature(
        PAYLOAD, sign_header(PAYLOAD), config.webhook_secret, now=NOW)
    assert result["ok"] is False


# --- signature: payload typing ----------------------------------------------

def test_decoded_string_payload_is_refused():
    """The single most common real-world webhook bug: handing the verifier
    the framework's decoded body. It usually works, which is why it is
    dangerous -- so it is refused outright."""
    result = object_stripe.verify_webhook_signature(
        PAYLOAD.decode(), sign_header(PAYLOAD), SECRET, now=NOW)
    assert result["ok"] is False
    assert "raw request bytes" in result["reason"]


@pytest.mark.parametrize("payload", [None, 12, {"id": "evt_1"}, ["a"]])
def test_non_bytes_payload_is_refused(payload):
    result = object_stripe.verify_webhook_signature(
        payload, sign_header(PAYLOAD), SECRET, now=NOW)
    assert result["ok"] is False
    assert "must be" in result["reason"]


def test_bytearray_and_memoryview_payloads_work():
    header = sign_header(PAYLOAD)
    for shape in (bytearray(PAYLOAD), memoryview(PAYLOAD)):
        assert object_stripe.verify_webhook_signature(
            shape, header, SECRET, now=NOW)["ok"] is True


def test_payload_that_is_not_valid_utf8_still_verifies():
    """The divergence from the docs' "{t}.{body}" string formulation: the
    MAC is over raw bytes. A body with an invalid UTF-8 sequence would
    make a decode-then-hash implementation raise -- i.e. 500 -- on input
    an attacker fully controls."""
    payload = b'{"id":"evt_1","raw":"\xff\xfe"}'
    result = object_stripe.verify_webhook_signature(
        payload, sign_header(payload), SECRET, now=NOW)
    assert result["ok"] is True


def test_empty_payload_still_verifies_against_its_own_signature():
    assert object_stripe.verify_webhook_signature(
        b"", sign_header(b""), SECRET, now=NOW)["ok"] is True


def test_no_reason_string_ever_leaks_the_secret_or_a_digest():
    expected = hmac.new(SECRET.encode(), str(NOW).encode() + b"." + PAYLOAD,
                        hashlib.sha256).hexdigest()
    for header, _ in MALFORMED:
        result = object_stripe.verify_webhook_signature(PAYLOAD, header, SECRET, now=NOW)
        assert SECRET not in result["reason"]
        assert expected not in result["reason"]
    stale = object_stripe.verify_webhook_signature(
        PAYLOAD, sign_header(PAYLOAD, timestamp=1), SECRET, now=NOW)
    assert SECRET not in stale["reason"] and expected not in stale["reason"]


@pytest.mark.parametrize("junk", [
    "t=1,v1=" + "\x00" * 64,
    "t=1;v1=abc",
    "t==1,v1==abc",
    "t=1,,v1=abc,",
    "T=1,V1=abc",
    "t=1,v1=abc" * 50,
    "\t\n",
    "t=99999999999999999999999999,v1=" + "a" * 64,
])
def test_hostile_headers_never_raise(junk):
    result = object_stripe.verify_webhook_signature(PAYLOAD, junk, SECRET, now=NOW)
    assert result["ok"] is False
    assert isinstance(result["reason"], str)


# --- timing safety ----------------------------------------------------------

_SENSITIVE_NAMES = {
    "expected", "offered", "offered_list", "candidate", "digest", "hexdigest",
    "signature", "signatures", "secret", "webhook_secret", "material", "mac",
    "compare_digest",
}


def _names_in(node):
    found = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found


def test_signature_comparison_uses_compare_digest_only():
    """Belt and braces for a rule whose regression would be invisible.

    A `==` on a hex digest compares byte by byte and returns early, which
    leaks the length of the correct prefix through timing -- enough, over
    many requests, to reconstruct a valid signature. Nothing in this
    module may ever compare secret-derived values with == or !=, and a
    reviewer cannot see the difference by reading the diff, so the check
    is mechanical.
    """
    source = inspect.getsource(object_stripe)
    assert "hmac.compare_digest(" in source

    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, right in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            names = _names_in(node.left) | _names_in(right)
            if names & _SENSITIVE_NAMES:
                offenders.append((node.lineno, sorted(names & _SENSITIVE_NAMES)))
    assert offenders == [], f"secret-derived == comparison at {offenders}"


def test_verification_never_calls_hexdigest_into_an_equality():
    source = inspect.getsource(object_stripe.verify_webhook_signature)
    assert "compare_digest" in source
    for line in source.splitlines():
        if "hexdigest" in line:
            assert "==" not in line and "!=" not in line


# --- parse_event ------------------------------------------------------------

def test_parse_event_returns_the_dict():
    event = object_stripe.parse_event(PAYLOAD)
    assert event["id"] == "evt_1Abc"
    assert event["type"] == "checkout.session.completed"


def test_parse_event_rejects_oversized_payload():
    body = b'{"id":"evt_1","pad":"' + b"x" * 2000 + b'"}'
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.parse_event(body, max_bytes=1000)
    assert "over the 1000-byte cap" in str(exc.value)


def test_parse_event_default_cap_is_one_megabyte():
    assert object_stripe.MAX_PAYLOAD_BYTES == 1_000_000
    body = b'{"id":"evt_1","pad":"' + b"x" * 1_000_001 + b'"}'
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.parse_event(body)


def test_parse_event_checks_size_before_parsing():
    """The cap must gate the parse, not follow it -- otherwise a 200MB
    body is already decoded and in memory by the time it is refused."""
    body = b"[" + b"1," * 500_000 + b"1]"
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.parse_event(body, max_bytes=100)
    assert "cap" in str(exc.value)


@pytest.mark.parametrize("body", [b"[]", b'"a string"', b"42", b"null", b"true"])
def test_parse_event_rejects_non_object_json(body):
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.parse_event(body)
    assert "not an object" in str(exc.value)


@pytest.mark.parametrize("body", [b"{", b"", b"   ", b"not json at all", b"{'a': 1}"])
def test_parse_event_rejects_unparseable(body):
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.parse_event(body)


def test_parse_event_rejects_invalid_utf8():
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.parse_event(b'{"id":"\xff\xfe"}')
    assert "UTF-8" in str(exc.value)


def test_parse_event_rejects_a_decoded_string():
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.parse_event(PAYLOAD.decode())
    assert "raw request bytes" in str(exc.value)


def test_parse_event_errors_are_stripe_errors():
    assert issubclass(object_stripe.StripePayloadError, object_stripe.StripeError)
    with pytest.raises(object_stripe.StripeError):
        object_stripe.parse_event(b"[]")


# --- event_marker -----------------------------------------------------------

def test_event_marker_shape():
    assert object_stripe.event_marker({"id": "evt_1Abc"}) == "stripe_evt/evt_1Abc"


@pytest.mark.parametrize("event", [
    {}, {"id": ""}, {"id": None}, {"id": 12}, {"id": ["evt_1"]}, {"identifier": "evt_1"},
])
def test_event_marker_requires_a_string_id(event):
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.event_marker(event)


@pytest.mark.parametrize("event", [None, "evt_1", ["evt_1"], 7])
def test_event_marker_requires_an_event_object(event):
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.event_marker(event)


@pytest.mark.parametrize("ident", [
    "evt_1\tevil", "evt_1\nevil", "evt_1\revil", "evt 1", "evt/1", "evt_1\x00",
    "../../etc/passwd", "evt_1;DROP", "évt_1",
])
def test_event_marker_refuses_ids_that_would_corrupt_a_tsv_row(ident):
    """The store is tab-separated rows. A tab or newline inside the
    provenance stamp does not look odd in a log -- it splits one record
    into two."""
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.event_marker({"id": ident})


def test_event_marker_refuses_an_absurdly_long_id():
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.event_marker({"id": "e" * 500})


def test_event_marker_error_does_not_echo_a_whole_hostile_id():
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.event_marker({"id": "evt_" + "A" * 300})
    assert len(str(exc.value)) < 120


# --- flatten_params ---------------------------------------------------------

def test_flatten_nested_dicts_and_lists():
    pairs = object_stripe.flatten_params(
        {"line_items": [{"price_data": {"currency": "usd", "unit_amount": 1250}}]})
    assert pairs == [
        ("line_items[0][price_data][currency]", "usd"),
        ("line_items[0][price_data][unit_amount]", "1250"),
    ]


def test_flatten_list_indices_are_positional():
    pairs = object_stripe.flatten_params({"items": [{"a": "1"}, {"a": "2"}]})
    assert pairs == [("items[0][a]", "1"), ("items[1][a]", "2")]


def test_flatten_booleans_are_lowercase_json_style():
    pairs = dict(object_stripe.flatten_params({"yes": True, "no": False}))
    assert pairs == {"yes": "true", "no": "false"}


def test_flatten_false_does_not_become_truthy_text():
    """bool is an int subclass; str(False) is "True"-shaped to Stripe (a
    non-empty string), so the bool branch has to come first."""
    assert object_stripe.flatten_params({"expand": False}) == [("expand", "false")]


def test_flatten_ints_and_decimals():
    pairs = dict(object_stripe.flatten_params({"n": 0, "neg": -5, "d": Decimal("1.5")}))
    assert pairs == {"n": "0", "neg": "-5", "d": "1.5"}


def test_flatten_omits_none_entirely():
    assert object_stripe.flatten_params({"a": "1", "b": None}) == [("a", "1")]


def test_flatten_none_inside_a_list_does_not_renumber_siblings():
    pairs = object_stripe.flatten_params({"xs": [None, {"a": "1"}]})
    assert pairs == [("xs[1][a]", "1")]


def test_flatten_empty_containers_emit_nothing():
    assert object_stripe.flatten_params({}) == []
    assert object_stripe.flatten_params({"a": {}, "b": []}) == []


def test_flatten_refuses_floats():
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.flatten_params({"unit_amount": 12.50})
    assert "float" in str(exc.value)


@pytest.mark.parametrize("value", [object(), b"bytes", {1, 2}])
def test_flatten_refuses_unsupported_types(value):
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.flatten_params({"x": value})


@pytest.mark.parametrize("key", ["a[b]", "a=b", "a&b", "", "a\nb", "a\tb"])
def test_flatten_refuses_names_that_could_forge_structure(key):
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.flatten_params({key: "1"})


def test_flatten_refuses_a_top_level_scalar():
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.flatten_params("mode=payment")


def test_flatten_caps_depth():
    deep = value = {}
    for _ in range(20):
        value["k"] = {}
        value = value["k"]
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.flatten_params(deep)
    assert "nest deeper" in str(exc.value)


def test_flatten_survives_a_self_referencing_dict():
    """Without a depth cap this is a RecursionError, which is a crash, not
    a refusal."""
    loop = {"a": {}}
    loop["a"]["self"] = loop
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.flatten_params(loop)


def test_encode_params_percent_encodes_structure_and_values():
    body = object_stripe.encode_params({"metadata": {"invoice_id": "inv 1&2"}})
    assert body == "metadata%5Binvoice_id%5D=inv+1%262"
    assert dict(urllib.parse.parse_qsl(body)) == {"metadata[invoice_id]": "inv 1&2"}


def test_encode_params_output_is_ascii():
    body = object_stripe.encode_params({"metadata": {"note": "café ☕"}})
    assert body.isascii()
    assert dict(urllib.parse.parse_qsl(body))["metadata[note]"] == "café ☕"


def test_a_value_cannot_introduce_a_parameter():
    """The injection question: can a metadata value forge a sibling key?"""
    body = object_stripe.encode_params(
        {"metadata": {"note": "x&mode=subscription"}, "mode": "payment"})
    pairs = urllib.parse.parse_qsl(body)
    assert [k for k, _ in pairs] == ["metadata[note]", "mode"]
    assert dict(pairs)["mode"] == "payment"


# --- api_request ------------------------------------------------------------

class Recorder:
    """A transport that records and never opens a socket."""

    def __init__(self, status=200, body=b'{"id":"cs_test_1","object":"checkout.session"}'):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, url, data, headers, method):
        self.calls.append({"url": url, "data": data,
                           "headers": dict(headers), "method": method})
        return self.status, self.body

    @property
    def last(self):
        return self.calls[-1]


def config(**overrides):
    base = {"secret_key": "sk_test_51abcDEFghiJKL", "webhook_secret": SECRET,
            "api_base": "https://api.stripe.test"}
    base.update(overrides)
    return object_stripe.StripeConfig(**base)


def test_api_request_sends_bearer_auth_and_form_body():
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "/v1/customers",
                              {"email": "a@b.test"}, transport=transport)
    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "https://api.stripe.test/v1/customers"
    assert call["headers"]["Authorization"] == "Bearer sk_test_51abcDEFghiJKL"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call["data"] == b"email=a%40b.test"


def test_api_request_returns_the_parsed_object():
    transport = Recorder(body=b'{"id":"cus_1","livemode":false}')
    result = object_stripe.api_request(config(), "POST", "/v1/customers", {},
                                       transport=transport)
    assert result == {"id": "cus_1", "livemode": False}


def test_post_gets_an_idempotency_key_automatically():
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    key = transport.last["headers"]["Idempotency-Key"]
    assert len(key) == 36 and key.count("-") == 4


def test_auto_idempotency_keys_differ_per_call():
    """Documenting the sharp edge: the auto key protects against Stripe's
    own retries, NOT against a caller retrying. Two calls are two charges,
    which is why a retrying caller must pass its own key."""
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    keys = [c["headers"]["Idempotency-Key"] for c in transport.calls]
    assert keys[0] != keys[1]


def test_explicit_idempotency_key_is_used_verbatim():
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "/v1/charges", {},
                              idempotency_key="invoice-42-attempt", transport=transport)
    assert transport.last["headers"]["Idempotency-Key"] == "invoice-42-attempt"


def test_get_carries_params_in_the_query_string_and_no_body():
    transport = Recorder(body=b'{"object":"list","data":[]}')
    object_stripe.api_request(config(), "GET", "/v1/events",
                              {"limit": 3, "type": "checkout.session.completed"},
                              transport=transport)
    call = transport.last
    assert call["data"] is None
    assert "Content-Type" not in call["headers"]
    assert "Idempotency-Key" not in call["headers"]
    assert call["url"].endswith("?limit=3&type=checkout.session.completed")


def test_get_without_params_has_a_clean_url():
    transport = Recorder(body=b"{}")
    object_stripe.api_request(config(), "GET", "/v1/balance", None, transport=transport)
    assert transport.last["url"] == "https://api.stripe.test/v1/balance"


def test_api_version_header_only_when_pinned():
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert "Stripe-Version" not in transport.last["headers"]

    object_stripe.api_request(config(api_version="2026-01-01"), "POST", "/v1/charges",
                              {}, transport=transport)
    assert transport.last["headers"]["Stripe-Version"] == "2026-01-01"


def test_stripe_error_body_becomes_a_stripe_error():
    body = json.dumps({"error": {
        "type": "card_error", "code": "card_declined", "param": "number",
        "message": "Your card was declined."}}).encode()
    transport = Recorder(status=402, body=body)
    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    err = exc.value
    assert err.status == 402
    assert err.type == "card_error"
    assert err.code == "card_declined"
    assert err.param == "number"
    assert err.message == "Your card was declined."
    assert str(err) == "Your card was declined."


def test_error_object_on_a_200_is_still_an_error():
    transport = Recorder(status=200, body=b'{"error":{"message":"nope"}}')
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)


def test_plain_5xx_without_an_error_object_is_an_error():
    transport = Recorder(status=500, body=b"{}")
    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert exc.value.status == 500
    assert "HTTP 500" in str(exc.value)


def test_redirect_status_is_an_error_not_a_result():
    """The default transport refuses to follow redirects (they would carry
    the Authorization header elsewhere), so a 3xx reaching this layer
    means the call never got to Stripe."""
    transport = Recorder(status=302, body=b'{"id":"not_really"}')
    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert exc.value.status == 302


def test_non_json_response_is_a_clear_error():
    transport = Recorder(status=502, body=b"<html><body>Bad Gateway</body></html>")
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert "non-JSON" in str(exc.value)
    assert "HTTP 502" in str(exc.value)


def test_non_object_json_response_is_a_clear_error():
    transport = Recorder(status=200, body=b"[1,2,3]")
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert "not an object" in str(exc.value)


def test_empty_2xx_body_is_an_empty_dict():
    transport = Recorder(status=204, body=b"")
    assert object_stripe.api_request(config(), "DELETE", "/v1/customers/cus_1", None,
                                     transport=transport) == {}


def test_empty_error_body_still_raises():
    transport = Recorder(status=500, body=b"")
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)


def test_oversized_response_is_refused():
    transport = Recorder(body=b"x" * (object_stripe.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=transport)
    assert "cap" in str(exc.value)


def test_transport_returning_junk_is_a_clear_error():
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.api_request(config(), "POST", "/v1/charges", {},
                                  transport=lambda *a: (200, "a string, not bytes"))
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.api_request(config(), "POST", "/v1/charges", {},
                                  transport=lambda *a: ("wat", b"{}"))


def test_transport_exception_is_wrapped_in_a_stripe_error():
    def boom(url, data, headers, method):
        raise OSError("connection reset")

    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=boom)
    assert "connection reset" in str(exc.value)


def test_a_secret_in_an_error_message_is_redacted():
    """Defence in depth: nothing deliberately puts a key in a message, but
    a proxy error page or an OSError from a lower layer might."""
    key = "sk_test_51abcDEFghiJKL"
    body = json.dumps({"error": {"message": f"bad key {key} supplied"}}).encode()
    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {},
                                  transport=Recorder(status=401, body=body))
    assert key not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_secret_in_a_transport_exception_is_redacted():
    key = "sk_test_51abcDEFghiJKL"

    def boom(url, data, headers, method):
        raise RuntimeError(f"failed talking to stripe with {key}")

    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(config(), "POST", "/v1/charges", {}, transport=boom)
    assert key not in str(exc.value)


def test_missing_secret_key_refuses_before_the_transport_is_called():
    transport = Recorder()
    with pytest.raises(object_stripe.StripeError) as exc:
        object_stripe.api_request(object_stripe.StripeConfig(webhook_secret=SECRET),
                                  "POST", "/v1/charges", {}, transport=transport)
    assert "secret key is not configured" in str(exc.value)
    assert transport.calls == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "TRACE", "", None, "post; drop"])
def test_unsupported_methods_are_refused(method):
    transport = Recorder()
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(), method, "/v1/charges", {}, transport=transport)
    assert transport.calls == []


def test_method_is_case_normalised():
    transport = Recorder()
    object_stripe.api_request(config(), "post", "/v1/charges", {}, transport=transport)
    assert transport.last["method"] == "POST"


# --- api_request: where the key is allowed to go ----------------------------

@pytest.mark.parametrize("base", [
    "http://api.stripe.com", "http://example.test", "ftp://api.stripe.com",
    "file:///etc/passwd", "api.stripe.com", "https://",
])
def test_unsafe_api_bases_are_refused(base):
    """A live secret key rides in a header on every request, so plaintext
    http to anything but loopback is refused outright."""
    transport = Recorder()
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(api_base=base), "POST", "/v1/charges", {},
                                  transport=transport)
    assert transport.calls == []


def test_an_empty_api_base_is_coerced_to_the_official_default():
    """Blank is not a destination: both StripeConfig and api_request fold an
    empty base to the official endpoint rather than refusing -- an operator
    who cleared the variable gets Stripe, never a relative-URL surprise.
    Coercion here is SAFER than rejection, which is why this is its own
    test instead of a row in the refusal table."""
    transport = Recorder()
    object_stripe.api_request(config(api_base=""), "POST", "/v1/charges", {},
                              transport=transport)
    assert transport.calls[0]["url"].startswith("https://api.stripe.com/")


@pytest.mark.parametrize("base", ["http://localhost:12111", "http://127.0.0.1:8080"])
def test_loopback_http_is_allowed_for_a_mock_server(base):
    transport = Recorder()
    object_stripe.api_request(config(api_base=base), "POST", "/v1/charges", {},
                              transport=transport)
    assert transport.last["url"].startswith(base)


@pytest.mark.parametrize("path", [
    "https://evil.test/v1/charges", "//evil.test/v1/charges",
    "/v1/../../secret", "/v1/charges\r\nX-Evil: 1", "/v1/ charges", "/v1/\x00",
])
def test_a_path_cannot_point_the_call_somewhere_else(path):
    transport = Recorder()
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(), "POST", path, {}, transport=transport)
    assert transport.calls == []


def test_a_bare_path_segment_is_accepted_and_rooted():
    transport = Recorder()
    object_stripe.api_request(config(), "POST", "v1/charges", {}, transport=transport)
    assert transport.last["url"] == "https://api.stripe.test/v1/charges"


def test_trailing_slash_on_the_base_does_not_double_up():
    transport = Recorder()
    object_stripe.api_request(config(api_base="https://api.stripe.test/"), "POST",
                              "/v1/charges", {}, transport=transport)
    assert transport.last["url"] == "https://api.stripe.test/v1/charges"


def test_default_transport_refuses_a_bad_url_before_any_socket():
    """Exercises the real default transport path far enough to prove the
    URL gate runs first; no connection is ever attempted."""
    with pytest.raises(object_stripe.StripeError):
        object_stripe.api_request(config(api_base="http://evil.test"), "POST",
                                  "/v1/charges", {})


# --- create_checkout_session ------------------------------------------------

CHECKOUT = {
    "amount_cents": 12500,
    "currency": "usd",
    "description": "Invoice INV-1042",
    "success_url": "https://pay.example.test/done?s={CHECKOUT_SESSION_ID}",
    "cancel_url": "https://pay.example.test/cancel",
    "metadata": {"invoice_id": "inv_1042", "portal_token": "tok_abc"},
}


def test_checkout_session_encodes_stripes_exact_key_syntax():
    transport = Recorder()
    object_stripe.create_checkout_session(config(), transport=transport, **CHECKOUT)
    body = transport.last["data"].decode()
    pairs = dict(urllib.parse.parse_qsl(body))

    assert pairs["mode"] == "payment"
    assert pairs["line_items[0][quantity]"] == "1"
    assert pairs["line_items[0][price_data][currency]"] == "usd"
    assert pairs["line_items[0][price_data][unit_amount]"] == "12500"
    assert pairs["line_items[0][price_data][product_data][name]"] == "Invoice INV-1042"
    assert pairs["success_url"] == CHECKOUT["success_url"]
    assert pairs["cancel_url"] == CHECKOUT["cancel_url"]

    # and the same thing asserted on the raw wire bytes, brackets encoded
    assert "line_items%5B0%5D%5Bprice_data%5D%5Bcurrency%5D=usd" in body
    assert transport.last["url"] == "https://api.stripe.test/v1/checkout/sessions"
    assert transport.last["method"] == "POST"
    assert "Idempotency-Key" in transport.last["headers"]


def test_checkout_metadata_survives_on_both_the_session_and_the_intent():
    """Session metadata does not reach the PaymentIntent or the charge, so
    a handler for payment_intent.succeeded would have no invoice_id to
    credit. Mirroring is required, not decorative."""
    transport = Recorder()
    object_stripe.create_checkout_session(config(), transport=transport, **CHECKOUT)
    pairs = dict(urllib.parse.parse_qsl(transport.last["data"].decode()))
    assert pairs["metadata[invoice_id]"] == "inv_1042"
    assert pairs["metadata[portal_token]"] == "tok_abc"
    assert pairs["payment_intent_data[metadata][invoice_id]"] == "inv_1042"
    assert pairs["payment_intent_data[metadata][portal_token]"] == "tok_abc"


def test_checkout_with_no_metadata_sends_no_metadata_keys():
    transport = Recorder()
    object_stripe.create_checkout_session(config(), transport=transport,
                                          **{**CHECKOUT, "metadata": {}})
    body = transport.last["data"].decode()
    assert "metadata" not in urllib.parse.unquote_plus(body)


def test_checkout_returns_the_session():
    transport = Recorder(body=b'{"id":"cs_test_9","url":"https://checkout.stripe.test/c/9"}')
    session = object_stripe.create_checkout_session(config(), transport=transport, **CHECKOUT)
    assert session["id"] == "cs_test_9"
    assert session["url"].startswith("https://")


@pytest.mark.parametrize("amount", [
    0, -1, -12500, 12.50, "12500", None, True, False, Decimal("125"),
    object_stripe.MAX_AMOUNT_CENTS + 1,
])
def test_bad_amounts_are_refused_before_any_transport_call(amount):
    transport = Recorder()
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.create_checkout_session(config(), transport=transport,
                                              **{**CHECKOUT, "amount_cents": amount})
    assert transport.calls == []


def test_boolean_amount_would_have_charged_one_cent():
    """bool is an int worth 1; without an explicit bool guard True is a
    valid, positive, one-cent charge."""
    with pytest.raises(object_stripe.StripePayloadError) as exc:
        object_stripe.create_checkout_session(config(), transport=Recorder(),
                                              **{**CHECKOUT, "amount_cents": True})
    assert "bool" in str(exc.value)


@pytest.mark.parametrize("currency", ["", "us", "usdd", "u$d", "12 ", None, "ｕｓｄ", "us d"])
def test_bad_currencies_are_refused_before_any_transport_call(currency):
    transport = Recorder()
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.create_checkout_session(config(), transport=transport,
                                              **{**CHECKOUT, "currency": currency})
    assert transport.calls == []


def test_currency_case_is_normalised():
    transport = Recorder()
    object_stripe.create_checkout_session(config(), transport=transport,
                                          **{**CHECKOUT, "currency": " EUR "})
    pairs = dict(urllib.parse.parse_qsl(transport.last["data"].decode()))
    assert pairs["line_items[0][price_data][currency]"] == "eur"


@pytest.mark.parametrize("field", ["success_url", "cancel_url"])
@pytest.mark.parametrize("url", [
    "", None, "javascript:alert(1)", "data:text/html,<script>", "/relative/path",
    "pay.example.test/done", "https://pay.example.test/a\nb", "ftp://pay.example.test",
])
def test_bad_return_urls_are_refused(field, url):
    transport = Recorder()
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.create_checkout_session(config(), transport=transport,
                                              **{**CHECKOUT, field: url})
    assert transport.calls == []


@pytest.mark.parametrize("description", ["", "   ", None, "x" * 501])
def test_bad_descriptions_are_refused(description):
    transport = Recorder()
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.create_checkout_session(config(), transport=transport,
                                              **{**CHECKOUT, "description": description})
    assert transport.calls == []


@pytest.mark.parametrize("metadata", [
    "invoice_id=1", ["invoice_id"], {"": "1"}, {"k" * 41: "1"},
    {"invoice_id": "v" * 501}, {f"k{i}": "1" for i in range(51)},
    {"invoice_id": 1.5}, {"inv[oice]": "1"},
])
def test_bad_metadata_is_refused_before_any_transport_call(metadata):
    transport = Recorder()
    with pytest.raises(object_stripe.StripePayloadError):
        object_stripe.create_checkout_session(config(), transport=transport,
                                              **{**CHECKOUT, "metadata": metadata})
    assert transport.calls == []


def test_metadata_scalars_are_stringified_and_nones_dropped():
    transport = Recorder()
    object_stripe.create_checkout_session(
        config(), transport=transport,
        **{**CHECKOUT, "metadata": {"n": 7, "flag": True, "gone": None}})
    pairs = dict(urllib.parse.parse_qsl(transport.last["data"].decode()))
    assert pairs["metadata[n]"] == "7"
    assert pairs["metadata[flag]"] == "true"
    assert "metadata[gone]" not in pairs


def test_checkout_refuses_when_stripe_is_unconfigured():
    transport = Recorder()
    with pytest.raises(object_stripe.StripeError):
        object_stripe.create_checkout_session(object_stripe.StripeConfig(),
                                              transport=transport, **CHECKOUT)
    assert transport.calls == []


# --- configuration ----------------------------------------------------------

def test_config_defaults_are_dormant():
    cfg = object_stripe.stripe_config_from_env({})
    assert cfg.secret_key == ""
    assert cfg.webhook_secret == ""
    assert cfg.api_base == "https://api.stripe.com"
    assert cfg.api_version == ""
    assert cfg.configured is False


def test_config_reads_the_env():
    cfg = object_stripe.stripe_config_from_env({
        "DBBASIC_STRIPE_SECRET_KEY": "sk_live_abc123456789",
        "DBBASIC_STRIPE_WEBHOOK_SECRET": "whsec_def123456789",
        "DBBASIC_STRIPE_API_BASE": "https://api.stripe.test",
        "DBBASIC_STRIPE_API_VERSION": "2026-01-01",
    })
    assert cfg.secret_key == "sk_live_abc123456789"
    assert cfg.webhook_secret == "whsec_def123456789"
    assert cfg.api_base == "https://api.stripe.test"
    assert cfg.api_version == "2026-01-01"
    assert cfg.configured is True


@pytest.mark.parametrize("env", [
    {"DBBASIC_STRIPE_SECRET_KEY": "sk_live_abc123456789"},
    {"DBBASIC_STRIPE_WEBHOOK_SECRET": "whsec_def123456789"},
    {"DBBASIC_STRIPE_SECRET_KEY": "  ", "DBBASIC_STRIPE_WEBHOOK_SECRET": "whsec_x1234567"},
    {},
])
def test_either_secret_missing_means_unconfigured(env):
    assert object_stripe.stripe_config_from_env(env).configured is False


def test_env_values_are_stripped():
    """A trailing newline in an env file is the classic cause of an
    invalid-signature storm on an otherwise correct secret."""
    cfg = object_stripe.stripe_config_from_env({
        "DBBASIC_STRIPE_SECRET_KEY": " sk_live_abc123456789\n",
        "DBBASIC_STRIPE_WEBHOOK_SECRET": "whsec_def123456789 ",
    })
    assert cfg.secret_key == "sk_live_abc123456789"
    assert cfg.webhook_secret == "whsec_def123456789"


def test_blank_api_base_falls_back_to_stripe():
    cfg = object_stripe.stripe_config_from_env({"DBBASIC_STRIPE_API_BASE": "   "})
    assert cfg.api_base == "https://api.stripe.com"


def test_repr_masks_both_secrets():
    secret_key = "sk_live_51SUPERSECRETMATERIAL"
    webhook_secret = "whsec_ANOTHERSECRETVALUE"
    cfg = object_stripe.StripeConfig(secret_key=secret_key, webhook_secret=webhook_secret)

    for rendering in (repr(cfg), str(cfg), f"{cfg}", "%s" % (cfg,), format(cfg)):
        assert secret_key not in rendering
        assert webhook_secret not in rendering
        assert "SUPERSECRETMATERIAL" not in rendering
        assert "ANOTHERSECRETVALUE" not in rendering
        assert "sk_live_***" in rendering
        assert "whsec_***" in rendering
        assert "configured=True" in rendering


def test_repr_of_an_unknown_key_shape_says_only_set():
    cfg = object_stripe.StripeConfig(secret_key="totally-custom-key-value")
    assert "totally-custom-key-value" not in repr(cfg)
    assert "secret_key=set" in repr(cfg)


def test_repr_of_a_short_prefix_only_secret_does_not_echo_it():
    cfg = object_stripe.StripeConfig(secret_key="sk_live_")
    assert "secret_key=set" in repr(cfg)


def test_repr_of_an_unconfigured_config_says_unset():
    text = repr(object_stripe.StripeConfig())
    assert "secret_key=unset" in text
    assert "webhook_secret=unset" in text
    assert "configured=False" in text


def test_config_is_not_a_tuple_that_could_be_unpacked_into_a_log():
    """A namedtuple would satisfy the same API while still dumping both
    secrets through str(tuple(cfg)) or print(*cfg) -- doors no __repr__
    override can close."""
    cfg = object_stripe.StripeConfig(secret_key="sk_live_51SUPERSECRET", webhook_secret=SECRET)
    assert not isinstance(cfg, tuple)
    with pytest.raises(TypeError):
        list(cfg)
    assert not hasattr(cfg, "__dict__")


def test_config_exposes_no_secret_dumping_helpers():
    cfg = object_stripe.StripeConfig(secret_key="sk_live_51SUPERSECRET")
    extras = [name for name in dir(cfg)
              if not name.startswith("_")
              and name not in {"secret_key", "webhook_secret", "api_base",
                               "api_version", "configured"}]
    assert extras == []


# --- module posture ---------------------------------------------------------

def test_module_never_logs():
    """Secrets and payloads must not be able to reach a log from here.
    The module makes no logging calls at all; a caller decides what to
    record, and the reasons/messages it is given are already safe."""
    source = inspect.getsource(object_stripe)
    for banned in ("import logging", "logging.", "print("):
        assert banned not in source


def test_module_imports_no_third_party():
    source = inspect.getsource(object_stripe)
    for banned in ("import stripe", "import requests", "from stripe", "from requests"):
        assert banned not in source
