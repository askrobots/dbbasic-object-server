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

## State Manager Interface

The daemon expects trigger objects to expose a state manager with:

```python
state_manager.reload()
state_manager.get_all()
state_manager.get(key)
state_manager.set(key, value)
```

Values used by scheduler, queue, and events are JSON strings stored by key.

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
