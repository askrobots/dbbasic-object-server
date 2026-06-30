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
data/schema_versions/
data/record_changes/
data/files/
data/schemas/
data/collections/
```

That covers object source, object state, current logs, rotated compressed logs,
source versions, schema change history, record change history, object-owned
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

Restore into clean directories first:

```bash
mkdir -p /tmp/dbbasic-restore

python -m object_backup restore \
  /var/backups/dbbasic-object-server/runtime-YYYYMMDD-HHMMSS.tar.gz \
  --objects-dir /tmp/dbbasic-restore/objects \
  --data-dir /tmp/dbbasic-restore/data \
  --json
```

By default, restore refuses to overwrite existing files. Use `--overwrite` only
after deciding that the target runtime may be replaced.

After restore, run health and object checks against the restored runtime before
promoting it. For example, start the server against the restored directories:

```bash
DBBASIC_OBJECTS_DIR=/tmp/dbbasic-restore/objects \
DBBASIC_DATA_DIR=/tmp/dbbasic-restore/data \
uvicorn object_server:app --host 127.0.0.1 --port 8001
```

Then verify `/health` and representative object routes through the HTTP API or
Scroll. A real recovery drill should do that on a second clean VM.

## Scheduling

The old private prototype had a backup object that scheduled daily tar/gzip
archives and retention cleanup. The public code now has the lower-level backup
primitive first. A future admin object or Scroll action should call this helper
instead of reimplementing archive safety rules.
