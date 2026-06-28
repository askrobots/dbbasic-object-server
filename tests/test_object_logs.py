import pytest

import object_logs
import object_versions


def write_log(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_get_object_logs_returns_empty_when_log_file_is_missing(tmp_path):
    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert logs == []


def test_get_object_logs_reads_tsv_entries(tmp_path):
    write_log(
        tmp_path / "data" / "logs" / "basics_counter" / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\tmethod\n"
        "a1\t2026-01-01T00:00:00\tINFO\tGET started\tGET\n"
        "a2\t2026-01-01T00:00:01\tERROR\tboom\tGET\n",
    )

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert logs == [
        {
            "entry_id": "a1",
            "timestamp": "2026-01-01T00:00:00",
            "level": "INFO",
            "message": "GET started",
            "method": "GET",
        },
        {
            "entry_id": "a2",
            "timestamp": "2026-01-01T00:00:01",
            "level": "ERROR",
            "message": "boom",
            "method": "GET",
        },
    ]


def test_get_object_logs_filters_by_level_and_limit(tmp_path):
    write_log(
        tmp_path / "data" / "logs" / "basics_counter" / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "a1\t2026-01-01T00:00:00\tINFO\tfirst\n"
        "a2\t2026-01-01T00:00:01\tERROR\tsecond\n"
        "a3\t2026-01-01T00:00:02\tERROR\tthird\n",
    )

    logs = object_logs.get_object_logs(
        "basics_counter",
        base_dir=tmp_path / "data",
        level="ERROR",
        limit=1,
    )

    assert logs == [
        {
            "entry_id": "a2",
            "timestamp": "2026-01-01T00:00:01",
            "level": "ERROR",
            "message": "second",
        }
    ]


def test_get_object_logs_reads_current_then_rotated_files(tmp_path):
    log_dir = tmp_path / "data" / "logs" / "basics_counter"
    write_log(
        log_dir / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "current\t2026-01-02T00:00:00\tINFO\tcurrent\n",
    )
    write_log(
        log_dir / "log-20260101-000000.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "old1\t2026-01-01T00:00:00\tINFO\told one\n",
    )
    write_log(
        log_dir / "log-20260101-010000.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "old2\t2026-01-01T01:00:00\tINFO\told two\n",
    )

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert [entry["entry_id"] for entry in logs] == ["current", "old1", "old2"]


@pytest.mark.parametrize(
    "object_id",
    [
        "",
        "../outside",
        "basics/counter",
        "basics.counter",
        "object id",
        "object@station",
        "object;drop",
        "object\x00id",
        "a" * 65,
    ],
)
def test_get_object_logs_rejects_invalid_object_ids(tmp_path, object_id):
    with pytest.raises(object_versions.InvalidObjectIdError):
        object_logs.get_object_logs(object_id, base_dir=tmp_path / "data")
