# Runtime Contract

This document records the public contract that DBBASIC Object Server code should preserve while the implementation is extracted from the working prototype.

The goal is compatibility. New public code should not create a parallel runtime shape that breaks existing DBBASIC objects, DBBASIC Scroll, or the current `dbbasic_object_core` import path without an explicit migration plan.

## Package Boundary

The existing runtime package is:

```python
from dbbasic_object_core.runtime.object_runtime import ObjectRuntime
```

For now, `dbbasic_object_core` is the compatibility target for the core runtime. If a future `dbbasic_object_server` package is added, it should start as a thin server or command-line layer around `dbbasic_object_core`, not as a replacement runtime namespace.

## Object Source Root

New object source should live under:

```text
objects/
```

The object source root can be overridden with:

```text
DBBASIC_OBJECTS_DIR=/path/to/objects
```

Public code should not use the old prototype directory name as a default.

## Object ID Resolution

The daemon-facing object ID rules are:

- `category_name` resolves to `objects/category/name.py`
- `u_{user_id}_{name}` resolves to `objects/users/{user_id}/{name}.py`
- trigger objects live under `objects/triggers/`

Public code should use `object_namespace.py` for object source lookup rather than reimplementing these rules in each server, daemon, or tool surface.

Current trigger object names:

- `scheduler`
- `queue`
- `events`

## Runtime Interface

The daemon expects a runtime object with:

```python
runtime.load_object(path, object_id=None)
```

`path` is a `Path` or string pointing to an object source file. `object_id` is optional when the ID can be derived from the file name, and required when executing a resolved target object by ID.

The returned object must expose:

```python
obj.logger
obj.state_manager
obj.execute(method, payload)
```

Loaded object modules receive `_logger` and `_state_manager`. The current public
runtime injects a minimal state manager with `get`, `set`, `get_all`, and
`reload`.

## Storage Boundary

The default runtime storage is file-backed:

- object source under `objects/`
- object state under `data/state/`
- object logs under `data/logs/`
- object versions under `data/versions/`
- object-owned files under `data/files/`
- collection records under `data/collections/`
- collection schemas under `data/schemas/`

SQL databases, SQLite, external HTTP APIs, and AI APIs are optional integrations.
They should be reachable from objects or packages when needed, but the base
runtime should not require a database service before the first useful app can
run.

That boundary is intentional. It keeps the single-VM deployment small, makes
backup/restore understandable, and lets Scroll inspect source, state, logs,
versions, schemas, and diagrams without turning SQL into the default storage
contract.

Runtime backups should use `object_backup.py`. The portable archive contains
object source plus `data/state/`, `data/logs/`, `data/versions/`, and
`data/files/`, `data/schemas/`, and `data/collections/`. It deliberately leaves
deployment secrets, service files, virtualenvs, git history, lock files, temp
files, and ephemeral rate-limit files outside the archive.

## Object Execution Result

Public code should use `object_execution.py` for the shared execution result shape.
The future ASGI server, daemon, Scroll execute button, and AI repair loop should
all be able to depend on the same fields:

- `object_id`
- `method`
- `path`
- `ok`
- `result`
- `error`
- `started_at`
- `finished_at`
- `duration_ms`

Execution failures should be captured as data instead of being flattened into
plain strings. Error data includes:

- `type`
- `message`
- `traceback`

This is part of the `100x dev loop`: execute an object, inspect the structured
error and traceback, patch the source, and keep the version trail close to the
runtime feedback.

## Object HTTP Responses

The runtime returns plain Python values. The ASGI/web layer turns those values
into HTTP responses.

Compatibility response shapes:

```python
return {"status": "ok", "count": 1}
```

Normal dicts are JSON responses and are not wrapped in another envelope.

```python
return {
    "content_type": "text/html; charset=utf-8",
    "body": "<!doctype html><h1>DBBASIC</h1>",
}
```

Dicts with `content_type` and `body` become raw HTTP responses. The body may be
text or bytes. This is the object shape used by generated views, HTML pages, and
binary/image objects.

```python
return (201, [("Content-Type", "text/plain")], [b"created"])
```

Tuples in `(status, headers, body)` form are passed through as low-level HTTP
responses. `body` may be bytes, text, or a list of bytes/text parts.

Plain string returns are served as `text/html; charset=utf-8`. Plain bytes
returns are served as `application/octet-stream`.

## Object Versions

Public code should use `object_versions.py` for source version storage. The
storage format intentionally matches the working prototype:

```text
data/versions/{object_id}/metadata.tsv
data/versions/{object_id}/v1.txt
data/versions/{object_id}/v2.txt
```

`metadata.tsv` fields are:

- `version_id`
- `timestamp`
- `author`
- `message`
- `hash`

