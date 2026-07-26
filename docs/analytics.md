# Analytics — First-Party, Server-Side, and Honest About It

Most platforms forgot analytics. You build a site, and to find out
whether anyone came you paste in someone else's JavaScript, which reports
a number you cannot check, derived from data you no longer solely hold.

This server records its own traffic. Every request appends one row to
`page_views`; `/visitors` folds those rows into people, bots and your own
operations, counted apart. No third party is involved, no script is
served to the browser, and the data is a TSV on your disk that you can
open with `less`.

This page argues why that is better, and states plainly what it costs —
because it does cost something, and a page that only listed advantages
would be exactly the kind of analytics writing this system exists in
opposition to.

## The structural argument: JavaScript can only see what ran JavaScript

This is the whole thing, and it is not a matter of degree.

A tag-based analytics product observes a page view when a script loads
and executes. It therefore cannot see:

- **Ad-blocked visitors.** The most technical part of any audience blocks
  the most common tags by default. For developer tools, that is precisely
  the people evaluating you.
- **Clients with no JavaScript** — curl, RSS readers, link previewers,
  text browsers, accessibility tooling, anything scripted.
- **Bots and scanners.** Invisible to the tag, and they are a large share
  of what actually arrives.
- **Errors.** A 404, a 500, a page that failed before the tag ran. The
  requests most worth knowing about are the ones least likely to report
  themselves.
- **Anything that is not a page.** API calls, webhooks, feeds, downloads.

The server, by definition, sees every request it answered. It cannot be
blocked from knowing what it did.

## The ownership argument

A hosted analytics vendor holds a complete copy of your traffic and
returns you a summary, subject to sampling, thresholds, and categories
like "not provided". You are shown a fraction of your own data, and the
fraction is chosen by the party who kept all of it.

Here the raw rows are yours, on your disk, in a format `grep` can read.
Nothing is sampled. Nothing is withheld. No third party receives a copy
of your visitors' requests as a side effect of you wanting to count them,
which is also the part your visitors would care about if anybody asked
them.

## What it captures that a tag cannot

Because it sits at the server, the same data answers questions analytics
products usually cannot:

- **Attack traffic.** Scanners walking a list of URLs that do not exist
  here, credential-stuffing attempts, payloads arriving in headers. One
  Log4Shell probe with an AWS-credential exfiltration template in its
  `Referer` was found this way — inert against a Python server, but worth
  seeing.
- **Which of your own machines did what.** Deploys and scripted calls are
  labelled `operator`, not hidden, so you can tell your traffic from
  everyone else's rather than guessing.
- **Failure shape.** A flood of 404s from one address is a security
  signal that a page-view counter would never show you.
- **Multi-site truth.** One process may answer for several hostnames; the
  `host` is recorded, so "did anyone read the pitch" and "did anyone try
  the product" are different questions with different answers.

## Who counts as a person

The hard part of first-party analytics is not collection, it is
classification. Traffic is sorted into three kinds and never merged:

**operator** — yours. An owner IP, or a request carrying the admin token.
Labelled rather than hidden: seeing your own visit is how you confirm the
page is recording anything at all, and a counter that silently drops the
only traffic you can verify is a counter you cannot trust.

**bot** — a client that never behaved like a person. The test is
behavioural, not a user-agent blocklist, because the blocklist is always
out of date and the behaviour is not. An address must have loaded a real
page **and** done one thing a prober does not: opened a second distinct
page, arrived from a referrer, or carried a session. A declared crawler
stays a bot even when it fetches something real — Googlebot is honest
about what it is, and honesty should not promote it. So does anything
sending an attack payload in a header.

**visitor** — everything else. A person, probably.

The rule earned its strictness. A first version counted any address that
loaded a real page, and `/` answers 200 to everyone: on a real week that
promoted nearly half the "visitors" — 187 fell to 92 — from probers who
touched the front page and left. A metric that turns a quiet day into a
busy one is worse than no metric, because it misleads exactly when
somebody is relying on it.

## What it costs — read this part

**"Unique" means a distinct IP address, and an IP is not a person.** An
office behind one connection counts once. A phone moving between wifi and
cellular counts twice. A CGNAT range counts thousands of people as a
handful.

That is the honest unit available to a server that sets no tracking
cookie and fingerprints nobody, and the alternative — a durable
cross-site identifier — is a thing this system deliberately does not
have. The `/visitors` page says so on the page, in words, rather than
presenting a confident number.

A first-party visitor cookie (below) improves this for anyone who accepts
one, and does not change the ceiling: it makes returning visitors
countable, and it still cannot tell a phone from a laptop.

Also absent, and not coming:

- **No cross-device identity.** The same person on a laptop and a phone
  is two visitors.
- **No demographics, interests, or inferred attributes.** Those come from
  a surveillance network; there isn't one.
