"""DBBASIC package manifest discovery, dry-run planning, and gated installs.

Packages are installable bundles of objects, schemas, permissions, seed data,
and migrations. Installs are intentionally conservative: object and schema files
can be created or replaced, seed files can be created, and permissions/migrations
wait for explicit merge/run semantics.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import object_collections
import object_namespace
import object_package_baselines
import object_permission_store
import object_permissions
import object_reconciles
import object_record_changes
import object_records
import object_schemas
import object_source
import object_state
from object_namespace import get_object_roots, object_id_from_path, resolve_object_id, validate_object_id
from object_versions import DEFAULT_DATA_DIR

PACKAGES_DIR = "packages"
MANIFEST_FILE = "dbbasic-package.json"
PACKAGE_MIGRATIONS_DIR = "package_migrations"

_PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MIGRATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# A package's `schedules` become task_* rows in the scheduler trigger's
# state, which is where object_daemon.process_scheduler reads them from.
# Before this existed, every recurring pass on a running server had been
# hand-entered into that state and appeared NOWHERE in the repository, so
# rebuilding a box silently lost its daily work -- exactly what the
# scheduler object's own docstring warns about. A schedule is part of what
# an app IS, so it ships with the app.
SCHEDULER_OBJECT_ID = "scheduler"
_TASK_KEY_PREFIX = "task_"
_SCHEDULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCHEDULE_TYPES = ("cron", "onetime")
_SCHEDULE_METHODS = ("POST", "GET", "PUT", "DELETE")
# Fields the daemon stamps back onto a task as it runs. An install must
# never touch them: they are the record of what actually happened, and a
# package upgrade is not a reason to forget it (doctrine #8 -- independent
# evidence is never edited).
_SCHEDULE_RUNTIME_FIELDS = ("next_run", "last_run", "run_count")

# A package's `nav` entries become rows in the `nav_entries` collection,
# which is where every navigation surface reads its doors from.
# Before this existed there were THREE hand-maintained lists of the same
# apps -- a JS array in site_nav, a Python tuple in site_home, and a
# collection->URL map for search hits -- and they had already drifted:
# 25 entries, 21 entries, and no mention at all of shop, intake, billing,
# timers, banking, shipping, receiving or payments. A door is part of what
# an app IS, so it ships with the app, exactly like its schedule.
NAV_COLLECTION = "nav_entries"
_NAV_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAV_SURFACES = ("public", "member", "operator", "hidden")
_NAV_DEFAULT_GROUP = "Apps"
_NAV_DEFAULT_ORDER = 100
# What the package OWNS: reinstalling restates every one of these.
_NAV_PACKAGE_FIELDS = ("label", "path", "blurb", "group", "surface", "order")
# What the OPERATOR owns. The package says what an app is; the operator
# says what they want to see. An install never touches this column, for
# the same reason it never restarts a paused schedule (doctrine #8 --
# somebody else's deliberate decision is not an install's to revert).
_NAV_OPERATOR_FIELDS = ("operator_hidden",)

# A package's `attention` entries become rows in the `attention_sources`
# collection, which is where the daemon's rollup pass reads its list of
# providers from and how a surface learns that a queue exists at all.
# Third use of the pattern that fixed cron (`schedules`) and navigation
# (`nav`), and for the same reason: this server is built out of gates and
# derived states, every one of which produces a queue of things a machine
# deliberately refused to decide -- scans `extracted`, time `submitted`,
# invoices overdue, bank lines unmatched. It has been computing those
# queues all along and throwing them away at the end of each pass;
# `system_scan_processor` literally returns a field named
# `needing_a_human` and nothing reads it. Only the package that owns a
# domain can honestly say what "needs a human" means in it, so the
# declaration ships with the app.
ATTENTION_COLLECTION = "attention_sources"
_ATTENTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ATTENTION_SEVERITIES = ("normal", "warning", "urgent")
_ATTENTION_DEFAULT_GROUP = "Apps"
_ATTENTION_DEFAULT_SEVERITY = "normal"
# What the package OWNS: reinstalling restates every one of these.
_ATTENTION_PACKAGE_FIELDS = ("object_id", "label", "path", "nav_id", "group", "severity")
# What the OPERATOR owns. A deployment that does not care about a queue
# silences it here rather than by editing somebody else's manifest, and an
# install never writes this column -- same posture as nav's
# `operator_hidden` and a paused schedule (doctrine #8).
_ATTENTION_OPERATOR_FIELDS = ("operator_muted",)


class InvalidPackageIdError(ValueError):
    """Raised when a package id is not safe for routes or storage."""


class PackageNotFoundError(LookupError):
    """Raised when a package directory or manifest is missing."""


class InvalidPackageManifestError(ValueError):
    """Raised when a package manifest is invalid."""


class PackageInstallError(RuntimeError):
    """Raised when a package install would be unsafe or unsupported."""


def validate_package_id(package_id: str) -> bool:
    """Return True when a package id is route-safe."""
    if not isinstance(package_id, str):
        return False
    return bool(_PACKAGE_ID_RE.fullmatch(package_id))


def list_packages(
    *,
    root: Path | str = PACKAGES_DIR,
) -> list[dict[str, Any]]:
    """Return package summaries for all package manifests under root."""
    packages_root = Path(root)
    if not packages_root.exists() or not packages_root.is_dir():
        return []

    packages = []
    for path in sorted(packages_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not validate_package_id(path.name):
            continue
        manifest_path = path / MANIFEST_FILE
        if manifest_path.is_file():
            packages.append(_package_summary(_load_package(path.name, path)))
    return packages


def get_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
) -> dict[str, Any]:
    """Return one normalized package manifest."""
    package_dir = _package_dir(package_id, root)
    return _load_package(package_id, package_dir)


DATA_DIR_ENV = "DBBASIC_DATA_DIR"


def resolve_base_dir(base_dir: Path | str | None = None) -> Path:
    """The data directory an install should write to.

    An explicit `base_dir` always wins. Otherwise the ENVIRONMENT decides,
    because `DBBASIC_DATA_DIR` is the one authoritative statement of where
    this server's data lives -- it is what the server itself reads, what
    every package object reads, and what the systemd unit sets.

    This used to fall straight through to the literal "data", ignoring the
    environment even when it was set to somewhere else entirely. The result
    was the worst shape a bug can take: an install that reported
    `"status": "written"` for every schema and seed, having written them
    into a freshly created `./data` beside the checkout that nothing reads.
    Nothing failed, nothing warned, and the package was simply absent from
    the running server. Hit twice in one afternoon on a box whose layout was
    well understood, which is the argument for fixing the default rather
    than remembering harder.

    Callers that genuinely want a relative "data" can still ask for it by
    name; what they can no longer do is get it by accident.
    """
    if base_dir is not None:
        return Path(base_dir)
    return Path(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def dry_run_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str | None = None,
    object_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return a non-mutating package install plan."""
    package_dir = _package_dir(package_id, root)
    package = _load_package(package_id, package_dir)
    base = resolve_base_dir(base_dir)

    warnings: list[str] = []
    objects = [
        _object_change(entry, package_dir=package_dir, object_roots=object_roots, warnings=warnings)
        for entry in package["objects"]
    ]
    schemas = [
        _schema_change(entry, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["schemas"]
    ]
    permissions = [
        _permission_change(
            entry,
            package_id=package_id,
            package_dir=package_dir,
            base_dir=base,
            warnings=warnings,
        )
        for entry in package["permissions"]
    ]
    seed = [
        _seed_change(entry, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["seed"]
    ]
    migrations = [
        _migration_change(entry, package=package_id, package_dir=package_dir, base_dir=base, warnings=warnings)
        for entry in package["migrations"]
    ]
    # A schedule may name an object this same install is about to write, so
    # "does the target exist" is asked of the union of what is already
    # resolvable and what this package provides -- not of the server as it
    # stands right now.
    existing_tasks = _load_scheduler_tasks(base)
    installable_object_ids = {entry["id"] for entry in package["objects"]}
    roots_for_lookup = list(object_roots) if object_roots is not None else get_object_roots()
    for entry in package["schedules"] + package["attention"]:
        if entry["object_id"] in installable_object_ids:
            continue
        if resolve_object_id(entry["object_id"], roots_for_lookup) is not None:
            installable_object_ids.add(entry["object_id"])
    schedules = [
        _schedule_change(
            entry,
            existing=existing_tasks,
            installable_object_ids=installable_object_ids,
            warnings=warnings,
        )
        for entry in package["schedules"]
    ]
    # Nav entries are compared against what is already registered, which
    # is how "two packages claim one door" becomes visible before either
    # of them lands. A missing nav_entries collection reads as "nothing
    # registered yet", so the plan is create-everything rather than an
    # error -- app-nav is not a dependency of being installable.
    registered_nav = _load_nav_entries(base, roots=object_roots) or {}
    nav = [
        _nav_change(entry, existing=registered_nav, package_id=package_id, warnings=warnings)
        for entry in package["nav"]
    ]
    # Attention sources compare the same way nav entries do, and against
    # the same missing-collection posture: app-nav absent reads as
    # "nothing declared yet" rather than an error, because a package must
    # not fail to install because the home screen is not installed.
    registered_attention = _load_attention_sources(base, roots=object_roots) or {}
    attention = [
        _attention_change(
            entry,
            existing=registered_attention,
            package_id=package_id,
            installable_object_ids=installable_object_ids,
            warnings=warnings,
        )
        for entry in package["attention"]
    ]

    return {
        "package": _package_summary(package),
        "mode": "dry_run",
        "install_enabled": False,
        "safe_to_install": not warnings,
        # WHERE, resolved and absolute. Every entry below reports a
        # `destination` relative to this, and a status of "written" means
        # nothing at all without knowing which tree it was written into --
        # an install can succeed completely into a directory the running
        # server does not read.
        "data_dir": str(base.resolve()),
        "objects": objects,
        "schemas": schemas,
        "permissions": permissions,
        "seed": seed,
        "migrations": migrations,
        "schedules": schedules,
        "nav": nav,
        "attention": attention,
        "warnings": warnings,
    }


def install_package(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str | None = None,
    object_roots: Iterable[Path] | None = None,
    allow_replace: bool = False,
    force: bool = False,
    before_write: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Install a package using the conservative public write contract.

    On an upgrade (an object/schema that already exists), writes are no
    longer blind: each artifact is three-way compared against its recorded
    baseline (see object_package_baselines) and its live content. Pristine
    artifacts fast-forward, customized-but-unchanged artifacts are kept, and
    genuine conflicts are parked as pending-reconcile records (never
    overwritten) unless force=True. See docs/upgrade-and-customization.md
    (Rule 1: Reconcile, Don't Replace).
    """
    # force implies allow_replace: forcing only makes sense when you may
    # touch existing artifacts, so the replace-without-allow_replace blocker
    # still applies unless force is also set.
    allow_replace = allow_replace or force
    roots = list(object_roots) if object_roots is not None else get_object_roots()
    if not roots:
        raise PackageInstallError("Package installs require at least one object root")

    package_dir = _package_dir(package_id, root)
    package = _load_package(package_id, package_dir)
    base = resolve_base_dir(base_dir)
    plan = dry_run_package(package_id, root=root, base_dir=base, object_roots=roots)

    blockers = _install_blockers(plan, package=package, allow_replace=allow_replace)
    if blockers:
        raise PackageInstallError("; ".join(blockers))

    object_writes = []
    for entry, planned in zip(package["objects"], plan["objects"], strict=True):
        source = _package_file(package_dir, entry["path"])
        existing = resolve_object_id(entry["id"], roots)
        # An object that already exists stays where it lives -- a customized
        # object must not be yanked out of the override root by an upgrade.
        #
        # A NEW object goes to the LAST root, which is the base/system root:
        # get_object_roots() returns [override, base] in LOOKUP order, so
        # roots[0] is the override root, and installing there was silently
        # wrong. object_namespace.get_base_object_roots says so directly --
        # "the root packages install into and reconcile against ... the
        # pristine, upgradeable copy" -- but this line used roots[0] and
        # contradicted it. The consequence was not cosmetic: iter_object_sources
        # labels anything under the override root kind="override", and
        # object_handlers.build_index indexes ONLY kind=="system" (Decision 2,
        # user-authored handlers deferred). So every package-installed object
        # declaring HANDLES installed successfully, reported "written", and
        # then never received a single event.
        #
        # With one root configured (no overrides) this is the same root it
        # always was.
        destination_root = (_root_for_path(existing, roots) if existing is not None
                            else roots[-1])
        if destination_root is None:
            raise PackageInstallError(f"Existing object is outside configured object roots: {entry['id']}")
        destination = existing or _object_destination(entry, destination_root)
        _ensure_inside(destination, destination_root, label="object")
        try:
            mapped_id = object_id_from_path(destination, destination_root)
        except ValueError as exc:
            raise PackageInstallError(
                f"Package object path is not a valid object destination: {entry['path']}"
            ) from exc
        if mapped_id != entry["id"]:
            raise PackageInstallError(
                f"Package object path does not map to object id {entry['id']}: {entry['path']}"
            )
        object_writes.append((planned, destination, destination_root, source.read_bytes()))

    schema_writes = []
    for entry, planned in zip(package["schemas"], plan["schemas"], strict=True):
        source = _package_file(package_dir, entry["path"])
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PackageInstallError(f"Package schema contains invalid JSON: {entry['path']}") from exc
        try:
            normalized = object_schemas.normalize_schema(entry["collection"], payload, source="manual")
        except ValueError as exc:
            raise PackageInstallError(f"Package schema is invalid: {entry['path']}") from exc
        schema_writes.append((entry, planned, normalized))

    seed_writes = []
    for entry, planned in zip(package["seed"], plan["seed"], strict=True):
        source = _package_file(package_dir, entry["path"])
        destination = base / "collections" / entry["collection"] / "records.tsv"
        _ensure_inside(destination, base / "collections", label="seed")
        seed_writes.append((entry, planned, destination, source.read_bytes()))

    permission_writes = []
    for entry, planned in zip(package["permissions"], plan["permissions"], strict=True):
        rules = _load_permission_rules(package_dir, entry, package_id=package_id)
        permission_writes.append((planned, rules))

    restore_point = before_write(plan) if before_write is not None else None

    existing_baseline = object_package_baselines.load_baseline(package_id, base_dir=base) or {}
    base_objects = existing_baseline.get("objects") or {}
    base_schemas = existing_baseline.get("schemas") or {}
    base_schema_bodies = existing_baseline.get("schema_bodies") or {}
    baseline_version = existing_baseline.get("version")

    reconciles: list[str] = []
    new_baseline_objects: dict[str, str] = {}
    new_baseline_schemas: dict[str, str] = {}
    new_baseline_schema_bodies: dict[str, Any] = {}

    installed_objects = []
    for planned, destination, destination_root, content in object_writes:
        object_id = planned["id"]
        extra: dict[str, Any] = {}

        if planned["action"] == "create":
            _write_file_atomic_bytes(destination, content)
            status = "written"
            new_baseline_objects[object_id] = object_package_baselines.sha256_text(content.decode("utf-8"))
        else:
            new_text = content.decode("utf-8")
            new_sha = object_package_baselines.sha256_text(new_text)
            live_text = object_source.get_object_source(object_id, roots)
            live_sha = object_package_baselines.sha256_text(live_text)
            old_sha = base_objects.get(object_id)

            if live_sha == new_sha:
                status = "unchanged"
                new_baseline_objects[object_id] = new_sha
            elif old_sha is not None and live_sha == old_sha:
                _write_file_atomic_bytes(destination, content)
                status = "updated"
                new_baseline_objects[object_id] = new_sha
            elif old_sha is not None and new_sha == old_sha:
                status = "kept"
                new_baseline_objects[object_id] = old_sha
            elif force:
                _write_file_atomic_bytes(destination, content)
                status = "forced"
                new_baseline_objects[object_id] = new_sha
            else:
                rec = object_reconciles.create_reconcile(
                    package=package_id,
                    target_version=package["version"],
                    baseline_version=baseline_version,
                    artifact={"kind": "object", "id": object_id},
                    mine=live_text,
                    theirs=new_text,
                    base_sha=old_sha,
                    base_dir=base,
                )
                reconciles.append(rec["id"])
                status = "conflict"
                extra["reconcile_id"] = rec["id"]
                new_baseline_objects[object_id] = old_sha if old_sha is not None else live_sha

        installed_objects.append(
            {
                **planned,
                "status": status,
                "destination": _relative_display_path(destination, destination_root),
                **extra,
            }
        )

    installed_schemas = []
    for entry, planned, normalized in schema_writes:
        collection = entry["collection"]
        new_sha = object_package_baselines.canonical_schema_hash(normalized)
        extra: dict[str, Any] = {}

        live = None
        if planned["action"] == "replace":
            try:
                live = object_schemas.get_schema(collection, base_dir=base, roots=roots)
            except object_schemas.SchemaNotFoundError:
                live = None

        if live is None:
            object_schemas.replace_schema(collection, normalized, base_dir=base)
            status = "written"
            new_baseline_schemas[collection] = new_sha
            new_baseline_schema_bodies[collection] = normalized
        else:
            live_sha = object_package_baselines.canonical_schema_hash(live)
            old_sha = base_schemas.get(collection)
            base_body = base_schema_bodies.get(collection)

            if live_sha == new_sha:
                status = "unchanged"
                new_baseline_schemas[collection] = new_sha
                new_baseline_schema_bodies[collection] = normalized
            elif old_sha is not None and live_sha == old_sha:
                object_schemas.replace_schema(collection, normalized, base_dir=base)
                status = "updated"
                new_baseline_schemas[collection] = new_sha
                new_baseline_schema_bodies[collection] = normalized
            elif old_sha is not None and new_sha == old_sha:
                status = "kept"
                new_baseline_schemas[collection] = old_sha
                new_baseline_schema_bodies[collection] = normalized
            elif force:
                object_schemas.replace_schema(collection, normalized, base_dir=base)
                status = "forced"
                new_baseline_schemas[collection] = new_sha
                new_baseline_schema_bodies[collection] = normalized
            elif base_body is not None:
                # Customized AND shipped changed: try a field-union merge
                # before parking a conflict (Rule 3: schemas are additive,
                # so a same-named field changed on both sides is the only
                # real conflict -- see docs/upgrade-and-customization.md).
                merged, collisions = object_schemas.merge_schema_fields(base_body, live, normalized)
                if not collisions:
                    object_schemas.replace_schema(collection, merged, base_dir=base)
                    status = "merged"
                    new_baseline_schemas[collection] = new_sha
                    new_baseline_schema_bodies[collection] = normalized
                else:
                    rec = object_reconciles.create_reconcile(
                        package=package_id,
                        target_version=package["version"],
                        baseline_version=baseline_version,
                        artifact={"kind": "schema", "collection": collection},
                        mine=json.dumps(live, indent=2, sort_keys=True),
                        theirs=json.dumps(normalized, indent=2, sort_keys=True),
                        base_sha=old_sha,
                        base_dir=base,
                        collisions=collisions,
                    )
                    reconciles.append(rec["id"])
                    status = "conflict"
                    extra["reconcile_id"] = rec["id"]
                    extra["collisions"] = collisions
                    new_baseline_schemas[collection] = old_sha if old_sha is not None else live_sha
                    new_baseline_schema_bodies[collection] = base_body
            else:
                # No base schema body on record (baseline predates this
                # feature, or was never stamped): fall back to parking a
                # conflict, unchanged from prior behavior.
                rec = object_reconciles.create_reconcile(
                    package=package_id,
                    target_version=package["version"],
                    baseline_version=baseline_version,
                    artifact={"kind": "schema", "collection": collection},
                    mine=json.dumps(live, indent=2, sort_keys=True),
                    theirs=json.dumps(normalized, indent=2, sort_keys=True),
                    base_sha=old_sha,
                    base_dir=base,
                )
                reconciles.append(rec["id"])
                status = "conflict"
                extra["reconcile_id"] = rec["id"]
                new_baseline_schemas[collection] = old_sha if old_sha is not None else live_sha
                if base_body is not None:
                    new_baseline_schema_bodies[collection] = base_body

        installed_schemas.append(
            {
                **planned,
                "status": status,
                "destination": f"schemas/{collection}.json",
                **extra,
            }
        )

    installed_seed = []
    for entry, planned, destination, content in seed_writes:
        # Seed into a collection that already holds records: MERGE by id rather
        # than skip. Add only the seed rows whose id isn't already present;
        # existing rows (live data, possibly customized) are never touched. This
        # is what lets several packages each seed a SHARED collection
        # (views/site_routes) -- previously only the first-installed package's
        # seed landed and every later one was skipped (needing manual inserts).
        # A fresh collection (no records yet) still takes the whole seed file.
        if planned.get("installed"):
            added = _merge_seed_rows(
                entry["collection"], content, base_dir=base, package_id=package_id
            )
            installed_seed.append(
                {
                    **planned,
                    "status": "merged" if added else "skipped",
                    "reason": (
                        f"merged {added} new row(s); existing preserved"
                        if added else "collection already has these rows; preserved"
                    ),
                    "added": added,
                    "destination": f"collections/{entry['collection']}/records.tsv",
                }
            )
            continue
        _write_file_atomic_bytes(destination, content)
        _attribute_seed_records(entry["collection"], package_id=package_id, base_dir=base)
        installed_seed.append(
            {
                **planned,
                "status": "written",
                "destination": f"collections/{entry['collection']}/records.tsv",
            }
        )

    installed_permissions = []
    for planned, rules in permission_writes:
        total, added = _merge_permission_rules(rules, base_dir=base)
        installed_permissions.append(
            {
                **planned,
                "status": "merged",
                "rules": total,
                "new_rules": added,
            }
        )

    # Schedules land last: a task board that points at an object is only
    # honest once that object is on disk.
    installed_schedules = _apply_schedules(
        package["schedules"], plan["schedules"], base_dir=base
    )

    # Nav lands after the seeds, because a package that ships the
    # nav_entries schema itself (app-nav) has to have written it before
    # its own doors can be registered.
    installed_nav = _apply_nav(
        package["nav"], plan["nav"], package_id=package_id, base_dir=base, roots=roots
    )

    # Attention sources land last of all, for the same reason schedules
    # do: a counter that names an object is only honest once that object
    # is on disk, and the row is what the daemon will start executing.
    installed_attention = _apply_attention(
        package["attention"], plan["attention"], package_id=package_id,
        base_dir=base, roots=roots
    )

    object_package_baselines.record_baseline(
        package_id,
        version=package["version"],
        objects=new_baseline_objects,
        schemas=new_baseline_schemas,
        schema_bodies=new_baseline_schema_bodies,
        base_dir=base,
    )

    result = {
        "package": plan["package"],
        "mode": "install",
        "install_enabled": True,
        "allow_replace": allow_replace,
        "safe_to_install": True,
        # The resolved data directory everything below landed in. Reported
        # because "written" is not a claim anybody can check without it --
        # see resolve_base_dir for the afternoon that made this necessary.
        "data_dir": plan["data_dir"],
        "objects": installed_objects,
        "schemas": installed_schemas,
        "permissions": installed_permissions,
        "seed": installed_seed,
        "migrations": plan["migrations"],
        "schedules": installed_schedules,
        "nav": installed_nav,
        "attention": installed_attention,
        "reconciles": reconciles,
        "warnings": [],
    }
    if restore_point is not None:
        result["restore_point"] = dict(restore_point)
    return result


def package_status(
    package_id: str,
    *,
    root: Path | str = PACKAGES_DIR,
    base_dir: Path | str = DEFAULT_DATA_DIR,
    object_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Return install/customization status for a package's baselined artifacts."""
    roots = list(object_roots) if object_roots is not None else get_object_roots()
    base = Path(base_dir)

    package = _load_package(package_id, _package_dir(package_id, root))
    summary = _package_summary(package)
    summary["pending_reconciles"] = object_reconciles.count_pending(base_dir=base, package=package_id)

    baseline = object_package_baselines.load_baseline(package_id, base_dir=base)
    if baseline is None:
        summary["installed"] = False
        summary["customized"] = False
        summary["artifacts"] = []
        return summary

    artifacts: list[dict[str, Any]] = []
    any_customized = False

    for object_id, base_sha in (baseline.get("objects") or {}).items():
        try:
            live = object_source.get_object_source(object_id, roots)
        except (FileNotFoundError, LookupError, OSError, ValueError, object_source.ObjectSourceError):
            state = "removed"
        else:
            state = "pristine" if object_package_baselines.sha256_text(live) == base_sha else "customized"
        if state == "customized":
            any_customized = True
        artifacts.append(
            {
                "kind": "object",
                "id": object_id,
                "state": state,
                "overridden": object_namespace.has_override(object_id),
            }
        )

    for collection, base_sha in (baseline.get("schemas") or {}).items():
        try:
            live = object_schemas.get_schema(collection, base_dir=base, roots=roots)
        except (object_schemas.SchemaNotFoundError, LookupError, OSError, ValueError):
            state = "removed"
        else:
            state = (
                "pristine"
                if object_package_baselines.canonical_schema_hash(live) == base_sha
                else "customized"
            )
        if state == "customized":
            any_customized = True
        artifacts.append({"kind": "schema", "collection": collection, "state": state})

    summary["installed"] = True
    summary["installed_version"] = baseline.get("version")
    summary["customized"] = any_customized
    summary["artifacts"] = artifacts
    return summary


def _package_dir(package_id: str, root: Path | str) -> Path:
    if not validate_package_id(package_id):
        raise InvalidPackageIdError(f"Invalid package id: {package_id}")

    package_dir = Path(root) / package_id
    manifest_path = package_dir / MANIFEST_FILE
    if not package_dir.is_dir() or not manifest_path.is_file():
        raise PackageNotFoundError(f"Package not found: {package_id}")
    return package_dir


def _load_package(package_id: str, package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / MANIFEST_FILE
    try:
        payload = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise InvalidPackageManifestError(f"Package manifest contains invalid JSON: {package_id}") from exc

    return _normalize_manifest(package_id, payload)


def _normalize_manifest(package_id: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvalidPackageManifestError(f"Package manifest must contain an object: {package_id}")

    manifest_id = _required_text(payload, "id", package_id=package_id)
    if manifest_id != package_id:
        raise InvalidPackageManifestError(
            f"Package manifest id does not match directory: {package_id}"
        )
    if not validate_package_id(manifest_id):
        raise InvalidPackageIdError(f"Invalid package id: {manifest_id}")

    version = _required_text(payload, "version", package_id=package_id)
    if not _VERSION_RE.fullmatch(version):
        raise InvalidPackageManifestError(f"Invalid package version: {package_id}")

    return {
        "id": manifest_id,
        "name": _required_text(payload, "name", package_id=package_id),
        "version": version,
        "description": _optional_text(payload.get("description")),
        "compatibility": _mapping_field(payload.get("compatibility"), package_id=package_id),
        "dependencies": _normalize_dependencies(payload.get("dependencies", []), package_id=package_id),
        "objects": _normalize_objects(payload.get("objects", []), package_id=package_id),
        "schemas": _normalize_collection_paths(
            payload.get("schemas", []),
            package_id=package_id,
            collection_key="collection",
            section="schemas",
        ),
        "permissions": _normalize_path_entries(
            payload.get("permissions", []),
            package_id=package_id,
            section="permissions",
        ),
        "seed": _normalize_collection_paths(
            payload.get("seed", []),
            package_id=package_id,
            collection_key="collection",
            section="seed",
        ),
        "migrations": _normalize_migrations(payload.get("migrations", []), package_id=package_id),
        "connectors": _normalize_connectors(payload.get("connectors", []), package_id=package_id),
        "schedules": _normalize_schedules(payload.get("schedules", []), package_id=package_id),
        "nav": _normalize_nav(payload.get("nav", []), package_id=package_id),
        "attention": _normalize_attention(payload.get("attention", []), package_id=package_id),
        # The documented opt-out for a package that ships a site_* object
        # which is not a door: a JS widget (site_thread), a fragment
        # (site_materialize_run_button), or a per-record document reached
        # from the record itself (site_packing_slip). Saying so in the
        # manifest is the point -- "this app has no front page" becomes a
        # reviewable claim rather than an omission nobody notices.
        "nav_optional": bool(payload.get("nav_optional", False)),
    }


def _package_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": package["id"],
        "name": package["name"],
        "version": package["version"],
        "description": package.get("description"),
        "status": "available",
        "object_count": len(package["objects"]),
        "schema_count": len(package["schemas"]),
        "permission_count": len(package["permissions"]),
        "seed_count": len(package["seed"]),
        "migration_count": len(package["migrations"]),
        "dependency_count": len(package["dependencies"]),
        "connector_count": len(package.get("connectors", [])),
        "schedule_count": len(package.get("schedules", [])),
        "nav_count": len(package.get("nav", [])),
        "attention_count": len(package.get("attention", [])),
    }


def _normalize_objects(payload: Any, *, package_id: str) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section="objects")
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="objects")
        object_id = _required_text(mapping, "id", package_id=package_id)
        if not validate_object_id(object_id):
            raise InvalidPackageManifestError(f"Invalid package object id: {object_id}")
        normalized.append(
            {
                "id": object_id,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section="objects",
                ),
            }
        )
    return normalized


def _normalize_collection_paths(
    payload: Any,
    *,
    package_id: str,
    collection_key: str,
    section: str,
) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section=section)
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section=section)
        collection = _required_text(mapping, collection_key, package_id=package_id)
        if not object_collections.validate_collection_name(collection):
            raise InvalidPackageManifestError(f"Invalid package collection: {collection}")
        normalized.append(
            {
                collection_key: collection,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section=section,
                ),
            }
        )
    return normalized


