# Private packages (closed-source overlay)

This directory is the **open-core overlay**: proprietary packages that are
*not* part of the MIT-licensed distribution live here. Everything in this
folder except this README is gitignored and never reaches source control.

The open server is fully functional without anything here. A private package
adds a proprietary capability on top — the first being **Mailcow email
hosting** (mailbox provisioning, wildcard addresses, inbound), the
closed counterpart to the open, generic `01` email *adapter*
(`object_email.py` + `app-email`), which stays MIT.

## How it works

The server treats this as a second package root, searched **ahead of**
`packages/`. So:

- Drop a normal package here (`packages-private/<id>/dbbasic-package.json`
  + `schemas/`, `objects/`, `seed/`, `permissions/`, exactly like an open
  package) and it is discovered, listed, and installable through the same
  admin/CLI flow — no special casing.
- If a private package shares an `id` with an open one, **the private copy
  wins** (an overlay/override). Use a distinct id for a genuinely new
  package; reuse an id only when you deliberately mean to shadow the open
  one.

Root resolution (highest precedence first):

1. `packages-private/` — this directory (env: `DBBASIC_PRIVATE_PACKAGES_DIR`)
2. `packages/` — the open-core packages (env: `DBBASIC_PACKAGES_DIR`)

## Why a package, not core code

Keeping proprietary integrations as *packages* — schema + permissions +
objects, declared as data — rather than edits to core keeps the boundary
clean: the open server never imports anything from here, upgrades to core
don't touch private packages, and a private package is deployed, versioned,
and reconciled through the exact same machinery as an open one. If a
private package needs a background worker, that is the one current seam that
still lands in core (there is no manifest hook to register a daemon pass
yet) — keep such code behind an env/feature flag and out of the open modules.
