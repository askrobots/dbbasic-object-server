# Traffic Limits

DBBASIC should reject bad or oversized traffic before object code runs. The
runtime keeps the object loop fast by failing early when a request is too large
or when the server is under pressure.

## Request Body Size

The ASGI server enforces a request body cap:

```text
DBBASIC_MAX_REQUEST_BYTES=1048576
```

The default is `1048576` bytes, or 1 MiB. If a request body is larger than the
configured value, the server returns:

```http
413 Payload Too Large
```

```json
{
  "status": "error",
  "error": "Request body too large",
  "max_bytes": 1048576
}
```

This applies before JSON parsing and before object execution. It checks
`Content-Length` when present and also counts streamed ASGI body chunks.

Large file upload paths should get their own file API and their own explicit
limits. Normal object `POST`, `PUT`, and `DELETE` requests should stay small.

## Concurrency

The ASGI server also enforces per-process concurrency caps:

```text
DBBASIC_MAX_CONCURRENT_REQUESTS=64
DBBASIC_MAX_CONCURRENT_EXECUTIONS=8
```

`DBBASIC_MAX_CONCURRENT_REQUESTS` limits non-health HTTP requests that are in
flight inside one server process. `/health` bypasses this limit so a monitor can
still check whether the process is alive under load.

`DBBASIC_MAX_CONCURRENT_EXECUTIONS` limits object method executions inside one
server process. Introspection routes, health checks, and other non-execution
requests do not consume object execution slots.

When either limit is full, the server returns:

```http
503 Service Unavailable
```

```json
{
  "status": "error",
  "error": "Server is busy",
  "limit": "object_executions",
  "max_concurrent": 8
}
```

Set either concurrency value to `0` to disable that app-level limit for local
experiments. Production and staging deployments should keep explicit limits.

These caps are per process. If uvicorn runs multiple workers, each worker has
its own counters. A reverse proxy or process manager still controls the total
machine-level shape.

## Rate Limits

Rate limiting is opt-in:

```text
DBBASIC_RATE_LIMIT_REQUESTS=1000
DBBASIC_RATE_LIMIT_WINDOW_SECONDS=60
```

The default `DBBASIC_RATE_LIMIT_REQUESTS=0` leaves the app-level limiter
disabled for local development. Public staging and production deployments should
set an explicit value.

When enabled, the server checks the rate limit before reading the request body
or running object code. Plain `GET /health` is excluded so external monitoring
can still tell whether the process is alive. Detailed health via
`capacity=true` or `metrics=true` is rate-limited and still requires the admin
token.

The limiter stores one timestamp bucket per identity under:

```text
data/ratelimit/
```

Those files are runtime guardrails, not application data. Backups exclude them.

Valid admin-token requests use a token bucket. Other requests use the client IP.
By default the server ignores `X-Forwarded-For` and `X-Real-IP`; enable proxy
header trust only when uvicorn is bound privately behind a trusted reverse
proxy:

```text
DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS=true
```

When the limit is full, the server returns:

```http
429 Too Many Requests
Retry-After: 30
```

```json
{
  "status": "error",
  "error": "Rate limit exceeded",
  "retry_after": 30,
  "limit": 1000,
  "window_seconds": 60
}
```

## Object Execution Timeout

Object execution timeout is opt-in:

```text
DBBASIC_OBJECT_TIMEOUT_SECONDS=5
```

The default `0` keeps local development on the direct in-process execution path.
When the value is above zero, object methods run in a short-lived worker process.
If the object does not finish before the wall-clock timeout, the server
terminates the worker and returns:

```http
504 Gateway Timeout
```

```json
{
  "status": "error",
  "error": "Execution failed: GET timed out for object basics_slow after 5 seconds"
}
```

Timeout failures are written to the object log with
`ObjectExecutionTimeoutError`, so Scroll and operators can see which object
timed out.

This boundary is intentionally narrow. It stops hung object work and releases
the execution slot, but it is not the final sandbox. CPU quotas, memory limits,
and a longer-lived worker pool are still future hardening steps.

## Capacity Health

`GET /health` stays public and cheap:

```json
{
  "status": "ok"
}
```

Detailed capacity is an operator surface and requires the admin token:

```http
GET /health?capacity=true
Authorization: Token <token>
```

It reports the configured request and object execution limits, current
in-flight slot counts, object count, storage status, version, uptime, request
counts, recent response timing, and basic process/system information. Scroll and
future station routers should use this detailed shape instead of guessing from
logs.

`GET /health?metrics=true` includes the same capacity payload plus detailed
request metrics such as status counts, top paths, and recent HTTP errors.

## High-Traffic Shape

Use layered limits:

- reverse proxy body limit before Python receives the request
- app body limit through `DBBASIC_MAX_REQUEST_BYTES`
- app request and object execution limits through the concurrency env vars
- per-IP and per-token rate limits before write or execute paths
- object execution wall-clock timeouts
- later CPU and memory isolation around worker processes

When the server is overloaded, it should return `429 Too Many Requests` for rate
limits or `503 Service Unavailable` for temporary capacity pressure. That is
better than letting a small VM run out of memory or accumulate unbounded object
work.

## Staging Defaults

For the first public staging VM:

```text
DBBASIC_MAX_REQUEST_BYTES=1048576
DBBASIC_MAX_CONCURRENT_REQUESTS=64
DBBASIC_MAX_CONCURRENT_EXECUTIONS=8
DBBASIC_OBJECT_TIMEOUT_SECONDS=5
DBBASIC_RATE_LIMIT_REQUESTS=1000
DBBASIC_RATE_LIMIT_WINDOW_SECONDS=60
DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS=true
DBBASIC_ENABLE_SOURCE_WRITES=false
```

Keep source writes closed until auth, permissions, backups, and rollback checks
are working. Keep public routes narrow. Let the object server prove the loop
without allowing arbitrary public code changes.

## Cluster Direction

Local capacity should become a station signal. A single station can already
return `503` when it is full. Later, station heartbeat or health data should
include capacity fields so a cluster router can avoid saturated stations before
forwarding object work.

That signal should include request slots, execution slots, recent error rate,
and basic machine load such as CPU and memory pressure. That keeps the cluster
model simple: each station knows its own limits, and the router uses those
limits to choose where work should go.

## Logs And Garbage Collection

Traffic controls also need disk controls:

- rotate object logs before they grow forever
- gzip rotated logs at rest
- keep a bounded number of rotated logs per object
- keep backup retention explicit
- delete temp files and stale lock files only through known runtime paths

The active object log stays plain TSV so tools can tail it. Rotated logs can be
read in place with normal Unix tools such as `gzip -cd`.

## Next Limits

The request, concurrency, rate-limit, and timeout caps are only the first
boundaries. The next production-hardening steps are:

- CPU and memory isolation for untrusted object code
- a longer-lived worker pool so timeout-enabled execution does not need a new
  process per request
