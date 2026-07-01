import gzip

import pytest

import object_correlation
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


def test_get_object_logs_reads_compressed_rotated_files(tmp_path):
    log_dir = tmp_path / "data" / "logs" / "basics_counter"
    write_log(
        log_dir / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "current\t2026-01-02T00:00:00\tINFO\tcurrent\n",
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(log_dir / "log-20260101-000000.tsv.gz", "wt", newline="") as f:
        f.write(
            "entry_id\ttimestamp\tlevel\tmessage\n"
            "old1\t2026-01-01T00:00:00\tINFO\told one\n"
        )

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert [entry["entry_id"] for entry in logs] == ["current", "old1"]


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
            "correlation_id": "",
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


def test_append_object_log_inherits_current_correlation_id(tmp_path):
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"
    token = object_correlation.set_current_correlation_id(correlation_id)
    try:
        object_logs.append_object_log(
            "basics_counter",
            "INFO",
            "inside object",
            base_dir=tmp_path / "data",
        )
    finally:
        object_correlation.reset_current_correlation_id(token)

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")

    assert logs[0]["correlation_id"] == correlation_id


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


def test_append_object_log_rotates_and_compresses_large_current_log(tmp_path):
    log_dir = tmp_path / "data" / "logs" / "basics_counter"
    write_log(
        log_dir / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "old\t2026-01-01T00:00:00\tINFO\t" + ("old message " * 80) + "\n",
    )

    entry = object_logs.append_object_log(
        "basics_counter",
        "INFO",
        "new message",
        base_dir=tmp_path / "data",
        max_log_bytes=128,
    )

    compressed_logs = list(log_dir.glob("log-*.tsv.gz"))
    assert len(compressed_logs) == 1
    with gzip.open(compressed_logs[0], "rt", newline="") as f:
        compressed_text = f.read()
    assert "old message" in compressed_text

    logs = object_logs.get_object_logs("basics_counter", base_dir=tmp_path / "data")
    assert [row["entry_id"] for row in logs] == [entry["entry_id"], "old"]
    assert logs[0]["message"] == "new message"


def test_append_object_log_can_rotate_without_compression(tmp_path):
    log_dir = tmp_path / "data" / "logs" / "basics_counter"
    write_log(
        log_dir / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "old\t2026-01-01T00:00:00\tINFO\t" + ("old message " * 80) + "\n",
    )

    object_logs.append_object_log(
        "basics_counter",
        "INFO",
        "new message",
        base_dir=tmp_path / "data",
        max_log_bytes=128,
        compress_rotated=False,
    )

    assert len(list(log_dir.glob("log-*.tsv"))) == 1
    assert not list(log_dir.glob("log-*.tsv.gz"))


def test_append_object_log_keeps_only_configured_rotated_files(tmp_path):
    log_dir = tmp_path / "data" / "logs" / "basics_counter"
    for index in range(3):
        write_log(
            log_dir / f"log-20260101-00000{index}.tsv",
            "entry_id\ttimestamp\tlevel\tmessage\n"
            f"old{index}\t2026-01-01T00:00:0{index}\tINFO\told {index}\n",
        )
    write_log(
        log_dir / "log.tsv",
        "entry_id\ttimestamp\tlevel\tmessage\n"
        "current\t2026-01-01T00:01:00\tINFO\t" + ("current message " * 80) + "\n",
    )

    object_logs.append_object_log(
        "basics_counter",
        "INFO",
        "new message",
        base_dir=tmp_path / "data",
        max_log_bytes=128,
        keep_rotated=2,
    )

    rotated_names = [path.name for path in object_logs._rotated_log_files(log_dir)]
    assert len(rotated_names) == 2
    assert "log-20260101-000000.tsv" not in rotated_names
    assert "log.tsv" not in rotated_names


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