- **No client-side interaction data** — scroll depth, rage clicks, time
  on page — unless an app deliberately posts an event. The server knows
  what it served, not what someone did with it afterwards.
- **Bounded history.** `page_views` is capped by age *and* row count
  (`DBBASIC_ANALYTICS_RETENTION_DAYS`, `DBBASIC_ANALYTICS_MAX_ROWS`),
  because a log nobody told how large it may get is a log that fails on
  the worst day rather than an ordinary one. Long-horizon trends need
  rolling those up before they age out.

If you need cross-device attribution and demographic segments, this will
not give them to you, and no amount of configuration will.

## Operating it

- **Off by default.** `DBBASIC_ANALYTICS=on` is an operator choice,
  because it adds a write to every request.
- Asset, health and polling paths are skipped; 4xx and 5xx are
  deliberately **kept**, because a 404 flood is the signal.
- The write is offloaded off the event loop and is best-effort:
  analytics must never break a response.
- Retention runs as a scheduled daemon pass with both bounds.
- Rollups (`rollup_definitions`) group the raw rows into top paths, top
  addresses, status codes and daily counts. Set `min_group_size` on the
  path and address rollups — without it, every one-hit scanner probe
  becomes its own row and the "top paths" report becomes a copy of the
  log rather than a summary of it.

## Goals and funnels — what exists, and what does not

`conversions` exists as a collection: `event_type`, optional
`session_id`/`user_id`, and a JSON `metadata` blob, append storage,
written server-side by whichever app owns the goal. A rollup can group it
by `event_type`.

**Nothing writes to it yet.** The shape is right and the wiring is
missing, and that is worth stating rather than implying the feature is
there. The natural implementation is small, because this system is full
of transitions and a conversion is simply a transition worth counting —
checkout completed, invoice paid, order collected, scan confirmed.

Funnels need one thing more: a thread to stitch a visitor's steps
together across requests. That is the visitor cookie, below.

## The visitor cookie — the one addition worth making, and its limits

Everything above is what a server can know without asking the browser to
remember anything. There is exactly one addition that changes the class
of question this can answer, and it deserves stating carefully, because
it is also the point at which analytics products usually stop being
honest.

`page_views.session_id` exists and reads a cookie. Nothing sets one for
an anonymous visitor today, so every visitor looks new on every visit and
a journey can only be stitched by address — precisely wrong for the
multi-visit paths that matter. Somebody reads the pitch on Monday, comes
back Thursday, and tries the product: today that is three strangers.

**A first-party cookie with a long-but-not-indefinite expiry fixes it**,
and unlocks the questions a site actually has:

- **New versus returning** — the most useful ratio a site has, and
  invisible without it.
- **Return paths** — what somebody read first, and what brought them
  back.
- **Time to conversion** — days between first visit and purchase, which
  is what tells you whether your funnel is a funnel or a queue.
- **Real funnels**, stitched across visits rather than within one.

The rules that keep it honest, and they are not optional:

1. **First-party and same-site only.** Set by this server, sent only to
   this server, `SameSite=Lax`, `HttpOnly`. Not readable by JavaScript,
   not a cross-site identifier — the same posture the shop's basket
   cookie already takes, and the reason page objects are never handed the
   identity session cookie.
2. **An opaque random token.** It says *this browser has been here
   before*. It carries no name, no email, no account, and no meaning
   outside this site's own logs.
3. **A stated expiry.** Long enough to see a return visit — six months is
   the usual honest choice — and short enough to lapse rather than
   follow somebody for years. "Indefinite" is how a session identifier
   quietly becomes a permanent one.
4. **It never becomes identity.** A signed-in `user_id` is already
   recorded separately, and the visitor token must not be joined to it to
   retroactively de-anonymise earlier browsing. That join is the exact
   move that turns analytics into surveillance, and it is one line of
   code away at all times — which is why it is written down as a rule
   rather than left to judgement.
5. **Honoured refusal.** Do Not Track and Global Privacy Control mean no
   cookie is set. Those visitors are still counted by the IP rule; they
   simply appear new each visit, which is the correct consequence of
   having asked not to be remembered.

The ceiling worth knowing: a cleared cookie is a new visitor, a second
browser is a second visitor, and a phone is a third. Returning-visitor
counts are a **floor, never a census** — they undercount, which is the
right direction for a number to be wrong in.

Funnels then become an ordinary fold over `page_views` grouped by that
token, and `conversions.session_id` is already the field waiting for it.

## Where to look

- `/visitors` — people, bots and your own traffic, by hour and by day,
  with referrers, landing pages and a per-host split.
- `/analytics` — path, status and referrer detail from the rollups.
- `object_analytics.py` — capture and configuration.
- `object_visitors.py` — the classifier and the fold, pure and tested.
