# Docker / Coolify Deployment

This is the second deployment path for DBBASIC Object Server, alongside the
bare-VM systemd installer in [`docs/single-vm-deployment.md`](single-vm-deployment.md).
It packages the same server into a container image so it can run under
Docker Compose directly, or be deployed with [Coolify](https://coolify.io/).

The goal is the same as the bare-VM path: stay boring, stay conservative on
first boot, and keep the object edit/run/inspect loop working the same way.
This page does not repeat what is not Docker-specific -- permissions,
source-write gates, package installs, first-auth setup, and traffic limits
are covered in [`docs/single-vm-deployment.md`](single-vm-deployment.md) and
the root [`README.md`](../README.md); read those too.

## What Ships in the Image vs. What Must Be a Volume

This is the concept the rest of this page assumes.

The image is stateless. It contains the server code and the installable
package fixtures under `packages/` -- both of those are tracked in git and
rebuilt into the image on every `docker build`.

Everything the server writes at runtime is state, not code, and must live on
a mounted volume outside the image:

- object source under `DBBASIC_OBJECTS_DIR`
- TSV-backed records, state, logs, versions, schemas, and changelogs under
  `DBBASIC_DATA_DIR`
- identity (accounts, users, sessions, credential hashes)
- the permission policy and audit log
- backups under `DBBASIC_BACKUPS_DIR`
- the auto-generated admin token (see "First Boot" below)

`Dockerfile` sets `DBBASIC_OBJECTS_DIR=/data/objects` and
`DBBASIC_DATA_DIR=/data/state` as the image's default paths, and
`docker-compose.yml` mounts two named volumes at exactly those paths. This
mirrors the same separation `docs/single-vm-deployment.md` keeps between
`/opt/dbbasic-object-server` (the git checkout) and
`/var/lib/dbbasic-object-server` (runtime data) on the bare-VM path -- code
and state never share a lifecycle.

That split is what makes the two halves of the deploy story work
independently (see "Deploy Cadence" below).

## Quickstart (Docker Compose)

From a checkout of this repository:

```bash
cp .env.example .env
# edit .env if you want to change any of the conservative defaults
docker compose up --build
```

This builds the image, starts the `object-server` service bound to
`0.0.0.0:8001` inside the container, and publishes it on `localhost:8001`.
Watch the logs for the first-boot admin token message (see "First Boot"
below), then:

```bash
curl http://localhost:8001/health
```

Object source and runtime data persist on the `objects` and `data` named
volumes declared in `docker-compose.yml`, so `docker compose up --build`
after a code change keeps existing objects, records, and identity intact.

`docker-compose.yml` includes a commented-out, clearly-labeled optional
`object-daemon` service for `object_daemon.py` (the scheduler/queue/event
background worker). It is not required for the app to run; uncomment it
only if you need scheduled tasks, queue processing, or webhook event
delivery running in the background.

## Coolify Deployment

Coolify can deploy this repository two ways:

- **Dockerfile application** -- Coolify detects the `Dockerfile` at the repo
  root and builds/runs it directly.
- **Docker Compose resource** -- Coolify deploys `docker-compose.yml`
  directly.

The Compose path is the cleaner one for this project, because it declares
the two persistent volumes explicitly instead of leaving them implicit.
Prefer it unless you have a reason to use the plain Dockerfile path.

Either way, in Coolify:

1. **Create the application** from this repository, pointing at the
   `Dockerfile` or `docker-compose.yml` per the choice above.
2. **Configure persistent storage.** In Coolify's Storage / Persistent
   Storage settings, add two volumes (or reuse the ones declared in
   `docker-compose.yml` if you deployed it as a Compose resource) mapped to
   the same container paths the image expects:

   ```text
   /data/objects
   /data/state
   ```

   These must match `DBBASIC_OBJECTS_DIR` and `DBBASIC_DATA_DIR`. If you
   change one, change the other.

3. **Set environment variables** through Coolify's Environment Variables UI,
   using `.env.example` as the list of what to set and why. At minimum,
   decide whether to set `DBBASIC_ADMIN_TOKEN` explicitly (recommended once
   this is a real deployment) or leave it unset for the container to
   generate one on first boot.
4. **Enable health-check-based deploys.** The image's `HEALTHCHECK`
   instruction (and `docker-compose.yml`'s `healthcheck:` block) call
   `GET /health`. Point Coolify's deployment health check at the same
   endpoint so it waits for a healthy container before cutting traffic over
   on redeploy, giving zero-downtime deploys for server-code changes.
5. **Put a domain in front of it.** Coolify's own proxy handles TLS and
   hostname routing to the container's published port, the same way Caddy
   sits in front of, not inside, the systemd unit on the bare-VM path. This
   repository's docs use `object.dbbasic.com` as the example public domain;
   use your own.

## Deploy Cadence

There are two independent kinds of change here, and only one of them needs
an image rebuild:

- **Server code changes** (anything in this git repository) require
  rebuilding and redeploying the image. Under Coolify this is a normal
  git-push-to-deploy flow: push to the branch Coolify watches, it rebuilds
  the image, runs the health check, and cuts over.
- **Object, schema, and package edits** made live through the admin HTTP API
  (the same API described in the root `README.md` and
  `docs/single-vm-deployment.md`) land directly on the persistent volumes.
  They take effect on the next request, with no image rebuild and no
  redeploy, exactly like the bare-VM deployment's edit/run/inspect loop.

This is the reason the volume split above exists: redeploying the image
never touches `/data/objects` or `/data/state`, so in-flight object work
survives ordinary code deploys.

## First Boot: the Admin Token

If `DBBASIC_ADMIN_TOKEN` is not set, `scripts/docker-entrypoint.sh` (the
image's container entrypoint) generates one, writes it to a file under the
persistent data volume so it survives restarts and rebuilds, and prints it
once, clearly, to the container's logs:

```bash
docker compose logs object-server | grep -A4 "generated a new admin token"
```

or, in Coolify, check the application's log viewer after first deploy.

The token is never baked into the image and never written anywhere in the
git checkout -- only into the runtime-only data volume. It is not
regenerated or overwritten on later boots.

Once you have confirmed the deployment works, set `DBBASIC_ADMIN_TOKEN`
explicitly (in `.env` for Compose, or in Coolify's environment variables
UI) rather than relying on the generated value. That gives you a token you
control and can rotate deliberately, and it is required if you ever need to
recreate the data volume from a backup.

## See Also

For everything not specific to Docker -- write gates, package installs,
permission audit/enforcement, traffic limits, backups, and bootstrapping the
first admin user and password login -- see:

- [`docs/single-vm-deployment.md`](single-vm-deployment.md)
- the root [`README.md`](../README.md), especially "First Auth Setup" and
  "Public Repository Safety"

## Security Notes

These mirror the root README's "Public Repository Safety" checklist:

- Never commit a real `.env`. Only `.env.example` (placeholder values and
  comments) belongs in git; `.env` is git-ignored.
- Never bake a real admin token, password hash, or session secret into an
  image layer. The entrypoint's generated token is written only to the
  mounted data volume, not to any path that gets committed or that survives
  outside that volume.
- Treat the persistent data volume as containing credentials-equivalent
  data: it holds the admin token file, identity credential hashes, and
  session records. Back it up and restrict access to it the same way you
  would `/var/lib/dbbasic-object-server` on the bare-VM path.
- Do not commit real IPs, hostnames, or deployment-specific details into
  this repository, including into Coolify configuration you might otherwise
  be tempted to paste into a doc or issue.