History is returned newest first and does not include source content. Fetching a
specific version returns metadata plus `content`.

Rollback is non-destructive. Rolling back to version `N` creates a new latest
version containing the old content, preserving the full history.

The future runtime should keep the prototype behavior:

- save an initial version when an object is first loaded and no history exists
- on source update, save the new code as a version, write it to the source file,
  reload the object, and log the update
- on rollback, create the rollback version, write it to the source file, reload
  the object, and log the rollback

Public server/runtime code should use `object_source.py` for the source file
read, update, and rollback steps so source writes and version storage do not
drift into separate implementations.

## State Manager Interface

The daemon expects trigger objects to expose a state manager with:

```python
state_manager.reload()
state_manager.get_all()
state_manager.get(key)
state_manager.set(key, value)
```

Values used by scheduler, queue, and events are JSON strings stored by key.

State storage intentionally matches the working prototype:

```text
data/state/{object_id}/state.tsv
```

Rows are either:

```text
key<TAB>value
key<TAB>value<TAB>timestamp
```

The public state reader skips an optional `key<TAB>value<TAB>timestamp` header,
ignores malformed rows, and coerces values to `int`, then `float`, otherwise
keeps strings.

The public runtime-owned state manager writes sorted rows in timestamp format:

```text
key<TAB>value<TAB>timestamp
```

There is still no public HTTP state-write endpoint. Object code writes state
through `_state_manager`.

## Log Storage

Log storage intentionally matches the working prototype:

```text
data/logs/{object_id}/log.tsv
data/logs/{object_id}/log-*.tsv
data/logs/{object_id}/log-*.tsv.gz
```

Rows are TSV with a header. The default fields are:

- `entry_id`
- `timestamp`
- `level`
- `message`
- `method`
- `status`
- `duration_ms`
- `error_type`
- `error`

Object code may add extra fields such as `method`, `user_id`, or request
metadata. The public log reader returns the current `log.tsv` first, then
rotated `log-*.tsv` and `log-*.tsv.gz` files in sorted order, and supports
exact `level` filtering and `limit`.

The active `log.tsv` stays plain TSV so appends, `tail`, and crash recovery stay
simple. When it reaches the configured size limit, the runtime rotates it to a
timestamped file and gzip-compresses the rotated file by default.

Log maintenance settings:

- `DBBASIC_LOG_MAX_BYTES` controls active log size before rotation. The default
  is `10485760` bytes. Values less than or equal to `0` disable rotation.
- `DBBASIC_LOG_COMPRESS_ROTATED` controls gzip compression of new rotated logs.
  The default is `true`. Set it to `false` for plain rotated TSV files.
- `DBBASIC_LOG_KEEP_ROTATED` controls garbage collection of rotated logs. The
  default is `32`. Values less than or equal to `0` keep all rotated logs.

Compressed rotated logs remain ordinary files on disk. Operators can inspect
them without expanding a second copy:

```bash
gzip -cd data/logs/site_home/log-*.tsv.gz
```

This is at-rest compression. HTTP response compression, log replication
compression, and backup-transfer compression are separate transport concerns.
Those should compress larger batches or responses, not tiny realtime events by
default.

The public ASGI execution path appends one log entry after each object method
run. Successful runs use `DEBUG` with `status=success`; failed runs use `ERROR`
with `status=error`, `error_type`, and `error`.

Loaded object modules also receive `_logger`. The public logger helper writes to
the same per-object TSV path and exposes:

```python
_logger.log(level, message, **fields)
_logger.debug(message, **fields)
_logger.info(message, **fields)
_logger.warning(message, **fields)
_logger.error(message, **fields)
_logger.critical(message, **fields)
_logger.get_logs(level=None, limit=None, offset=0, **filters)
```

This keeps object-owned application logs and runtime execution logs together
without creating a second logging format.

## Object Files

Object-owned files live under:

```text
data/files/{object_id}/
```

The public helper module is `object_files.py`. It currently exposes read-only
operations:

```python
list_object_files(object_id, base_dir="data")
read_object_file(object_id, filename, base_dir="data")
```

`list_object_files` returns dictionaries with:

- `name`
- `size`
- `modified`

`read_object_file` returns raw bytes plus the same metadata. Filenames are
validated before filesystem access. Empty names, absolute paths, null bytes, and
`..` traversal are rejected.

Upload and delete are intentionally not public yet. They need explicit request
size limits, content policy, audit logs, and server-enforced permissions.

## Object Collections

Collections are a read-only derived view. The public helper modules are
`object_collections.py` and `object_records.py`.

The server infers collection names from object IDs:

- `site_home` belongs to `site`
- `apps_widget_counter` belongs to `apps`
- `u_42_deals` belongs to `deals`

