# Backup And Restore

DBBASIC runtime backups protect the object loop by keeping source, state, logs,
files, versions, schemas, and collection records recoverable together.

The public backup helper is:

```bash
python -m object_backup
```

It creates a tar/gzip archive with a JSON manifest and a narrow runtime backup
set.

## Included

Runtime backups include:

```text
objects/
data/state/
data/logs/
data/versions/
data/source_changes/
data/schema_versions/
data/record_changes/
data/package_changes/
data/file_changes/
data/files/
data/schemas/
data/collections/
```

That covers object source, object state, current logs, rotated compressed logs,
source versions, source change history, schema change history, record change
history, package change history, object file change history, object-owned
files, schema metadata, and TSV-backed collection records.

## Excluded

Runtime backups deliberately exclude:

- deployment secrets
- environment files
- systemd service files
- git history
- virtualenvs
- caches
- lock files
- temp files
- ephemeral rate-limit files

The environment file and service unit still need operator-managed backups, but
they should be stored through a secret-aware server backup process, not inside
the portable runtime archive.

## Create

```bash
mkdir -p /var/backups/dbbasic-object-server

python -m object_backup create \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --objects-dir /var/lib/dbbasic-object-server/objects \
  --data-dir /var/lib/dbbasic-object-server/data
```

The command writes `dbbasic-backup-manifest.json` inside the archive. The
manifest records the format version, timestamp, included roots, file count, byte
count, and warnings.

## Restore Points

Server-side mutations that can rewrite multiple runtime files should create a
restore point before writing. Package installs use:

```python
object_backup.create_runtime_restore_point("package-hello-world")
```

By default, restore points are written under:

```text
data/backups/
```

Set `DBBASIC_BACKUPS_DIR` to move those archives to a mounted backup volume or
another operator-managed path. Runtime backup archives do not include
`data/backups/`, so restore points do not recursively back up other archives.

Package restore uses the recorded restore point from a package install change,
not an arbitrary package path. That package-scoped HTTP route
(`POST /packages/{id}/restore`, gated by `DBBASIC_ENABLE_PACKAGE_RESTORE`)
is separate from full-runtime restore, which stays CLI-only. It calls
`restore_runtime_backup(..., overwrite=True, prune_extra=True)` so files created
by the package are removed if they were not present in the snapshot. Pruning is
limited to known runtime roots such as `objects/`, `data/state/`,
`data/logs/`, `data/collections/`, `data/schemas/`, and
`data/source_changes/`, `data/package_changes/`, `data/file_changes/`;
`data/backups/` is left alone.

## Verify

```bash
python -m object_backup verify \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --json
```

Verification checks that:

- the archive can be read
- the manifest is present
- the manifest count matches the archive contents
- archive paths stay under `objects/` or supported `data/` roots
- symlinks, hard links, device files, and other unsafe member types are rejected

## Restore

Restore is a **deliberate CLI operation**, not an HTTP verb. It is
destructive and whole-instance, so it is intentionally not exposed on the
admin surface (`/admin/status` reports `capabilities.backups.can_restore:
false`); a "restore" button one misclick from wiping live data is a
footgun. The pairing is: **download a backup from Scroll or
`GET /admin/backups/{id}/download` to get it, and this CLI to apply it.**

Runtime backups live under `data/backups/` (or `DBBASIC_BACKUPS_DIR`) and
are named `YYYYMMDDTHHMMSSffffffZ-<label>.tar.gz` (label `manual` for
on-demand, `package-<id>` for install restore points). Always verify
first (see above).

### Dry-run into clean directories first

```bash
mkdir -p /tmp/dbbasic-restore/objects /tmp/dbbasic-restore/data

python -m object_backup restore \
  /var/lib/dbbasic-object-server/data/backups/20260708T145041382734Z-manual.tar.gz \
  --objects-dir /tmp/dbbasic-restore/objects \
  --data-dir /tmp/dbbasic-restore/data \
  --json
```

By default restore refuses to overwrite existing files, so this dry run
never touches a live runtime. Start the server against the restored
directories and check it before trusting the archive:

```bash
DBBASIC_OBJECTS_DIR=/tmp/dbbasic-restore/objects \
DBBASIC_DATA_DIR=/tmp/dbbasic-restore/data \
uvicorn object_server:app --host 127.0.0.1 --port 8001
```

Then verify `/health` and representative object routes. A real recovery
drill should do this on a second clean VM.

### Recovering the live runtime

Stop the service first so nothing writes during the restore. Use
`--overwrite` to replace existing files, and `--prune-extra` (which
requires `--overwrite`) only when you want an exact match to the archive,
removing files added since — a powerful, opt-in flag:

```bash
sudo systemctl stop dbbasic-object-server

sudo -u dbbasic /opt/dbbasic-object-server/.venv/bin/python \
  -m object_backup restore \
  /var/lib/dbbasic-object-server/data/backups/<id>.tar.gz \
  --objects-dir /var/lib/dbbasic-object-server/objects \
  --data-dir   /var/lib/dbbasic-object-server/data \
  --overwrite --prune-extra --json

sudo systemctl start dbbasic-object-server
curl http://127.0.0.1:8001/health
```

Pruning is limited to known runtime roots (`objects/`, `data/state/`,
`data/logs/`, `data/collections/`, `data/schemas/`, `data/source_changes/`,
`data/package_changes/`, `data/file_changes/`); `data/backups/` is left
alone, so restoring never destroys your other archives.

## HTTP Surface

On-demand backup and download are exposed on the admin API (and used by
Scroll's Backup screen) — see
[the HTTP contract](http-api-contract.md#backups):

- `GET /admin/backups` — inventory (`id`, `created_at`, `size`, `kind`, `scope`).
- `POST /admin/backups` — create a full-runtime backup now.
- `GET /admin/backups/{id}/download` — stream the archive.

All admin-gated (a backup contains credentials and service keys, so never
public). Restore is deliberately absent from this surface — use the CLI
above.

## Scheduling

Automatic backups are a **config option, off by default**. The run itself
is an external timer (systemd or cron) invoking `object_backup.py create`
on a cadence with retention; `DBBASIC_BACKUP_SCHEDULE` records the
operator's intent so `/admin/status` and Scroll can display it. A sample
daily systemd timer:

```ini
# /etc/systemd/system/dbbasic-backup.service
[Service]
Type=oneshot
User=dbbasic
EnvironmentFile=/etc/dbbasic-object-server.env
WorkingDirectory=/opt/dbbasic-object-server
ExecStart=/opt/dbbasic-object-server/.venv/bin/python -m object_backup create \
  /var/lib/dbbasic-object-server/data/backups/scheduled-%%i.tar.gz
```

```ini
# /etc/systemd/system/dbbasic-backup.timer
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
```

Note that on-box backups do not survive losing the VM — pair them with
provider-level disk backups (or copy archives to a bucket) for disaster
recovery. On-demand create and download always work regardless of any
schedule.
