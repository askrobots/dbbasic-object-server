# HTTP API Contract

This document records the HTTP shape DBBASIC Object Server should preserve while
the public server is rebuilt.

The goal is compatibility. Existing clients, MCP tools, Scroll, scripts, and
automation should not need a new API shape just because the internals become
cleaner.

Examples use:

```text
http://127.0.0.1:8001
```

Do not replace these examples with private LAN IPs, cloud IPs, customer domains,
or deployment-specific station names.

## Client Defaults

Existing clients expect JSON by default and commonly send:

```http
Accept: application/json
Authorization: Token <token>
```

Authentication is required for source, state, logs, versions, object creation,
source updates, rollback, and destructive deletes. Basic object execution may be
public or authenticated depending on server policy and the object being called.

The current public ASGI slice enforces that sensitive read surface with the
temporary admin token from `DBBASIC_ADMIN_TOKEN`. Source update and rollback
also require `DBBASIC_ENABLE_SOURCE_WRITES=true`. The real role, object, and row
permission system still needs to replace this temporary admin-only boundary
before general use.

## Correlation IDs

Every HTTP response includes:

```http
X-DBBASIC-Correlation-ID: <uuid-v4>
```

Clients may send their own UUIDv4 correlation ID:

```http
X-DBBASIC-Correlation-ID: 123e4567-e89b-42d3-a456-426614174000
```

Missing, empty, or invalid values are replaced with a server-generated UUIDv4.
The response header always contains the accepted value. Source update responses,
rollback responses, and execution error bodies also include `correlation_id`.

The same ID is written into source version metadata, source-change entries,
object-owned runtime logs, execution error logs, and permission audit entries
when those records are created inside the request. Scroll and AI tools can use
that ID to connect one user action to the source version, logs, permission
decision, and runtime error it produced.

## Resource IDs

New DBBASIC-facing URL and API resources should use UUIDv4 IDs by default:

- record IDs
- account and user IDs
- session/action/change IDs
- package install and restore IDs
- external links and imported resource handles

Imported systems may keep legacy compatibility IDs. For example, a Django table
with integer primary keys can be imported with its old ID preserved in a
compatibility column, while the DBBASIC route-facing `id` should be UUIDv4 for
new records. This keeps URLs non-enumerable, makes exports easier to merge, and
avoids coupling object packages to one database sequence.

The current identity registry generates UUIDv4 account IDs, user IDs, and
session IDs when the server creates them. It still accepts existing string
account/user IDs and legacy `sess_...` session rows so old clients, Scroll
prototypes, and migration tools do not break while the public runtime is being
extracted.

## Health

Plain health is a public liveness check:

```http
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

Detailed health is for operators, Scroll, and future cluster routing:

```http
GET /health?capacity=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "version": "0.0.1",
  "station_id": "standalone",
  "uptime": "2h 10m",
  "requests": 120,
  "errors": 0,
  "rps": 1.4,
  "objects": {
    "count": 4
  },
  "capacity": {
    "requests": {
      "in_flight": 1,
      "max": 64,
      "available": 63,
      "limited": true
    },
    "object_executions": {
      "in_flight": 0,
      "max": 8,
      "available": 8,
      "limited": true
    }
  },
  "checks": {
    "storage": {
      "status": "ok"
    }
  }
}
```

`GET /health?metrics=true` returns the same detailed health payload plus a
`metrics` object with request counts, status counts, response timing, top paths,
and recent HTTP errors. This preserves the older dashboard/Scroll direction
without exposing those details through the public liveness route.

## Admin Status

Admin status is the compact operator snapshot Scroll and staging dashboards can
use instead of stitching together many routes on first load:

```http
GET /admin/status
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "timestamp": "2026-01-01T00:00:00+00:00",
  "version": "0.0.1",
  "station_id": "standalone",
  "health": {
    "status": "ok",
    "metrics": {}
  },
  "inventory": {
    "objects": 4,
    "collections": 2,
    "schemas": 2,
    "packages": 3
  },
  "capabilities": {
    "source_writes": {
      "enabled": false,
      "env": "DBBASIC_ENABLE_SOURCE_WRITES"
    },
    "package_installs": {
      "enabled": false,
      "env": "DBBASIC_ENABLE_PACKAGE_INSTALLS"
    },
    "permission_enforcement": {
      "enabled": false,
      "requested": false,
      "blocked": true,
      "env": "DBBASIC_ENABLE_PERMISSION_ENFORCEMENT"
    },
    "identity": {
      "trusted_headers_enabled": false,
      "require_known_identity_users": true,
      "session_login_enabled": false,
      "session_login_token_configured": false
    }
  },
  "packages": [
    {
      "id": "system-dashboard",
      "name": "System Dashboard",
      "version": "0.1.0",
      "status": "installed",
      "install": {
        "installed_count": 1,
        "installable_count": 1,
        "safe_to_install": true,
        "install_enabled": false,
        "warnings": []
      },
      "changes": {
        "total": 1,
        "latest": {
          "action": "installed"
        }
      }
    }
  ],
  "permissions": {
    "enforcement_enabled": false,
    "audit_enabled": false,
    "readiness": {
      "ready": false
    },
    "warnings": []
  }
}
```

If the server is degraded, the route may return `503` with the same response
shape and `"status": "degraded"`. This route is admin-token gated because it
contains configuration, package, and capacity details.

## Daemon Status

Daemon status is the read-only operator snapshot for the scheduler, queue,
event delivery, and cleanup primitives:

```http
GET /daemon/status
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "timestamp": "2026-01-01T00:00:00+00:00",
  "daemon": {
    "mode": "polling",
    "croniter_available": true,
    "object_roots": {
      "count": 1
    },
    "triggers": {
      "scheduler": {
        "object_id": "scheduler",
        "source_present": true
      },
      "queue": {
        "object_id": "queue",
        "source_present": true
      },
      "events": {
        "object_id": "events",
        "source_present": true
      }
    }
  },
  "scheduler": {
    "object_id": "scheduler",
    "source_present": true,
    "tasks": {
      "total": 2,
      "active": 1,
      "due": 1,
      "future": 0,
      "invalid": 0,
      "next_run": 1767225600,
      "next_run_iso": "2026-01-01T00:00:00+00:00"
    }
  },
  "queue": {
    "object_id": "queue",
    "source_present": true,
    "messages": {
      "total": 3,
      "pending_visible": 1,
      "pending_delayed": 1,
      "expired_pending": 0,
      "invalid": 0
    }
  },
  "events": {
    "object_id": "events",
    "source_present": true,
    "events": {
      "total": 10,
      "latest": {
        "id": "evt_123",
        "event_type": "collection.record.created",
        "timestamp": 1767225600
      }
    },
    "subscriptions": {
      "total": 2,
      "by_delivery_status": {
        "ok": 1,
        "failed": 1
      },
      "pending_deliveries": 1
    }
  },
  "cleanup": {
    "event_retention": {
      "keep_count": 1000,
      "keep_seconds": 604800
    },
    "rate_limit_files": 0
  }
}
```

This endpoint does not execute daemon work or expose event payloads. It reads
the same TSV state that the daemon uses so Scroll can show due scheduler tasks,
visible/delayed queue messages, failed subscription delivery, event retention,
and cleanup pressure without requiring a separate queue dashboard service.

## Daemon Scheduler And Queue Controls

Scheduler and queue controls are admin-token gated. They operate on the same
`data/state/scheduler/state.tsv` and `data/state/queue/state.tsv` files that
`object_daemon.py` already polls, so the HTTP API can manage work without
introducing Celery, Redis, Flower, or a second queue service.

These routes redact task/message payloads by default. Pass
`include_payload=true` only on trusted operator screens.

```http
GET /daemon/scheduler/tasks?status=active&limit=100&offset=0
POST /daemon/scheduler/tasks
PATCH /daemon/scheduler/tasks/{task_id}
DELETE /daemon/scheduler/tasks/{task_id}

