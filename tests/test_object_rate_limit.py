from concurrent.futures import ThreadPoolExecutor

import object_rate_limit


def test_rate_limit_blocks_after_configured_limit(tmp_path):
    first = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=2,
        window_seconds=60,
        now=100.0,
    )
    second = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=2,
        window_seconds=60,
        now=101.0,
    )
    third = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=2,
        window_seconds=60,
        now=102.0,
    )

    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0
    assert third.allowed is False
    assert third.retry_after == 58


def test_rate_limit_prunes_expired_entries(tmp_path):
    first = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=1,
        window_seconds=10,
        now=100.0,
    )
    second = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=1,
        window_seconds=10,
        now=111.0,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert second.remaining == 0


def test_rate_limit_keeps_identities_separate(tmp_path):
    first = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.10",
        limit=1,
        window_seconds=60,
        now=100.0,
    )
    second = object_rate_limit.check_rate_limit(
        directory=tmp_path,
        identity="ip:203.0.113.11",
        limit=1,
        window_seconds=60,
        now=101.0,
    )

    assert first.allowed is True
    assert second.allowed is True


def test_disabled_rate_limit_does_not_create_storage(tmp_path):
    result = object_rate_limit.check_rate_limit(
        directory=tmp_path / "ratelimit",
        identity="ip:203.0.113.10",
        limit=0,
        window_seconds=60,
        now=100.0,
    )

    assert result.allowed is True
    assert not (tmp_path / "ratelimit").exists()


def test_rate_limit_file_is_safe_under_concurrent_access(tmp_path):
    def check(_):
        return object_rate_limit.check_rate_limit(
            directory=tmp_path,
            identity="ip:203.0.113.10",
            limit=1000,
            window_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(check, range(50)))

    assert all(result.allowed for result in results)
    bucket_files = [
        path
        for path in tmp_path.iterdir()
        if path.name.endswith(".txt") and not path.name.endswith(".lock")
    ]
    assert len(bucket_files) == 1
    timestamps = bucket_files[0].read_text().splitlines()
    assert len(timestamps) == 50
    for timestamp in timestamps:
        float(timestamp)


def test_degraded_fails_open_by_default_but_flags_it(tmp_path):
    """When storage is unusable, the GLOBAL limiter (fail_closed=False, the
    default) must stay up -- a broken ratelimit dir cannot take the whole
    site down -- but the result is flagged `degraded` so the caller can log
    it. It must NOT silently return a normal allow."""
    # A file where the ratelimit directory is expected -> mkdir/open raises
    # OSError inside check_rate_limit.
    blocker = tmp_path / "ratelimit"
    blocker.write_text("not a directory")

    result = object_rate_limit.check_rate_limit(
        directory=blocker, identity="visitor", limit=5, window_seconds=60
    )
    assert result.degraded is True
    assert result.allowed is True  # fail-open default keeps the site up


def test_degraded_fails_closed_when_requested(tmp_path):
    """A public-write surface passes fail_closed=True: when the abuse counter
    is unavailable, deny rather than admit an unmetered flood."""
    blocker = tmp_path / "ratelimit"
    blocker.write_text("not a directory")

    result = object_rate_limit.check_rate_limit(
        directory=blocker, identity="visitor", limit=5, window_seconds=60,
        fail_closed=True,
    )
    assert result.degraded is True
    assert result.allowed is False  # fail-closed denies the write


def test_read_error_does_not_masquerade_as_empty_bucket(tmp_path):
    """A genuine OSError reading the bucket must surface as degraded, not as
    an empty timestamp list (which would silently fail open regardless of
    posture)."""
    rate_dir = tmp_path / "ratelimit"
    rate_dir.mkdir()
    # Make the bucket a directory so read_text() raises OSError (IsADirectory).
    key = __import__("hashlib").sha256(b"visitor").hexdigest()[:32]
    (rate_dir / f"{key}.txt").mkdir()

    result = object_rate_limit.check_rate_limit(
        directory=rate_dir, identity="visitor", limit=5, window_seconds=60,
        fail_closed=True,
    )
    assert result.degraded is True
    assert result.allowed is False