def _normalize_path_entries(
    payload: Any,
    *,
    package_id: str,
    section: str,
) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section=section)
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section=section)
        normalized.append(
            {
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section=section,
                )
            }
        )
    return normalized


def _normalize_migrations(payload: Any, *, package_id: str) -> list[dict[str, str]]:
    entries = _list_field(payload, package_id=package_id, section="migrations")
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="migrations")
        migration_id = _required_text(mapping, "id", package_id=package_id)
        if not _MIGRATION_ID_RE.fullmatch(migration_id):
            raise InvalidPackageManifestError(f"Invalid package migration id: {migration_id}")
        normalized.append(
            {
                "id": migration_id,
                "path": _safe_relative_path(
                    _required_text(mapping, "path", package_id=package_id),
                    package_id=package_id,
                    section="migrations",
                ),
            }
        )
    return normalized


def _normalize_connectors(payload: Any, *, package_id: str) -> list[dict[str, str]]:
    """A `connectors` entry declares that a collection is reconciled against an
    external system by a connector module inside this package (see
    object_connectors / plan/vocabulary/03-external-connectors-spec.md):
    `{collection, module, entry?}`. `module` is validated as a package-relative
    path (escape-checked again at load time); `entry` defaults to `reconcile`."""
    entries = _list_field(payload, package_id=package_id, section="connectors")
    normalized = []
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="connectors")
        collection = _required_text(mapping, "collection", package_id=package_id)
        if not object_collections.validate_collection_name(collection):
            raise InvalidPackageManifestError(f"Invalid package connector collection: {collection}")
        module = _safe_relative_path(
            _required_text(mapping, "module", package_id=package_id),
            package_id=package_id,
            section="connectors",
        )
        entry_name = _optional_text(mapping.get("entry")) or "reconcile"
        if not entry_name.isidentifier():
            raise InvalidPackageManifestError(f"Invalid connector entry: {entry_name}")
        normalized.append({"collection": collection, "module": module, "entry": entry_name})
    return normalized


