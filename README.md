# DBBASIC Object Server

DBBASIC Object Server is a Python runtime for live, versioned application objects.

This repository is being assembled from an existing working prototype. The public codebase is intentionally moving in small reviewed slices so each piece can be tested, documented, and checked for private deployment details before release.

The rest of the server will move here as it is cleaned up for release.

## What DBBASIC Objects Are

A DBBASIC object is a small unit of application behavior. In the current prototype, objects are Python files that can expose HTTP behavior, keep local state, write logs, attach files, and retain versions.

The goal is to make small web and business applications easier to build, inspect, operate, and evolve:

- code, data, state, logs, files, and versions live close together
- objects can be executed through HTTP endpoints
- state and logs are stored in simple file-backed formats
- background work runs through daemon-managed scheduler, queue, and event loops
- companion tools such as DBBASIC Scroll can inspect and operate the runtime

## Current Public Contents

This repository currently contains:

- `object_namespace.py` - object source discovery and object ID resolution
- `object_execution.py` - structured object execution results and error capture
- `object_daemon.py` - background worker for scheduler, queue, events, and cleanup

It does not yet contain the full object server, API handlers, object runtime, sample applications, package system, or production deployment files.

## Object Source Directories

New DBBASIC object source should live under `objects/`.

Set `DBBASIC_OBJECTS_DIR` to point at a custom object source directory during migration or deployment.

## Current Extraction Slice

The current public slice defines the object namespace and execution contracts before the full server is copied over:

- `object_namespace.py` is the shared source of truth for object source lookup
- `object_execution.py` is the shared result shape for object execution success and failure
- `object_daemon.py` uses that resolver instead of keeping separate path rules
- system object IDs such as `basics_counter` resolve under `objects/basics/counter.py`
- user object IDs such as `u_42_deals` resolve under `objects/users/42/deals.py`
- trigger objects resolve under `objects/triggers/`
- execution failures are captured as structured error data with traceback text
- the old prototype source directory name is intentionally not a public default

This matters because the future ASGI server, daemon, Scroll integration, tests, and migration tools should all use the same object ID rules instead of drifting into separate routing systems.

This is the first public piece of the `100x dev loop`: keep the direct CGI-style mental model while using ASGI to avoid classic CGI's fork-per-request cost. The object loop then adds the part normal frameworks usually do not keep together: source, state, logs, versions, runtime errors, and execution feedback. That combination should make it practical for humans and AI tools to run an object, inspect the failure, patch the source, and keep a version trail without making Git the inner development loop.

This namespace slice comes first so the later ASGI server can sit on top of a clean object model instead of re-growing framework routing complexity.

See `docs/runtime-contract.md` for the daemon-facing runtime contract that future implementation commits should preserve.

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