GET /daemon/queue/messages?status=pending&queue_name=default&limit=100&offset=0
POST /daemon/queue/messages
PATCH /daemon/queue/messages/{message_id}
DELETE /daemon/queue/messages/{message_id}
Authorization: Token <token>
```

Create one scheduled task:

```json
{
  "object_id": "system_dashboard",
  "method": "POST",
  "type": "onetime",
  "schedule": "2026-07-01T12:00:00Z",
  "payload": {
    "refresh": true
  }
}
```

Response:

```json
{
  "status": "ok",
  "task": {
    "id": "018ff3f4-5f80-4df0-8e25-bcf6ad6fda01",
    "object_id": "system_dashboard",
    "method": "POST",
    "type": "onetime",
    "schedule": "2026-07-01T12:00:00Z",
    "status": "active",
    "next_run": 1782907200,
    "payload_present": true
  }
}
```

Patch examples:

```json
{"status": "paused"}
```

```json
{"next_run": "2026-07-01T13:00:00Z"}
```

Enqueue one message:

```json
{
  "object_id": "system_dashboard",
  "method": "POST",
  "queue_name": "default",
  "priority_level": 5,
  "payload": {
    "refresh": true
  }
}
```

Response:

```json
{
  "status": "ok",
  "message": {
    "id": "2de66c27-149b-475b-a6f0-0c08a8d850f5",
    "queue_name": "default",
    "message": {
      "object_id": "system_dashboard",
      "method": "POST",
      "payload_present": true
    },
    "priority_level": 5,
    "status": "pending"
  }
}
```

Queue patch actions:

```json
{"action": "cancel"}
```

```json
{"action": "retry"}
```

These controls are for trusted operators. They do not change the rule that
untrusted object execution still needs a stronger worker boundary, quotas, and
permission enforcement before public signup is safe.

## Rate Limits

When `DBBASIC_RATE_LIMIT_REQUESTS` is set above zero, the server rate-limits
non-health traffic before reading request bodies or running object code. Plain
`GET /health` is excluded so load balancers and uptime checks keep working under
pressure.

The current public server uses a valid admin token as the rate-limit identity
for admin requests. Other requests use the client IP address. Proxy headers such
as `X-Forwarded-For` are ignored unless `DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS`
is enabled on a server that is only reachable through a trusted reverse proxy.

Limit response:

```http
429 Too Many Requests
Retry-After: 30
```

```json
{
  "status": "error",
  "error": "Rate limit exceeded",
  "retry_after": 30,
  "limit": 1000,
  "window_seconds": 60
}
```

## Identity

Scroll and gateway code can inspect the subject the object server will use for
permission checks:

```http
GET /identity
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "subject": {
    "user_id": "7",
    "account_id": "acme",
    "roles": ["sales"],
    "subscriptions": ["pro"],
    "authenticated": true
  },
  "auth": {
    "method": "trusted_headers",
    "trusted_headers_enabled": true,
    "trusted_headers_present": true
  },
  "permissions": {
    "enforcement_enabled": true,
    "enforcement_requested": true,
    "enforcement_blocked": false,
    "audit_enabled": true
  }
}
```

Current auth methods are:

- `anonymous` - no trusted identity was accepted.
- `admin_token` - `Authorization: Token <DBBASIC_ADMIN_TOKEN>` or Bearer matched
  the server admin token.
- `session_token` - `Authorization: Token <session-token>` or Bearer matched an
  active file-backed DBBASIC identity session.
- `trusted_headers` - `DBBASIC_PERMISSION_TRUST_HEADERS=true` and at least one
  `X-DBBASIC-*` identity header was present.

Trusted identity headers are ignored unless explicitly enabled. This keeps the
current staging server safe while giving future login/session gateways and
Scroll one stable endpoint for the active account, user, roles, and
subscriptions.

### Identity Accounts And Users

The server has a small file-backed identity registry so sessions can be minted
from known users instead of every client inventing its own subject payload. These
routes are admin-gated.

Accounts are stored under `data/identity/accounts.tsv`:

```http
GET /identity/accounts
Authorization: Token <admin-token>
```

```http
POST /identity/accounts
Authorization: Token <admin-token>
Content-Type: application/json
```

```json
{
  "account_id": "acme",
  "name": "Acme Corp",
  "subscriptions": ["pro"]
}
```

Users are stored under `data/identity/users.tsv`:

```http
GET /identity/users
Authorization: Token <admin-token>
```

```http
GET /identity/users?account_id=acme
Authorization: Token <admin-token>
```

```http
POST /identity/users
Authorization: Token <admin-token>
Content-Type: application/json
```

```json
{
  "user_id": "u_7",
  "account_id": "acme",
  "email": "alice@example.com",
  "display_name": "Alice",
  "roles": ["sales"],
  "subscriptions": ["team"]
}
```

Single-record reads are:

```http
GET /identity/accounts/acme
GET /identity/users/u_7
```

`status` can be `active` or `disabled`. Disabled accounts and users cannot mint
new sessions.

### Identity Sessions

The server can mint scoped subject tokens for Scroll, gateways, or controlled
apps. Sessions are stored under `data/identity/sessions.tsv`. Only token hashes
are written to disk; the raw token is returned once at creation time.

Create a session:

```http
POST /identity/sessions
Authorization: Token <admin-token>
Content-Type: application/json
```

```json
{
  "user_id": "7",
  "account_id": "acme",
  "roles": ["sales"],
  "subscriptions": ["pro"],
  "label": "scroll desktop",
  "ttl_seconds": 86400
}
```

Response:

```json
{
  "status": "ok",
  "session": {
    "session_id": "9d4fe8bd-6859-43ef-8f83-f7063c54f7bc",
    "user_id": "7",
    "account_id": "acme",
    "roles": ["sales"],
    "subscriptions": ["pro"],
    "label": "scroll desktop",
    "created_at": "2026-06-30T18:00:00Z",
    "expires_at": "2026-07-01T18:00:00Z",
    "revoked_at": null,
    "active": true
  },
  "token": "returned-once"
}
```

If `user_id` already exists in `data/identity/users.tsv`, omitted `account_id`,
`roles`, and `subscriptions` are filled from the registered user and account.
This lets Scroll or a login gateway mint a session without duplicating policy
metadata in every request. If `DBBASIC_REQUIRE_KNOWN_IDENTITY_USERS=true`, the
server rejects sessions for unknown users.

For a non-admin login gateway or local trusted client, the current-session route
can mint a session for an existing active user when
`DBBASIC_ENABLE_SESSION_LOGIN=true`:

```http
POST /identity/session
Authorization: Token <session-login-token>
Content-Type: application/json
```

```json
{
  "user_id": "7",
  "label": "scroll desktop",
  "ttl_seconds": 86400
}
```

This route always requires the user to exist in `data/identity/users.tsv`.
It also requires `DBBASIC_SESSION_LOGIN_TOKEN`; this token is separate from the
admin token so a login gateway can mint sessions without broad admin access.
It accepts only `user_id`, `label`, and `ttl_seconds`; caller-supplied
`account_id`, `roles`, or `subscriptions` are rejected. The session subject is
loaded from the registered user and account so a login request cannot grant
itself stronger permissions. This is a session-mint primitive, not password
authentication yet.

List sessions:

```http
GET /identity/sessions
Authorization: Token <admin-token>
```

Revoke a session:

```http
DELETE /identity/sessions/{session_id}
Authorization: Token <admin-token>
```

Inspect the current session with a session token:

```http
GET /identity/session
Authorization: Token <session-token>
```

Revoke the current session with the same token:

```http
DELETE /identity/session
Authorization: Token <session-token>
```

The current-session route does not accept the admin token as a user session.
It is for Scroll, app clients, and login gateways that need a stable non-admin
session lifecycle.

Session tokens do not grant admin access. They only supply the active subject
used by permission checks. Admin routes still require `DBBASIC_ADMIN_TOKEN`.

## Permissions Policy

The public server now has a persisted permission policy shape. The endpoints are
admin-gated while broader login and account management mature.

```http
GET /permissions/policy
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "policy": {
    "access_mode": "role_based",
    "roles": {},
    "user_roles": {},
    "rules": [],
    "admin_roles": ["admin", "superuser"]
  }
}
```

Update:

```http
PUT /permissions/policy
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "policy": {
    "access_mode": "role_based",
    "roles": {"sales": {"label": "Sales"}},
    "user_roles": {"7": ["sales"]},
    "rules": [
      {
        "effect": "allow",
        "principal": "role:sales",
        "actions": ["read"],
        "collection": "contacts",
        "row_filter": {"owner_id": "$user_id"}
      }
    ],
    "admin_roles": ["admin"]
  }
}
```

The policy is stored under `data/permissions/policy.json`. Missing policy files
load as a conservative `role_based` policy with no grants.

## Permissions Status

Scroll and operators can inspect whether the active identity and permission
configuration is ready for enforcement:

```http
GET /permissions/status
Authorization: Token <admin-token>
```

Response:

```json
{
  "status": "ok",
  "permissions": {
    "enforcement_enabled": false,
    "enforcement_requested": false,
    "enforcement_blocked": false,
    "allow_unready_enforcement": false,
    "audit_enabled": true,
    "trusted_headers_enabled": false,
    "require_known_identity_users": true,
    "admin_token_configured": true,
    "session_login_enabled": true,
    "session_login_token_configured": true
  },
  "identity": {
    "accounts": {"count": 1, "active": 1, "disabled": 0},
    "users": {"count": 3, "active": 3, "disabled": 0},
    "sessions": {"count": 2, "active": 1, "revoked": 1}
  },
  "policy": {
    "valid": true,
    "policy_file_exists": true,
    "access_mode": "role_based",
    "rules_count": 4,
    "allow_rules": 3,
    "deny_rules": 1
  },
  "readiness": {
    "can_enable_enforcement": true,
    "blockers": []
  },
  "warnings": []
}
```

This endpoint is intentionally read-only. `DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true`
requests enforcement, but the server only makes `enforcement_enabled` effective
when `readiness.can_enable_enforcement` is true. If readiness is blocked,
`enforcement_requested` is true, `enforcement_enabled` is false, and
`enforcement_blocked` is true. The explicit recovery/test override is
`DBBASIC_ALLOW_UNREADY_PERMISSION_ENFORCEMENT=true`.

Readiness currently requires an admin recovery token, a valid policy, an
available non-admin identity path for identity-gated modes, and at least one
allow grant for `role_based` policy. The accepted identity paths are trusted
proxy headers, guarded session login for existing users, or an already-active
DBBASIC session.

## Permissions Check

Scroll and admin tools can ask the server to evaluate one permission decision.
This is useful for previewing draft rules and for future "test as role" screens.

```http
POST /permissions/check
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "subject": {
    "user_id": "7",
    "account_id": "customer-acme",
    "roles": ["sales"],
    "subscriptions": ["pro"]
  },
  "action": "read",
  "collection": "contacts",
  "object_id": null,
  "record": null
}
```

If the request includes a `policy` object, the server evaluates against that
inline policy without saving it. Otherwise it evaluates against the persisted
policy. The optional `now` field accepts an ISO timestamp for previewing
temporary access windows.

Response:

```json
{
  "status": "ok",
  "decision": {
    "allowed": true,
    "reason": "sales reps only see own contacts",
    "code": "allowed",
    "http_status": 200,
    "row_filter": {"owner_id": "$user_id"},
    "fields": null,
    "denied_fields": []
  }
}
```

When a paid entitlement is missing, the decision can include:

```json
{
  "allowed": false,
  "reason": "subscription required",
  "code": "payment_required",
  "http_status": 402,
  "row_filter": {},
  "fields": null,
  "denied_fields": []
}
```

## Permission Route Enforcement

Permission policy checks can be applied to object and collection-record routes
when explicitly requested:

```text
DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true
```

The request becomes effective only when `/permissions/status` reports no
readiness blockers, unless `DBBASIC_ALLOW_UNREADY_PERMISSION_ENFORCEMENT=true`
is also set. When enforcement is effective, denied route checks return the
`http_status`, `reason`, and `code` from the permission decision before object
source, state, logs, versions, execution, or collection-record data work runs.

Audit-only mode records the same decisions without changing responses:

```text
DBBASIC_ENABLE_PERMISSION_AUDIT=true
```

Audit entries are written as JSON lines under:

```text
data/permissions/audit.jsonl
```

Trusted identity headers are off by default and should only be enabled behind a
trusted auth gateway or reverse proxy:

```text
DBBASIC_PERMISSION_TRUST_HEADERS=true
```

Supported headers are `X-DBBASIC-User-Id`, `X-DBBASIC-Account-Id`,
`X-DBBASIC-Roles`, and `X-DBBASIC-Subscriptions`.

Collection record read routes use the `read` action. Collection record write
routes use `create`, `update`, and `delete`. In enforcement mode the server
applies row filters before pagination, evaluates detail and write requests
against the selected record, blocks owner-changing updates that would escape the
allowed row filter, and redacts `fields` / `denied_fields` from returned
records. Audit-only mode can log write decisions, but it does not grant
mutation access without the admin token.

Operators and Scroll can read recent audit entries through an admin-gated
endpoint:

```http
GET /permissions/audit?limit=100
Authorization: Token <token>
```

Optional filters:

- `action`
- `object_id`
- `collection`
- `allowed=true|false`
- `enforced=true|false`

Response:

```json
{
  "status": "ok",
  "entries": [
    {
      "timestamp": "2026-06-29T00:00:00Z",
      "method": "GET",
      "object_id": "site_home",
      "collection": "site",
      "action": "execute",
      "enforced": false,
      "decision": {"allowed": false, "code": "forbidden"}
    }
  ],
  "count": 1
}
```

## Object List

```http
GET /objects?format=json
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "objects": [
    {
      "object_id": "basics_counter",
      "path": "basics/counter.py",
      "owner": "system"
    }
  ],
  "count": 1
}
```

Clients should accept either a top-level list or an object with an `objects`
field. New server code should prefer the object form above.

## Admin Object Inspection

`/objects` remains the runtime surface: object execution and public object
routes live there. Scroll and operator dashboards should use the admin
inspection surface so staging can expose read-only object metadata without
opening broad object execution routes at the reverse proxy.

```http
GET /admin/objects
Authorization: Token <token>
```

The response matches `GET /objects?format=json`.

```http
GET /admin/objects/{object_id}
GET /admin/objects/{object_id}?metadata=true
GET /admin/objects/{object_id}?source=true&format=json
GET /admin/objects/{object_id}?state=true
GET /admin/objects/{object_id}?logs=true&format=json&limit=100
GET /admin/objects/{object_id}?versions=true&limit=10
GET /admin/objects/{object_id}?version=1
GET /admin/objects/{object_id}?source_changes=true&limit=100
Authorization: Token <token>
```

If no inspection query is supplied, the server returns metadata. The admin
inspection surface never executes the object. Unsupported query flags return a
400 response so a client cannot accidentally turn an operator view into an
execution endpoint.

## Admin Collection And Schema Inspection

`/collections` and `/schemas` remain the data and schema API surfaces. Scroll
and operator dashboards should use the admin inspection aliases for read-only
collection, record, changelog, schema, and schema-version views. This lets a
public staging reverse proxy expose inspection routes without exposing the broad
write-capable collection and schema routes.

```http
GET /admin/collections
GET /admin/collections/{collection}
GET /admin/collections/{collection}/records
GET /admin/collections/{collection}/records/{record_id}
GET /admin/collections/{collection}/changes
GET /admin/collections/{collection}/records/{record_id}/changes
GET /admin/schemas
GET /admin/schemas/{collection}
GET /admin/schemas/{collection}?versions=true&limit=10
GET /admin/schemas/{collection}?version=1
Authorization: Token <token>
```

The responses match the underlying read-only `GET /collections*` and
`GET /schemas*` responses. These admin aliases are GET-only and admin-token
gated. They do not create records, update records, delete records, replace
schemas, roll back schemas, install packages, or execute objects.

## Collections

```http
GET /collections
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collections": [
    {
      "name": "site",
      "object_count": 2,
      "file_count": 1,
      "state_object_count": 1,
      "log_object_count": 1,
      "has_records": true,
      "owners": ["system"],
      "kinds": {"system": 2},
      "permission": {
        "access_mode": "role_based",
        "rule_count": 1,
        "allow_count": 1,
        "deny_count": 0,
        "actions": ["execute", "read"],
        "principals": ["role:admin"]
      }
    }
  ],
  "count": 1
}
```

Read one collection with object details:

```http
GET /collections/{collection}
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collection": {
    "name": "site",
    "object_count": 1,
    "file_count": 0,
    "state_object_count": 0,
    "log_object_count": 0,
    "has_records": false,
    "owners": ["system"],
    "kinds": {"system": 1},
    "permission": {
      "access_mode": "role_based",
      "rule_count": 0,
      "allow_count": 0,
      "deny_count": 0,
      "actions": [],
      "principals": []
    },
    "objects": [
      {
        "object_id": "site_home",
        "path": "site/home.py",
        "owner": "system",
        "kind": "system",
        "state_count": 0,
        "has_logs": false,
        "file_count": 0
      }
    ]
  }
}
```

Collections are a derived view over object source IDs, source folders, object
state/files/log presence, record-file presence, and permission rules. The server
does not store a separate collection table yet. This keeps the existing
`/objects` contract stable while giving tools such as Scroll a cleaner grouping
API.

The public server keeps collection summary routes read-only and admin-token
gated for now. Missing collections return `404`; unsafe collection names return
`400`.

## Collection Records

List records:

```http
GET /collections/{collection}/records?limit=100&offset=0
Authorization: Token <token>
```

By default this route is admin-token gated. If
`DBBASIC_ENABLE_PERMISSION_AUDIT=true` or
`DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true` is set, the route uses the
persisted permission policy instead. Enforcement uses the `read` action with the
collection name, applies row filters before `limit` / `offset`, and redacts
fields according to the matching decision.

Response:

```json
{
  "status": "ok",
  "collection": "contacts",
  "records": [
    {
      "id": "c1",
      "first_name": "Ada",
      "last_name": "Lovelace"
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

Read one record:

```http
GET /collections/{collection}/records/{record_id}
Authorization: Token <token>
```

The detail route is also admin-token gated by default. With permission
enforcement enabled, the server loads the record, checks the same `read`
permission against that record, and then applies field allow/deny rules before
returning it.

Response:

```json
{
  "status": "ok",
  "collection": "contacts",
  "record": {
    "id": "c1",
    "first_name": "Ada",
    "last_name": "Lovelace"
  }
}
```

Record files live under:

```text
data/collections/{collection}/records.tsv
```

The TSV file must have a header row and an `id` column. Values are returned as
strings.

For new DBBASIC-created records, the server generates a UUIDv4 `id` when the
request omits one. During migrations, imported rows may preserve legacy integer
or slug IDs for compatibility, but new public routes and generated UI should
not assume sequential IDs.

Create one record:

```http
POST /collections/{collection}/records
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "first_name": "Grace",
  "last_name": "Hopper"
}
```

Response:

```json
{
  "status": "ok",
  "collection": "contacts",
  "record": {
    "id": "8c2d4de7-89fe-45f7-9cf9-f0f42610e7be",
    "first_name": "Grace",
    "last_name": "Hopper"
  }
}
```

Successful creates return `201`. Explicit record IDs are still accepted for
imports and compatibility. Duplicate record IDs return `409`.

Update one record:

```http
PUT /collections/{collection}/records/{record_id}
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "last_name": "Lovelace",
  "email": "ada@example.com"
}
```

The record `id` cannot be changed.

Delete one record:

```http
DELETE /collections/{collection}/records/{record_id}
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collection": "contacts",
  "record": {
    "id": "c1",
    "first_name": "Ada",
    "last_name": "Lovelace"
  },
  "deleted": true
}
```

Missing collections or records return `404`; unsafe collection, record, or field
names return `400`. Missing subscription entitlements can return
`402 Payment Required` when the active policy requires them. By default all
record mutations require the admin token. With
`DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true`, mutations can also be authorized
by persisted policy rules using `create`, `update`, and `delete`.

If `data/schemas/{collection}.json` exists, `POST` and `PUT` mutations validate
known fields before the TSV write. Required fields, create defaults, basic
scalar types, enum values, length/numeric/pattern rules, and computed/read-only
fields are enforced by the server. Unknown fields are still accepted so tools
can add data before a schema is complete.

When `DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true`, schema field permissions are
also enforced for collection records. Fields resolved as `hidden` are removed
from `GET` responses. Submitted fields resolved as `read` or `hidden` are
rejected with `403`:

```json
{
  "status": "error",
  "error": "Record field 'margin' is not editable for this subject",
  "code": "forbidden",
  "denied_fields": ["margin"]
}
```

Read collection record change history:

```http
GET /collections/{collection}/changes?limit=100&offset=0
Authorization: Token <token>
```

Read change history for one record:

```http
GET /collections/{collection}/records/{record_id}/changes?limit=100&offset=0
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collection": "contacts",
  "record_id": "c1",
  "changes": [
    {
      "change_id": "0f8b5e96-d8b5-4a0d-8b7d-c3c2e2092d24",
      "timestamp": "2026-06-29T12:00:00+00:00",
      "collection": "contacts",
      "record_id": "c1",
      "action": "update",
      "actor": "admin",
      "message": "Updated record",
      "changed_fields": ["name"],
      "before": {"id": "c1", "name": "Ada"},
      "after": {"id": "c1", "name": "Ada Lovelace"}
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

Successful `POST`, `PUT`, and `DELETE` record mutations append JSONL entries
under:

```text
data/record_changes/{collection}/changes.jsonl
```

This is the durable audit and admin-history surface. When record events are
enabled, successful `POST`, `PUT`, and `DELETE` mutations also publish
`collection.record.created`, `collection.record.updated`, and
`collection.record.deleted` events from the same change entry.

## Events

Events are the daemon-compatible notification surface for triggers, listeners,
webhooks, and future worker-style objects. They are intentionally separate from
record change history. Change history is the durable audit trail; events are the
delivery queue.

Collection record mutations publish metadata-only events by default. Set
`DBBASIC_ENABLE_RECORD_EVENTS=false` to turn that off. Event payloads include
the change id, collection, record id, action, actor, timestamp, and changed
field names; they intentionally do not copy full `before` or `after` snapshots
until subscriber permissions are enforced.

The current endpoints are admin-gated:

```http
GET /events?event_type=collection.record.created&limit=100&offset=0
DELETE /events?keep_count=1000&keep_seconds=604800
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "events": [
    {
      "id": "789f0c18-7845-4620-a2b0-8bbad752f91b",
      "event_type": "collection.record.created",
      "payload": {
        "change_id": "0f8b5e96-d8b5-4a0d-8b7d-c3c2e2092d24",
        "collection": "contacts",
        "record_id": "c1",
        "action": "create",
        "actor": "admin",
        "timestamp": "2026-06-29T12:00:00Z",
        "changed_fields": ["id", "name"]
      },
      "source": "record_changes",
      "actor": "admin",
      "timestamp": 1782734400,
      "created_at": "2026-06-29T12:00:00Z"
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

Publish one event:

```http
POST /events
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "event_type": "collection.record.created",
  "source": "record_changes",
  "payload": {"collection": "contacts", "record_id": "c1"}
}
```

Successful publishes return `201`.

Prune the delivery queue:

```http
DELETE /events?keep_count=1000&keep_seconds=604800
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "retention": {
    "deleted": 12,
    "kept": 1000,
    "scanned": 1012,
    "protected": 1,
    "corrupt_deleted": 0,
    "keep_count": 1000,
    "keep_seconds": 604800
  }
}
```

`keep_count=0` disables count-based pruning. `keep_seconds=0` disables
age-based pruning. Subscriptions are never deleted by event pruning, and the
event referenced by a subscription `last_event_id` is protected to avoid
surprise replay. Failed delivery attempts are protected too, so the daemon and
Scroll can still see the pending retry event. Configure the default
publish/daemon cleanup policy with `DBBASIC_EVENT_KEEP_COUNT` and
`DBBASIC_EVENT_KEEP_SECONDS`.

Subscriptions are stored in the same daemon-compatible `events` object state:

```http
GET /events/subscriptions?event_type=collection.record.created
POST /events/subscriptions
DELETE /events/subscriptions?event_type=collection.record.created&subscriber_id=scroll
Authorization: Token <token>
```

Create or replace a subscription:

```json
{
  "event_type": "collection.record.created",
  "subscriber_id": "scroll",
  "callback_url": "https://example.com/hooks/dbbasic"
}
```

Response:

```json
{
  "status": "ok",
  "subscription": {
    "id": "scroll",
    "event_type": "collection.record.created",
    "callback_url": "https://example.com/hooks/dbbasic",
    "created_at": 1782734400,
    "created_at_iso": "2026-06-29T12:00:00Z",
    "created_by": "admin",
    "last_event_id": null,
    "delivery": {
      "status": "idle",
      "attempts": 0,
      "successes": 0,
      "failures": 0,
      "last_attempted_event_id": null,
      "last_attempt_at": null,
      "last_attempt_at_iso": null,
      "last_success_event_id": null,
      "last_success_at": null,
      "last_success_at_iso": null,
      "last_failure_event_id": null,
      "last_failure_at": null,
      "last_failure_at_iso": null,
      "last_status_code": null,
      "last_error": null
    }
  }
}
```

The daemon updates `delivery` after each callback attempt. Successful delivery
sets `status=ok` and advances `last_event_id`; failed delivery sets
`status=failed`, records the last error/status, and leaves `last_event_id`
unchanged so the event can be retried.

Delivery status is exposed separately so Scroll and operator dashboards can
show pending, delivered, and failed work without loading event payloads or
callback secrets:

```http
GET /events/deliveries?event_type=collection.record.created&pending=true
GET /events/deliveries?delivery_status=failed
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "deliveries": [
    {
      "id": "scroll",
      "subscriber_id": "scroll",
      "event_type": "collection.record.created",
      "callback_url_present": true,
      "pending": true,
      "pending_count": 2,
      "last_event_id": null,
      "next_pending_event": {
        "id": "789f0c18-7845-4620-a2b0-8bbad752f91b",
        "event_type": "collection.record.created",
        "source": "record_changes",
        "actor": "admin",
        "timestamp": 1782734400,
        "created_at": "2026-06-29T12:00:00Z"
      },
      "latest_pending_event": {
        "id": "98b9fd7c-9e38-48a6-9134-153473c0172a",
        "event_type": "collection.record.created",
        "source": "record_changes",
        "actor": "admin",
        "timestamp": 1782734460,
        "created_at": "2026-06-29T12:01:00Z"
      },
      "delivery": {
        "status": "failed",
        "attempts": 1,
        "successes": 0,
        "failures": 1,
        "last_attempted_event_id": "789f0c18-7845-4620-a2b0-8bbad752f91b",
        "last_attempt_at": 1782734401,
        "last_attempt_at_iso": "2026-06-29T12:00:01Z",
        "last_status_code": 500,
        "last_error": "callback failed"
      }
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

`callback_url` is redacted by default. Trusted detail views may request
`include_callback_url=true`. Event payloads are also excluded by default; use
`include_events=true&event_limit=10` only for trusted operator/detail views.

## Packages

Packages are DBBASIC bundles: objects, schemas, permissions, seed data, and
migrations collected under one package directory. GitHub can host the source,
but Scroll/Object Server should inspect and later install packages through the
DBBASIC API so backups, dry-runs, changelogs, and rollback points stay part of
the object loop.

The first public package surface can list packages, return dry-run plans, and
perform conservative installs when both admin auth and the explicit package
install flag are enabled. Dry-runs and installs append compact package changelog
rows so operators can see which packages were reviewed, installed, or rejected.

Package directory shape:

```text
packages/{package_id}/
  dbbasic-package.json
  objects/
  schemas/
  permissions/
  seed/
  migrations/
```

List packages:

```http
GET /packages
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "packages": [
    {
      "id": "hello-world",
      "name": "Hello World",
      "version": "0.1.0",
      "description": "Small package proving DBBASIC package discovery and dry-run planning.",
      "status": "available",
      "object_count": 1,
      "schema_count": 0,
      "permission_count": 0,
      "seed_count": 0,
      "migration_count": 0,
      "dependency_count": 0
    }
  ],
  "count": 1
}
```

Read one manifest:

```http
GET /packages/{package_id}
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "package": {
    "id": "hello-world",
    "name": "Hello World",
    "version": "0.1.0",
    "description": "Small package proving DBBASIC package discovery and dry-run planning.",
    "compatibility": {"object_server": ">=0.0.1"},
    "dependencies": [],
    "objects": [
      {"id": "hello_world", "path": "objects/hello/world.py"}
    ],
    "schemas": [],
    "permissions": [],
    "seed": [],
    "migrations": []
  }
}
```

Dry-run a future install/update:

```http
GET /packages/{package_id}?dry_run=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "dry_run": {
    "package": {
      "id": "hello-world",
      "name": "Hello World",
      "version": "0.1.0",
      "description": "Small package proving DBBASIC package discovery and dry-run planning.",
      "status": "available",
      "object_count": 1,
      "schema_count": 0,
      "permission_count": 0,
      "seed_count": 0,
      "migration_count": 0,
      "dependency_count": 0
    },
    "mode": "dry_run",
    "install_enabled": false,
    "safe_to_install": true,
    "objects": [
      {
        "id": "hello_world",
        "path": "objects/hello/world.py",
        "exists": true,
        "action": "create",
        "installed": false
      }
    ],
    "schemas": [],
    "permissions": [],
    "seed": [],
    "migrations": [],
    "warnings": []
  },
  "change": {
    "change_id": "0a34c2e6-3144-4900-8f99-fd7c7ec77c61",
    "timestamp": "2026-06-30T12:00:00+00:00",
    "package_id": "hello-world",
    "package_version": "0.1.0",
    "action": "dry_run",
    "actor": "admin",
    "message": "Dry run package install",
    "details": {
      "safe_to_install": true,
      "install_enabled": false,
      "objects": {"create": 1},
      "schemas": {},
      "permissions": {},
      "seed": {},
      "migrations": {},
      "warnings": []
    }
  }
}
```

Install a reviewed package:

```http
POST /packages/{package_id}/install
Authorization: Token <token>
Content-Type: application/json

{"allow_replace": false}
```

Package installs require:

```text
DBBASIC_ADMIN_TOKEN=...
DBBASIC_ENABLE_PACKAGE_INSTALLS=true
```

The first install implementation is deliberately narrow:

- object files are written under the configured object root
- schema JSON is validated and written under `data/schemas/`
- seed TSV is written only when `data/collections/{collection}/records.tsv`
  does not already exist
- replacing objects or schemas requires `{"allow_replace": true}`
- package permissions and migrations are rejected until merge/run semantics are
  explicit
- a runtime restore point is created before any live object, schema, or seed
  file is changed

Response:

```json
{
  "status": "ok",
  "install": {
    "package": {
      "id": "hello-world",
      "name": "Hello World",
      "version": "0.1.0",
      "description": "Small package proving DBBASIC package discovery and dry-run planning.",
      "status": "available",
      "object_count": 1,
      "schema_count": 0,
      "permission_count": 0,
      "seed_count": 0,
      "migration_count": 0,
      "dependency_count": 0
    },
    "mode": "install",
    "install_enabled": true,
    "allow_replace": false,
    "safe_to_install": true,
    "objects": [
      {
        "id": "hello_world",
        "path": "objects/hello/world.py",
        "exists": true,
        "action": "create",
        "installed": false,
        "status": "written",
        "destination": "hello/world.py"
      }
    ],
    "schemas": [],
    "permissions": [],
    "seed": [],
    "migrations": [],
    "warnings": [],
    "restore_point": {
      "path": "data/backups/20260630T120100Z-package-hello-world.tar.gz",
      "format_version": 1,
      "created_at": "2026-06-30T12:01:00Z",
      "files": 14,
      "bytes": 12345,
      "warnings": []
    }
  },
  "changes": {
    "requested": {
      "change_id": "c0c3f4a6-8b10-47cb-8309-b381354f6cc0",
      "action": "install_requested"
    },
    "installed": {
      "change_id": "c77681de-5b1e-4a5e-adc9-16701ca230f5",
      "action": "installed"
    }
  },
  "restore_point": {
    "path": "data/backups/20260630T120100Z-package-hello-world.tar.gz",
    "format_version": 1,
    "created_at": "2026-06-30T12:01:00Z",
    "files": 14,
    "bytes": 12345,
    "warnings": []
  }
}
```

Restore a package install restore point:

```http
POST /packages/{package_id}/restore
Authorization: Token <token>
Content-Type: application/json

{"change_id": "c77681de-5b1e-4a5e-adc9-16701ca230f5", "confirm": "restore-runtime"}
```

Package restore requires:

```text
DBBASIC_ADMIN_TOKEN=...
DBBASIC_ENABLE_PACKAGE_RESTORE=true
```

The change id must point at a recorded package change with restore-point
metadata. The server restores that runtime snapshot with overwrite and
prune-extra enabled, then appends `restore_requested` and `rolled_back` package
change rows. This is intentionally a whole-runtime rollback, not a selective
package uninstall, and the restore path must live under the configured backup
directory.

Response:

```json
{
  "status": "ok",
  "restore": {
    "backup_path": "data/backups/20260630T120100Z-package-hello-world.tar.gz",
    "objects_dir": "objects",
    "data_dir": "data",
    "files": 14,
    "bytes": 12345,
    "overwritten": true,
    "pruned_files": 3,
    "pruned_dirs": 2
  },
  "restore_point": {
    "path": "data/backups/20260630T120100Z-package-hello-world.tar.gz",
    "format_version": 1,
    "created_at": "2026-06-30T12:01:00Z",
    "files": 14,
    "bytes": 12345,
    "warnings": []
  },
  "from_change": {
    "change_id": "c77681de-5b1e-4a5e-adc9-16701ca230f5",
    "action": "installed"
  },
  "changes": {
    "requested": {
      "change_id": "9fd9b09c-22bf-41c2-b6ee-3a96ca195fef",
      "action": "restore_requested"
    },
    "rolled_back": {
      "change_id": "9a63dcde-ec78-426b-8787-f1e8ec401190",
      "action": "rolled_back"
    }
  }
}
```

Read package changelog:

```http
GET /packages/{package_id}/changes?limit=100&offset=0
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "package_id": "hello-world",
  "changes": [
    {
      "change_id": "0a34c2e6-3144-4900-8f99-fd7c7ec77c61",
      "timestamp": "2026-06-30T12:00:00+00:00",
      "package_id": "hello-world",
      "package_version": "0.1.0",
      "action": "dry_run",
      "actor": "admin",
      "message": "Dry run package install",
      "details": {
        "safe_to_install": true,
        "install_enabled": false,
        "objects": {"create": 1},
        "schemas": {},
        "permissions": {},
        "seed": {},
        "migrations": {},
        "warnings": []
      }
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

Package ids are route-safe lowercase names such as `hello-world` or
`crm-starter`. Package manifests use relative paths only. Absolute paths,
`..`, null bytes, unsupported permission/migration writes, and unsafe
source/data destinations are rejected at this layer.

## Schemas

```http
GET /schemas
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "schemas": [
    {
      "name": "invoices",
      "title": "Invoices",
      "source": "manual",
      "version": 1,
      "field_count": 4
    }
  ],
  "count": 1
}
```

Read one schema:

```http
GET /schemas/{collection}
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "message": "Schema updated to version 1",
  "version_id": 1,
  "collection": "invoices",
  "schema": {
    "name": "invoices",
    "title": "Invoices",
    "source": "manual",
    "version": 1,
    "fields": [
      {
        "name": "customer_id",
        "type": "relation",
        "required": true,
        "relation": {"collection": "contacts"},
        "validation": {"not_null": true}
      },
      {
        "name": "total",
        "type": "computed",
        "required": false,
        "computed": "sum(line_items)"
      },
      {
        "name": "cost_price",
        "type": "currency",
        "required": false,
        "permissions": {"admin": "edit", "sales": "hidden"},
        "ui": {"section": "totals"}
      }
    ],
    "field_count": 3
  }
}
```

Replace one manual schema:

```http
PUT /schemas/{collection}
Content-Type: application/json
Authorization: Token <token>
```

Request:

```json
{
  "schema": {
    "title": "Invoices",
    "ui": {"default_view": "form"},
    "views": [{"name": "invoice_admin", "type": "form"}],
    "fields": [
      {
        "name": "invoice_date",
        "type": "date",
        "required": true,
        "layout": {"column": 1}
      },
      {
        "name": "margin",
        "type": "currency",
        "permissions": {"admin": "edit", "sales": "hidden"}
      }
    ]
  }
}
```

Response:

```json
{
  "status": "ok",
  "schema": {
    "name": "invoices",
    "title": "Invoices",
    "source": "manual",
    "version": 1,
    "fields": [
      {"name": "invoice_date", "type": "date", "required": true},
      {
        "name": "margin",
        "type": "currency",
        "required": false,
        "permissions": {"admin": "edit", "sales": "hidden"}
      }
    ],
    "field_count": 2,
    "ui": {"default_view": "form"},
    "views": [{"name": "invoice_admin", "type": "form"}]
  }
}
```

List schema versions:

```http
GET /schemas/{collection}?versions=true&limit=10
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collection": "invoices",
  "versions": [
    {
      "version_id": 2,
      "timestamp": "2026-06-29T12:00:00",
      "author": "admin",
      "message": "Add margin field",
      "hash": "..."
    },
    {
      "version_id": 1,
      "timestamp": "2026-06-29T11:50:00",
      "author": "admin",
      "message": "Initial schema",
      "hash": "..."
    }
  ],
  "count": 2
}
```

Read one schema version:

```http
GET /schemas/{collection}?version=1
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "collection": "invoices",
  "version": {
    "version_id": 1,
    "timestamp": "2026-06-29T11:50:00",
    "author": "admin",
    "message": "Initial schema",
    "hash": "...",
    "content": "{...}",
    "schema": {
      "name": "invoices",
      "title": "Invoices",
      "source": "manual",
      "version": 1,
      "fields": [],
      "field_count": 0
    }
  }
}
```

Rollback is non-destructive. It creates a new latest version containing the old
schema content and then replaces the live schema file:

```http
POST /schemas/{collection}
Content-Type: application/json
Authorization: Token <token>
```

Request:

```json
{
  "action": "rollback",
  "version_id": 1,
  "author": "admin",
  "message": "Restore first invoice form"
}
```

Response:

```json
{
  "status": "ok",
  "message": "Rolled back schema to version 1",
  "version_id": 1,
  "new_version_id": 3,
  "collection": "invoices",
  "schema": {
    "name": "invoices",
    "title": "Invoices",
    "source": "manual",
    "version": 1,
    "fields": [],
    "field_count": 0
  }
}
```

Schema files live under:

```text
data/schemas/{collection}.json
data/schema_versions/{collection}/metadata.tsv
data/schema_versions/{collection}/vN.json
```

Manual schemas can be replaced through the admin-token gated `PUT` route. Writes
are atomic file replacements under `data/schemas/`, and each write records a
schema version. If a collection has no manual schema, the server may return an
empty derived schema for that collection so Scroll can still show the collection
and later attach fields. Missing schemas return `404`; unsafe schema names
return `400`.

Schema `permissions` and `ui` fields are preserved for generated admin screens.
With permission enforcement enabled, schema `permissions` also refine record
reads and writes after the broader server policy has allowed the row.

## Create Object

```http
POST /objects
Content-Type: application/json
Authorization: Token <token>
```

Request:

```json
{
  "name": "deals",
  "code": "def GET(request):\n    return {\"status\": \"ok\"}\n",
  "description": "Optional client-provided description"
}
```

Response:

```json
{
  "status": "ok",
  "object_id": "u_42_deals",
  "message": "Object created: u_42_deals"
}
```

The `description` field may be stored, ignored, or used for metadata, but it
should not make object creation fail.

## Execute Object

```http
GET /objects/{object_id}
POST /objects/{object_id}
PUT /objects/{object_id}
DELETE /objects/{object_id}
```

`GET` passes query parameters to the object. `POST`, `PUT`, and `DELETE` pass a
JSON body when present.

Compatibility details from the working prototype:

- `POST` merges query parameters into the JSON body without overriding body keys.
- `POST` with a non-JSON body passes raw bytes as the `body` field plus query
  parameters.
- `PUT` and `DELETE` reject invalid JSON bodies with `400`.
- request bodies over `DBBASIC_MAX_REQUEST_BYTES` are rejected with `413`.
- rate-limited requests are rejected with `429`.
- full request or object execution capacity is rejected with `503`.
- object executions over `DBBASIC_OBJECT_TIMEOUT_SECONDS` are rejected with `504`.
- `POST` with `{"action": "rollback"}` is reserved for rollback.
- `PUT /objects/{object_id}?source=true` is reserved for source updates.

Example:

```http
POST /objects/basics_counter
Content-Type: application/json
```

```json
{
  "action": "increment"
}
```

Execution responses are object-defined. The server should pass successful object
results through without wrapping them in a new envelope.

Compatibility response shapes:

- normal dicts return JSON
- dicts with `content_type` and `body` return raw HTTP responses
- `(status, headers, body)` tuples return low-level HTTP responses
- strings return `text/html; charset=utf-8`
- bytes return `application/octet-stream`

Example HTML object response:

```python
def GET(request):
    return {
        "content_type": "text/html; charset=utf-8",
        "body": "<!doctype html><h1>DBBASIC</h1>",
    }
```

Example tuple response:

```python
def POST(request):
    return (201, [("Content-Type", "text/plain")], [b"created"])
```

On server-side execution failure, return a non-2xx status with:

```json
{
  "status": "error",
  "error": "Execution failed: ..."
}
```

Future structured error fields may be added, but existing clients must still be
able to read the `error` field.

If `DBBASIC_OBJECT_TIMEOUT_SECONDS` is set above zero, the public ASGI server
runs object methods in a subprocess. When the timeout expires, the worker is
terminated and the client receives:

```http
504 Gateway Timeout
```

```json
{
  "status": "error",
  "error": "Execution failed: GET timed out for object basics_slow after 5 seconds"
}
```

The timeout is a wall-clock boundary. It is not a complete CPU or memory
sandbox.

Objects named in `DBBASIC_TRUSTED_IN_PROCESS_OBJECTS` keep the fast in-process
execution path even when timeouts are enabled. This is for reviewed
server-owned objects, such as a high-traffic public homepage. Do not use it for
unreviewed user code.

## Source

```http
GET /objects/{object_id}?source=true&format=json
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "source": "def GET(request):\n    return {\"status\": \"ok\"}\n"
}
```

HTML source rendering can exist for browsers, but JSON must stay available with
`format=json` or an `Accept: application/json` request.

## Update Source

```http
PUT /objects/{object_id}?source=true
Content-Type: application/json
Authorization: Token <token>
```

Request:

```json
{
  "code": "def GET(request):\n    return {\"status\": \"updated\"}\n",
  "author": "api",
  "message": "Updated via client"
}
```

Response:

```json
{
  "status": "ok",
  "message": "Code updated to version 2",
  "version_id": 2,
  "object_id": "u_42_deals"
}
```

The update must save a source version, write the source file, and leave enough
information for clients to show the new version number.

The current public server keeps this endpoint closed by default. Local
development source writes require:

```bash
DBBASIC_ENABLE_SOURCE_WRITES=true
DBBASIC_ADMIN_TOKEN=replace-with-a-local-dev-token
```

The token value shown here is a placeholder. Real deployments must generate a
server-specific secret outside the source tree. Production code should replace
that temporary gate with the real auth and permission system before this
endpoint is exposed to users.

## State

```http
GET /objects/{object_id}?state=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "state": {
    "count": 3
  }
}
```

State is currently read from:

```text
data/state/{object_id}/state.tsv
```

Rows may be `key<TAB>value` or `key<TAB>value<TAB>timestamp`. The public server
currently exposes state read-only.

## Metadata

```http
GET /objects/{object_id}?metadata=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "metadata": {
    "object_id": "basics_counter",
    "source_path": "basics/counter.py",
    "owner": "system",
    "kind": "system",
    "last_modified": 1760000000.0,
    "state_count": 1,
    "state_keys": ["count"],
    "log_count": 3,
    "file_count": 1,
    "version_count": 2
  }
}
```

The public server reports source paths relative to the object source root, not
absolute local filesystem paths. Metadata may grow over time. Existing clients
expect the top-level `metadata` field.

## Files

```http
GET /objects/{object_id}?files=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "site_home",
  "files": [
    {
      "name": "report.txt",
      "size": 1200,
      "modified": 1760000000.0
    }
  ],
  "count": 1
}
```

Download one object-owned file:

```http
GET /objects/{object_id}?file=report.txt
Authorization: Token <token>
```

The server reads files from:

```text
data/files/{object_id}/
```

Filenames are validated before filesystem access. Empty names, absolute paths,
null bytes, and `..` traversal are rejected with `400`. Missing files return
`404`.

This public slice is read-only. Upload and delete routes should wait for
explicit size limits, content policy, audit trails, and permission enforcement.

## Logs

```http
GET /objects/{object_id}?logs=true&format=json&limit=100
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "logs": [
    {
      "entry_id": "a1",
      "timestamp": "2026-01-01T00:00:00",
      "level": "DEBUG",
      "message": "GET completed successfully",
      "method": "GET",
      "status": "success",
      "duration_ms": "1.25"
    }
  ],
  "count": 1
}
```

Optional query parameters:

- `level` filters by exact log level, such as `INFO` or `ERROR`.
- `limit` defaults to `100` in the public ASGI server.

Logs are currently read from:

```text
data/logs/{object_id}/log.tsv
data/logs/{object_id}/log-*.tsv
data/logs/{object_id}/log-*.tsv.gz
```

The TSV header defines the fields. The normal fields are `entry_id`,
`timestamp`, `level`, and `message`; object code may add extra columns such as
`method`, `status`, `duration_ms`, `error_type`, `error`, or `user_id`.

The public ASGI server appends one execution log entry after each object method
run. Successful runs use `level=DEBUG` and `status=success`; failed runs use
`level=ERROR`, `status=error`, and include error fields. The endpoint itself
remains read-only.

Log storage may rotate and gzip old logs on disk. The HTTP response shape does
not change; clients still receive JSON log entries. Transport compression for
large log responses should be handled through normal HTTP compression, such as a
reverse proxy honoring `Accept-Encoding`, rather than changing this JSON shape.

## Source Changes

```http
GET /objects/{object_id}?source_changes=true&limit=100&offset=0
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "changes": [
    {
      "change_id": "123e4567-e89b-42d3-a456-426614174000",
      "timestamp": "2026-01-01T00:00:00",
      "object_id": "basics_counter",
      "action": "source_update",
      "version_id": 2,
      "from_version_id": null,
      "actor": "api",
      "message": "Updated via client",
      "correlation_id": "123e4567-e89b-42d3-a456-426614174001",
      "details": {}
    }
  ],
  "count": 1,
  "total": 1,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

History is newest first and does not include source content. Use the versions
endpoint to inspect or restore source snapshots. Source changes are the operator
activity timeline for source edits and rollbacks.

## Versions

```http
GET /objects/{object_id}?versions=true&limit=10
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "versions": [
    {
      "version_id": 2,
      "timestamp": "2026-01-01T00:00:00",
      "author": "api",
      "message": "Updated via client",
      "hash": "..."
    }
  ],
  "count": 1
}
```

History is newest first and does not include source content.

## Specific Version

```http
GET /objects/{object_id}?version=2
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "object_id": "basics_counter",
  "version": {
    "version_id": 2,
    "timestamp": "2026-01-01T00:00:00",
    "author": "api",
    "message": "Updated via client",
    "hash": "...",
    "content": "def GET(request):\n    return {\"status\": \"ok\"}\n"
  }
}
```

## Rollback

```http
POST /objects/{object_id}
Content-Type: application/json
Authorization: Token <token>
```

Request:

```json
{
  "action": "rollback",
  "version_id": 1,
  "author": "api",
  "message": "Rollback to version 1"
}
```

Response:

```json
{
  "status": "ok",
  "message": "Rolled back to version 1",
  "version_id": 1,
  "new_version_id": 3,
  "object_id": "u_42_deals"
}
```

Rollback is non-destructive. The server should create a new latest version from
the old source, write that source to the object file, and preserve history.

The historical response reports the requested rollback version in `version_id`.
Future responses may add `new_version_id`, but must not remove `version_id`
without a client migration.

The current public server supports `new_version_id` and uses the same temporary
source-write gate as source updates:

```bash
DBBASIC_ENABLE_SOURCE_WRITES=true
DBBASIC_ADMIN_TOKEN=replace-with-a-local-dev-token
```

The token value shown here is a placeholder. Real deployments must generate a
server-specific secret outside the source tree. Production code should replace
that temporary gate with the real auth and permission system before rollback is
exposed to users.

## Destroy Object

```http
DELETE /objects/{object_id}?destroy=true
Authorization: Token <token>
```

Response:

```json
{
  "status": "ok",
  "message": "Object destroyed: u_42_deals",
  "object_id": "u_42_deals"
}
```

Destructive delete must be authenticated and authorization-checked.

## Routing Compatibility

The historical server supports explicit station routing:

```http
GET /objects/{object_id}@{station_id}
```

The public v1 server does not need to promise distributed correctness, but code
should not accidentally treat `@` routing as a normal object ID. If station
routing is disabled, return a clear error instead of executing the wrong object.

## Compatibility Rules

- Keep `/objects` and `/objects/{object_id}` as the main HTTP surface.
- Keep query flags such as `source=true`, `state=true`, and `versions=true`.
- Keep `PUT /objects/{object_id}?source=true` for source updates.
- Keep rollback as `POST /objects/{object_id}` with `action=rollback`.
- Keep top-level fields existing clients read: `status`, `error`, `object_id`,
  `objects`, `source`, `state`, `metadata`, `logs`, `versions`, `version`,
  `version_id`, `message`, and `count`.
- Add fields only when old clients can ignore them safely.
- Do not require clients to move to a new path such as `/api/v1` without a
  compatibility layer.
