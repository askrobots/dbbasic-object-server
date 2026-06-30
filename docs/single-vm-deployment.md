# Single VM Deployment

This is the first deployment target for DBBASIC Object Server: one small VM,
one ASGI process, one object source directory, and one data directory.

The goal is not to claim the public runtime is production-ready. The goal is to
keep a clean staging server running so install, restart, logs, object storage,
and the edit/run/inspect loop are tested on a real machine.

This shape should stay cheap and understandable: one small VM can run the
server, objects, TSV-backed state, logs, versions, HTTPS proxy, and monitoring
without requiring a separate database tier for the first useful app.

## Deployment Goal

The first deployment path should stay closer to a simple Unix/PHP-style upload
than a large multi-service app stack:

- one VM is enough for the first useful business app
- no container runtime is required for the base deployment
- no separate database server is required before the app proves itself
- server code, live object source, runtime data, and secrets live in separate
  Unix paths
- normal upgrades are `git pull`, package install, service restart, checks
- object edits do not require a full server redeploy
- every manual step should become scriptable after it stays boring

This does not mean skipping safety. The simple path still uses a service user,
systemd, a reverse proxy, HTTPS, private config, filesystem checks, backups, and
explicit public route allowlists.

## Verified Baseline

The first staging install was verified on:

- Ubuntu 24.04.4 LTS
- Python 3.12.3
- Git 2.43.0
- Caddy 2.11.4
- systemd with the DBBASIC service and Caddy running as separate units

Record the baseline on each new VM. Run the Caddy commands after Caddy is
installed, if the VM does not already have it.

```bash
lsb_release -a
python3 --version
git --version
caddy version
systemctl is-active dbbasic-object-server caddy
cd /opt/dbbasic-object-server
sudo -u dbbasic git rev-parse --short HEAD
```

Do not commit the VM's real IP address, hostname, or provider-specific details
to this repository.

## System Updates

Before using a new VM for staging, apply normal OS updates and reboot if Ubuntu
requests it:

```bash
sudo apt update
apt list --upgradable
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
sudo DEBIAN_FRONTEND=noninteractive apt-get autoremove -y
test -f /var/run/reboot-required && cat /var/run/reboot-required || true
```

If packages are kept back because they need new dependencies, install them
explicitly when they are normal platform packages:

```bash
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y fwupd
```

After a reboot, verify the services and endpoint again:

```bash
sudo systemctl is-active dbbasic-object-server caddy
curl https://dbbasic.example.com/
```

## Provider Monitoring

For DigitalOcean Droplets, the metrics agent is useful on staging VMs because it
adds CPU, memory, disk, and network graphs plus alerting while the server is
under active development. DigitalOcean describes this as a free opt-in service.
It sends system telemetry for monitoring, not customer content.

Install it after normal OS updates:

```bash
curl -fsSL https://repos.insights.digitalocean.com/install.sh -o /tmp/do-agent-install.sh
sudo bash /tmp/do-agent-install.sh
```

Verify it:

```bash
systemctl is-active do-agent
systemctl is-enabled do-agent
dpkg-query -W do-agent
systemctl --no-pager --full status do-agent
```

Metrics usually appear in the Droplet's Insights tab after a few minutes. This
is provider-specific VM monitoring; it does not replace DBBASIC's own object
logs, state, versions, or future application/runtime metrics.

## Layout

Use separate paths for server code, object source, and runtime data:

```text
/opt/dbbasic-object-server
/var/lib/dbbasic-object-server/objects
/var/lib/dbbasic-object-server/data
```

- `/opt/dbbasic-object-server` is the git checkout.
- `/var/lib/dbbasic-object-server/objects` contains live object source files.
- `/var/lib/dbbasic-object-server/data` contains state, logs, and versions.

Keeping objects and data outside the checkout lets the server be upgraded
without mixing application data into git history.

## Install

