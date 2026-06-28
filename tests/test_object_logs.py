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


def test_append_object_log_creates_tsv_entry(tmp_path):
    entry = object_logs.append_object_log(
        "basics_counter",
        "DEBUG",
        "GET completed successfully",
        base_dir=tmp_path / "data",
        method="GET",
        status="success",
        duration_ms=1.25,
    )

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert entry["entry_id"]
    assert logs == [
        {
            "entry_id": entry["entry_id"],
            "timestamp": entry["timestamp"],
            "level": "DEBUG",
            "message": "GET completed successfully",
            "method": "GET",
            "status": "success",
            "duration_ms": "1.25",
            "error_type": "",
            "error": "",
        }
    ]


def test_append_object_log_extends_existing_header(tmp_path):
    write_log(
        tmp_path / "data" / "logs" / "basics_counter" / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "a1\t2026-01-01T00:00:00\tINFO\told\n",
    )

    object_logs.append_object_log(
        "basics_counter",
        "ERROR",
        "GET failed: boom",
        base_dir=tmp_path / "data",
        method="GET",
        status="error",
        error_type="RuntimeError",
        error="boom",
    )

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert logs[0]["message"] == "old"
    assert logs[0]["status"] == ""
    assert logs[1]["method"] == "GET"
    assert logs[1]["status"] == "error"
    assert logs[1]["error_type"] == "RuntimeError"


def test_object_logger_writes_object_owned_logs(tmp_path):
    logger = object_logs.ObjectLogger("basics_counter", base_dir=tmp_path / "data")

    logger.info("Counter incremented", user_id="user-1", count=2)
    logger.error("Counter failed", error_code="E_COUNTER")

    logs = logger.get_logs()

    assert [entry["level"] for entry in logs] == ["INFO", "ERROR"]
    assert logs[0]["message"] == "Counter incremented"
    assert logs[0]["user_id"] == "user-1"
    assert logs[0]["count"] == "2"
    assert logs[1]["error_code"] == "E_COUNTER"


def test_object_logger_filters_logs(tmp_path):
    logger = object_logs.ObjectLogger("basics_counter", base_dir=tmp_path / "data")

    logger.debug("first")
    logger.error("second", status="error")
    logger.error("third", status="error")

    logs = logger.get_logs(level="ERROR", limit=1, status="error")

    assert len(logs) == 1
    assert logs[0]["message"] == "second"


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
