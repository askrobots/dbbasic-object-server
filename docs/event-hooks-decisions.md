# Event Hooks — Decisions And Why

Decisions for Phase 5 (event handler objects) and sequencing against Phase 6
(user_prefs + feature_flags), per docs/upgrade-and-customization.md Rules 4–5.
Written before implementation so future contributors know why the direction
was chosen, not just what it is.

## Decision 1: Post-commit, best-effort dispatch

Handlers run **after** a record write commits, isolated in the normal object
sandbox; a handler failure is logged and never breaks or rolls back the
write. No pre-write mutation/rejection in this phase.

**Why.** The write path is the platform's most load-bearing promise: a valid
write succeeds fast. Synchronous in-path handlers make every write's latency
and reliability hostage to the worst installed handler — the exact footgun
that made in-transaction trigger systems (classic SQL triggers, early
Salesforce Apex) notorious, and why the systems that aged well converged on
post-commit: Stripe/GitHub webhooks, Django's on_commit signals, Rails
after_commit. Post-commit covers the real 90% — notify, sync, enrich other
records, log, kick off follow-on work. Validation/derivation already has a
home (schema rules, computed fields); pre-write hooks can be a later,
separately-gated phase if genuinely needed. Same shape as our realtime push:
`_publish_record_change_event` fires post-commit; handlers are one more
subscriber to that stream — the hook point already exists and adding
handlers cannot destabilize writes.

Safety posture: gated off by default (`DBBASIC_ENABLE_EVENT_HANDLERS`),
depth/reentry guard (a handler's own writes fire events but dispatch depth
is capped, no infinite loops), standard sandbox timeout per handler,
failures land in the object's log with the triggering event's correlation id.

## Decision 2: System/package objects only (this phase)

Only operator-installed objects may declare `HANDLES = [...]`; they run in a
defined service context. User-authored (`u_*`) handlers are deferred.

**Why.** "User code runs when *other people's* data changes" is a privilege
question with real teeth (whose rows can the handler read? who is the actor
on its writes?). The operator case needs none of that resolved — the
operator already owns the instance. Deferring user handlers keeps this
phase's blast radius to code the operator explicitly installed, and the
identity-threading design gets to mature before it's exposed to untrusted
authors. Same staging we used for overrides (opt-in, dormant by default).

## Decision 3: Phase 6 (prefs + flags) ships first

**Why.** (1) It's data + permission rules on machinery that already exists —
collections, owner-scoped row filters, the generators — near-zero new engine
code, so it's the fast clean win. (2) Feature flags are *how event handlers
should ship*: handlers land dark behind a flag, get canaried, and roll back
by flipping a flag instead of uninstalling code. Building flags first means
the riskier phase ships the safe way. (3) Flags convert a whole class of
would-be code customizations into configuration, shrinking the reconcile
surface — the same motive as the rest of the upgrade system.

Phase 6 shape: `user_prefs` collection (owner-scoped `user_id/key/value`),
`feature_flags` collection for instance toggles, package manifests may
declare `features` with defaults stamped at install. Effective value =
user override → instance toggle → package default, resolved by one shared
helper (a `/flags` object; `window.dbbasicFlags`).

## Order

1. Phase 6: user_prefs + feature_flags.
2. Phase 5a: `HANDLES` dispatch (post-commit, system objects, flag-gated).
3. Phase 5b: generator page slots (`before_list`, `row_actions`) — separable,
   UI-only, can ship alongside either.
4. Later, only if pulled by real need: pre-write hooks; user-authored
   handlers running as their author.

## Update (2026-07-23): pre-write hooks landed

The "later, only if pulled by real need" condition was met — the need being
custom cross-field/cross-collection validation on generatively-formed
collections (the extensibility question every "schema-driven" system must
answer). `hooks.before_write` now runs a declared object synchronously in the
generic write path: after permission checks, before persist; reject or
transform; **fail closed**; opt-in per collection so the latency concern above
stays bounded (no-hook collections pay one cached dict lookup). After-write
work remains the event system's job — nothing here changes that split. See
[`validation-and-logic.md`](validation-and-logic.md) for the contract.
