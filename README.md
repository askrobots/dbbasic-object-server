# DBBASIC Object Server

DBBASIC Object Server runs live, versioned Python application objects.

This repository is being assembled from an existing working prototype. The public codebase is intentionally moving in small reviewed slices so each piece can be tested, documented, and checked for private deployment details before release.

The rest of the server will move here as it is cleaned up for release.

## The Core Idea

A DBBASIC object is one small Python file that can do useful application work.

An object can be an API endpoint, page, report, worker, webhook, admin action,
scheduled job, or business record handler. It can also keep state, write logs,
store files, and keep old source versions.

The point is to keep the things needed for development close together:

- source
- state
- logs
- files
- versions
- runtime errors
- execution output

That gives DBBASIC a short loop:

```text
edit one object -> run it -> see output/logs/state/errors -> fix it -> keep the version trail
```

This is the `100x dev loop` this project is trying to protect.

## Why It Is Different

DBBASIC is not trying to copy Rails, Django, or a normal MVC framework.

Those patterns can still be built with objects when they are useful, but they
are not required. The server starts with the object itself.

The old CGI model had a simple idea: a request could map directly to code. The
problem was speed, because classic CGI started a new process for every request.

DBBASIC keeps the direct mental model but uses ASGI so the server stays running.
Then it adds the missing parts: source, state, logs, files, versions, runtime
errors, and rollback all belong near the object.

That makes the system useful for humans and AI tools:

- change one object without redeploying the whole app
- execute it immediately
- inspect what happened
- patch the source
- keep or roll back the version

## What Objects Can Do

- handle HTTP requests
- run from queues, schedules, events, or tools
- state and logs are stored in simple file-backed formats
- companion tools such as DBBASIC Scroll can inspect and operate the runtime

## Current Public Contents

This repository currently contains:

- `object_namespace.py` - object source discovery and object ID resolution
- `object_execution.py` - structured object execution results and error capture
- `object_versions.py` - source version metadata, content snapshots, and rollback
- `object_daemon.py` - background worker for scheduler, queue, events, and cleanup

It does not yet contain the full object server, API handlers, object runtime, sample applications, package system, or production deployment files.

## Object Source Directories

New DBBASIC object source should live under `objects/`.

Set `DBBASIC_OBJECTS_DIR` to point at a custom object source directory during migration or deployment.

## Current Extraction Slice

The current public slice is not the whole server yet. It defines the first shared
rules the rest of the server will use:

- `object_namespace.py` maps object IDs to files under `objects/`
- `object_execution.py` returns success or error results from object runs
- `object_versions.py` keeps source history as `metadata.tsv` plus `vN.txt` files
- `object_daemon.py` runs scheduled, queued, and event work
- `basics_counter` maps to `objects/basics/counter.py`
- `u_42_deals` maps to `objects/users/42/deals.py`
- rollbacks create a new version instead of deleting history
- the old prototype source directory name is intentionally not a public default

These pieces come first so the ASGI server, daemon, Scroll, tests, and migration
tools all agree on the same object rules.

See `docs/runtime-contract.md` for the daemon-facing runtime contract and
`docs/http-api-contract.md` for the HTTP API shape that existing clients expect.

Read `SECURITY.md` and `CONTRIBUTING.md` before copying code or documentation from private prototypes into this repository.

## Status

Early public assembly.

The object server has been useful internally, but this repository is intentionally starting small so the public codebase can be reviewed and cleaned as it grows.

Near-term work:

- move the core object runtime into this repository
- add a minimal runnable server
- document object conventions
- add tests
- define permissions and execution boundaries
- document production deployment

## Public Repository Safety

This repository is being extracted from a working private prototype in small, reviewed commits.

Before code or docs are copied here, they should be checked for private deployment details, secrets, credentials, local paths, real hostnames, and real IP addresses.

Public sample configuration should use only safe placeholder values:

- `127.0.0.1` for localhost samples
- `192.0.2.0/24`, `198.51.100.0/24`, or `203.0.113.0/24` for documentation IPs
- `example.com`, `example.net`, or `example.org` for documentation domains
- `.env.example` for configuration shape, never real `.env` values

Do not commit real LAN IPs, cloud IPs, customer hostnames, API tokens, private URLs, personal filesystem paths, or deployment-specific station names.

## DBBASIC Scroll

DBBASIC Scroll is the companion app for connecting to an object server, browsing objects, executing them, inspecting source/state/logs/versions/files, and managing the system.

Scroll will remain optional: the object server should be usable through HTTP and command-line tools without requiring the GUI.

## License

MIT License. See `LICENSE`.
