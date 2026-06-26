# Security Policy

DBBASIC Object Server is being extracted from a working private prototype in small, reviewed public commits.

Security work is part of the product, not an afterthought. The runtime will eventually execute user-owned Python objects, persist state/logs/files, expose HTTP endpoints, and support tools such as DBBASIC Scroll. Treat changes to execution, authentication, authorization, persistence, file access, source editing, logs, and deployment as security-sensitive.

## Supported Versions

This repository is pre-release. There are no supported production versions yet.

Public commits should still be reviewed as if they may become part of the production runtime.

## Reporting Security Issues

Please do not open a public issue that includes:

- live deployment hostnames or IP addresses
- credentials, API keys, tokens, cookies, or private URLs
- exploit details that would enable immediate abuse
- private object source, state, logs, files, or customer data

Send security reports to:

```text
dan@askrobots.com
```

Use a concise subject such as:

```text
Security report: dbbasic-object-server
```

Include enough information to reproduce the issue safely with local or placeholder configuration.

## Public Sample Values

Use only safe placeholder values in public reports, code, tests, and documentation:

- `127.0.0.1` for localhost samples
- `192.0.2.0/24`, `198.51.100.0/24`, or `203.0.113.0/24` for documentation IPs
- `example.com`, `example.net`, or `example.org` for documentation domains

Do not include real LAN IPs, cloud IPs, customer hostnames, private station names, filesystem paths, tokens, or secrets.

## Security-Sensitive Areas

Review these areas carefully before merging changes:

- object execution
- source loading and import restrictions
- permissions and row filters
- authentication and session handling
- state, log, file, and version storage
- queue, scheduler, and event processing
- external callbacks and webhooks
- cluster/station communication
- backup, restore, and migration paths
- DBBASIC Scroll access to source, state, logs, files, and versions

## Current Status

The public repository currently contains a small daemon and tests. It does not yet contain the full runtime, auth system, API server, permissions layer, or production deployment configuration.

Do not assume this repository is production-ready until the runtime, permissions, execution boundaries, and deployment process are documented and tested.
