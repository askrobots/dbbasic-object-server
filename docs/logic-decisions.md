# Business Logic Decisions

The living log of *where rules go and why* — the companion to
[`business-logic-patterns.md`](business-logic-patterns.md) (the placement
guide) the same way [`ui-decisions.md`](ui-decisions.md) companions the
generative renderers. Because the primitives are shared, a placement decision
here is not a per-module choice — it applies to every module at once, and an
inconsistent placement is a bug, not a style preference.

Format per decision: **Decision · Rationale · Applies to · Status.**

---

## Decided

### 1. Stamp point-in-time facts; derive live facts.
- **Decision:** A value that is a fact about a *moment* (the tax rate an
  invoice was issued under, the price a line was sold at, the owner who
  created a record) is **stamped into the record at write time** — by the
  server or a pre-write hook — and never recomputed from today's reference
  data. A value that should always reflect *current* stored state (an invoice
  total, a payment balance, a full name) is **derived** by a formula/rollup
  and recomputes when inputs change.
- **Rationale:** Reference data changes; history must not. Deriving
  yesterday's invoice tax from today's rate table silently rewrites the past
  — the classic sales-tax bug. Conversely, stamping a total that should track
  its lines guarantees drift. The question "moment-fact or live-fact?" decides
  stamp vs derive every time.
- **Applies to:** tax rates, sold-at prices, FX rates on transaction dates,
  ownership; vs. totals, balances, counts, display names.
- **Status:** doctrine. First stamping precedent: server-side `owner_id`;
  first derivation adopters: fin_journals totals, contacts.full_name.

### 2. Time-driven state belongs to the daemon; never to read-time checks.
- **Decision:** When the passage of time changes a record's meaning (an
  invoice becomes *overdue*, a task becomes *stale*, an approval expires),
  a **daemon scheduled pass** makes the transition through the ordinary
  write/transition machinery — the state is *stored*. No surface ever
  computes "is it overdue?" at read time.
- **Rationale:** Nothing writes when time passes, so materialize-on-write
  can't see it — and read-time checks multiply into disagreeing copies (list
  says overdue, report says current). A stored transition is permission-
  checked, transition-guarded, realtime-pushed, notify-able, and audited —
  one truth for every surface. The daemon's `after_hours` auto-transitions
  already established the pattern.
- **Applies to:** aging/overdue states, expiries, escalation steps, SLA
  timers.
- **Status:** doctrine; aging/dunning are the next adopters (need the
  payments collection first).

### 3. Money never mutates; it moves.
- **Decision:** Corrections to money (and inventory) are **new, append-only
  compensating records** — a refund pointing at its payment, a reversing
  journal, a stock move — never edits to the original. Invariants on the
  movement ("refund ≤ payment − prior refunds", "journal balances") are
  pre-write **hooks**; displayed positions ("refunded_cents", "on-hand") are
  **rollups**.
- **Rationale:** Auditability is structural, not procedural: if the original
  can't change, the history is the ledger. Every mature money system
  converges here (double-entry is 700 years of this doctrine); systems that
  edit payment rows in place cannot answer "was this refund correctly
  credited?" because the question has no record to point at.
- **Applies to:** payments/refunds, journals (posted = immutable + reversals),
  stock_moves; `storage: append` is the substrate.
- **Status:** doctrine. Journals + stock_moves live it today; refunds adopt it
  when payments land.

### 4. Extract the primitive after the pattern repeats — never before.
- **Decision:** New business logic is first written **concretely** in the
  cheapest correct primitive for that one module — usually a hook (three
  lines of real code) or a plain object. Only after substantially the same
  shape appears in **two or three real modules** is it promoted to a
  declarative schema key, and the concrete versions are then deleted onto it.
- **Rationale:** This is how every good layer here was actually built: four
  hand-rolled comment tables → `capabilities.comments`; eight bespoke view
  pages → one detail renderer; one balance rule → the hook primitive; the
  hook's repeated shapes → formulas and rollups. The graveyard of "business
  operating systems" is full of the opposite order: a workflow/rules engine
  designed first, with reality forced through it — and the first rule that
  doesn't fit becomes the wall. An abstraction extracted from three working
  instances fits by construction. (UI precedent: ui-decisions #10.)
- **Applies to:** every "should this be declarative?" debate. The hook is the
  staging area; schema keys are the graduation.
- **Status:** doctrine — the house method, now named.

### 5. State freezes fields — in hooks now, a schema key when it repeats.
- **Decision:** "A posted invoice's amounts are immutable" (and every
  rule of the shape *fields X are locked when status = Y*) is expressed in
  each module's pre-write hook for now: reject when `existing.status` is in
  the frozen set and `changes` touches a locked field. When the third module
  writes that same hook, promote it to a declarative field/schema key
  (working name `locked_when`) and delete the hooks onto it, per #4.
- **Rationale:** It's clearly a repeating shape, but guessing the declarative
  form before seeing real instances is how bad keys get designed — does it
  lock per-field or per-schema? Does admin bypass? Does it compose with
  transitions? The hooks will tell us.
- **Applies to:** posted journals/invoices, completed orders, archived
  records.
- **Status:** pattern named; first hook instances pending (journals are the
  natural first).

### 6. Gates before the write; reactions after — never crossed.
- **Decision:** A pre-write hook may reject or stamp but never notify, email,
  or call external systems. Notifications and side effects run **after** the
  write via notify_rules / trigger objects / connectors, and never block or
  undo it.
- **Rationale:** A gate with side effects leaks that a denied write was
  attempted, couples save latency to SMTP, and can half-happen. A reaction
  that gates would mean the write's fate depends on a mail server. The two
  postures also fail differently by design: gates fail **closed** (a broken
  gate rejects), reactions and derived values fail **soft** (a broken formula
  stores empty; a failed notify retries).
- **Applies to:** every hook, every notify rule, every connector.
- **Status:** doctrine (established in
  [`event-hooks-decisions.md`](event-hooks-decisions.md); restated here as
  the load-bearing boundary of the whole map).

---

## How to use this

- Placing a new rule? Walk
  [`business-logic-patterns.md`](business-logic-patterns.md)'s flowchart; if
  the placement needed a judgment call, record it here.
- If two modules place the same kind of rule differently, that's a bug: pick
  one, log it, and fix the outlier.
- Counting instances for #4: when you write the second concrete copy of a
  shape, note it here so the third triggers extraction.
