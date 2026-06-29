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

## Permissions Policy

The public server now has a persisted permission policy shape. The endpoints are
admin-gated while the real user/session layer is still being extracted.

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
when explicitly enabled:

```text
DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true
```

When enforcement is enabled, denied route checks return the
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

Create one record:

```http
POST /collections/{collection}/records
Authorization: Token <token>
Content-Type: application/json
```

```json
{
  "id": "c2",
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
    "id": "c2",
    "first_name": "Grace",
    "last_name": "Hopper"
  }
}
```

Successful creates return `201`. Duplicate record IDs return `409`.

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
      }
    ],
    "field_count": 2
  }
}
```

Schema files live under:

```text
data/schemas/{collection}.json
```

Manual schemas are read-only through the current public HTTP surface. If a
collection has no manual schema, the server may return an empty derived schema
for that collection so Scroll can still show the collection and later attach
fields. Missing schemas return `404`; unsafe schema names return `400`.

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
