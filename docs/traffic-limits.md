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

## High-Traffic Shape

Use layered limits:

- reverse proxy body limit before Python receives the request
- app body limit through `DBBASIC_MAX_REQUEST_BYTES`
- per-IP and per-token rate limits before write or execute paths
- concurrency caps for total requests and object executions
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
DBBASIC_ENABLE_SOURCE_WRITES=false
```

Keep source writes closed until auth, permissions, backups, and rollback checks
are working. Keep public routes narrow. Let the object server prove the loop
without allowing arbitrary public code changes.

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

The request body cap is only the first boundary. The next production-hardening
steps are:

- execution wall-clock timeout
- max concurrent requests
- max concurrent object executions
- rate limiting by IP and token
- CPU and memory isolation for untrusted object code

