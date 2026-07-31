# Working on this repository

For an AI agent (or a new human) about to change something here.

This file exists because of a specific, repeated failure: an agent lands
cold, reads a few source files, infers the conventions from what it can
see, and writes code that is plausible and wrong. Everything below is a
thing that was learned expensively — usually from a bug that reached
production — and is cheap to read.

**Read [`docs/architecture.md`](docs/architecture.md) next.** It maps the
whole system. This file is only what you cannot infer.

---

## 1. Check whether the layer already exists

The single most common mistake here, and the one that costs the most:
building a per-surface workaround for something the platform already does
uniformly.

Before you add a mechanism, search for it. Recent real examples:

- An agent was about to build claim/heartbeat machinery for a task runner.
  **`expected_rev` optimistic concurrency already existed** — implemented,
  flagged, specified, with a worked example titled *"claim becomes
  race-safe"* — and was used by exactly one object in the repo.
- An agent was about to build an agent-coordination feed. **`feed_posts`
  already existed**, described as "the shared coordination feed for humans
  and agents", with `claim` and `release` already in its `kind` enum.

**Proposing a new layer is the tell.** When you catch yourself designing
one, stop and grep first. Prefer deleting a workaround over adding a
second mechanism.

---

## 2. The doctrine

From [`docs/logic-decisions.md`](docs/logic-decisions.md), which is the
authority — these are the headlines. A placement decision there applies to
every module at once, so an inconsistent placement is a bug, not a style
preference.

1. **Stamp point-in-time facts; derive live facts.** A rate on an approved
   time entry, a price on an invoice line, promotion terms on a redemption,
   the model and price on a template run — copied at the moment of the act,
   so editing the source tomorrow restates nothing that already happened.
2. **Time-driven state belongs to the daemon**, never to a read-time check.
3. **Money never mutates; it moves.** Corrections are compensating entries.
4. **Extract the primitive after the pattern repeats — never before.**
5. **State freezes fields.**
6. **Gates before the write; reactions after — never crossed.**
7. **Generated documents carry provenance and face the same gates.** A
   replayed event posts nothing twice.
8. **Independent evidence is never edited** to agree with our records.
9. **Storage-level writers perform their own reactions**, and must say so.
10. **An amount is an integer in its denomination's smallest unit.** No
    float touches money, anywhere, ever.

Two corollaries that were learned the hard way and are not obvious:

- **A fold must not replace a value it cannot reproduce.** A totals handler
  recomputed an invoice from its lines, found no per-line tax, and silently
  restated a customer's £21.16 as £20.00.
- **Balances are derived, never stored.** A wallet balance IS its entries;
  stock levels ARE the moves. There is no mutable number to drift.

---

## 3. Traps that produce silent failures

These do not raise. They just quietly never happen.

**`def EVENT`, with `POST = EVENT` as an alias.** The change dispatcher and
the scheduler call `EVENT`. An object that declares only `POST` is a
handler that never runs and never complains. This has been a production
bug here more than once, and it survives in-process tests that call `POST`
directly.

**A gate must sum the evidence, not read the cache.** `hook_wallet_entries`
sums the ledger rather than trusting `wallets.balance_minor`. A gate that
trusts a derived number it could recompute authorises the overdraft the
instant that number goes stale.

**A claim is a compare-and-set, not a write.** Anything shaped like "read a
row, decide it is available, write that you own it" must pass
`expected_rev=object_records.compute_record_rev(row)`. Without it two
passes both read, both write, and both proceed — the transition guard does
*not* save you, because it is a no-op when old == new.

**Hooks cannot close a race.** A `BEFORE_WRITE` hook runs *before* the
write lock, so its checks are advisory. Layered advisory checks do not
compose into an atomic one. The only atomic operation available is
`expected_rev` on a single record.

**A handler in the override root never fires.** `object_handlers.build_index`
indexes only sources whose `kind` is `"system"` (event-hooks-decisions.md
Decision 2). An object under the override root is `kind="override"`, so it
declares `HANDLES`, installs cleanly, reports `"written"` — and receives
nothing. Found in production when a package install put a new object in
`roots[0]`, which is the override root in lookup order. If a handler is
silent, check its `kind` before you check its code.