On a fresh Debian or Ubuntu VM:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
sudo useradd --system --home /var/lib/dbbasic-object-server --shell /usr/sbin/nologin dbbasic
sudo mkdir -p /opt/dbbasic-object-server
sudo mkdir -p /var/lib/dbbasic-object-server/objects
sudo mkdir -p /var/lib/dbbasic-object-server/data
sudo chown -R dbbasic:dbbasic /opt/dbbasic-object-server /var/lib/dbbasic-object-server
sudo chmod 755 /opt/dbbasic-object-server
sudo chmod 750 /var/lib/dbbasic-object-server
sudo chmod 750 /var/lib/dbbasic-object-server/objects
sudo chmod 750 /var/lib/dbbasic-object-server/data
```

Clone and install the server:

```bash
sudo -u dbbasic git clone https://github.com/askrobots/dbbasic-object-server.git /opt/dbbasic-object-server
cd /opt/dbbasic-object-server
sudo -u dbbasic python3 -m venv .venv
sudo -u dbbasic .venv/bin/python -m pip install -e '.[server]'
```

## Environment

Create `/etc/dbbasic-object-server.env`:

```text
DBBASIC_OBJECTS_DIR=/var/lib/dbbasic-object-server/objects
DBBASIC_DATA_DIR=/var/lib/dbbasic-object-server/data
DBBASIC_BACKUPS_DIR=/var/lib/dbbasic-object-server/data/backups
DBBASIC_PACKAGES_DIR=/opt/dbbasic-object-server/packages
DBBASIC_ENABLE_SOURCE_WRITES=false
DBBASIC_ENABLE_PACKAGE_INSTALLS=false
DBBASIC_ADMIN_TOKEN=replace-with-a-generated-token
DBBASIC_MAX_REQUEST_BYTES=1048576
DBBASIC_MAX_CONCURRENT_REQUESTS=64
DBBASIC_MAX_CONCURRENT_EXECUTIONS=8
DBBASIC_OBJECT_TIMEOUT_SECONDS=5
DBBASIC_TRUSTED_IN_PROCESS_OBJECTS=site_home
DBBASIC_RATE_LIMIT_REQUESTS=1000
DBBASIC_RATE_LIMIT_WINDOW_SECONDS=60
DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS=true
DBBASIC_ENABLE_PERMISSION_AUDIT=false
DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=false
DBBASIC_PERMISSION_TRUST_HEADERS=false
DBBASIC_ENABLE_RECORD_EVENTS=true
DBBASIC_EVENT_KEEP_COUNT=1000
DBBASIC_EVENT_KEEP_SECONDS=604800
DBBASIC_LOG_MAX_BYTES=10485760
DBBASIC_LOG_COMPRESS_ROTATED=true
DBBASIC_LOG_KEEP_ROTATED=32
```

Generate a local token on the VM:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use that generated value for `DBBASIC_ADMIN_TOKEN`. Do not reuse test tokens,
README placeholders, or tokens from another server. It is required for local or
admin object listing and introspection requests such as source, state, logs,
metadata, and versions.

For the first VM boot, leave source writes disabled. After health checks,
backups, and proxy access are working, a staging server can enable source
writes:

```text
DBBASIC_ENABLE_SOURCE_WRITES=true
```

Permission audit and enforcement should also stay closed on first boot. Turn on
audit mode first to watch route decisions without blocking users:

```text
DBBASIC_ENABLE_PERMISSION_AUDIT=true
```

Only enable blocking after the persisted policy and auth gateway are confirmed:

```text
DBBASIC_ENABLE_PERMISSION_ENFORCEMENT=true
```

`DBBASIC_PERMISSION_TRUST_HEADERS=true` is only appropriate when a trusted proxy
or auth gateway strips client-supplied identity headers and writes fresh
`X-DBBASIC-*` headers itself.

Do not commit real tokens, VM hostnames, private URLs, or deployment-specific
paths to this repository.

After creating the file, keep it readable by root and the service group only:

```bash
sudo chown root:dbbasic /etc/dbbasic-object-server.env
sudo chmod 640 /etc/dbbasic-object-server.env
```

## Filesystem Check

The public package includes a small single-VM layout checker. Run it after
installing the server and after changing ownership or deployment paths:

```bash
cd /opt/dbbasic-object-server
set -a
. /etc/dbbasic-object-server.env
set +a
.venv/bin/python -m deployment_checks
```

The checker validates the normal Unix boundary:

- server code under `/opt/dbbasic-object-server`
- live object source under `/var/lib/dbbasic-object-server/objects`
- runtime data under `/var/lib/dbbasic-object-server/data`
- deployment secrets under `/etc/dbbasic-object-server.env`
- systemd unit under `/etc/systemd/system/dbbasic-object-server.service`

Errors should be fixed before exposing routes publicly. Warnings usually mean a
runtime path is visible to other local users. On a single-purpose staging VM
that may not break anything, but the safer default is `750` for object and data
directories.

## Traffic Limits

Keep the public server narrow while it is staging.

`DBBASIC_MAX_REQUEST_BYTES` limits inbound HTTP request bodies before JSON
parsing or object execution. The default is 1 MiB:

```text
DBBASIC_MAX_REQUEST_BYTES=1048576
DBBASIC_MAX_CONCURRENT_REQUESTS=64
DBBASIC_MAX_CONCURRENT_EXECUTIONS=8
```

Configure the same or smaller request body limit in the reverse proxy so Caddy
or nginx can reject oversized traffic before Python handles it. The app-level
limit stays in place because proxy configuration can drift.

The concurrency limits are per process. Under overload, the server returns
`503` rather than queueing unlimited work on a small VM. Future production
hardening still needs CPU/memory isolation and a longer-lived worker pool.
`DBBASIC_OBJECT_TIMEOUT_SECONDS` runs object methods in a worker process and
returns `504` if the wall-clock timeout is exceeded.
`DBBASIC_TRUSTED_IN_PROCESS_OBJECTS` is a comma-separated allowlist for
reviewed server-owned objects that should keep the fast in-process path, such as
the public homepage. Do not add user-created objects to this list.
The rate limit values above return `429` with `Retry-After` when one IP or
valid admin token exceeds the configured window.
`DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS` is appropriate only because uvicorn is
bound to `127.0.0.1` behind Caddy.

See `traffic-limits.md` for the operating model.

## Log Maintenance

Object logs are part of the runtime feedback loop, so staging servers should not
let them grow forever.

The active log for an object stays plain TSV:

```text
/var/lib/dbbasic-object-server/data/logs/{object_id}/log.tsv
```

When it reaches `DBBASIC_LOG_MAX_BYTES`, the server rotates it. New rotated logs
are gzip-compressed by default:

```text
/var/lib/dbbasic-object-server/data/logs/{object_id}/log-YYYYMMDD-HHMMSS-ffffff.tsv.gz
```

Inspect compressed logs without expanding a second copy:

```bash
gzip -cd /var/lib/dbbasic-object-server/data/logs/site_home/log-*.tsv.gz
```

Garbage collection is controlled by `DBBASIC_LOG_KEEP_ROTATED`. The default
keeps the newest 32 rotated files per object and deletes older rotated logs
after a successful rotation. Set it to `0` to keep all rotated logs.

This is at-rest compression. For in-motion compression, start with reverse-proxy
HTTP compression for large log responses. Later station replication should
compress batches, not individual tiny events.

Event rows are a delivery queue, not the durable audit log. The server and
daemon prune old `data/state/events/state.tsv` event rows with:

```bash
DBBASIC_EVENT_KEEP_COUNT=1000
DBBASIC_EVENT_KEEP_SECONDS=604800
```

Set either value to `0` to disable that retention rule. Subscriptions stay in
place, and any event referenced by a subscription `last_event_id` is protected so
the daemon keeps its delivery cursor.

## Packages

Packages are read from `DBBASIC_PACKAGES_DIR`. The public server currently
supports package listing, dry-run planning, and package installs only when
`DBBASIC_ENABLE_PACKAGE_INSTALLS=true`. Dry-run and install requests append
compact audit rows under
`/var/lib/dbbasic-object-server/data/package_changes/{package_id}/changes.jsonl`.
Package installs create a runtime restore point before writing source, schema,
or seed files. Restore points default to
`/var/lib/dbbasic-object-server/data/backups/`; set `DBBASIC_BACKUPS_DIR` if
they should live on another mounted volume.

Keep `DBBASIC_ENABLE_PACKAGE_INSTALLS=false` on public staging unless `/packages`
is reachable only from a private admin surface. The first install path writes
objects and schemas, creates seed TSV files only when no data exists, and rejects
permissions/migrations until merge/run semantics are explicit.

## Minimal Object

Create one object so the VM can prove execution, state, and logs:

```bash
sudo -u dbbasic mkdir -p /var/lib/dbbasic-object-server/objects/site
sudo -u dbbasic tee /var/lib/dbbasic-object-server/objects/site/home.py >/dev/null <<'PY'
def GET(request):
    count = _state_manager.get("count", 0) + 1
    _state_manager.set("count", count)
    return {
        "status": "ok",
        "message": "DBBASIC Object Server is running",
        "count": count,
    }
