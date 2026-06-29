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
