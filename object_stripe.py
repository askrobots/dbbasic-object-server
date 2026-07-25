"""Stripe primitives: webhook signature verification and the API call.

Stripe is **a payment method, never the system of record**
(plan/billing-metering-spec.md section 6). We already own invoices,
payments, dunning and the books; Stripe's only jobs are to collect a card
and to tell us it happened. So this module is deliberately small and
deliberately dumb: it verifies that an inbound webhook really came from
Stripe, it turns a request into a form-encoded HTTPS call, and it hands
back plain dicts. It writes no records, reads no collections, registers no
object, and knows nothing about invoices. The webhook endpoint and the
actions that call these primitives live elsewhere.

stdlib only -- ``hmac``/``hashlib`` for the signature, ``urllib`` for the
call. No ``stripe`` SDK, no ``requests``. A money path is exactly the wrong
place to inherit a dependency's transitive attack surface, and Stripe's
wire protocol is form-encoded HTTP with a bearer token: there is nothing
here an SDK would do better.

**Custody.** Both secrets are deploy-time, server-wide credentials and
live in the operator's env file (docs/secrets-and-credentials.md #1):
``DBBASIC_STRIPE_SECRET_KEY`` and ``DBBASIC_STRIPE_WEBHOOK_SECRET``. They
are never a record field, never in a backup, and -- enforced here -- never
in a repr, a log line, or an exception message. ``StripeConfig.__repr__``
masks, and every message that could carry a Stripe response is passed
through ``_redact()`` before it reaches an exception. The module makes no
logging calls of its own at all; what an operator sees is what a caller
chose to print, and none of it contains key material.

**Dormant unless configured**, the same posture as ``smtp_config_from_env``:
an unconfigured deployment builds a config object fine, reports
``configured == False``, and refuses to call out or to verify anything.
The refusal on the verify side matters more than it looks -- see the
"empty secret" note on :func:`verify_webhook_signature`.

Four decisions here diverge from the obvious reading of Stripe's docs, and
each one is a bug in something someone shipped:

1. **The signature is computed over raw bytes, never a decoded string.**
   The documented signed payload is ``"{t}.{body}"``, which invites
   ``payload.decode()``. Decoding is at best a no-op and at worst a
   ``UnicodeDecodeError`` on a hand-crafted body -- i.e. a webhook endpoint
   that 500s on garbage, which is an invitation to probe. We concatenate
   ``t`` + ``b"."`` + the raw bytes and never decode. A ``str`` payload is
   refused outright rather than re-encoded, because a body that has been
   through a JSON round trip is not the body Stripe signed.
2. **An empty webhook secret can never verify.** HMAC with an empty key is
   a perfectly well-defined function that *anyone* can compute, so an
   unconfigured deployment that "verifies" against ``""`` accepts forged
   events from the whole internet. Missing secret is an explicit refusal.
3. **The default transport never follows redirects.** urllib's redirect
   handler replays the original headers at the new location, which would
   walk the ``Authorization: Bearer sk_live_...`` header off to whatever
   host answered. Any 3xx is an error here, not a hop.
4. **Checkout metadata is mirrored onto the PaymentIntent.** Session
   metadata does not propagate to the payment_intent/charge, so a handler
   watching ``payment_intent.succeeded`` sees no ``invoice_id`` and the
   payment cannot be tied back. ``payment_intent_data[metadata]`` carries
   the same dict so the round trip survives whichever event you settle on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Callable

DEFAULT_API_BASE = "https://api.stripe.com"

#: Stripe's own replay window, and the one their libraries default to.
DEFAULT_TOLERANCE_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 10

MAX_PAYLOAD_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000

# Bounds on attacker-supplied shapes. None of these are Stripe limits --
# they are the point past which an input stops being a webhook and starts
# being a resource-exhaustion attempt. A real Stripe-Signature header with
# two rotating secrets is under 200 characters.
MAX_SIGNATURE_HEADER_CHARS = 2048
MAX_SIGNATURES = 16
MAX_TIMESTAMP_CHARS = 20

MAX_PARAM_DEPTH = 8
MAX_URL_CHARS = 2048

# Stripe's documented ceilings, checked locally so a bad amount fails at
# the call site (with a message naming the field) instead of coming back
# as an opaque API error after a round trip.
MAX_AMOUNT_CENTS = 99_999_999
MAX_DESCRIPTION_CHARS = 500
MAX_METADATA_KEYS = 50
MAX_METADATA_KEY_CHARS = 40
MAX_METADATA_VALUE_CHARS = 500
MAX_EVENT_ID_CHARS = 255

ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})

EVENT_MARKER_PREFIX = "stripe_evt/"

USER_AGENT = "dbbasic-object-server (stdlib urllib)"

# The only hosts we will talk to over plaintext http: a mock Stripe on the
# loopback interface during a test or a local dev run. Everything else must
# be https, because the request carries a live secret key in a header.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_ASCII_DIGITS = frozenset("0123456789")
_EVENT_ID_CHARS = frozenset(string.ascii_letters + string.digits + "_-")

# Prefixes safe to show in a repr: they identify which *kind* of key is
# loaded (and, crucially, whether a live key is sitting in a staging box)
# without revealing a single character of the secret material after them.
_KEY_PREFIXES = ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "whsec_")


class StripeError(Exception):
    """A Stripe call that did not produce a usable result.

    Carries Stripe's own error taxonomy when it has one (``type``,
    ``code``, ``param``) plus the HTTP ``status``. ``message`` is safe to
    put in front of an operator: it is either our own text or Stripe's,
    and both are run through :func:`_redact` so no key material can ride
    along in an error string.
    """

    def __init__(self, message: str, *, error_type: str = "", code: str = "",
                 status: int | None = None, param: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.type = error_type
        self.code = code
        self.status = status
        self.param = param


class StripePayloadError(StripeError):
    """Bytes we were handed, or handed back, that cannot be used as-is:
    oversized, not JSON, not an object, or a parameter we refuse to send."""


# --- configuration ----------------------------------------------------------

class StripeConfig:
    """Deploy-time Stripe configuration, read from ``DBBASIC_STRIPE_*``.

    A plain class with ``__slots__`` rather than a dataclass or a
    namedtuple, and that is a security choice, not a style one. A
    dataclass generates a ``__repr__`` that prints every field; a
    namedtuple additionally *is* a tuple, so ``str(tuple(config))``,
    passing the config to any stdout write and any ``%``-formatting of the whole object dump
    both secrets in cleartext into whatever log caught them -- and no
    override of ``__repr__`` closes those doors. Here the only rendering
    of the object is the masked one below.
    """

    __slots__ = ("secret_key", "webhook_secret", "api_base", "api_version")

    def __init__(self, *, secret_key: str = "", webhook_secret: str = "",
                 api_base: str = DEFAULT_API_BASE, api_version: str = "") -> None:
        self.secret_key = secret_key or ""
        self.webhook_secret = webhook_secret or ""
        self.api_base = api_base or DEFAULT_API_BASE
        self.api_version = api_version or ""

    @property
    def configured(self) -> bool:
        """Both halves present: a key to call with and a secret to verify
        with. Either one alone is a half-wired integration -- it would
        take money without being able to hear that it did, or the
        reverse -- so neither counts as configured."""
        return bool(self.secret_key and self.webhook_secret)

    def __repr__(self) -> str:
        return (f"StripeConfig(api_base={self.api_base!r}, "
                f"api_version={self.api_version!r}, "
                f"secret_key={_mask(self.secret_key)}, "
                f"webhook_secret={_mask(self.webhook_secret)}, "
                f"configured={self.configured})")

    __str__ = __repr__


def _mask(value: str) -> str:
    """"unset", "set", or the key's non-secret kind prefix.

    ``sk_live_***`` tells an operator the thing they actually need to know
    at a glance -- test key or live key -- and nothing else. The prefix is
    only shown when there is material behind it to hide, so a truncated
    secret that *is* just the prefix never gets echoed back whole.
    """
    text = str(value or "")
    if not text:
        return "unset"
    for prefix in _KEY_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix) + 4:
            return prefix + "***"
    return "set"


def stripe_config_from_env(env: Mapping[str, str] | None = None) -> StripeConfig:
    """Read the operator's env into a config. Never raises, never logs.

    ``api_base`` is overridable so a test or a local mock server is a
    matter of configuration rather than of monkeypatching this module's
    internals -- the internals are exactly what a test of a money path
    should not be reaching into.

    ``api_version`` is optional and unset by default (Stripe then uses the
    account's pinned version). Setting ``DBBASIC_STRIPE_API_VERSION``
    pins it per-request instead, which is the safer posture once the
    integration is live: an account-level version bump changes response
    shapes underneath running code.

    Values are stripped, because a trailing newline in an env file is the
    classic reason a correct secret produces an invalid-signature storm.
    """
    env = os.environ if env is None else env
    return StripeConfig(
        secret_key=(env.get("DBBASIC_STRIPE_SECRET_KEY") or "").strip(),
        webhook_secret=(env.get("DBBASIC_STRIPE_WEBHOOK_SECRET") or "").strip(),
        api_base=(env.get("DBBASIC_STRIPE_API_BASE") or "").strip() or DEFAULT_API_BASE,
        api_version=(env.get("DBBASIC_STRIPE_API_VERSION") or "").strip(),
    )


def _redact(text: Any, config: Any) -> str:
    """Blank any configured secret out of a string bound for an exception.

    Belt and braces: nothing here deliberately puts a key in a message,
    but the message may quote a response body or an OSError from a layer
    that did. Short values are left alone -- redacting a two-character
    "secret" would black out half the sentence for no benefit.
    """
    out = str(text)
    for secret in (getattr(config, "secret_key", ""), getattr(config, "webhook_secret", "")):
        material = str(secret or "")
        if len(material) >= 8:
            out = out.replace(material, "***")
    return out


# --- webhook signatures -----------------------------------------------------
#
# The whole security of the inbound path is this function. A webhook
# endpoint is a public, unauthenticated URL that creates payment records;
# if the signature check is wrong, anyone who guesses the URL can credit
# themselves an invoice. Three properties are load-bearing:
#
#   * comparison is `hmac.compare_digest` and nothing else. A byte-at-a-
#     time `==` on a hex digest leaks the correct prefix through timing,
#     which is a forgery oracle, not a theoretical concern.
#   * the timestamp window is enforced, so a *genuine* signed event
#     captured off the wire cannot be replayed a week later.
#   * nothing raises. A 500 on malformed input tells a prober that their
#     input reached code, and distinguishes shapes of garbage from each
#     other; every path here returns a dict.


def _refused(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def _parse_signature_header(header: Any) -> tuple[str, list[str], str]:
    """``Stripe-Signature`` -> (t token, v1 candidates, refusal reason).

    Stripe emits ``t=1700000000,v1=abc...`` with no spaces. We strip
    whitespace around every element and around each key and value anyway:
    a proxy that reformats a header into ``t=1, v1=abc`` is a plausible
    thing to sit behind, and whitespace cannot change what gets verified
    (the HMAC input is the *token*, and the token is constrained to ASCII
    digits below). Leniency here costs nothing; leniency about the digest
    itself would cost everything, so there is none of that.

    Unknown schemes are ignored, which is how ``v0`` (Stripe's test-mode
    scheme, not a signature over the real payload) and anything Stripe
    adds later are handled -- ignored, never treated as a signature. A
    header carrying *only* v0 therefore reads as "no v1", which is a
    refusal.

    Two ``t`` values is a refusal rather than a pick-the-first: an
    ambiguous timestamp is exactly the shape of a request-smuggling
    attempt, and there is no correct answer to choose.
    """
    if not isinstance(header, str):
        return "", [], "signature header is missing"
    raw = header.strip()
    if not raw:
        return "", [], "signature header is missing"
    if len(raw) > MAX_SIGNATURE_HEADER_CHARS:
        return "", [], f"signature header is longer than {MAX_SIGNATURE_HEADER_CHARS} characters"

    timestamps: list[str] = []
    signatures: list[str] = []
    for element in raw.split(","):
        item = element.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not value:
            continue
        if name == "t":
            timestamps.append(value)
        elif name == "v1":
            signatures.append(value)

    if not timestamps:
        return "", [], "signature header has no t= timestamp"
    if len(timestamps) > 1:
        return "", [], "signature header carries more than one t= timestamp"
    if not signatures:
        return "", [], "signature header has no v1= signature"
    if len(signatures) > MAX_SIGNATURES:
        return "", [], f"signature header carries more than {MAX_SIGNATURES} v1 signatures"
    return timestamps[0], signatures, ""


def _timestamp_value(token: str) -> int | None:
    """The ``t`` token as an int, or None when it is not a plain integer.

    ``str.isdigit()`` is deliberately not used: it is True for Arabic-Indic
    and other Unicode digits, and ``int()`` happily parses them, so
    ``t=١٧٠٠`` would sail through a naive check. Only ASCII digits, and
    only a sane number of them -- a 5000-digit token is a CPU attack on
    ``int()`` before it is anything else.
    """
    if not token or len(token) > MAX_TIMESTAMP_CHARS:
        return None
    if not set(token) <= _ASCII_DIGITS:
        return None
    return int(token)


def verify_webhook_signature(payload_bytes: bytes, signature_header: str,
                             webhook_secret: str, *,
                             tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
                             now: int | None = None) -> dict:
    """Verify a ``Stripe-Signature`` header against the raw request body.

    Returns ``{"ok": True, "timestamp": <int>}`` or
    ``{"ok": False, "reason": <text>}``. **Never raises** and never returns
    a reason that echoes the expected digest or any part of the secret --
    the reasons are written for an operator reading a log, and every one of
    them describes only what the *caller* sent.

    ``payload_bytes`` must be the bytes read off the socket. Not the body
    a JSON round trip produced, not a decoded string: Stripe signed those
    exact bytes, and ``json.dumps(json.loads(body))`` is a different
    sequence of them. A ``str`` is refused rather than quietly encoded,
    because refusing surfaces the mistake at the first test while encoding
    hides it until a payload with an unusual escape shows up in production.

    ``now`` is injectable so the replay window is testable at its exact
    boundary; the window is symmetric (a timestamp that far in the future
    is refused too, allowing for ordinary clock skew in between).
    """
    try:
        if isinstance(payload_bytes, str):
            return _refused("payload must be the raw request bytes, not a decoded string")
        if not isinstance(payload_bytes, (bytes, bytearray, memoryview)):
            return _refused(f"payload must be bytes, got {type(payload_bytes).__name__}")
        body = bytes(payload_bytes)

        # An empty key is not "no verification", it is verification that
        # every attacker can also perform. Fail closed.
        material = webhook_secret if isinstance(webhook_secret, str) else ""
        if not material.strip():
            return _refused("webhook secret is not configured")

        token, offered_list, refusal = _parse_signature_header(signature_header)
        if refusal:
            return _refused(refusal)

        stamp = _timestamp_value(token)
        if stamp is None:
            return _refused("t= timestamp is not a plain unsigned integer")

        moment = int(time.time()) if now is None else int(now)
        window = int(tolerance_seconds)
        drift = moment - stamp
        if abs(drift) > window:
            direction = "old" if drift > 0 else "in the future"
            return _refused(
                f"timestamp is {abs(drift)}s {direction}, outside the {window}s tolerance")

        # The signed payload uses the timestamp token *as it arrived*, not
        # a reformatted int: Stripe hashed the characters it sent, so a
        # round trip through int() would break on any representation we
        # did not anticipate.
        expected = hmac.new(
            material.encode("utf-8"),
            token.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest().encode("ascii")

        for candidate in offered_list:
            try:
                offered = candidate.encode("ascii")
            except UnicodeEncodeError:
                # compare_digest refuses non-ASCII str; a non-ASCII digest
                # is not a digest, so skip rather than blow up.
                continue
            if hmac.compare_digest(offered, expected):
                return {"ok": True, "timestamp": stamp}

        return _refused("no v1 signature matched")
    except Exception as exc:                      # pragma: no cover - belt and braces
        # Nothing above should reach here. If something does, a webhook
        # endpoint still must not 500 and must not leak the exception text,
        # which could quote the payload back at whoever sent it.
        return _refused(f"signature could not be checked ({type(exc).__name__})")


def parse_event(payload_bytes: bytes, *, max_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """The verified body as an event dict.

    Call this *after* :func:`verify_webhook_signature`, never before:
    parsing untrusted JSON is cheap but acting on it is not, and the size
    cap here is the second line of defence, not the first (the endpoint
    should refuse an oversized body before it is ever read into memory).

    Raises :class:`StripePayloadError` for anything unusable -- over the
    cap, not UTF-8, not JSON, or a JSON value that is not an object. A
    top-level list or string is not an event no matter how well-formed it
    is, and the caller's ``event["type"]`` would otherwise raise something
    far less clear several frames later.
    """
    if isinstance(payload_bytes, str):
        raise StripePayloadError(
            "Event payload must be the raw request bytes, not a decoded string")
    if not isinstance(payload_bytes, (bytes, bytearray, memoryview)):
        raise StripePayloadError(
            f"Event payload must be bytes, got {type(payload_bytes).__name__}")
    body = bytes(payload_bytes)
    cap = int(max_bytes)
    if len(body) > cap:
        raise StripePayloadError(
            f"Event payload is {len(body)} bytes, over the {cap}-byte cap")
    if not body.strip():
        raise StripePayloadError("Event payload is empty")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StripePayloadError("Event payload is not valid UTF-8") from exc
    try:
        event = json.loads(text)
    except ValueError as exc:
        raise StripePayloadError(f"Event payload is not valid JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise StripePayloadError(
            f"Event payload is a JSON {type(event).__name__}, not an object")
    return event


def event_marker(event: Mapping[str, Any]) -> str:
    """``stripe_evt/<id>`` -- the ``generated_from`` idempotency stamp.

    This string is what makes webhook processing exactly-once without a
    ProcessedStripeEvent table: the payment (and the journal behind it) is
    stamped with it, and the composer already refuses to compose the same
    provenance twice (object_finance.compose_posted_journal). Stripe
    retries a webhook for days, so this is not an edge case -- it is the
    normal path.

    The id is validated hard, and the reason is this store: records are
    tab-separated rows. An id containing a tab or a newline would not
    merely look odd, it would split one record into two. Stripe ids are
    ``[A-Za-z0-9_]``; anything else is either an attack or a bug, and both
    should stop here rather than at the row writer.
    """
    if not isinstance(event, Mapping):
        raise StripePayloadError(
            f"Not a Stripe event object: {type(event).__name__}")
    ident = event.get("id")
    if not isinstance(ident, str) or not ident:
        raise StripePayloadError("Stripe event has no string id to stamp provenance with")
    if len(ident) > MAX_EVENT_ID_CHARS or not set(ident) <= _EVENT_ID_CHARS:
        raise StripePayloadError(f"Refusing an unusable Stripe event id: {ident[:32]!r}")
    return EVENT_MARKER_PREFIX + ident


# --- parameters -------------------------------------------------------------
#
# Stripe's API takes form-encoded parameters and expresses structure with
# brackets: nested objects become key[sub], arrays become key[0]. There is
# no JSON request body option for most endpoints, so this encoding *is*
# the API, and getting a bracket wrong means a silently-ignored parameter
# rather than an error -- a checkout session that quietly loses its
# metadata still succeeds, and the payment then cannot be tied to its
# invoice. Hence a separate, directly testable function.


def flatten_params(params: Any) -> list[tuple[str, str]]:
    """Nested params -> Stripe's flat bracketed (name, value) pairs.

    ``{"line_items": [{"price_data": {"currency": "usd"}}]}`` becomes
    ``[("line_items[0][price_data][currency]", "usd")]``.

    Rules, each of which has a reason:

    * ``None`` is **omitted entirely**. Stripe treats a present-but-empty
      parameter as "set this to empty", which is not the same request as
      not mentioning it.
    * ``bool`` before ``int``, because ``bool`` *is* an ``int`` in Python
      and ``str(True)`` is ``"True"``, which Stripe reads as a non-empty
      string -- i.e. truthy either way, so ``False`` would silently mean
      ``True``. Emitted as ``"true"``/``"false"``.
    * ``float`` is **refused**. Money never travels as a float in this
      codebase (object_money's opening paragraph), and a float that
      reached here is a bug worth failing on rather than rounding into a
      charge. ``Decimal`` and ``str`` are accepted for the rare genuinely
      fractional Stripe field.
    * Names may not contain ``[``, ``]``, ``&``, ``=`` or control
      characters: those are the structure of the encoding, and a metadata
      key that contained one could otherwise forge a sibling parameter.
    * Depth is capped, which also makes a self-referencing dict a clean
      error instead of a RecursionError.
    """
    return _flatten(params, "", 0)


def _flatten(value: Any, prefix: str, depth: int) -> list[tuple[str, str]]:
    if depth > MAX_PARAM_DEPTH:
        raise StripePayloadError(
            f"Parameters nest deeper than {MAX_PARAM_DEPTH} levels")
    if value is None:
        return []
    if isinstance(value, Mapping):
        pairs: list[tuple[str, str]] = []
        for key, item in value.items():
            name = str(key)
            if not name or any(ch in name for ch in "[]&=") or not name.isprintable():
                raise StripePayloadError(f"Unusable parameter name: {key!r}")
            child = f"{prefix}[{name}]" if prefix else name
            pairs.extend(_flatten(item, child, depth + 1))
        return pairs
    if isinstance(value, (list, tuple)):
        pairs = []
        for index, item in enumerate(value):
            pairs.extend(_flatten(item, f"{prefix}[{index}]", depth + 1))
        return pairs
    if not prefix:
        raise StripePayloadError(
            f"Top-level parameters must be a mapping, got {type(value).__name__}")
    return [(prefix, _scalar(value))]


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise StripePayloadError(
            "Refusing a float parameter: amounts are integer minor units and "
            "fractional fields must be passed as a string or Decimal")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return value
    raise StripePayloadError(f"Unsupported parameter type: {type(value).__name__}")


def encode_params(params: Any) -> str:
    """Flattened params as an ``application/x-www-form-urlencoded`` body.

    Brackets come out percent-encoded (``line_items%5B0%5D%5B...``), which
    is what Stripe's own client library sends and what the standard calls
    for; Stripe's docs show them literally only for readability. Encoding
    them removes any question about who is allowed to introduce structure:
    the answer is only :func:`flatten_params`, never a value.
    """
    return urllib.parse.urlencode(flatten_params(params))


# --- the API call -----------------------------------------------------------


def _api_url(api_base: str, path: str) -> str:
    """Join base and path, refusing every way this could point elsewhere.

    The request carries a live secret key in a header, so where it is
    pointed is a security question, not a plumbing one:

    * scheme must be http or https (no ``file:``, no ``gopher:``);
    * plaintext http is allowed only to loopback, for a mock server -- a
      secret key must never cross a network in the clear;
    * the path may not be an absolute or protocol-relative URL, so a
      caller-built path cannot redirect the call to another host;
    * no whitespace or control characters, which is request-line and
      header injection.
    """
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        raise StripeError("Stripe API base is not configured")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https"):
        raise StripeError(
            f"Refusing a non-HTTP Stripe API base: {parts.scheme or 'no scheme'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise StripeError("Stripe API base names no host")
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise StripeError(
            f"Refusing to send a Stripe secret key over plaintext http to {host}")

    tail = str(path or "").strip()
    if "://" in tail:
        raise StripeError(f"Refusing an absolute URL as an API path: {path!r}")
    if not tail.startswith("/"):
        tail = "/" + tail
    if tail.startswith("//"):
        raise StripeError(f"Refusing a protocol-relative API path: {path!r}")
    if "/.." in tail:
        raise StripeError(f"Refusing a traversing API path: {path!r}")
    if any(ch.isspace() or not ch.isprintable() for ch in tail):
        raise StripeError("Refusing an API path containing whitespace or control characters")

    url = base + tail
    if len(url) > MAX_URL_CHARS:
        raise StripeError(f"Stripe request URL is longer than {MAX_URL_CHARS} characters")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    urllib's default handler rebuilds the request at the new location and
    carries the original headers with it -- including
    ``Authorization: Bearer sk_live_...``. A redirect off api.stripe.com is
    therefore a credential-exfiltration primitive, and Stripe's API has no
    legitimate reason to emit one. Returning None makes urlopen raise
    HTTPError for the 3xx instead, which :func:`api_request` turns into a
    plain error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _default_transport(url: str, data: bytes | None, headers: Mapping[str, str],
                       method: str) -> tuple[int, bytes]:
    """urllib, with a timeout, no redirects, and a bounded read.

    An HTTP error status comes back as ``(status, body)`` rather than as an
    exception, because Stripe's error body is the useful part -- the
    ``code`` on a card decline is the thing a caller acts on. Only failures
    with no HTTP answer at all (timeout, DNS, refused connection) raise.
    """
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return int(response.status or 0), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            body = b""
        finally:
            exc.close()
        return int(exc.code), body
    except (socket.timeout, TimeoutError) as exc:
        raise StripeError(
            f"Stripe request timed out after {DEFAULT_TIMEOUT_SECONDS}s") from exc
    except urllib.error.URLError as exc:
        raise StripeError(f"Stripe request failed: {exc.reason}") from exc
    except OSError as exc:
        raise StripeError(f"Stripe request failed: {exc}") from exc


def api_request(config: StripeConfig, method: str, path: str,
                params: dict | None = None, *, idempotency_key: str | None = None,
                transport: Callable[..., tuple[int, bytes]] | None = None) -> dict:
    """One Stripe API call. Returns the parsed response object.

    ``transport(url, data, headers, method) -> (status, body_bytes)`` is
    injectable so that no test ever opens a socket, and so a caller can
    wrap the call (a circuit breaker, a recorder) without this module
    growing an opinion about either.

    **Idempotency is on by default for POSTs.** Stripe deduplicates
    retries by ``Idempotency-Key``; without one, a request that succeeded
    at Stripe but timed out on our side charges the customer twice when
    anyone retries it. A uuid4 is generated when the caller does not
    supply one, so the default is safe. But note the limit, because it is
    the sharp edge of this function: an auto-generated key protects only
    against a retry *inside* Stripe's infrastructure, not against a caller
    calling ``api_request`` twice. **A caller that retries must pass the
    same ``idempotency_key`` both times** -- ideally one derived from the
    thing being paid for (an invoice id), not from the attempt.

    Raises :class:`StripeError` on a Stripe error object or any non-2xx,
    and :class:`StripePayloadError` on a response that is not a JSON
    object. Every message is redacted before it leaves.
    """
    verb = str(method or "").strip().upper()
    if verb not in ALLOWED_METHODS:
        raise StripeError(f"Unsupported HTTP method for the Stripe API: {method!r}")

    key = str(getattr(config, "secret_key", "") or "").strip()
    if not key:
        # Checked before anything is built, so an unconfigured deployment
        # fails at the call site instead of sending an anonymous request.
        raise StripeError("Stripe secret key is not configured")

    url = _api_url(getattr(config, "api_base", "") or DEFAULT_API_BASE, path)
    encoded = encode_params(params or {})

    body: bytes | None = None
    if verb == "GET":
        if encoded:
            url = f"{url}?{encoded}"
    else:
        body = encoded.encode("ascii")

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    version = str(getattr(config, "api_version", "") or "").strip()
    if version:
        headers["Stripe-Version"] = version

    stamp = idempotency_key
    if stamp is None and verb == "POST":
        stamp = str(uuid.uuid4())
    if stamp:
        headers["Idempotency-Key"] = str(stamp)

    send = _default_transport if transport is None else transport
    try:
        status, raw = send(url, body, headers, verb)
    except StripeError:
        raise
    except Exception as exc:
        raise StripeError(_redact(f"Stripe transport failed: {exc}", config)) from exc

    return _parse_response(status, raw, config)


def _parse_response(status: Any, raw: Any, config: Any) -> dict:
    """Turn (status, bytes) into a dict or the right exception.

    Anything at or above 300 is an error, not just 4xx/5xx: the default
    transport refuses redirects on purpose, and a 3xx that arrived anyway
    means the call did not reach Stripe's API, so returning its body as if
    it were a resource would be the worst possible outcome. An ``error``
    object in the body is honoured regardless of status, because a
    response that describes an error is one whatever the header said.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        raise StripePayloadError(f"Transport returned a non-numeric status: {status!r}")
    if not isinstance(raw, (bytes, bytearray)):
        raise StripePayloadError(
            f"Transport returned {type(raw).__name__}, not response bytes")
    payload_bytes = bytes(raw)
    if len(payload_bytes) > MAX_RESPONSE_BYTES:
        raise StripePayloadError(
            f"Stripe response is over the {MAX_RESPONSE_BYTES}-byte cap")

    if not payload_bytes.strip():
        if code >= 300:
            raise StripeError(f"Stripe returned HTTP {code} with an empty body", status=code)
        return {}

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        snippet = payload_bytes[:200].decode("utf-8", "replace")
        raise StripePayloadError(_redact(
            f"Stripe returned HTTP {code} with a non-JSON body: {snippet!r}",
            config)) from exc
    if not isinstance(payload, dict):
        raise StripePayloadError(
            f"Stripe returned HTTP {code} with a JSON {type(payload).__name__}, "
            "not an object")

    error = payload.get("error")
    if isinstance(error, dict) or code >= 300:
        detail = error if isinstance(error, dict) else {}
        message = str(detail.get("message") or "").strip() or f"Stripe returned HTTP {code}"
        raise StripeError(
            _redact(message, config),
            error_type=str(detail.get("type") or ""),
            code=str(detail.get("code") or ""),
            status=code,
            param=str(detail.get("param") or ""),
        )
    return payload


# --- checkout ---------------------------------------------------------------


def _checked_amount(amount_cents: Any) -> int:
    """A positive whole number of minor units, and nothing else.

    ``bool`` is excluded explicitly: ``True`` is an ``int`` worth 1, so
    without this check ``amount_cents=True`` would charge one cent.
    """
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise StripePayloadError(
            "amount_cents must be a whole number of minor units, got "
            f"{type(amount_cents).__name__}")
    if amount_cents <= 0:
        raise StripePayloadError(f"amount_cents must be positive, got {amount_cents}")
    if amount_cents > MAX_AMOUNT_CENTS:
        raise StripePayloadError(
            f"amount_cents {amount_cents} is over Stripe's {MAX_AMOUNT_CENTS} limit")
    return amount_cents


def _checked_currency(currency: Any) -> str:
    """Stripe's lowercase three-letter code.

    Case is normalised rather than refused -- "USD" is unambiguous and a
    case mismatch has no security meaning -- but the shape is enforced,
    and ASCII-only, so a full-width "ｕｓｄ" cannot slip past ``isalpha()``.
    """
    code = str(currency or "").strip().lower()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise StripePayloadError(
            f"currency must be a three-letter code, got {currency!r}")
    return code


def _checked_return_url(field: str, url: Any) -> str:
    """An http(s) URL a browser will be sent to after payment.

    The scheme check is the point: these values end up as a redirect
    target, and ``javascript:`` or ``data:`` there is a script-injection
    hand-off dressed up as a config field. Control characters are refused
    for the same reason they are in a path.
    """
    text = str(url or "").strip()
    if not text:
        raise StripePayloadError(f"{field} is required")
    if len(text) > MAX_URL_CHARS:
        raise StripePayloadError(f"{field} is longer than {MAX_URL_CHARS} characters")
    if any(ch.isspace() or not ch.isprintable() for ch in text):
        raise StripePayloadError(
            f"{field} contains whitespace or control characters")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise StripePayloadError(
            f"{field} must be an http(s) URL, got {text[:64]!r}")
    return text


def _checked_metadata(metadata: Any) -> dict:
    """Stripe's metadata limits, enforced locally.

    Metadata is how ``invoice_id`` and the portal token survive the round
    trip, so a silently-dropped key here is a payment that cannot be
    matched to what it paid for. Failing loudly before the call beats
    discovering it in a webhook handler that has no invoice to credit.
    """
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise StripePayloadError(
            f"metadata must be a mapping, got {type(metadata).__name__}")
    if len(metadata) > MAX_METADATA_KEYS:
        raise StripePayloadError(
            f"metadata has {len(metadata)} keys, over Stripe's {MAX_METADATA_KEYS} limit")
    out: dict[str, str] = {}
    for key, value in metadata.items():
        name = str(key).strip()
        if not name or len(name) > MAX_METADATA_KEY_CHARS:
            raise StripePayloadError(f"Unusable metadata key: {key!r}")
        if value is None:
            continue
        text = _scalar(value)
        if len(text) > MAX_METADATA_VALUE_CHARS:
            raise StripePayloadError(
                f"metadata[{name}] is {len(text)} characters, over Stripe's "
                f"{MAX_METADATA_VALUE_CHARS} limit")
        out[name] = text
    return out


def create_checkout_session(config: StripeConfig, *, amount_cents: int, currency: str,
                            description: str, success_url: str, cancel_url: str,
                            metadata: dict,
                            transport: Callable[..., tuple[int, bytes]] | None = None) -> dict:
    """A one-off ``mode=payment`` Checkout Session for a single amount.

    One inline ``price_data`` line item rather than a Stripe Price object:
    the price book lives here (billing_plans), and creating a mirrored
    Price in Stripe for every invoice is the first step towards Stripe
    becoming a second, divergent system of record.

    ``metadata`` is set on **both** the session and the PaymentIntent. This
    is not belt-and-braces, it is required: session metadata does not
    propagate down to the payment_intent or the charge, so a handler for
    ``payment_intent.succeeded`` (or a payout reconciliation months later
    reading the charge) would find nothing tying the money to an invoice.
    Mirroring it means the identifier survives whichever object the
    downstream code happens to be holding.

    Every argument is validated before the transport is touched, so a bad
    amount or currency costs nothing and cannot half-create anything.
    """
    amount = _checked_amount(amount_cents)
    code = _checked_currency(currency)

    name = str(description or "").strip()
    if not name:
        raise StripePayloadError("description is required (it is the line item's name)")
    if len(name) > MAX_DESCRIPTION_CHARS:
        raise StripePayloadError(
            f"description is longer than {MAX_DESCRIPTION_CHARS} characters")

    success = _checked_return_url("success_url", success_url)
    cancel = _checked_return_url("cancel_url", cancel_url)
    tags = _checked_metadata(metadata)

    params: dict[str, Any] = {
        "mode": "payment",
        "success_url": success,
        "cancel_url": cancel,
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": code,
                "unit_amount": amount,
                "product_data": {"name": name},
            },
        }],
    }
    if tags:
        params["metadata"] = tags
        params["payment_intent_data"] = {"metadata": tags}

    return api_request(config, "POST", "/v1/checkout/sessions", params, transport=transport)