def _normalize_schedules(payload: Any, *, package_id: str) -> list[dict[str, Any]]:
    """A `schedules` entry declares a recurring pass the app needs to work:
    `{id, object_id, schedule, type?, method?, payload?, description?}`.

    Time-driven work belongs to the daemon (docs/logic-decisions.md #2),
    and until now the declaration of WHICH work lived only in a running
    server's state. That made a schedule invisible to review, absent from
    a fresh install, and lost on a rebuild. Declaring it here makes the
    recurring pass part of the package, like the object it calls.

    The id becomes a `task_<id>` state key, so it is restricted to the
    characters a key can safely hold; every other field is validated here
    rather than at 3am inside the daemon, where a bad cron string is a
    pass that quietly does nothing.
    """
    entries = _list_field(payload, package_id=package_id, section="schedules")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="schedules")
        schedule_id = _required_text(mapping, "id", package_id=package_id)
        if not _SCHEDULE_ID_RE.fullmatch(schedule_id):
            raise InvalidPackageManifestError(f"Invalid package schedule id: {schedule_id}")
        if schedule_id in seen:
            raise InvalidPackageManifestError(
                f"Duplicate package schedule id: {schedule_id}"
            )
        seen.add(schedule_id)

        object_id = _required_text(mapping, "object_id", package_id=package_id)
        if not validate_object_id(object_id):
            raise InvalidPackageManifestError(
                f"Invalid package schedule object_id: {object_id}"
            )

        schedule_type = (_optional_text(mapping.get("type")) or "cron").lower()
        if schedule_type not in _SCHEDULE_TYPES:
            raise InvalidPackageManifestError(
                f"Package schedule type must be cron|onetime: {schedule_id}"
            )

        expression = _required_text(mapping, "schedule", package_id=package_id)
        _validate_schedule_expression(expression, schedule_type, schedule_id=schedule_id)

        method = (_optional_text(mapping.get("method")) or "POST").upper()
        if method not in _SCHEDULE_METHODS:
            raise InvalidPackageManifestError(
                f"Invalid package schedule method: {schedule_id}"
            )

        task_payload = mapping.get("payload", {})
        if task_payload in (None, ""):
            task_payload = {}
        if not isinstance(task_payload, Mapping):
            raise InvalidPackageManifestError(
                f"Package schedule payload must be an object: {schedule_id}"
            )

        normalized.append({
            "id": schedule_id,
            "object_id": object_id,
            "method": method,
            "payload": dict(task_payload),
            "schedule": expression,
            "type": schedule_type,
            "description": _optional_text(mapping.get("description")),
        })
    return normalized


