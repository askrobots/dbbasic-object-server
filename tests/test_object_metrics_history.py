"""Tests for TSV-backed metrics history snapshots."""

import object_metrics_history


def test_append_and_read_round_trip(tmp_path):
    object_metrics_history.append_snapshot(
        {
            "uptime_seconds": 120.5,
            "requests": 42,
            "errors": 1,
            "rps": 0.35,
            "error_rate": 2.38,
            "p50_ms": 9.1,
            "p95_ms": 40.2,
            "cpu_percent": 3.5,
            "memory_used_percent": 41.0,
            "disk_used_percent": 20.3,
        },
        base_dir=tmp_path,
    )
    object_metrics_history.append_snapshot(
        {"requests": 50, "errors": 1, "rps": 0.4},
        base_dir=tmp_path,
    )

    rows = object_metrics_history.read_history(base_dir=tmp_path)

    assert len(rows) == 2
    assert rows[0]["requests"] == 42
    assert rows[0]["p95_ms"] == 40.2
    assert rows[0]["timestamp"].endswith("Z")
    assert rows[1]["requests"] == 50
    assert rows[1]["cpu_percent"] is None


def test_history_is_capped_to_max_rows(tmp_path):
    for index in range(8):
        object_metrics_history.append_snapshot(
            {"requests": index},
            base_dir=tmp_path,
            max_rows=5,
        )

    rows = object_metrics_history.read_history(base_dir=tmp_path)

    assert len(rows) == 5
    assert [row["requests"] for row in rows] == [3, 4, 5, 6, 7]


def test_read_history_respects_limit_and_missing_file(tmp_path):
    assert object_metrics_history.read_history(base_dir=tmp_path) == []

    for index in range(5):
        object_metrics_history.append_snapshot({"requests": index}, base_dir=tmp_path)

    rows = object_metrics_history.read_history(base_dir=tmp_path, limit=2)
    assert [row["requests"] for row in rows] == [3, 4]