PY
```

This object resolves as `site_home`.

For a public staging page, the same object can return HTML instead of JSON:

```python
def GET(request):
    count = int(_state_manager.get("count", 0)) + 1
    _state_manager.set("count", count)
    _logger.info("site_home served", count=count, response_type="html")

    return {
        "content_type": "text/html; charset=utf-8",
        "body": f"<!doctype html><h1>DBBASIC Object Server</h1><p>Served {count} times.</p>",
    }
```

That keeps the staging page honest: it is a live object with state and logs, not
a static file.

## Run Manually

Before creating a service, run the server by hand:

```bash
cd /opt/dbbasic-object-server
set -a
. /etc/dbbasic-object-server.env
set +a
.venv/bin/uvicorn object_server:app --host 127.0.0.1 --port 8001 --no-access-log
```

In another shell:

```bash
curl http://127.0.0.1:8001/health
curl -H "Authorization: Token $DBBASIC_ADMIN_TOKEN" 'http://127.0.0.1:8001/health?capacity=true'
curl -H "Authorization: Token $DBBASIC_ADMIN_TOKEN" http://127.0.0.1:8001/objects
curl http://127.0.0.1:8001/objects/site_home
curl -H "Authorization: Token $DBBASIC_ADMIN_TOKEN" 'http://127.0.0.1:8001/objects/site_home?state=true'
curl -H "Authorization: Token $DBBASIC_ADMIN_TOKEN" 'http://127.0.0.1:8001/objects/site_home?logs=true'
```

## systemd

Create `/etc/systemd/system/dbbasic-object-server.service`:

```ini
[Unit]
Description=DBBASIC Object Server
After=network-online.target
Wants=network-online.target

