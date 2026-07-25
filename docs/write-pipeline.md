# The Write Pipeline: What Happens, In What Order, And What Can Stop It

[`zero-to-code.md`](zero-to-code.md) says where each kind of logic lives.
This page is its runtime twin: when a record is written, **every stage that
runs, in order, with its failure semantics** — because "code working with
code, signals going around" is exactly where frameworks went dark. Django
signals and Rails callbacks earned their reputation not because reactions
are wrong but because nobody could answer *why did my record change?*
without a debugger. Here the pipeline is fixed, short, and this page is its
single description. Verified against `object_server.py` and
`object_records.py`; if this page and the code disagree, fix one of them —
that disagreement is a bug.

## The synchronous path (inside the request)

Everything here happens before the client gets its response, in this order:

| # | stage | on failure | lives in |
|---|---|---|---|
| 1 | **permission check** (collection + row rules) | 403, nothing ran | policy |
| 2 | **field-write denial** (schema field permissions) | 403, names the fields | schema |
| 3 | **owner stamp** — sessions get `owner_id` written server-side; clients cannot spoof it (admin *tokens* skip this: seeding and migration set ownership deliberately) | — | server |
| 4 | **pre-write hook** (`hooks.before_write`) — may reject (its own 4xx) or transform (stamp fields; id/owner/read-only/computed protected) | **fails CLOSED**: a broken hook is a 500 and no write | `hook/` object |
| 5 | **auto `created_at`**, then **computed fields materialize** — rollups first, then formulas, so a formula may read a rollup | a broken formula stores `""` — **fails SOFT**, the write proceeds | `object_computed` |
| 6 | **schema validation** — types, required, enums, relations-exist, integer-cents; runs on the *final* record, so a hook's transform is validated too | 400, no write | schema |
| 7 | **transition check** — is this status move declared, and may *this* subject make it (`when` guards) | 403/409, no write | schema |
| 8 | **persist under the file lock**, then the **change-log entry** (actor, before/after, changed fields) | write errors surface as 4xx/5xx | `object_records` |

Two asymmetries in there are doctrine, not accident: **gates fail closed,
derivations fail soft** (a validation gate that silently passes on error is
not a gate; a caption that blanks on error is just a blank caption), and
**hooks run before validation** so that what a hook stamps is still checked
like any client input.

## The fan-out (after the write, same request, best-effort)

Once the record is durably written, the server fans out — in this order,
each stage isolated so one failing never blocks the next, and none of them
can un-write the record:

1. **rollup re-triggers** — a payment write recomputes its invoice's rollup
   fields (single-hop by design: a thread-local guard stops A→B→A cascades;
   a chain that needs more hops is a design smell, not a setting)
2. **realtime push** — permission-filtered signal (collection, id, action);
   subscribers re-fetch through the enforced API, so the socket never
   leaks record bodies
3. **event handlers** (`HANDLES` objects) — the reaction layer: composers,
   status flips. Payload carries the raw verb (`create`) while the event
   name uses the participle (`…created`) — accept both
4. **durable event log** (when enabled) — for pollers and webhooks

## The asynchronous tail (the daemon, seconds later)

Independent passes over the change log and collections, each with its own
cursor, each at-least-once: **notify** (rules → in-app/email intents),
**email outbox** drain, **change dispatch** (off by default — replays
handlers for storage-level writes so reactions stop depending on which
path wrote), **auto-transitions** (the 48h approve), **scheduled runners**
(aging, dunning, recurring journals, matcher, escalation — every run's
outcome a `scheduler_runs` record), **queue messages** (delay, retry,
backoff).

## How chains work without becoming Django signals

The reaction graph is real: a payment triggers the books composer, which
writes a journal, whose lines re-trigger the journal's rollups. What keeps
it legible instead of haunted:

- **One hop is synchronous, everything else is a record.** A handler's
  write does not re-enter the dispatcher (storage-level writes don't
  dispatch), so there is no unbounded cascade — a deeper chain must go
  through the change-dispatch pass, which has a cursor, markers, and a log.
- **Everything generated says who generated it**: `generated_from`
  provenance on composed records, `actor` on every change-log entry.
  "Why did my record change?" is a query, not a debugging session:
  `record_changes` names the actor; a composed journal names its source.
- **Idempotency by provenance** means redelivery is safe by construction —
  a replayed event composes nothing, so at-least-once is a guarantee, not
  a hazard.
- **Reactions may not gate and gates may not react**
  ([`logic-decisions.md`](logic-decisions.md) #6). The moment a reaction
  can block a write, or a gate can send an email, the pipeline above stops
  being a truthful description — that boundary is what makes this page
  possible.

## Multi-collection workflows

An order becomes an invoice becomes a payment becomes a journal; a task is
claimed, submitted, approved, settled. There is deliberately **no workflow
engine** — a business process here is a chain of *(transition on one
record) → (reaction that writes the next record)*, and each link is
individually visible in the table above. The process's state is not hidden
in an engine: it IS the records, their statuses, and their provenance
stamps, which is why an AI operator can answer "where is this order stuck?"
by reading collections. If a real orchestration need repeats (sagas,
compensation across many collections), that is a doctrine-#4 moment —
extract it *then*, from the concrete chains that exist.

## Reading a live system

- `record_changes` — who did what, before/after, per record
- `generated_from` — what machine wrote this, from what source
- `/scheduler` — what the clock did and what each run returned
- execution logs — every hook and handler run, observable like any object
- boot lines — the daemon prints every pass's posture (`Books: NOT READY…`,
  `Queue: enabled`, `Change dispatch: disabled`) so a disabled layer is
  announced, not discovered
