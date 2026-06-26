# DBBASIC Object Server

DBBASIC Object Server is a Python runtime for live, versioned application objects.

This repository is being assembled from an existing working prototype. The first public commit contains the object daemon: a background worker for scheduled tasks, queue processing, event delivery, and runtime cleanup.

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

- `object_daemon.py` - background worker for scheduler, queue, events, and cleanup

It does not yet contain the full object server, API handlers, object runtime, sample applications, package system, or production deployment files.

## Object Source Directories

New DBBASIC object source should live under `objects/`.

Set `DBBASIC_OBJECTS_DIR` to point at a custom object source directory during migration or deployment.

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