def _validate_schedule_expression(
    expression: str,
    schedule_type: str,
    *,
    schedule_id: str,
) -> None:
    """Reject a schedule that could never fire, at install time.

    The daemon treats an unparseable expression as "no next run" and moves
    on in silence, so a typo here is a pass that never runs and never
    complains -- the failure this whole feature exists to end. Cron is
    checked with croniter where it is installed, and structurally where it
    is not, so validation degrades rather than disappearing.
    """
    if "\t" in expression or "\n" in expression:
        raise InvalidPackageManifestError(
            f"Package schedule expression may not contain tabs or newlines: {schedule_id}"
        )
    if schedule_type == "onetime":
        from datetime import datetime

        try:
            datetime.fromisoformat(expression.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidPackageManifestError(
                f"Package onetime schedule must be an ISO timestamp: {schedule_id}"
            ) from exc
        return

    try:
        from croniter import croniter
    except ImportError:
        croniter = None
    if croniter is not None:
        if not croniter.is_valid(expression):
            raise InvalidPackageManifestError(
                f"Invalid package cron expression: {schedule_id}"
            )
        return
    if len(expression.split()) not in (5, 6):
        raise InvalidPackageManifestError(
            f"Package cron expression must have 5 or 6 fields: {schedule_id}"
        )


def _load_scheduler_tasks(base_dir: Path) -> dict[str, dict[str, Any]]:
    """Every task currently on the daemon's board, keyed by schedule id.

    Unreadable rows are skipped rather than raising: one hand-edited task
    must not make a package uninstallable.
    """
    try:
        state = object_state.get_object_state(SCHEDULER_OBJECT_ID, base_dir=base_dir)
    except Exception:
        return {}
    tasks: dict[str, dict[str, Any]] = {}
    for key, value in state.items():
        if not str(key).startswith(_TASK_KEY_PREFIX):
            continue
        try:
            task = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(task, Mapping):
            tasks[str(key)[len(_TASK_KEY_PREFIX):]] = dict(task)
    return tasks


def _schedule_change(
    entry: Mapping[str, Any],
    *,
    existing: Mapping[str, dict[str, Any]],
    installable_object_ids: set[str],
    warnings: list[str],
) -> dict[str, Any]:
    live = existing.get(entry["id"])
    if live is None:
        action = "create"
    elif all(live.get(field) == entry[field]
             for field in ("object_id", "method", "payload", "schedule", "type")) \
            and (live.get("description") or None) == entry["description"]:
        # The description is compared too, so a plan that says "unchanged"
        # is one where nothing at all is about to be written -- a dry run
        # that under-reports is worse than no dry run.
        action = "unchanged"
    else:
        action = "update"

    if entry["object_id"] not in installable_object_ids:
        # A schedule aimed at an object that will not exist is the silent
        # failure this feature exists to end, so it blocks the install
        # rather than landing as a task that can never run.
        warnings.append(
            f"Schedule {entry['id']} targets an object this install will not "
            f"provide: {entry['object_id']}"
        )

    change = {
        "id": entry["id"],
        "object_id": entry["object_id"],
        "schedule": entry["schedule"],
        "type": entry["type"],
        "exists": live is not None,
        "action": action,
    }
    if live is not None:
        change["status"] = live.get("status", "active")
        change["run_count"] = live.get("run_count", 0)
    return change


def _apply_schedules(
    schedules: Iterable[Mapping[str, Any]],
    planned: Iterable[Mapping[str, Any]],
    *,
    base_dir: Path,
) -> list[dict[str, Any]]:
    """Write the package's schedules onto the daemon's task board.

    Two things are deliberately preserved across an install:

    **Run history** -- last_run/run_count/next_run belong to the daemon,
    not the package. Resetting them on an upgrade would make an operator's
    "when did this last work?" unanswerable.

    **A pause** -- if someone paused a task, it stays paused. The package
    declares what SHOULD run; an operator decides what DOES right now, and
    a reinstall that silently restarts a deliberately-stopped nightly pass
    is how an upgrade becomes an incident.

    A changed expression clears next_run so the daemon recomputes it from
    the new one; an unchanged expression keeps the pending firing exactly
    where it was, so reinstalling an app does not skip tonight's run.
    """
    entries = list(schedules)
    plans = list(planned)
    if not entries:
        return []

    manager = object_state.ObjectStateManager(SCHEDULER_OBJECT_ID, base_dir=base_dir)
    applied = []
    for entry, plan in zip(entries, plans, strict=True):
        key = f"{_TASK_KEY_PREFIX}{entry['id']}"
        raw = manager.get(key)
        live: dict[str, Any] = {}
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                live = dict(parsed)

        task = {
            "id": entry["id"],
            "object_id": entry["object_id"],
            "method": entry["method"],
            "payload": entry["payload"],
            "schedule": entry["schedule"],
            "type": entry["type"],
            "status": live.get("status", "active"),
        }
        if entry.get("description"):
            task["description"] = entry["description"]
        for field in _SCHEDULE_RUNTIME_FIELDS:
            if field in live:
                task[field] = live[field]
        if live and live.get("schedule") != entry["schedule"]:
            task["next_run"] = None

        if live and json.dumps(live, sort_keys=True) == json.dumps(task, sort_keys=True):
            status = "unchanged"
        else:
            manager.set(key, json.dumps(task))
            status = "updated" if live else "written"

        applied.append({**plan, "status": status, "task_status": task["status"],
                        "destination": f"state/{SCHEDULER_OBJECT_ID}/{key}"})
    return applied


def _normalize_nav(payload: Any, *, package_id: str) -> list[dict[str, Any]]:
    """A `nav` entry declares a door the app puts on the site:
    `{id, label, path, blurb?, surface?, group?, order?}`.

    Navigation used to be a hand-maintained list, and there were three of
    them -- the app switcher's JS array, the home page's tile list, and
    the search result URL map -- each edited by whoever remembered. They
    disagreed by four entries and between them knew about none of the
    eight newest apps, so the front door of the server advertised a
    server that no longer existed. A menu built by hand rots the moment
    somebody ships without editing it; a menu FOLDED over what every
    installed package declares cannot.

    The id becomes the `nav_entries` record id, so it is restricted to
    the characters a record id can safely hold. `path` is validated for
    shape only -- whether anything answers it is a routing question this
    module deliberately does not pretend to answer (see `_nav_change`).
    """
    entries = _list_field(payload, package_id=package_id, section="nav")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="nav")
        nav_id = _required_text(mapping, "id", package_id=package_id)
        if not _NAV_ID_RE.fullmatch(nav_id):
            raise InvalidPackageManifestError(f"Invalid package nav id: {nav_id}")
        if nav_id in seen:
            raise InvalidPackageManifestError(f"Duplicate package nav id: {nav_id}")
        seen.add(nav_id)

        label = _required_text(mapping, "label", package_id=package_id)
        path = _required_text(mapping, "path", package_id=package_id)
        if not path.startswith("/"):
            raise InvalidPackageManifestError(
                f"Package nav path must start with '/': {nav_id}"
            )
        if any(character.isspace() for character in path):
            # A path with a space or a newline in it is a link that never
            # resolves and a TSV cell that can break the row it lives in.
            raise InvalidPackageManifestError(
                f"Package nav path may not contain whitespace: {nav_id}"
            )

        surface = (_optional_text(mapping.get("surface")) or "member").lower()
        if surface not in _NAV_SURFACES:
            raise InvalidPackageManifestError(
                f"Package nav surface must be public|member|operator|hidden: {nav_id}"
            )

        order = mapping.get("order", _NAV_DEFAULT_ORDER)
        if order in (None, ""):
            order = _NAV_DEFAULT_ORDER
        if isinstance(order, bool) or not isinstance(order, int):
            raise InvalidPackageManifestError(
                f"Package nav order must be an integer: {nav_id}"
            )

        normalized.append({
            "id": nav_id,
            "label": label,
            "path": path,
            "blurb": _optional_text(mapping.get("blurb")) or "",
            "group": _optional_text(mapping.get("group")) or _NAV_DEFAULT_GROUP,
            "surface": surface,
            "order": order,
        })
    return normalized


