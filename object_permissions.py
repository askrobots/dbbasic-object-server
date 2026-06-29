"""Permission primitives for DBBASIC objects and collections.

The object server owns authorization. Scroll can edit and preview permissions,
but clients should not be trusted to enforce them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from object_namespace import parse_user_object_id

PUBLIC_USER_ID = "public"

READ = "read"
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
EXECUTE = "execute"
SOURCE = "source"
STATE = "state"
LOGS = "logs"
FILES = "files"
VERSIONS = "versions"
ADMIN = "admin"
SHARE = "share"

PUBLIC_READ_ACTIONS = frozenset({READ, EXECUTE})
OWNER_ACTIONS = frozenset({READ, UPDATE, DELETE, EXECUTE, SOURCE, STATE, LOGS, FILES, VERSIONS, SHARE})
ADMIN_ACTIONS = frozenset(
    {READ, CREATE, UPDATE, DELETE, EXECUTE, SOURCE, STATE, LOGS, FILES, VERSIONS, ADMIN, SHARE}
)


@dataclass(frozen=True)
class PermissionSubject:
    """The authenticated actor being checked."""

    user_id: str | None = None
    account_id: str | None = None
    roles: tuple[str, ...] = ()
    subscriptions: tuple[str, ...] = ()

    @classmethod
    def anonymous(cls) -> "PermissionSubject":
        return cls()

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None


@dataclass(frozen=True)
class PermissionRule:
    """One allow/deny rule.

    Principal values intentionally stay stringly and portable:
    ``public``, ``registered``, ``owner``, ``role:admin``, ``user:42``,
    ``account:acme``, or ``subscription:pro``.
    """

    effect: str
    actions: frozenset[str]
    principal: str
    collection: str | None = None
    object_id: str | None = None
    row_filter: Mapping[str, Any] = field(default_factory=dict)
    fields: frozenset[str] | None = None
    denied_fields: frozenset[str] = field(default_factory=frozenset)
    valid_from: str | None = None
    expires_at: str | None = None
    reason: str = ""

    @classmethod
    def allow(
        cls,
        principal: str,
        actions: Iterable[str],
        *,
        collection: str | None = None,
        object_id: str | None = None,
        row_filter: Mapping[str, Any] | None = None,
        fields: Iterable[str] | None = None,
        denied_fields: Iterable[str] = (),
        valid_from: str | None = None,
        expires_at: str | None = None,
        reason: str = "",
    ) -> "PermissionRule":
        return cls(
            effect="allow",
            principal=principal,
            actions=frozenset(actions),
            collection=collection,
            object_id=object_id,
            row_filter=dict(row_filter or {}),
            fields=frozenset(fields) if fields is not None else None,
            denied_fields=frozenset(denied_fields),
            valid_from=valid_from,
            expires_at=expires_at,
            reason=reason,
        )

    @classmethod
    def deny(
        cls,
        principal: str,
        actions: Iterable[str],
        *,
        collection: str | None = None,
        object_id: str | None = None,
        row_filter: Mapping[str, Any] | None = None,
        valid_from: str | None = None,
        expires_at: str | None = None,
        reason: str = "",
    ) -> "PermissionRule":
        return cls(
            effect="deny",
            principal=principal,
            actions=frozenset(actions),
            collection=collection,
            object_id=object_id,
            row_filter=dict(row_filter or {}),
            valid_from=valid_from,
            expires_at=expires_at,
            reason=reason,
        )


@dataclass(frozen=True)
class PermissionPolicy:
    """Portable server-side permission state.

    ``roles`` names broad roles such as ``admin`` or ``sales``. ``user_roles``
    assigns users to roles. ``rules`` contains grants and explicit denies.
    """

    access_mode: str = "role_based"
    roles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    user_roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    rules: tuple[PermissionRule, ...] = ()
    admin_roles: tuple[str, ...] = ("admin", "superuser")


@dataclass(frozen=True)
class PermissionDecision:
    """The result Scroll and the server can explain."""

    allowed: bool
    reason: str
    code: str
    http_status: int
    row_filter: Mapping[str, Any] = field(default_factory=dict)
    fields: frozenset[str] | None = None
    denied_fields: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def allow(
        cls,
        reason: str,
        *,
        row_filter: Mapping[str, Any] | None = None,
        fields: Iterable[str] | None = None,
        denied_fields: Iterable[str] = (),
    ) -> "PermissionDecision":
        return cls(
            True,
            reason,
            "allowed",
            200,
            row_filter=dict(row_filter or {}),
            fields=frozenset(fields) if fields is not None else None,
            denied_fields=frozenset(denied_fields),
        )

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        code: str = "forbidden",
        http_status: int = 403,
    ) -> "PermissionDecision":
        return cls(False, reason, code, http_status)


def check_permission(
    subject: PermissionSubject | None,
    action: str,
    *,
    policy: PermissionPolicy | None = None,
    collection: str | None = None,
    object_id: str | None = None,
    record: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> PermissionDecision:
    """Return allow/deny/reason for one action.

    If a matching allow rule has a row filter and no record was supplied, the
    decision is allowed with the filter attached so list/query code can apply it.
    """

    actor = subject or PermissionSubject.anonymous()
    normalized_action = action.lower()
    explicit_policy = policy is not None
    active_policy = policy or PermissionPolicy(access_mode="public")
    roles = _subject_roles(actor, active_policy)
    checked_at = now or datetime.now(timezone.utc)

    if _has_admin_role(roles, active_policy):
        return PermissionDecision.allow("admin role")

    explicit_deny = _matching_rule(
        active_policy.rules,
        actor,
        roles,
        normalized_action,
        collection=collection,
        object_id=object_id,
        record=record,
        effect="deny",
        now=checked_at,
    )
    if explicit_deny is not None:
        return PermissionDecision.deny(explicit_deny.reason or "explicit deny")

    mode_decision = _access_mode_decision(
        actor,
        active_policy.access_mode,
        normalized_action,
        collection=collection,
        object_id=object_id,
    )
    if mode_decision.allowed:
        return mode_decision

    allow_rule = _matching_rule(
        active_policy.rules,
        actor,
        roles,
        normalized_action,
        collection=collection,
        object_id=object_id,
        record=record,
        effect="allow",
        now=checked_at,
    )
    if allow_rule is not None:
        return PermissionDecision.allow(
            allow_rule.reason or f"{allow_rule.principal} rule",
            row_filter=allow_rule.row_filter,
            fields=allow_rule.fields,
            denied_fields=allow_rule.denied_fields,
        )

    fallback = _owner_fallback(
        actor,
        normalized_action,
        object_id=object_id,
        record=record,
        allow_system_public=not explicit_policy or active_policy.access_mode == "public",
    )
    if fallback.allowed:
        return fallback

    return mode_decision


def _access_mode_decision(
    subject: PermissionSubject,
    access_mode: str,
    action: str,
    *,
    collection: str | None,
    object_id: str | None,
) -> PermissionDecision:
    mode = access_mode.lower()

    if mode == "public":
        if action in PUBLIC_READ_ACTIONS:
            return PermissionDecision.allow("public access")
        return PermissionDecision.deny("public access is read-only")

    if mode == "registered":
        if subject.is_authenticated and action in PUBLIC_READ_ACTIONS:
            return PermissionDecision.allow("registered access")
        return PermissionDecision.deny(
            "registered user required",
            code="authentication_required",
            http_status=401,
        )

    if mode == "subscription":
        if subject.subscriptions and action in PUBLIC_READ_ACTIONS:
            return PermissionDecision.allow("subscription access")
        if subject.subscriptions:
            return PermissionDecision.deny("subscription access is read-only")
        return PermissionDecision.deny(
            "subscription required",
            code="payment_required",
            http_status=402,
        )

    if mode == "password":
        return PermissionDecision.deny(
            "password gate must authenticate before policy check",
            code="authentication_required",
            http_status=401,
        )

    if mode == "role_based":
        return PermissionDecision.deny("no matching role rule")

    if mode == "private":
        return _owner_fallback(
            subject,
            action,
            object_id=object_id,
            record=None,
            allow_system_public=False,
        )

    return PermissionDecision.deny(f"unknown access mode: {access_mode}")


def _matching_rule(
    rules: Iterable[PermissionRule],
    subject: PermissionSubject,
    roles: frozenset[str],
    action: str,
    *,
    collection: str | None,
    object_id: str | None,
    record: Mapping[str, Any] | None,
    effect: str,
    now: datetime,
) -> PermissionRule | None:
    for rule in rules:
        if rule.effect != effect:
            continue
        if not _rule_is_active(rule, now):
            continue
        if not _action_matches(rule, action):
            continue
        if not _resource_matches(rule, collection=collection, object_id=object_id):
            continue
        if not _principal_matches(rule.principal, subject, roles, record=record):
            continue
        if record is not None and rule.row_filter and not _record_matches_filter(record, rule.row_filter, subject):
            continue
        return rule
    return None


def _rule_is_active(rule: PermissionRule, now: datetime) -> bool:
    checked_at = _normalize_datetime(now)

    if rule.valid_from is not None:
        valid_from = _parse_timestamp(rule.valid_from)
        if valid_from is None or checked_at < valid_from:
            return False

    if rule.expires_at is not None:
        expires_at = _parse_timestamp(rule.expires_at)
        if expires_at is None or checked_at >= expires_at:
            return False

    return True


def _action_matches(rule: PermissionRule, action: str) -> bool:
    return "*" in rule.actions or action in rule.actions


def _resource_matches(
    rule: PermissionRule,
    *,
    collection: str | None,
    object_id: str | None,
) -> bool:
    if rule.collection is not None and rule.collection != collection:
        return False
    if rule.object_id is not None and rule.object_id != object_id:
        return False
    return True


def _principal_matches(
    principal: str,
    subject: PermissionSubject,
    roles: frozenset[str],
    *,
    record: Mapping[str, Any] | None,
) -> bool:
    if principal == "public":
        return True
    if principal == "registered":
        return subject.is_authenticated
    if principal == "owner":
        return _subject_owns(subject, record=record)
    if principal.startswith("role:"):
        return principal.removeprefix("role:") in roles
    if principal.startswith("user:"):
        return subject.user_id == principal.removeprefix("user:")
    if principal.startswith("account:"):
        return subject.account_id == principal.removeprefix("account:")
    if principal.startswith("subscription:"):
        return principal.removeprefix("subscription:") in subject.subscriptions
    return False


def _owner_fallback(
    subject: PermissionSubject,
    action: str,
    *,
    object_id: str | None,
    record: Mapping[str, Any] | None,
    allow_system_public: bool = True,
) -> PermissionDecision:
    if object_id:
        parsed = parse_user_object_id(object_id)
        if parsed is None and allow_system_public and action in PUBLIC_READ_ACTIONS:
            return PermissionDecision.allow("system object public read")
        if parsed is not None and subject.user_id == str(parsed[0]) and action in OWNER_ACTIONS:
            return PermissionDecision.allow("object owner")

    if record is not None and _subject_owns(subject, record=record) and action in OWNER_ACTIONS:
        return PermissionDecision.allow("record owner")

    return PermissionDecision.deny("not owner")


def _subject_owns(
    subject: PermissionSubject,
    *,
    record: Mapping[str, Any] | None,
) -> bool:
    if record is None or subject.user_id is None:
        return False

    for key in ("owner_id", "user_id", "created_by"):
        if _string_value(record.get(key)) == subject.user_id:
            return True

    return False


def _record_matches_filter(
    record: Mapping[str, Any],
    row_filter: Mapping[str, Any],
    subject: PermissionSubject,
) -> bool:
    for key, expected in row_filter.items():
        if _string_value(record.get(key)) != _string_value(_resolve_filter_value(expected, subject)):
            return False
    return True


def _resolve_filter_value(value: Any, subject: PermissionSubject) -> Any:
    if value == "$user_id":
        return subject.user_id
    if value == "$account_id":
        return subject.account_id
    return value


def _subject_roles(subject: PermissionSubject, policy: PermissionPolicy) -> frozenset[str]:
    roles = set(subject.roles)
    if subject.user_id is not None:
        roles.update(policy.user_roles.get(subject.user_id, ()))
    return frozenset(roles)


def _has_admin_role(roles: frozenset[str], policy: PermissionPolicy) -> bool:
    return any(role in roles for role in policy.admin_roles)


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
