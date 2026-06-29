# Single VM Deployment

This is the first deployment target for DBBASIC Object Server: one small VM,
one ASGI process, one object source directory, and one data directory.

The goal is not to claim the public runtime is production-ready. The goal is to
keep a clean staging server running so install, restart, logs, object storage,
and the edit/run/inspect loop are tested on a real machine.

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
DBBASIC_ENABLE_SOURCE_WRITES=false
```

For the first VM boot, leave source writes disabled. After health checks and
proxy access are working, a staging server can enable source writes with a
strong local token:

```text
DBBASIC_ENABLE_SOURCE_WRITES=true
DBBASIC_ADMIN_TOKEN=replace-with-a-generated-token
```

Generate a local token on the VM:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not commit real tokens, VM hostnames, private URLs, or deployment-specific
paths to this repository.

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
.venv/bin/uvicorn object_server:app --host 127.0.0.1 --port 8001
```

In another shell:

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/objects
curl http://127.0.0.1:8001/objects/site_home
curl 'http://127.0.0.1:8001/objects/site_home?state=true'
curl 'http://127.0.0.1:8001/objects/site_home?logs=true'
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
ExecStart=/opt/dbbasic-object-server/.venv/bin/uvicorn object_server:app --host 127.0.0.1 --port 8001
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
```

Watch logs:

```bash
sudo journalctl -u dbbasic-object-server -f
```

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

The minimum backup set is:

```text
/var/lib/dbbasic-object-server/objects
/var/lib/dbbasic-object-server/data
/etc/dbbasic-object-server.env
/etc/systemd/system/dbbasic-object-server.service
```

For staging, a daily tarball or VM snapshot is enough. Before calling this a
production deployment, backup and restore should be tested by restoring those
paths on a second clean VM and running the health/object checks again.

## Current Limits

This deployment shape is for staging and dogfooding.

Known limits:

- the current direct Python loader is not a production sandbox
- source writes still use a temporary admin token gate
- object permissions are not enforced yet
- request body size and execution timeout limits are not complete
- WebSocket/SSE runtime behavior is still design-stage

That is still useful. A clean VM lets DBBASIC prove that install, restart,
object lookup, execution, state, logs, and versions work away from a developer
machine.