**A hook is wired by the SCHEMA, not by its name.** `hook_app_settings`
does nothing until `app_settings.json` declares
`"hooks": {"before_write": "hook_app_settings"}`. A correctly named hook
object that no schema points at passes every unit test that calls it
directly and never runs in production — which is exactly how one shipped.
Assert the schema declaration, the way `test_disputes`, `test_pickup_slots`
and `test_inventory_adjustments` already do.

**Two spellings of the same instant do not compare equal.** ISO-8601
sorts lexicographically only within ONE format. `+` (0x2B) sorts below `Z`
(0x5A), so `...T14:00:00+00:00` < `...T14:00:00Z` — the same moment reads
as older. Anything comparing timestamps from two different writers must
parse them, not sort the strings; `object_agents._utc_naive` is the
canonical example, added after a board that had passed every test reported
live workers as stale.

**Retention and unbounded logs.** Three separate incidents (page views,
restore points, change logs — one reached 944MB describing 1,818 rows). A
log nobody told how big it may get fails on the worst day rather than an
ordinary one. If you add a writer, say how it is bounded.

---

## 4. Refusals

Things that are decisions, not gaps. Do not "fix" them.

- **`plan/` is never committed.** It is gitignored and enforced by
  `pre-commit` and `pre-push` hooks that beat `git add -f`. It holds
  private specs and is the most useful reading in the project — read it,
  never commit it.
- **Never rewrite existing objects at runtime, or in a loop.** Regeneration
  belongs in a scheduled job with tests.
- **Never auto-retry a paid, non-idempotent external call.** It looks like
  robustness and is an unbounded spend. A retry is a new record, started by
  a person.
- **A capability that is absent must refuse, not stub.** No placeholder
  output, no "provider unavailable, here is a default". Return a 409 naming
  exactly what to configure. A stub that pretends to have worked is worse
  than a failure.
- **Never claim more than you can prove.** `/ledger-integrity` refuses a
  clean verdict for a digest no independent party holds; `/privacy` refuses
  to render before a controller is named; the notary states what it does
  *not* prove beside every answer. Overstatement is the failure mode these
  features are guarding against.

---

## 5. How work is shaped here

**Pure logic at the root, I/O in packages.** `object_cart`, `object_rates`,
`object_promotions`, `object_ledger_head`, `object_template_runs` — no I/O,
no clock, no data directory, testable without either. The package objects
in `packages/app-*/objects/` do the reading and writing and call them.

**Packages declare; the installer registers.** Schedules, nav entries and
attention sources are declared in `dbbasic-package.json`, not configured on
a running box. Each of those registries retired a class of silent drift.

**Docstrings carry the argument, not the mechanics.** The convention here
is to record *what was tried, what broke, and what was rejected* — because
the next person (or model) needs the reasoning far more than a restatement
of the code. Match that density; it is the most valuable thing in the
repository.

**Tests state the claim in the name**, and a docstring says why the failure
would matter. When a cross-package gap needs a condition you do not own,
write a strict-xfail acceptance test rather than a workaround.

**Verify by reproduction, not by reasoning.** Two concurrency bugs this
week were each dismissed as theoretical and then reproduced in a dozen
lines of threading. Before claiming a bug, reproduce it; after fixing one,
break the fix and confirm the test fails.

**Walking a flow by hand finds what tests do not.** Tests exercise objects
directly; the server runs them through the event dispatcher. Several real
money bugs were found only by doing the thing on the live box.

---

## 6. Orientation

| Question | File |
|---|---|
| How does the whole system fit together? | [`docs/architecture.md`](docs/architecture.md) |
| Where does a business rule go? | [`docs/business-logic-patterns.md`](docs/business-logic-patterns.md) |
| Why is it there? | [`docs/logic-decisions.md`](docs/logic-decisions.md) |
| What runs on every write? | [`docs/write-pipeline.md`](docs/write-pipeline.md) |
| How do I add an app? | [`docs/package-authoring.md`](docs/package-authoring.md) |
| When does code enter an app at all? | [`docs/zero-to-code.md`](docs/zero-to-code.md) |
| What is the HTTP contract? | [`docs/http-api-contract.md`](docs/http-api-contract.md) |

Most apps here need **no code at all** — a schema, permission rules and
seeded view records produce list, board, form, detail, JSON API, MCP tools,
realtime push and search. If you are writing an object, first check whether
the thing you want is declarable.