Permission rules can also name a collection before any object exists for it.
Record files can name a collection before any object exists for it too. That
lets Scroll show planned or data-backed collections while the server keeps
`/objects` as the stable source of executable behavior.

Collection summaries include:

- object count
- object owner set
- object kind counts
- object-owned file count
- number of objects with state
- number of objects with logs
- whether a TSV record file exists
- exact permission rule summary for that collection

The log portion is intentionally a cheap file-presence check. Listing
collections should not scan every log entry on a busy server.

Collection records live under:

```text
data/collections/{collection}/records.tsv
```

The TSV file must have a header row and an `id` column. The current public
record API is:

```python
list_collection_records(collection, base_dir="data", limit=100, offset=0)
read_collection_records(collection, base_dir="data")
get_collection_record(collection, record_id, base_dir="data")
create_collection_record(collection, record, base_dir="data")
update_collection_record(collection, record_id, changes, base_dir="data")
delete_collection_record(collection, record_id, base_dir="data")
```

Values are stored and returned as strings. Record writes preserve existing
columns and append new columns from submitted fields. Record IDs are unique and
cannot be changed by update.

If a manual schema exists for the collection, create/update validates known
fields before writing the TSV:

- `required: true`, `validation.required`, and `validation.not_null` require a
  non-empty value
- `default` fills missing or empty fields during create
- `type` can validate `integer`, `number`, `currency`, `boolean`, `date`,
  `datetime`, and `enum`
- `enum`, `validation.choices`, or `validation.in` restrict allowed values
- `validation.min`, `validation.max`, `validation.min_length`,
  `validation.max_length`, `validation.pattern`, and `validation.regex` are
  enforced
- `type: "computed"`, `computed`, or read-only metadata rejects submitted
  writes for that field

Unknown fields are still allowed. This preserves the DBBASIC rule that schemas
can tighten important collections without turning every object into a migration
project.

The HTTP record routes are admin-gated by default. When permission audit or
enforcement is enabled, read routes use the server permission policy. Record
lists apply row filters before pagination; record detail checks evaluate the
selected row and can redact fields. Record writes require either the admin token
or enforcement mode with an allowed `create`, `update`, or `delete` decision.
Audit-only mode records write decisions without granting mutation access.

## Collection Schemas

Schema metadata lives under:

```text
data/schemas/{collection}.json
```

The public helper module is `object_schemas.py`. It currently exposes read-only
operations:

```python
list_schemas(base_dir="data")
get_schema(collection, base_dir="data")
```

A schema file can describe fields, validation rules, computed values, UI hints,
field permissions, and relations:

```json
{
  "title": "Invoices",
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
      "name": "cost_price",
      "type": "currency",
      "permissions": {"admin": "edit", "sales": "hidden"},
      "ui": {"section": "totals"}
    }
  ]
}
```

If a collection exists but no manual schema file exists, the server can return
an empty derived schema. That lets Scroll render the collection first and add
field metadata later without breaking the object API.

Schema metadata is not the permission system by itself. Permission rules still
control access modes, ownership, sharing, row filters, and field allow/deny
lists. Schema fields tell tools what exists; permissions decide who can see or
change it.

Schema writes are intentionally not public yet. They need source/package audit
trails, server-enforced permissions, and migration rules before they become a
write API.

## Scheduler State

Scheduler state keys begin with:

```text
task_
```

Each value is a JSON object with fields such as:

- `id`
- `object_id`
- `method`
- `payload`
- `type`
- `schedule`
- `status`
- `next_run`
- `last_run`
- `run_count`

The daemon executes active tasks when `next_run` is due.

## Queue State

Queue state keys begin with:

```text
msg_
```

Each value is a JSON object with fields such as:

- `id`
- `queue_name`
- `message`
- `priority_level`
- `status`
- `created_at`
- `visible_after`
- `expires_at`
- `attempts`
- `max_attempts`

`message` should include:

- `object_id`
- `method`
- `payload`

The daemon marks messages as `processing`, `completed`, `pending`, `expired`, or `failed`.

## Event State

Event subscription keys begin with:

```text
sub_
```

Event keys begin with:

```text
event_
```

Subscriptions include:

- `id`
- `event_type`
- `callback_url`
- `last_event_id`

Events include:

- `id`
- `event_type`
- `payload`
- `timestamp`

The daemon delivers matching events to the subscription callback URL and records `last_event_id`.

## Public Safety

Tests and docs should use only safe placeholder values:

- `127.0.0.1` for localhost samples
- `192.0.2.0/24`, `198.51.100.0/24`, or `203.0.113.0/24` for documentation IPs
- `example.com`, `example.net`, or `example.org` for documentation domains

Do not commit real deployment IPs, hostnames, tokens, private URLs, or personal filesystem paths.
