"""Restore points: swept, but only the ones nobody asked for.

Found by looking at a production droplet: **157 restore points at ~91MB
each — 14.3GB against 4.0GB free**, on the box that also serves
dbbasic.com, with 93 of them written in the preceding two days. One is
created automatically before every package install and nothing ever
removed them. Left alone the disk fills in about a day, and a full disk
there takes production down with it.

That is the fourth appearance of one pattern in this project — page_views,
restore points, change logs, restore points again. A log nobody told how
big it may get fails on the worst day rather than an ordinary one.

WHAT MAKES A DEFAULT SAFE HERE, and why this pass differs from the
change-log one (which stays off unless an operator asks): those archives
are not the operator's data. Nobody asked for 157 of them; they are
scaffolding the server writes on its own behalf. A change log, by
contrast, IS the audit trail — so how much of it to keep is not a
decision a daemon makes for somebody.

So the split is by kind, and everything a human asked for is untouchable.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import object_backup_index
import object_daemon


def make_archive(backups, label, *, age_days=0, size=1024):
    """Write a file shaped like a real backup archive.

    Named the way object_backup._backup_filename_timestamp names them, so
    `_entry` classifies the kind from the label exactly as it does in
    production — the classification IS the safety property here.
    """
    stamp = (datetime.now(tz=timezone.utc) - timedelta(days=age_days))
    compact = stamp.isoformat().replace("-", "").replace(":", "").replace(".", "")
    compact = compact.replace("+0000", "") + "Z"
    path = backups / f"{compact}-{label}.tar.gz"
    path.write_bytes(b"x" * size)
    mtime = stamp.timestamp()
    import os
    os.utime(path, (mtime, mtime))
    return path


def backups_of(tmp_path):
    directory = object_backup_index.backups_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# --- what is prunable, and what is never ---------------------------------------

def test_a_manual_backup_is_never_swept_however_old(tmp_path):
    """THE safety property. A person pressed a button; the daemon does not
    get to decide that was a mistake."""
    backups = backups_of(tmp_path)
    make_archive(backups, "manual", age_days=400)
    for n in range(40):
        make_archive(backups, f"package-app-{n}", age_days=n + 10)

    result = object_backup_index.prune_backups(data_dir=tmp_path, keep_last=5)

    assert result["protected"] == 1
    survivors = {entry["id"] for entry in
                 object_backup_index.list_backups(data_dir=tmp_path)}
    assert any(name.endswith("-manual.tar.gz") for name in survivors)


def test_a_named_restore_point_is_never_swept_either(tmp_path):
    """`package-*` is the machine's scaffolding. A labelled point somebody
    chose ('before-migration') is a decision, and decisions survive."""
    backups = backups_of(tmp_path)
    make_archive(backups, "before-the-big-migration", age_days=365)
    for n in range(30):
        make_archive(backups, f"package-app-{n}", age_days=n + 10)

    object_backup_index.prune_backups(data_dir=tmp_path, keep_last=5)

    survivors = {entry["id"] for entry in
                 object_backup_index.list_backups(data_dir=tmp_path)}
    assert any("before-the-big-migration" in name for name in survivors)


def test_the_newest_automatic_points_survive(tmp_path):
    backups = backups_of(tmp_path)
    for n in range(30):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=10, keep_newer_than_days=0)

    assert result["kept"] == 10
    assert result["removed"] == 20
    remaining = object_backup_index.list_backups(data_dir=tmp_path)
    assert len(remaining) == 10
    # Newest-first ordering means the survivors are the RECENT ones.
    assert "package-app-0" in str(remaining[0]["id"])


def test_a_burst_of_installs_cannot_evict_the_mornings_safety_net(tmp_path):
    """The window exists for exactly this: twenty installs in one
    afternoon would otherwise push out every point from before lunch,
    which is when you actually want to roll back to."""
    backups = backups_of(tmp_path)
    for n in range(25):
        make_archive(backups, f"package-burst-{n}", age_days=0)
    make_archive(backups, "package-this-morning", age_days=1)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=10, keep_newer_than_days=7)

    assert result["removed"] == 0            # everything is inside the window
    survivors = {entry["id"] for entry in
                 object_backup_index.list_backups(data_dir=tmp_path)}
    assert any("this-morning" in name for name in survivors)


def test_there_is_a_floor_so_the_safety_net_cannot_be_swept_to_nothing(tmp_path):
    """keep_last=0 must not mean 'delete them all'. Sweeping to the bone
    would remove the very thing this feature exists to provide."""
    backups = backups_of(tmp_path)
    for n in range(20):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=0, keep_newer_than_days=0)

    assert result["kept"] == object_backup_index.MIN_KEEP
    assert result["kept"] >= 5


def test_a_dry_run_reports_without_removing(tmp_path):
    backups = backups_of(tmp_path)
    for n in range(20):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=5, keep_newer_than_days=0, dry_run=True)

    assert result["dry_run"] is True
    assert result["removed"] == 15
    assert result["freed_bytes"] > 0
    assert len(object_backup_index.list_backups(data_dir=tmp_path)) == 20


def test_an_undeletable_archive_is_reported_rather_than_aborting_the_sweep(
        tmp_path, monkeypatch):
    """A partially unwritable backup directory should still be swept as
    far as it can be. Raising mid-loop would leave the disk full AND the
    sweep half-done, with no record of which half."""
    backups = backups_of(tmp_path)
    for n in range(20):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)
    # Unlink permission on POSIX belongs to the DIRECTORY, not the file, so
    # one unremovable archive cannot be built portably. Fail one unlink
    # directly instead -- the property under test is that the loop
    # continues and reports, not how the OS refused.
    victim = sorted(p.name for p in backups.iterdir())[0]
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == victim:
            raise PermissionError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=5, keep_newer_than_days=0)

    assert result["errors"]
    assert victim in result["errors"][0]
    assert result["removed"] == 14        # the other fourteen still went


# --- the daemon pass ------------------------------------------------------------

def test_the_pass_is_on_by_default_unlike_the_change_log_one(tmp_path, monkeypatch):
    """The change-log pass returns None with no policy configured, because
    an audit trail is the operator's. This one runs unasked, because the
    files are the server's own scaffolding."""
    backups = backups_of(tmp_path)
    for n in range(40):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)

    result = object_daemon.process_restore_point_retention(base_dir=tmp_path)
    assert result is not None
    assert result["removed"] > 0


