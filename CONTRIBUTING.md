# Contributing

DBBASIC Object Server is being extracted from a working private prototype in small, reviewed public commits.

The goal is to keep the public repository useful, testable, and safe while the production runtime is moved here piece by piece.

## Contribution Rules

- Keep commits small enough to review.
- Include focused tests with implementation changes.
- Do not copy large private subsystems into this repository before reviewing them for security and product fit.
- Do not commit secrets, credentials, private URLs, real deployment hostnames, real LAN or cloud IPs, customer data, private object source, local filesystem paths, or deployment-specific station names.
- Use `objects/` for DBBASIC object source. Do not introduce a new public object source root without updating the runtime contract.
- Preserve compatibility with the existing `dbbasic_object_core` runtime while package layout is being decided.
- Treat object execution, permissions, authentication, source loading, state, logs, files, versions, backups, and migrations as security-sensitive.

## Public Placeholder Values

Use safe placeholders in docs, tests, examples, and reports:

- `127.0.0.1` for localhost samples
- `192.0.2.0/24`, `198.51.100.0/24`, or `203.0.113.0/24` for documentation IPs
- `example.com`, `example.net`, or `example.org` for documentation domains
- `.env.example` for configuration shape, never real `.env` values

## Runtime Contract

Before changing daemon behavior, object source lookup, scheduler behavior, queue processing, event delivery, or cleanup behavior, read `docs/runtime-contract.md`.

The public runtime should avoid drifting away from the working private runtime. When a compatibility break is necessary, document it in the same commit as the code and tests.

## Local Checks

Run the tests before committing:

```bash
pytest
```

Run a safety scan before publishing copied code or docs:

```bash
rg -n "192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|/Users/|/Volumes/|token|secret|password|api[_-]?key|BEGIN .*PRIVATE|askrobots\.com" .
```

Some hits are expected in policy documents, project metadata, or contact information. Review every hit before pushing.

## Pull Requests

A useful pull request should include:

- a short description of what changed
- the tests that were added or updated
- the local test result
- any runtime-contract or security implications

Docs-only changes do not need new tests, but they should still avoid private deployment details.
