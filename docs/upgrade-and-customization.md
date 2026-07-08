# Upgrades And Customization — The Solutions Document

This is the doctrine for the hardest problem in packaged app systems: **how a
third-party package upgrades in place while the operator's data AND
customizations survive**. Rails and Django never solved this as a platform
property — apps/engines existed, but customizing meant forking and upgrading
meant being your own integrator. The systems that solved it (Salesforce
managed packages, Odoo modules, Debian conffiles, WordPress child themes)
converged on the same handful of ideas. This document adapts them to DBBASIC
so any future contributor — human or AI — builds *with* the upgrade system
instead of fighting it.

## The Three Layers (never entangle them)

Every installed app is three independent layers:

1. **Package baseline** — what the package shipped: objects, schemas,
   permission fragments, seed. Versioned, replaceable, owned by the package.
2. **Local customization** — what this instance changed on top: edited
   source, added fields, extra rules, user code. Owned by the operator.
3. **Data** — the records in `data/collections/<name>/records.tsv`. Owned by
   the users. Lives entirely outside the package (already true today).

An upgrade replaces layer 1. It must never silently destroy layers 2 or 3.
Layer 3 is solved: seed is install-once (see `package-authoring.md`). This
document is about layer 2.

## Rule 0: Provenance Baselines

Nothing else works without this. On install/upgrade, stamp every shipped
artifact with its baseline:

```json
{"package": "app-articles", "version": "0.2.0", "sha256": "<hash of shipped source>"}
```

stored per object and per schema (e.g. `data/package_baselines/<pkg>.json`).
Then "is this customized?" is a computable question: hash the live source and
compare to the baseline. Without the stamp you cannot distinguish a human's
edit from the shipped file, and every upgrade is a coin flip.

## Rule 1: Reconcile, Don't Replace

On upgrade, per object/schema, three-way compare `old_baseline` vs `live` vs
`new_shipped` (exactly `git merge` / dpkg conffile semantics):

| live vs baseline | shipped changed? | action |
|---|---|---|
| pristine | — | fast-forward to new version, update baseline (silent) |
| customized | no | keep the customization (silent) |
| customized | yes, hunks don't overlap | three-way merge, apply, flag "merged" |
| customized | yes, conflict | **park it** — write a pending-reconcile record holding both versions, leave live untouched, surface for a human decision (keep mine / take theirs / view diff) |

`allow_replace` means "fast-forward the pristine ones." Discarding a
customization requires a separate explicit force. The existing version stores
(`object_versions`, `object_schema_versions`) are the undo; the install
restore point is the rollback. A "reconcile inbox" (Scroll screen + CLI list)
holds parked conflicts — upgrades never block on them.

## Rule 2: Customize By Overlay, Not By Editing Core

The reconcile engine is the safety net. The *preferred* path makes conflicts
structurally impossible: customizations live in different files than the
package's, so upgrades and edits never touch the same bytes.

- **System vs user objects.** Package-shipped objects are *system* objects
  (baseline-stamped, upgrade-managed). Operator-created objects are *user*
  objects (never touched by any upgrade). The namespace should make this
  visible: editing a system object warns "this came from app-articles 0.2.0 —
  override instead?"
- **Override objects.** A user object can *shadow* a system object by id:
  the resolver checks the override root first (`objects/overrides/…` or an
  `overrides: {site_articles: my_articles}` map). The shipped object stays
  pristine and freely upgradeable; the override persists across upgrades.
  This is Odoo `_inherit` / WordPress child themes done with plain files.
- **Extension points.** Objects should invite extension instead of forking:
  named hooks (see Rule 4) and small overridable pieces rather than one
  monolith page.

## Rule 3: Data Fields That Survive Schema Upgrades

Schemas are the friendly layer because changes are mostly **additive**, and
records are read *through* the schema — an old row simply reads empty for a
new field (proven by the `created_at` rollout).