def _load_nav_entries(
    base_dir: Path,
    *,
    roots: Iterable[Path] | None = None,
) -> dict[str, dict[str, str]] | None:
    """Every nav entry currently registered, keyed by record id.

    ``None`` means the `nav_entries` collection is not installed at all
    (app-nav absent). That is not an error: a package must never fail to
    install because the navigation app is missing, any more than an app
    should fail because nobody has opened the menu yet.
    """
    try:
        rows = object_records.read_collection_records(
            NAV_COLLECTION, base_dir=base_dir, roots=roots)
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError):
        return None
    except (OSError, ValueError):
        # A hand-edited row must not make a package uninstallable, the
        # same posture _load_scheduler_tasks takes.
        return {}
    return {str(row.get("id") or ""): dict(row) for row in rows if row.get("id")}


def _nav_change(
    entry: Mapping[str, Any],
    *,
    existing: Mapping[str, Mapping[str, str]],
    package_id: str,
    warnings: list[str],
) -> dict[str, Any]:
    live = existing.get(entry["id"])
    if live is None:
        action = "create"
    elif all(str(live.get(field) or "") == str(entry[field]) for field in _NAV_PACKAGE_FIELDS):
        # Every package-owned field is compared, blurb and order
        # included, so a plan that says "unchanged" is one where nothing
        # at all is about to be written -- a dry run that under-reports
        # is worse than no dry run.
        action = "unchanged"
    else:
        action = "update"

    # Whether a path is actually SERVED is not knowable here: routing has
    # three sources (convention, site_routes records, views records) and
    # two of them are data this install may be about to seed. Guessing
    # would produce false blockers, so the only thing refused is the one
    # collision that is unambiguous and destructive: two packages fighting
    # over one door, where whichever installs last silently wins.
    owner = str((live or {}).get("package") or "")
    if owner and owner != package_id:
        warnings.append(
            f"Nav entry {entry['id']} is already registered by another package: {owner}"
        )
    for other_id, other in existing.items():
        if other_id == entry["id"]:
            continue
        other_owner = str(other.get("package") or "")
        if other_owner and other_owner != package_id and other.get("path") == entry["path"]:
            warnings.append(
                f"Nav entry {entry['id']} claims a path already registered by "
                f"{other_owner}: {entry['path']}"
            )

    return {
        "id": entry["id"],
        "label": entry["label"],
        "path": entry["path"],
        "group": entry["group"],
        "surface": entry["surface"],
        "exists": live is not None,
        "action": action,
    }


