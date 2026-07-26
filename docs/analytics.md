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

That is the honest unit available to a server that sets no *cross-site*
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

## Goals and funnels

`conversions` is a collection: `event_type`, optional
`session_id`/`user_id`, and a JSON `metadata` blob, append storage,
written server-side by whichever app owns the goal. A rollup can group it
by `event_type`.

**It is written now, and the implementation was as small as promised** —
because this system is full of transitions and a conversion is simply a
transition worth counting. `system_record_conversion` (app-analytics)
declares `HANDLES` on the changes that already happen and records four
goals:

| event_type | the transition |
|---|---|
| `order_confirmed` | a sale order reached `confirmed` or anywhere past it |
| `order_collected` | a pickup order was actually handed over |
| `payment_received` | money arrived against an invoice, and did not bounce |
| `scan_confirmed` | a human confirmed a scanned document into the books |

Nothing new is observed. Four facts the server already knew simply never
landed anywhere a report could see them.

It is a **reaction**, in the same posture as `system_order_email`:
post-commit, best-effort, never a gate, and it cannot fail the write that
confirmed the order. On a box with no app-analytics installed it counts
nothing and says which — because silence would read as "already counted",
and the shop would never find out it had no numbers.

**Idempotent by provenance.** The change dispatcher is at-least-once, and
`status is confirmed` is a state an order *sits in* rather than an edge it
crosses, so the handler sees the same order on every later write. Each
conversion carries its source in the metadata blob
(`{"source": "orders/ord-1"}`) and one whose `(event_type, source)` pair
already exists is not written again. A double-counted conversion is a
permanent overcount in a report nothing downstream will ever correct.

**These four rows carry no `session_id`, and that is stated rather than
hidden.** A back-office transition has no browser anywhere near it — an
order confirmed by staff the next morning was not a page view — and the
basket's `carts.session_token` is a different identifier in a different
namespace, so stamping it here would merge two populations and produce a
funnel that looks stitched and is not. The funnel counts them in
`unthreaded_conversions` and says so on the page. The field is there for
the day an app records a goal from a request that did carry the cookie.

### The folds

`object_conversions.py` is the pure half — rows in, numbers out, no I/O,
the same posture as `object_visitors.py`. Three folds, each of which
returns its own caveat **as data** so a surface cannot render the number
without being handed the qualification to render beside it:

- **`funnel(page_views, conversions, steps)`** — ordered steps, each a
  path prefix (`/shop`) or an `event_type`. Per step: distinct visitors
  who reached it and drop-off from the step before. It is a *sequence* —
  a visitor counts at step N only if they reached N−1 and then did N
  afterwards, because somebody who bought last week and browsed today did
  not convert today. Threaded by `session_id`, falling back to the IP
  address for rows that carry none, and the result reports
  `ip_stitched` / `ip_stitched_pct` per step and overall. A funnel that
  hides how much of itself is guesswork is the kind of analytics this
  page exists in opposition to.
- **`returning_visitors(page_views, window_days)`** — new versus
  returning by token, returning `floor: True` and the caveat text itself.
  See the ceiling below.
- **`time_to_conversion(page_views, conversions)`** — median and spread of
  days between a visitor's first recorded page and their conversion, with
  the two ways of not knowing reported apart: `unthreaded` (no token at
  all) and `no_first_view` (the first visit aged out of a bounded log).
  Dropping the second silently would bias the median toward fast
  conversions — the number would improve every time retention was
  shortened, which is the most dangerous shape a metric can have.

### Describing your own funnel

A funnel is a settings row, not code. `analytics.funnel_steps` in
`app_settings` is a JSON list:

```json
["/shop", "/checkout", "order_confirmed"]
```

A step beginning with `/` is a path prefix; anything else is a conversion
`event_type`. The long form takes labels:
`[{"label": "Browsed", "path": "/shop"}, …]`. With nothing configured,
`/visitors` shows what to configure — built from the paths and event types
this server has actually recorded, so it can be copied rather than
invented — instead of an empty table that reads as "nobody converted". A
malformed setting is reported as a malformed setting, because a
misconfigured funnel and a funnel nobody entered look identical and only
one of them is your fault.

## The visitor cookie — the one addition worth making, and its limits

Everything above is what a server can know without asking the browser to
remember anything. There is exactly one addition that changes the class
of question this can answer, and it deserves stating carefully, because
it is also the point at which analytics products usually stop being
honest.

Before it, `page_views.session_id` read a cookie that nothing ever set,
so every visitor looked new on every visit and a journey could only be
stitched by address — precisely wrong for the multi-visit paths that
matter. Somebody reads the pitch on Monday, comes back Thursday, and
tries the product: that was three strangers.

