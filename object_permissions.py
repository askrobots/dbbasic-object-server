"""Permission primitives for DBBASIC objects and collections.

The object server owns authorization. Scroll can edit and preview permissions,
but clients should not be trusted to enforce them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
ACCESS_MODES = frozenset({"public", "password", "registered", "subscription", "role_based", "private"})
RULE_EFFECTS = frozenset({"allow", "deny"})


@dataclass(frozen=True)
class PermissionSubject:
    """The authenticated actor being checked.

    ``project_ids`` are the projects shared with this subject and
    ``writable_project_ids`` narrows that to grants with ``permission ==
    "write"``; both are resolved by the server from grant records before
    checks run — the engine itself stays pure and does no IO.
    """

    user_id: str | None = None
    account_id: str | None = None
    roles: tuple[str, ...] = ()
    subscriptions: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    owned_project_ids: tuple[str, ...] = ()
    writable_project_ids: tuple[str, ...] = ()

    @classmethod
    def anonymous(cls) -> "PermissionSubject":
        return cls()

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    def with_projects(
        self,
        project_ids: Iterable[str],
        owned_project_ids: Iterable[str] = (),
        writable_project_ids: Iterable[str] = (),
    ) -> "PermissionSubject":
        return replace(
            self,
            project_ids=tuple(project_ids),
            owned_project_ids=tuple(owned_project_ids),
            writable_project_ids=tuple(writable_project_ids),
        )


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
    package: str | None = None

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


def subject_from_dict(payload: Mapping[str, Any] | None) -> PermissionSubject:
    """Build a subject from JSON-compatible data."""
    if payload is None:
        return PermissionSubject.anonymous()
    if not isinstance(payload, Mapping):
        raise ValueError("Permission subject must be an object")

    return PermissionSubject(
        user_id=_optional_string(payload.get("user_id")),
        account_id=_optional_string(payload.get("account_id")),
        roles=_string_tuple(payload.get("roles", ()), "subject.roles"),
        subscriptions=_string_tuple(payload.get("subscriptions", ()), "subject.subscriptions"),
        project_ids=_string_tuple(payload.get("project_ids", ()), "subject.project_ids"),
        owned_project_ids=_string_tuple(
            payload.get("owned_project_ids", ()), "subject.owned_project_ids"
        ),
        writable_project_ids=_string_tuple(
            payload.get("writable_project_ids", ()), "subject.writable_project_ids"
        ),
    )


def rule_from_dict(payload: Mapping[str, Any]) -> PermissionRule:
    """Build one rule from JSON-compatible data."""
    if not isinstance(payload, Mapping):
        raise ValueError("Permission rule must be an object")

    effect = _required_string(payload, "effect").lower()
    if effect not in RULE_EFFECTS:
        raise ValueError(f"Permission rule effect must be one of: {', '.join(sorted(RULE_EFFECTS))}")

    actions = _string_set(payload.get("actions"), "rule.actions")
    if not actions:
        raise ValueError("Permission rule actions must not be empty")

    principal = _required_string(payload, "principal")
    row_filter = payload.get("row_filter", {})
    if not isinstance(row_filter, Mapping):
        raise ValueError("Permission rule row_filter must be an object")

    fields_payload = payload.get("fields")
    fields = _string_set(fields_payload, "rule.fields") if fields_payload is not None else None
    denied_fields = _string_set(payload.get("denied_fields", ()), "rule.denied_fields")

    return PermissionRule(
        effect=effect,
        actions=frozenset(actions),
        principal=principal,
        collection=_optional_string(payload.get("collection")),
        object_id=_optional_string(payload.get("object_id")),
        row_filter=dict(row_filter),
        fields=frozenset(fields) if fields is not None else None,
        denied_fields=frozenset(denied_fields),
        valid_from=_optional_string(payload.get("valid_from")),
        expires_at=_optional_string(payload.get("expires_at")),
        reason=_optional_string(payload.get("reason")) or "",
        package=_optional_string(payload.get("package")),
    )


def policy_from_dict(payload: Mapping[str, Any]) -> PermissionPolicy:
    """Build a policy from JSON-compatible data."""
    if not isinstance(payload, Mapping):
        raise ValueError("Permission policy must be an object")

    access_mode = _optional_string(payload.get("access_mode")) or "role_based"
    if access_mode not in ACCESS_MODES:
        raise ValueError(f"Permission access_mode must be one of: {', '.join(sorted(ACCESS_MODES))}")

    roles_payload = payload.get("roles", {})
    if not isinstance(roles_payload, Mapping):
        raise ValueError("Permission policy roles must be an object")
    roles: dict[str, dict[str, Any]] = {}
    for role, metadata in roles_payload.items():
        role_name = _string_value(role)
        if not role_name:
            raise ValueError("Permission role names must be strings")
        if not isinstance(metadata, Mapping):
            raise ValueError(f"Permission role metadata must be an object: {role_name}")
        roles[role_name] = dict(metadata)

    user_roles_payload = payload.get("user_roles", {})
    if not isinstance(user_roles_payload, Mapping):
        raise ValueError("Permission policy user_roles must be an object")
    user_roles: dict[str, tuple[str, ...]] = {}
    for user_id, role_names in user_roles_payload.items():
        normalized_user_id = _string_value(user_id)
        if not normalized_user_id:
            raise ValueError("Permission user_roles keys must be strings")
        user_roles[normalized_user_id] = _string_tuple(
            role_names,
            f"user_roles.{normalized_user_id}",
        )

    rules_payload = payload.get("rules", ())
    if not isinstance(rules_payload, (list, tuple)):
        raise ValueError("Permission policy rules must be a list")
    rules = tuple(rule_from_dict(rule) for rule in rules_payload)

    return PermissionPolicy(
        access_mode=access_mode,
        roles=roles,
        user_roles=user_roles,
        rules=rules,
        admin_roles=_string_tuple(payload.get("admin_roles", ("admin", "superuser")), "admin_roles"),
    )


def rule_to_dict(rule: PermissionRule) -> dict[str, Any]:
    """Return a JSON-compatible rule."""
    payload: dict[str, Any] = {
        "effect": rule.effect,
        "actions": sorted(rule.actions),
        "principal": rule.principal,
        "row_filter": dict(rule.row_filter),
        "denied_fields": sorted(rule.denied_fields),
        "reason": rule.reason,
    }

    if rule.collection is not None:
        payload["collection"] = rule.collection
    if rule.object_id is not None:
        payload["object_id"] = rule.object_id
    if rule.fields is not None:
        payload["fields"] = sorted(rule.fields)
    if rule.valid_from is not None:
        payload["valid_from"] = rule.valid_from
    if rule.expires_at is not None:
        payload["expires_at"] = rule.expires_at
    if rule.package is not None:
        payload["package"] = rule.package

    return payload


def policy_to_dict(policy: PermissionPolicy) -> dict[str, Any]:
    """Return a JSON-compatible policy."""
    return {
        "access_mode": policy.access_mode,
        "roles": {role: dict(metadata) for role, metadata in sorted(policy.roles.items())},
        "user_roles": {
            user_id: list(role_names)
            for user_id, role_names in sorted(policy.user_roles.items())
        },
        "rules": [rule_to_dict(rule) for rule in policy.rules],
        "admin_roles": list(policy.admin_roles),
    }


def decision_to_dict(decision: PermissionDecision) -> dict[str, Any]:
    """Return a JSON-compatible permission decision."""
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "code": decision.code,
        "http_status": decision.http_status,
        "row_filter": dict(decision.row_filter),
        "fields": sorted(decision.fields) if decision.fields is not None else None,
        "denied_fields": sorted(decision.denied_fields),
    }


def subject_roles(subject: PermissionSubject, policy: PermissionPolicy) -> frozenset[str]:
    """Return roles assigned directly and through the active policy."""
    return _subject_roles(subject, policy)


def subject_has_admin_role(subject: PermissionSubject, policy: PermissionPolicy) -> bool:
    """Return True when the subject has one of the policy admin roles."""
    return _has_admin_role(_subject_roles(subject, policy), policy)


def record_matches_filter(
    record: Mapping[str, Any],
    row_filter: Mapping[str, Any],
    subject: PermissionSubject,
) -> bool:
    """Return True when a record's stored values satisfy a filter/guard.

    Shared by row filters and schema transition guards: every key
    resolves its expected value through the same closed set of
    $-variables (see ``_resolve_filter_value``) or treats it as a
    literal string. An empty stored field never matches a $-variable.
    """
    return _record_matches_filter(record, row_filter, subject)


def principal_matches(
    principal: str,
    subject: PermissionSubject,
    policy: PermissionPolicy,
    *,
    record: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when a portable principal matches the subject."""
    return _principal_matches(
        principal,
        subject,
        _subject_roles(subject, policy),
        record=record,
    )


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
        resolved = _resolve_filter_value(expected, subject)
        actual = _string_value(record.get(key))
        if isinstance(resolved, tuple):
            if actual is None or actual not in {_string_value(item) for item in resolved}:
                return False
        elif actual != _string_value(resolved):
            return False
    return True


def _resolve_filter_value(value: Any, subject: PermissionSubject) -> Any:
    if value == "$user_id":
        return subject.user_id
    if value == "$account_id":
        return subject.account_id
    if value == "$accessible_projects":
        return tuple(subject.project_ids)
    if value == "$owned_projects":
        return tuple(subject.owned_project_ids)
    if value == "$writable_projects":
        return tuple(subject.writable_project_ids)
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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ValueError(f"Expected string-compatible value, got {type(value).__name__}")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload:
        raise ValueError(f"Permission field '{key}' is required")
    value = _optional_string(payload[key])
    if value is None or not value:
        raise ValueError(f"Permission field '{key}' must be a non-empty string")
    return value


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    return tuple(sorted(_string_set(value, field_name)))


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"Permission field '{field_name}' must be a list of strings")

    values: set[str] = set()
    for item in value:
        string_value = _optional_string(item)
        if string_value is None or not string_value:
            raise ValueError(f"Permission field '{field_name}' contains an invalid value")
        values.add(string_value)
    return frozenset(values)


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