def _apply_nav(
    nav: Iterable[Mapping[str, Any]],
    planned: Iterable[Mapping[str, Any]],
    *,
    package_id: str,
    base_dir: Path,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Register the package's doors in the `nav_entries` collection.

    Two things are deliberately preserved across an install:

    **The operator's `hidden`** -- `operator_hidden` is the one column an
    install never writes on an existing row. The package declares what
    the app IS; the operator decides what they want to SEE, and an
    upgrade that silently un-hides a page somebody deliberately took off
    the menu is the same class of incident as one that restarts a paused
    nightly pass.

    **Somebody else's row** -- entries this package did not register are
    not touched and not deleted. Removing a `nav` entry from a manifest
    leaves the row alone, matching the rest of install: deregistering
    never destroys.

    A missing `nav_entries` collection is reported and skipped rather
    than raised. The nav app is not a dependency of being installable.
    """
    entries = list(nav)
    plans = list(planned)
    if not entries:
        return []

    existing = _load_nav_entries(base_dir, roots=roots)
    if existing is None:
        return [
            {**plan, "status": "skipped",
             "reason": f"{NAV_COLLECTION} collection is not installed"}
            for plan in plans
        ]

    applied = []
    for entry, plan in zip(entries, plans, strict=True):
        live = existing.get(entry["id"])
        row = {
            "package": package_id,
            "label": entry["label"],
            "path": entry["path"],
            "blurb": entry["blurb"],
            "group": entry["group"],
            "surface": entry["surface"],
            "order": str(entry["order"]),
        }
        if live is None:
            object_records.create_collection_record(
                NAV_COLLECTION,
                {"id": entry["id"], "operator_hidden": "false", **row},
                base_dir=base_dir, roots=roots, actor=f"package:{package_id}",
            )
            status = "written"
        elif all(str(live.get(field) or "") == row[field] for field in row):
            status = "unchanged"
        else:
            # `row` carries no operator_hidden key at all, so the update
            # cannot reach that column even by accident.
            object_records.update_collection_record(
                NAV_COLLECTION, entry["id"], row,
                base_dir=base_dir, roots=roots, actor=f"package:{package_id}",
            )
            status = "updated"

        applied.append({**plan, "status": status,
                        "destination": f"collections/{NAV_COLLECTION}/{entry['id']}"})
    return applied


def _normalize_attention(payload: Any, *, package_id: str) -> list[dict[str, Any]]:
    """An `attention` entry declares one queue of things that need a human:
    `{id, object_id, label, path, nav_id?, group?, severity?}`.

    Every gate and derived state on this server ends in a pile somebody
    has to look at -- receipts waiting to be confirmed into expenses, time
    entries waiting for an approver, invoices past their due date, bank
    lines nothing matched. The system computes those piles constantly and
    then discards them: `system_scan_processor` returns a field called
    `needing_a_human` that no surface has ever read. What was missing was
    not the arithmetic, it was a place to declare that the arithmetic
    MEANS something -- and the only honest place for that definition is
    the package that owns the domain, because nobody else knows that
    `extracted` is a waiting room and `confirmed` is not.

    `object_id` names the provider, an object exposing `COUNT(request) ->
    {"count": int, "detail"?: str}`. `path` is validated for shape only,
    exactly as `nav` paths are: whether anything answers it is a routing
    question this module deliberately does not pretend to answer.
    `nav_id` is the door this count decorates, so the app list can read
    "Invoices - 5 overdue" without a second surface to maintain; it is
    optional because a queue can belong to a package that ships no page
    at all (app-intake is one).
    """
    entries = _list_field(payload, package_id=package_id, section="attention")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        mapping = _entry_mapping(entry, package_id=package_id, section="attention")
        attention_id = _required_text(mapping, "id", package_id=package_id)
        if not _ATTENTION_ID_RE.fullmatch(attention_id):
            raise InvalidPackageManifestError(
                f"Invalid package attention id: {attention_id}")
        if attention_id in seen:
            raise InvalidPackageManifestError(
                f"Duplicate package attention id: {attention_id}")
        seen.add(attention_id)

        object_id = _required_text(mapping, "object_id", package_id=package_id)
        if not validate_object_id(object_id):
            raise InvalidPackageManifestError(
                f"Invalid package attention object_id: {object_id}")

        label = _required_text(mapping, "label", package_id=package_id)
        path = _required_text(mapping, "path", package_id=package_id)
        if not path.startswith("/"):
            raise InvalidPackageManifestError(
                f"Package attention path must start with '/': {attention_id}"
            )
        if any(character.isspace() for character in path):
            # A count nobody can click is a number on a poster. A path
            # with a space or a newline in it is both a link that never
            # resolves and a TSV cell that can break the row it lives in.
            raise InvalidPackageManifestError(
                f"Package attention path may not contain whitespace: {attention_id}"
            )

        nav_id = _optional_text(mapping.get("nav_id")) or ""
        if nav_id and not _NAV_ID_RE.fullmatch(nav_id):
            raise InvalidPackageManifestError(
                f"Invalid package attention nav_id: {attention_id}")

        severity = (_optional_text(mapping.get("severity"))
                    or _ATTENTION_DEFAULT_SEVERITY).lower()
        if severity not in _ATTENTION_SEVERITIES:
            raise InvalidPackageManifestError(
                f"Package attention severity must be normal|warning|urgent: {attention_id}"
            )

        normalized.append({
            "id": attention_id,
            "object_id": object_id,
            "label": label,
            "path": path,
            "nav_id": nav_id,
            "group": _optional_text(mapping.get("group")) or _ATTENTION_DEFAULT_GROUP,
            "severity": severity,
        })
    return normalized


def _load_attention_sources(
    base_dir: Path,
    *,
    roots: Iterable[Path] | None = None,
) -> dict[str, dict[str, str]] | None:
    """Every attention source currently registered, keyed by record id.

    ``None`` means the `attention_sources` collection is not installed at
    all (app-nav absent). Same posture as `_load_nav_entries`: a package
    must never fail to install because the home screen is missing, any
    more than an app should fail because nobody has looked at it yet.
    """
    try:
        rows = object_records.read_collection_records(
            ATTENTION_COLLECTION, base_dir=base_dir, roots=roots)
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError):
        return None
    except (OSError, ValueError):
        # A hand-edited row must not make a package uninstallable, the
        # same posture _load_nav_entries takes.
        return {}
    return {str(row.get("id") or ""): dict(row) for row in rows if row.get("id")}


def _attention_change(
    entry: Mapping[str, Any],
    *,
    existing: Mapping[str, Mapping[str, str]],
    package_id: str,
    installable_object_ids: set[str],
    warnings: list[str],
) -> dict[str, Any]:
    live = existing.get(entry["id"])
    if live is None:
        action = "create"
    elif all(str(live.get(field) or "") == str(entry[field])
             for field in _ATTENTION_PACKAGE_FIELDS):
        # Every package-owned field is compared, nav_id and severity
        # included, so a plan that says "unchanged" is one where nothing
        # at all is about to be written.
        action = "unchanged"
    else:
        action = "update"

    if entry["object_id"] not in installable_object_ids:
        # A counter aimed at an object that will not exist reads as zero
        # forever, and a queue that reads zero forever is indistinguishable
        # from a queue that is empty. That silent failure is the entire
        # reason this section exists, so it blocks the install.
        warnings.append(
            f"Attention source {entry['id']} targets an object this install "
            f"will not provide: {entry['object_id']}"
        )

    # Two packages writing one attention_sources row is two definitions of
    # what needs a human, where whichever installed last silently wins.
    # Unlike nav there is no path-collision rule: two queues legitimately
    # point at one list (an orders page can be both "to pick" and "to
    # invoice"), and blocking that would be a false blocker.
    owner = str((live or {}).get("package") or "")
    if owner and owner != package_id:
        warnings.append(
            f"Attention source {entry['id']} is already registered by another "
            f"package: {owner}"
        )

    return {
        "id": entry["id"],
        "object_id": entry["object_id"],
        "label": entry["label"],
        "path": entry["path"],
        "nav_id": entry["nav_id"],
        "group": entry["group"],
        "severity": entry["severity"],
        "exists": live is not None,
        "action": action,
    }


def _apply_attention(
    attention: Iterable[Mapping[str, Any]],
    planned: Iterable[Mapping[str, Any]],
    *,
    package_id: str,
    base_dir: Path,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, Any]]:
    """Register the package's queues in the `attention_sources` collection.

    Two things are deliberately preserved across an install:

    **The operator's `operator_muted`** -- the one column an install never
    writes on an existing row. The package declares what a queue MEANS;
    the operator decides whether this deployment wants to be told about
    it, and an upgrade that silently un-mutes a counter somebody turned
    off is the same class of incident as one that restarts a paused
    nightly pass.

    **Somebody else's row** -- sources this package did not register are
    not touched and not deleted. Removing an `attention` entry from a
    manifest leaves the row alone, matching the rest of install:
    deregistering never destroys. (The rollup pass is what stops feeding
    a stale row, because it only executes providers it can still resolve.)

    A missing `attention_sources` collection is reported and skipped
    rather than raised. The home screen is not a dependency of being
    installable.
    """
    entries = list(attention)
    plans = list(planned)
    if not entries:
        return []

    existing = _load_attention_sources(base_dir, roots=roots)
    if existing is None:
        return [
            {**plan, "status": "skipped",
             "reason": f"{ATTENTION_COLLECTION} collection is not installed"}
            for plan in plans
        ]

    applied = []
    for entry, plan in zip(entries, plans, strict=True):
        live = existing.get(entry["id"])
        row = {
            "package": package_id,
            "object_id": entry["object_id"],
            "label": entry["label"],
            "path": entry["path"],
            "nav_id": entry["nav_id"],
            "group": entry["group"],
            "severity": entry["severity"],
        }
        if live is None:
            object_records.create_collection_record(
                ATTENTION_COLLECTION,
                {"id": entry["id"], "operator_muted": "false", **row},
                base_dir=base_dir, roots=roots, actor=f"package:{package_id}",
            )
            status = "written"
        elif all(str(live.get(field) or "") == row[field] for field in row):
            status = "unchanged"
        else:
            # `row` carries no operator_muted key at all, so the update
            # cannot reach that column even by accident.
            object_records.update_collection_record(
                ATTENTION_COLLECTION, entry["id"], row,
                base_dir=base_dir, roots=roots, actor=f"package:{package_id}",
            )
            status = "updated"

        applied.append({**plan, "status": status,
                        "destination": f"collections/{ATTENTION_COLLECTION}/{entry['id']}"})
    return applied


def iter_connectors(*, root: Path | str = PACKAGES_DIR) -> list[dict[str, str]]:
    """Every connector declaration under a package root, each with its module
    resolved to an absolute path proven to live INSIDE its package. Robust to a
    single bad package: a manifest that won't parse, or a module that is missing
    or escapes the package dir, is skipped (never raises) so one bad package
    can't blind the whole reconcile pass."""
    packages_root = Path(root)
    if not packages_root.is_dir():
        return []
    out: list[dict[str, str]] = []
    for path in sorted(packages_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not validate_package_id(path.name):
            continue
        if not (path / MANIFEST_FILE).is_file():
            continue
        try:
            package = _load_package(path.name, path)
        except (InvalidPackageManifestError, InvalidPackageIdError, OSError, ValueError):
            continue
        for decl in package.get("connectors", []):
            status = _package_file_status(path, decl["module"])
            if not status["exists"]:  # missing module, or escapes the package
                continue
            out.append({
                "package_id": package["id"],
                "collection": decl["collection"],
                "module": str((path / decl["module"]).resolve()),
                "entry": decl["entry"],
            })
    return out


def _normalize_dependencies(payload: Any, *, package_id: str) -> list[dict[str, str | None]]:
    entries = _list_field(payload, package_id=package_id, section="dependencies")
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            dependency_id = entry
            version = None
        else:
            mapping = _entry_mapping(entry, package_id=package_id, section="dependencies")
            dependency_id = _required_text(mapping, "id", package_id=package_id)
            version = _optional_text(mapping.get("version"))
        if not validate_package_id(dependency_id):
            raise InvalidPackageManifestError(f"Invalid package dependency id: {dependency_id}")
        normalized.append({"id": dependency_id, "version": version})
    return normalized


def _object_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    object_roots: Iterable[Path] | None,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package object file: {entry['path']}")
    installed = resolve_object_id(entry["id"], object_roots) is not None
    return {
        "id": entry["id"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "replace" if installed else "create",
        "installed": installed,
    }


def _schema_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package schema file: {entry['path']}")
    installed = (base_dir / "schemas" / f"{entry['collection']}.json").is_file()
    return {
        "collection": entry["collection"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "replace" if installed else "create",
        "installed": installed,
    }


def _seed_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package seed file: {entry['path']}")
    installed = (base_dir / "collections" / entry["collection"] / "records.tsv").is_file()
    return {
        "collection": entry["collection"],
        "path": entry["path"],
        "exists": file_status["exists"],
        # Seed is install-once: on an upgrade where the collection already has
        # records, seeding is skipped so live data is preserved (not merged).
        "action": "skip" if installed else "create",
        "installed": installed,
    }


def _migration_change(
    entry: Mapping[str, str],
    *,
    package: str,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package migration file: {entry['path']}")
    marker = base_dir / PACKAGE_MIGRATIONS_DIR / package / f"{entry['id']}.json"
    applied = marker.is_file()
    return {
        "id": entry["id"],
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": "skip" if applied else "apply",
        "applied": applied,
    }


def _permission_change(
    entry: Mapping[str, str],
    *,
    package_id: str,
    package_dir: Path,
    base_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    change = {
        "path": entry["path"],
        "action": "merge",
        "exists": False,
        "rules": 0,
        "new_rules": 0,
    }
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package permissions file: {entry['path']}")
        return change
    change["exists"] = True

    try:
        rules = _load_permission_rules(package_dir, entry, package_id=package_id)
    except PackageInstallError as exc:
        warnings.append(str(exc))
        return change

    change["rules"] = len(rules)
    try:
        policy = object_permission_store.load_policy(base_dir)
        existing_keys = {_rule_merge_key(rule) for rule in policy.rules}
    except ValueError:
        existing_keys = set()
    change["new_rules"] = sum(1 for rule in rules if _rule_merge_key(rule) not in existing_keys)
    return change


def _load_permission_rules(
    package_dir: Path,
    entry: Mapping[str, str],
    *,
    package_id: str,
) -> list[object_permissions.PermissionRule]:
    source = _package_file(package_dir, entry["path"])
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageInstallError(
            f"Package permissions file contains invalid JSON: {entry['path']}"
        ) from exc

    rules_payload = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(rules_payload, list):
        raise PackageInstallError(
            f"Package permissions file must contain a rules list: {entry['path']}"
        )

    rules = []
    for rule_payload in rules_payload:
        if not isinstance(rule_payload, dict):
            raise PackageInstallError(
                f"Package permission rules must be objects: {entry['path']}"
            )
        merged_payload = {**rule_payload, "package": package_id}
        try:
            rules.append(object_permissions.rule_from_dict(merged_payload))
        except ValueError as exc:
            raise PackageInstallError(
                f"Package permission rule is invalid in {entry['path']}: {exc}"
            ) from exc
    return rules


def _rule_merge_key(rule: object_permissions.PermissionRule) -> str:
    payload = object_permissions.rule_to_dict(rule)
    payload.pop("reason", None)
    payload.pop("package", None)
    return json.dumps(payload, sort_keys=True)


def _merge_permission_rules(
    rules: list[object_permissions.PermissionRule],
    *,
    base_dir: Path,
) -> tuple[int, int]:
    """Append new package rules to the policy; return (total, newly added)."""
    policy = object_permission_store.load_policy(base_dir)
    existing_keys = {_rule_merge_key(rule) for rule in policy.rules}

    added = []
    for rule in rules:
        if _rule_merge_key(rule) in existing_keys:
            continue
        added.append(rule)
        existing_keys.add(_rule_merge_key(rule))

    if added:
        merged = object_permissions.PermissionPolicy(
            access_mode=policy.access_mode,
            rules=tuple(policy.rules) + tuple(added),
            roles=policy.roles,
            user_roles=policy.user_roles,
            admin_roles=policy.admin_roles,
        )
        object_permission_store.save_policy(merged, base_dir=base_dir)

    return len(rules), len(added)


def _path_change(
    entry: Mapping[str, str],
    *,
    package_dir: Path,
    section: str,
    action: str,
    warnings: list[str],
) -> dict[str, Any]:
    file_status = _package_file_status(package_dir, entry["path"])
    if not file_status["exists"]:
        warnings.append(f"Missing package {section} file: {entry['path']}")
    return {
        "path": entry["path"],
        "exists": file_status["exists"],
        "action": action,
    }


def _package_file_status(package_dir: Path, relative_path: str) -> dict[str, bool]:
    package_root = package_dir.resolve()
    candidate = package_dir / relative_path
    resolved = candidate.resolve(strict=False)
    inside = resolved == package_root or package_root in resolved.parents
    return {
        "exists": inside and candidate.is_file(),
        "inside_package": inside,
    }


def _install_blockers(
    plan: Mapping[str, Any],
    *,
    package: Mapping[str, Any],
    allow_replace: bool,
) -> list[str]:
    blockers = [str(warning) for warning in plan.get("warnings", [])]

    if package["migrations"]:
        blockers.append("Package migration execution is not implemented yet")

    if not allow_replace:
        for entry in plan["objects"]:
            if entry["action"] == "replace":
                blockers.append(f"Object already exists; set allow_replace=true: {entry['id']}")
        for entry in plan["schemas"]:
            if entry["action"] == "replace":
                blockers.append(
                    f"Schema already exists; set allow_replace=true: {entry['collection']}"
                )

    # Existing seed data is NOT a blocker: seed is install-once, so an upgrade
    # of an already-installed package skips seeding and preserves live records
    # (see the seed apply step). This is what makes "upgrade the app, keep the
    # data" work — the package ships code + schema + a seed template, while the
    # records live outside the package in the server's data dir.

    return blockers


def _package_file(package_dir: Path, relative_path: str) -> Path:
    status = _package_file_status(package_dir, relative_path)
    if not status["inside_package"]:
        raise PackageInstallError(f"Package file escapes package directory: {relative_path}")
    if not status["exists"]:
        raise PackageInstallError(f"Package file does not exist: {relative_path}")
    return package_dir / relative_path


def _object_destination(entry: Mapping[str, str], object_root: Path) -> Path:
    relative = Path(entry["path"])
    if relative.parts and relative.parts[0] == "objects":
        relative = Path(*relative.parts[1:])
    if not relative.parts or relative.suffix != ".py":
        raise PackageInstallError(f"Package object path must point to a Python file: {entry['path']}")
    destination = object_root / relative
    _ensure_inside(destination, object_root, label="object")
    return destination


def _ensure_inside(path: Path, root: Path, *, label: str) -> None:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PackageInstallError(f"Package {label} destination escapes its root: {path}") from exc


def _root_for_path(path: Path | None, roots: Iterable[Path]) -> Path | None:
    if path is None:
        return None
    for root in roots:
        try:
            _ensure_inside(path, root, label="object")
        except PackageInstallError:
            continue
        return root
    return None


def _merge_seed_rows(
    collection: str, content: bytes, *, base_dir: Path, package_id: str
) -> int:
    """Add the seed rows whose id isn't already in ``collection``; leave every
    existing row untouched. Returns how many were added. Seed rows carry
    explicit ids and created_at, which ``create_collection_record`` honors
    (require_id + preserve_read_only), so a re-run adds nothing. One bad row is
    skipped, never fails the install."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return 0
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    if not rows:
        return 0
    try:
        existing = {
            r.get("id")
            for r in object_records.read_collection_records(collection, base_dir=base_dir)
        }
    except (object_collections.CollectionNotFoundError,
            object_collections.InvalidCollectionNameError, OSError, ValueError):
        existing = set()
    added = 0
    for row in rows:
        record_id = (row.get("id") or "").strip()
        if not record_id or record_id in existing:
            continue
        try:
            object_records.create_collection_record(
                collection, {k: v for k, v in row.items() if v is not None},
                base_dir=base_dir, actor=f"package:{package_id}", preserve_read_only=True,
            )
            existing.add(record_id)
            added += 1
        except Exception:  # noqa: BLE001 -- one malformed seed row never fails the install
            continue
    return added


def _write_file_atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def _attribute_seed_records(collection: str, *, package_id: str, base_dir: Path) -> None:
    """Emit an attributed "create" change for every row a seed file just wrote.

    Seed writes land the whole records.tsv in one atomic byte-for-byte
    copy (see the seed_writes loop above), bypassing
    object_records.create_collection_record entirely -- so, unlike a
    normal record write, nothing emits a change automatically. This is
    the seed-install side of universal attribution: read the file back
    (install-once, so every row it now holds is a fresh create) and log
    one change per row, attributed to the installing package rather than
    left unattributed.
    """
    try:
        rows = object_records.read_collection_records(collection, base_dir=base_dir)
    except (object_collections.CollectionNotFoundError, object_collections.InvalidCollectionNameError):
        return
    for row in rows:
        object_record_changes.append_record_change(
            collection=collection,
            record_id=row["id"],
            action="create",
            before=None,
            after=row,
            actor=f"package-install:{package_id}",
            base_dir=base_dir,
        )


def _relative_display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.name


def _safe_relative_path(value: str, *, package_id: str, section: str) -> str:
    if "\x00" in value:
        raise InvalidPackageManifestError(f"Package {section} path contains a null byte: {package_id}")
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise InvalidPackageManifestError(f"Package {section} path must be relative: {package_id}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidPackageManifestError(f"Package {section} path is not safe: {package_id}")
    return path.as_posix()


def _required_text(payload: Mapping[str, Any], key: str, *, package_id: str) -> str:
    value = payload.get(key)
    text = _optional_text(value)
    if text is None:
        raise InvalidPackageManifestError(f"Package manifest requires '{key}': {package_id}")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _mapping_field(value: Any, *, package_id: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidPackageManifestError(f"Package compatibility must be an object: {package_id}")
    return dict(value)


def _list_field(value: Any, *, package_id: str, section: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidPackageManifestError(f"Package {section} must be a list: {package_id}")
    return value


def _entry_mapping(value: Any, *, package_id: str, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidPackageManifestError(f"Package {section} entries must be objects: {package_id}")
    return value
