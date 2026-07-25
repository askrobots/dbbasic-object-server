# From Zero Code to Some Code

The question every generative system eventually gets asked: *fine, the
metadata builds the app — but when I need real code, where does it go?*
This page is the known path. Every app on this server grows through the
same stages, in the same order, and the order is the point: **code enters
through the write path first and the page last** — the exact reverse of
the classic web framework, where code entered through the controller
first and that is where the logic rotted.

## Stage 0 — an app with no code at all

A schema (`schemas/notes.json`), permission rules, and two seeded view
records (an index at `/notes`, a detail at `/notes/{id}`). From those
alone: list, search, sort, create/edit forms, a detail page, realtime
updates, the JSON API, MCP tools for agents, and global search. All of it
reads the schema; none of it is emitted as code
([`generative-ui.md`](generative-ui.md), including why this is *not*
scaffolding).

`app-notes` ships this way today: a package registering zero objects.

## Stage 1 — logic as declarations (still no code)

Most business rules are not code and never should be:

- shape, bounds, required, enums, FK-exists → **schema validation**
- who may do what, whose rows are whose → **permission rules**
- legal state moves and who may make them → **`transitions` + `when`**
- same-record derivations → **`formula` fields**
- sums over children → **`rollup` fields**

Every surface enforces these because they live on the shared write path,
not in a page. A large fraction of real apps end here.

## Stage 2 — the first line of Python is a gate

The first rule that *cannot* be declared is almost always a cross-record
check: "a payment may not exceed the invoice balance", "a journal must
balance before posting", "a loss move cannot have a destination". That is
a **pre-write hook** — one file, one function:

```python
# objects/hook/payments.py       schema: "hooks": {"before_write": "hook_payments"}
def BEFORE_WRITE(request):
    if too_much(request["record"]):
        return {"error": "...", "status": 409}   # reject
    record = dict(request["record"]); record["rate"] = looked_up   # or stamp
    return {"record": record}
```

Hooks run on the shared write path, so the form, the API, and an AI agent
all hit the same gate. They fail closed. This is where code enters the
system, and it enters *enforcing*, not rendering.

## Stage 3 — reactions

Something should *happen* after a write: a status flips, a journal is
composed, someone is notified. Notifications are data (`notify_rules`);
follow-on writes are an **event handler** — a `system/` object with
`HANDLES = ["payments.record.created"]` and an `EVENT(request)` function.
Reactions are post-commit and best-effort: they may never block the write
that already happened ([`logic-decisions.md`](logic-decisions.md) #6), and
anything they generate is idempotent by provenance (#7).

## Stage 4 — verbs

Users (and agents) do things that are not CRUD: *reverse this journal*,
*apply this count*, *import this statement*, *resolve this bank line*.
Each is an **action object** — `objects/action/<verb>.py` with a `POST`
that checks identity, validates, and writes through the same records
layer. A verb is the honest home for anything a button should do that a
form cannot.

## Stage 5 — time

Aging, dunning, recurring entries, escalation: time-driven state belongs
to the **daemon** (#2), as a runner object registered on the scheduler
(the `/scheduler` board shows every run's outcome) or a message on the
queue (delay, retry, backoff — already built). A runner is just an object
with a `POST`; it is callable by hand for testing and by the clock in
production.

## Stage 6 — presentation code, last of all

Only when a page must *show* something no generic block can express:

- first an **`object` block** — one hand-written panel inside an otherwise
  generated page (`{"kind": "object", "object_id": "site_my_panel"}`);
- then, rarely, a **full page object** — `site/notes.py` overrides the
  generated `/notes` by convention the moment the file exists, and
  deleting it reverts. Reports are the legitimate case: the trial balance,
  the statements, the reconciliation tie-out are folds across collections
  with audit-grade warnings, and they stay hand-written on purpose.

The rule that keeps stage 6 safe: **a page may never be the only place a
rule exists.** A gate written into a page does not gate the API or an
agent — it provably does not hold — so enforcement stays in stages 1–2
where every surface shares it. Pages show; they never decide.

## The whole path at a glance

| stage | you add | it lives in | enforced for |
|---|---|---|---|
| 0 | schema + rules + 2 view rows | data | every surface |
| 1 | declarations (transitions, formulas, rollups) | schema | every surface |
| 2 | a gate | `hook/` on the write path | every surface |
| 3 | a reaction | `system/` handler, `notify_rules` | post-commit |
| 4 | a verb | `action/` | its callers |
| 5 | a schedule | runner + scheduler/queue | the clock |
| 6 | a panel, then a page | `object` block → `site/` | presentation only |

## Where this page came from

This path was not designed and then implemented — it was **extracted**. A
week of building real modules concretely (payments, the books spine, bank
reconciliation, inventory losses, cash counts) kept producing the same
placement decisions, and once every kind of business rule had exactly one
home, the homes sorted themselves into the order an app acquires them.
The taxonomy is the load-bearing thing; the path is its corollary.

That is also why earlier frameworks never had a page like this. They
documented their *mechanisms* (models, controllers, middleware, signals)
but never fixed where *logic* lives — any rule could go anywhere, so there
was no gradient from zero code to some code, only a cliff. "Fat models,
skinny controllers" was a slogan, not a placement table with enforcement
behind it. A path only exists where placement is decided.

The runtime twin of this page is [`write-pipeline.md`](write-pipeline.md):
once logic is placed, that page says exactly when it runs, in what order,
and what can stop a write.

Placement questions beyond this page: the flowchart in
[`business-logic-patterns.md`](business-logic-patterns.md) decides where
any individual rule goes, and [`logic-decisions.md`](logic-decisions.md)
records why.