def test_the_pass_respects_its_marker_and_does_not_run_every_tick(tmp_path):
    backups = backups_of(tmp_path)
    for n in range(40):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)

    first = object_daemon.process_restore_point_retention(base_dir=tmp_path)
    second = object_daemon.process_restore_point_retention(base_dir=tmp_path)
    assert first is not None
    assert second is None                     # marker suppresses the re-run


def test_the_env_knobs_are_honoured(tmp_path, monkeypatch):
    backups = backups_of(tmp_path)
    for n in range(40):
        make_archive(backups, f"package-app-{n}", age_days=n + 30)
    monkeypatch.setenv(object_daemon.RESTORE_POINT_KEEP_LAST_ENV, "7")
    monkeypatch.setenv(object_daemon.RESTORE_POINT_KEEP_DAYS_ENV, "0")

    result = object_daemon.process_restore_point_retention(base_dir=tmp_path)
    assert result["kept"] == 7


# --- the budget -----------------------------------------------------------------
#
# The first cut of this policy had only keep_last and an age window. A dry
# run against the real droplet freed 78MB out of 14.3GB: 93 archives had
# been written in two days, so the window protected almost all of them --
# and because the data directory grows, the RECENT archives are the large
# ones (~91MB each against ~1.7MB for the oldest). A policy expressed in
# COUNT and AGE cannot bound a quantity measured in BYTES.

def test_the_budget_outranks_the_age_window(tmp_path):
    """The exact shape that made the first policy useless: everything is
    inside the window, and the window must not be able to blow the disk
    budget to honour it."""
    backups = backups_of(tmp_path)
    for n in range(40):                       # 40 x 1MB, all written today
        make_archive(backups, f"package-burst-{n}", age_days=0, size=1024 * 1024)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=5, keep_newer_than_days=7,
        max_total_bytes=10 * 1024 * 1024)     # room for ~10

    assert result["kept"] <= 11
    assert result["removed"] >= 29
    assert result["kept_bytes"] <= 11 * 1024 * 1024


def test_the_floor_survives_even_a_budget_smaller_than_it(tmp_path):
    """keep_last is unconditional: a budget set below what the newest few
    weigh must not delete them. A rollback point you cannot afford is
    still a rollback point."""
    backups = backups_of(tmp_path)
    for n in range(20):
        make_archive(backups, f"package-app-{n}", age_days=n, size=1024 * 1024)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=8, keep_newer_than_days=0,
        max_total_bytes=1024)                 # 1KB: less than one archive

    assert result["kept"] == 8


def test_a_zero_budget_disables_the_ceiling(tmp_path):
    backups = backups_of(tmp_path)
    for n in range(30):
        make_archive(backups, f"package-app-{n}", age_days=0, size=1024 * 1024)

    result = object_backup_index.prune_backups(
        data_dir=tmp_path, keep_last=5, keep_newer_than_days=7,
        max_total_bytes=0)

    assert result["removed"] == 0             # window keeps everything again