[Service]
User=dbbasic
Group=dbbasic
WorkingDirectory=/opt/dbbasic-object-server
EnvironmentFile=/etc/dbbasic-object-server.env
ExecStart=/opt/dbbasic-object-server/.venv/bin/uvicorn object_server:app --host 127.0.0.1 --port 8001 --no-access-log
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/dbbasic-object-server

[Install]
WantedBy=multi-user.target
```

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dbbasic-object-server
sudo systemctl status dbbasic-object-server
curl http://127.0.0.1:8001/health
curl -H "Authorization: Token $DBBASIC_ADMIN_TOKEN" 'http://127.0.0.1:8001/health?capacity=true'
```

Watch logs:

```bash
sudo journalctl -u dbbasic-object-server -f
```

`--no-access-log` keeps successful request lines out of journald. DBBASIC keeps
the useful application trail in object logs and `/health?metrics=true`, which
are the surfaces Scroll should read. If access analytics are needed later,
build them as an explicit app/object instead of relying on raw process logs.

Cap journald so process logs cannot consume the VM:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/99-dbbasic.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=128M
SystemKeepFree=1G
MaxRetentionSec=7day
Compress=yes
EOF
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=128M
sudo journalctl --disk-usage
```

## Upgrade

For a staging VM that already follows this layout, a normal server-code upgrade
should be small and repeatable:

```bash
cd /opt/dbbasic-object-server
sudo -u dbbasic git pull --ff-only
sudo -u dbbasic .venv/bin/python -m pip install -e '.[server]'
sudo systemctl restart dbbasic-object-server
```

Then verify the local service, filesystem layout, public proxy, and deployed
commit:

```bash
cd /opt/dbbasic-object-server
set -a
. /etc/dbbasic-object-server.env
set +a
.venv/bin/python -m deployment_checks
curl http://127.0.0.1:8001/health
curl https://dbbasic.example.com/
sudo systemctl is-active dbbasic-object-server caddy
sudo -u dbbasic git rev-parse --short HEAD
```

If the upgrade only changes live objects under
`/var/lib/dbbasic-object-server/objects`, the server should not need a git
deploy. That is the object loop: edit one object, run it, inspect state/logs,
and keep the version trail.

## HTTPS Proxy

For a public or staging hostname, put Caddy or nginx in front of uvicorn and keep
uvicorn bound to `127.0.0.1`.

Install Caddy if the VM does not already have a reverse proxy:

```bash
sudo apt install -y caddy
```

Example Caddyfile using documentation domains:

```caddyfile
dbbasic.example.com {
    reverse_proxy 127.0.0.1:8001
}
```

Replace `dbbasic.example.com` only on the VM. Do not commit real deployment
hostnames into this repository.

If Caddy was already installed, inspect what it is serving before taking over
the hostname:

```bash
sudo systemctl status caddy --no-pager
sudo sed -n '1,220p' /etc/caddy/Caddyfile
sudo find /usr/share/caddy -maxdepth 3 -type f -printf '%p\t%s bytes\n'
```

Back up the old config before changing it:

```bash
sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-dbbasic
```

For the earliest public staging endpoint, expose only the hello object and
health check until auth, permissions, and source visibility are ready:

```caddyfile
dbbasic.example.com {
    handle / {
        rewrite * /objects/site_home
        reverse_proxy 127.0.0.1:8001
    }

    handle /health {
        reverse_proxy 127.0.0.1:8001
    }

    handle /objects/site_home* {
        reverse_proxy 127.0.0.1:8001
    }

    handle {
        respond "Not found" 404
    }
}
```

Validate and reload:

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Then test:

```bash
curl https://dbbasic.example.com/
curl https://dbbasic.example.com/health
curl https://dbbasic.example.com/objects/site_home
curl https://dbbasic.example.com/objects
```

The first three should return responses from the object server. The root and
`site_home` responses may be JSON or HTML depending on the object. The full
`/objects` route should stay blocked by Caddy in this early staging mode.

## Public Code Execution Controls

In the early staging deployment, do not expose the full object server API
directly to the internet.

Use all of these controls together:

- keep uvicorn bound to `127.0.0.1`
- expose only specific routes through Caddy
- keep `DBBASIC_ENABLE_SOURCE_WRITES=false`
- do not add a public object-create route until permissions are enforced
- keep live object source and data out of the git checkout

With that shape, outside users can only run the explicitly proxied objects. They
cannot list every object, read source, write source, roll back source, or create
new runnable code through the public hostname.

## Backup

The portable runtime backup set is:

```text
/var/lib/dbbasic-object-server/objects
/var/lib/dbbasic-object-server/data/state
/var/lib/dbbasic-object-server/data/logs
/var/lib/dbbasic-object-server/data/versions
/var/lib/dbbasic-object-server/data/schema_versions
/var/lib/dbbasic-object-server/data/record_changes
/var/lib/dbbasic-object-server/data/package_changes
/var/lib/dbbasic-object-server/data/files
/var/lib/dbbasic-object-server/data/schemas
/var/lib/dbbasic-object-server/data/collections
```

Create and verify a portable runtime backup with:

```bash
python -m object_backup create \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --objects-dir /var/lib/dbbasic-object-server/objects \
  --data-dir /var/lib/dbbasic-object-server/data

python -m object_backup verify \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --json
```

Restore into clean directories first:

```bash
python -m object_backup restore \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --objects-dir /tmp/dbbasic-restore/objects \
  --data-dir /tmp/dbbasic-restore/data \
  --json
```

The runtime archive deliberately excludes deployment secrets, environment files,
systemd service files, git history, virtualenvs, caches, lock files, temp files,
and ephemeral rate-limit files.

The server environment file and systemd unit still need to be backed up by an
operator-controlled process:

```text
/etc/dbbasic-object-server.env
/etc/systemd/system/dbbasic-object-server.service
```

Keep those secret-aware backups separate from portable runtime archives. Before
calling this a production deployment, restore runtime backups on a second clean
VM and run the health/object checks again.

See `backup-restore.md` for the runtime archive contract and restore rules.

## Current Limits

This deployment shape is for staging and dogfooding.

Known limits:

- the current direct Python loader is not a production sandbox
- source writes still use a temporary admin token gate
- route permission enforcement is available, but off until a deployment enables it
- real user/session auth still needs to replace trusted staging headers
- WebSocket/SSE runtime behavior is still design-stage

That is still useful. A clean VM lets DBBASIC prove that install, restart,
object lookup, execution, state, logs, and versions work away from a developer
machine.
