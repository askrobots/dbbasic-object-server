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
obj.state_manager
obj.execute(method, payload)
```

Loaded object modules receive `_state_manager`. The current public runtime
injects a minimal manager with `get`, `set`, `get_all`, and `reload`.

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
rotated `log-*.tsv` files in sorted order, and supports exact `level` filtering
and `limit`.

The public ASGI execution path appends one log entry after each object method
run. Successful runs use `DEBUG` with `status=success`; failed runs use `ERROR`
with `status=error`, `error_type`, and `error`. A future runtime logger helper
should use the same storage path and field rules instead of creating a second
log format.

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