**A first-party cookie with a long-but-not-indefinite expiry fixes it.**
It is `dbbasic_visitor`, set by `object_server` on a page response when
analytics is on and the request carried none, and its value is stamped
into `page_views.session_id` on the same request that mints it — so the
first page of a visit is threaded rather than the second. It unlocks the
questions a site actually has:

- **New versus returning** — the most useful ratio a site has, and
  invisible without it.
- **Return paths** — what somebody read first, and what brought them
  back.
- **Time to conversion** — days between first visit and purchase, which
  is what tells you whether your funnel is a funnel or a queue.
- **Real funnels**, stitched across visits rather than within one.

The rules that keep it honest are not optional, and none of them is left
to judgement: each is enforced in code and pinned by a test in
`tests/test_conversions.py`, because a rule that lives only in a document
is a rule the next person breaks in one line without noticing.

1. **First-party and same-site only.** Set by this server, sent only to
   this server, `Path=/`, `SameSite=Lax`, `HttpOnly`. Not readable by
   JavaScript, not a cross-site identifier — the same posture the shop's
   basket cookie already takes, and the reason page objects are never
   handed the identity session cookie. It is also withheld from page
   objects themselves (`_object_visible_cookies` drops it alongside
   `dbbasic_session`): they have no use for it, since the server stamps
   it into `page_views` itself, and a token only the capture hook can see
   cannot be joined to anything by a package the operator installed.
2. **An opaque random token.** `secrets.token_urlsafe`. It says *this
   browser has been here before*. It carries no name, no email, no
   account, and no meaning outside this site's own logs — and nothing
   derived from the request, because a token hashed from IP and user
   agent would be a stable identifier nobody could ever clear.
3. **A stated expiry.** `DBBASIC_ANALYTICS_VISITOR_DAYS`, default 180 —
   long enough to see a return visit, short enough to lapse rather than
   follow somebody for years. Junk and non-positive values fall back to
   the default rather than being read as unbounded: "indefinite" is how a
   session identifier quietly becomes a permanent one.
4. **It never becomes identity.** A signed-in `user_id` is already
   recorded separately, and the visitor token must not be joined to it to
   retroactively de-anonymise earlier browsing. That join is the exact
   move that turns analytics into surveillance, and it is one line of
   code away at all times — so `build_page_view` and `build_conversion`
   write **at most one of the two columns**, preferring the anonymous
   one. A signed-in member's traffic is still counted; it simply is not
   labelled with who they are. Where there is no token (a DNT visitor, an
   API call) `user_id` is written as before, because there is nothing for
   it to be correlated with.
5. **Honoured refusal.** `DNT: 1` and `Sec-GPC: 1` mean no cookie is set —
   not a shorter one, not an anonymised one. Those visitors are still
   **counted** by the IP rule, because refusing to be remembered is not
   refusing to be counted; they simply appear new each visit, which is the
   correct consequence of having asked not to be remembered. `DNT: 0` is
   consent and an absent header is no statement at all; reading either as
   a refusal would mean this server never remembered anybody.

It is also never set on a path a browser did not choose to visit: assets,
health checks and polling endpoints (`should_capture`) and the API and
collection surfaces a script talks to (`is_page_path`). An API client is
not a browser and cannot be a returning visitor; handing it a cookie only
writes an identifier into somebody's cron job. A 404 *is* a page — often
the first one a visitor sees — so those are threaded like any other.

The ceiling worth knowing: a cleared cookie is a new visitor, a second
browser is a second visitor, and a phone is a third. Returning-visitor
counts are a **floor, never a census** — they undercount, which is the
right direction for a number to be wrong in. `/visitors` says so on the
page, next to the number, and reports how many addresses had no cookie at
all so the size of the blind spot is on the same screen as the ratio it
is missing from.

Funnels are then an ordinary fold over `page_views` grouped by that
token — `object_conversions.funnel` — and `conversions.session_id` is the
field waiting for the goals that are recorded from a real request.

## Where to look

- `/visitors` — people, bots and your own traffic, by hour and by day,
  with referrers, landing pages and a per-host split; then new versus
  returning, goals by `event_type`, your configured funnel and time to
  conversion. Deliberately one page rather than a second `/funnels`: "how
  many came", "how many had been before" and "how many bought" are one
  question asked at increasing depth, and a conversion rate whose
  denominator lives on another screen is a conversion rate nobody
  computes.
- `/analytics` — path, status and referrer detail from the rollups.
- `object_analytics.py` — capture, configuration and the visitor-cookie
  decision.
- `object_visitors.py` — the classifier and the traffic fold, pure and
  tested.
- `object_conversions.py` — the goal record, the funnel, new-versus-
  returning and time-to-conversion, pure and tested.
- `packages/app-analytics/objects/system/record_conversion.py` — the
  handler that turns transitions into `conversions` rows, idempotently.