- **Field union on upgrade.** Merge shipped schema changes with locally added
  fields rather than replacing the field list. Conflict = same field name
  with a different type/enum — park it like a source conflict. A locally
  added `priority` field must survive an app-notes upgrade.
- **Namespace local fields** (`x_` prefix by convention, enforced gently) so
  a package can never ship a field that collides with an operator's.
- **Extension data (`extra`).** Give every collection an optional JSON
  overflow field — the json_p idea: user-defined per-record data that no
  schema upgrade can ever collide with, because it isn't in the schema's
  flat namespace at all. Schemas may declare typed *views* into it
  (`{"name": "x_mood", "store": "extra"}`) so forms/lists render extension
  fields with zero migration. TSV stays one column; the column holds JSON.
- **Migrations** (the currently-stubbed manifest section) are for the rare
  non-additive change: a shipped script that backfills/transforms records,
  run once, marker-tracked, restore-point-first. Until implemented, packages
  must keep schema changes additive.

## Rule 4: Hooks — Behavior Customization Without Forking

Let operators attach code to the system's existing event stream instead of
editing package objects. The substrate exists: every record write already
publishes `collection.record.created/updated/deleted` (it drives /ws).

- **Event handler objects.** A user object subscribes declaratively:
  `HANDLES = ["notes.record.created"]`; the server invokes it on the event
  (same sandboxed execution as any object). Custom side effects — notify,
  enrich, sync — with zero edits to the shipped app. Handlers are user
  objects, so upgrades never touch them.
- **Page hooks.** Generated pages expose named slots (`before_list`,
  `row_actions`) that resolve to user objects if present. The generative
  renderer makes this cheap: one hook added to `site_list` appears in every
  app at once.

## Rule 5: User Prefs And Feature Flags

A `user_prefs` collection (`user_id`, `key`, `value`, owner-scoped rows) —
schema-driven like everything else — gives:

- **Per-user settings** for any app (theme, defaults, layout) with zero new
  endpoints; the generators can read/write it directly.
- **Feature flags.** Packages ship features dark:
  `features.json` → `{"kanban_view": {"default": "off"}}`. Instance-level
  toggles live in a `feature_flags` collection; per-user overrides in
  `user_prefs`. Pages check flags via one shared helper. This decouples
  *shipping* code from *enabling* it — upgrades can carry big features
  safely off, canary them per user, and roll back by flipping a flag rather
  than reinstalling. It also shrinks customization pressure: many "edits"
  are really "turn this on/off," which should be config, not forked source.

## What Future Coders (AI Or Human) Must Do

When touching packages or upgrade code:

1. Never write an upgrade path that overwrites a file without checking its
   baseline hash first. If the baseline system doesn't exist yet, build it
   before the feature that needs it.
2. Never block an install on something preservable — preserve and skip
   (like seed), or park and surface (like conflicts). Blocking teaches
   operators to bypass the installer, which is how systems rot.
3. Keep schema changes additive; put anything non-additive in a migration.
4. New customization mechanisms must be overlay-shaped: separate file,
   separate collection, separate namespace — never "edit the shipped thing."
5. Data outside the package, keyed by collection, always.

## Build Order

1. **Provenance stamps** (small; unblocks everything) — record baselines at
   install, expose `customized: true/false` per object in package status.
2. **Reconcile-on-upgrade** — the table above + pending-reconcile records +
   CLI list; Scroll renders the reconcile inbox.
3. **Override objects** — resolver-level shadowing; the conflict-free path.
4. **Schema field-union + `extra` extension data.**
5. **Event handler objects** (`HANDLES`), then page hooks.
6. **user_prefs + feature_flags** collections and the shared flag helper.

Each phase is independently shippable and independently valuable; together
they make "upgrade the app, keep your data and your customizations" a
platform guarantee — the property that separated the systems that lasted
(Salesforce, Odoo, Debian, WordPress) from the frameworks that made every
operator their own integrator.
