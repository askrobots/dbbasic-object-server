import pytest

import object_state
import object_versions


def write_state(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_get_object_state_returns_empty_when_state_file_is_missing(tmp_path):
    state = object_state.get_object_state("basics_counter", base_dir=tmp_path / "data")

    assert state == {}


def test_get_object_state_reads_old_key_value_format(tmp_path):
    write_state(
        tmp_path / "data" / "state" / "basics_counter" / "state.tsv",
        "count\t3\nname\tcounter\n",
    )

    state = object_state.get_object_state("basics_counter", base_dir=tmp_path / "data")

    assert state == {"count": 3, "name": "counter"}


def test_get_object_state_reads_timestamp_format_and_skips_header(tmp_path):
    write_state(
        tmp_path / "data" / "state" / "basics_counter" / "state.tsv",
        "key\tvalue\ttimestamp\n"
        "count\t3\t1710000000.1\n"
        "rate\t3.5\t1710000000.2\n"
        "enabled\ttrue\t1710000000.3\n",
    )

    state = object_state.get_object_state("basics_counter", base_dir=tmp_path / "data")

    assert state == {
        "count": 3,
        "rate": 3.5,
        "enabled": "true",
    }


def test_get_object_state_ignores_blank_and_malformed_rows(tmp_path):
    write_state(
        tmp_path / "data" / "state" / "basics_counter" / "state.tsv",
        "\n"
        "only_key\n"
        "valid\tvalue\n",
    )

    state = object_state.get_object_state("basics_counter", base_dir=tmp_path / "data")

    assert state == {"valid": "value"}


def test_object_state_manager_sets_and_persists_values(tmp_path):
    manager = object_state.ObjectStateManager("basics_counter", base_dir=tmp_path / "data")

    manager.set("count", 1)
    manager.set("name", "counter")

    assert manager.get("count") == 1
    assert manager.get("missing", "default") == "default"
    assert manager.get_all() == {"count": 1, "name": "counter"}
    assert object_state.get_object_state("basics_counter", base_dir=tmp_path / "data") == {
        "count": 1,
        "name": "counter",
    }


def test_object_state_manager_reload_reads_fresh_state(tmp_path):
    first = object_state.ObjectStateManager("basics_counter", base_dir=tmp_path / "data")
    second = object_state.ObjectStateManager("basics_counter", base_dir=tmp_path / "data")

    first.set("count", 2)

    assert second.get("count") is None
    second.reload()
    assert second.get("count") == 2


def test_object_state_manager_deletes_and_persists_values(tmp_path):
    data_dir = tmp_path / "data"
    manager = object_state.ObjectStateManager("basics_counter", base_dir=data_dir)

    manager.set("count", 3)
    manager.set("name", "counter")
    manager.delete("count")
    manager.delete("missing")

    assert manager.get_all() == {"name": "counter"}
    assert object_state.get_object_state("basics_counter", base_dir=data_dir) == {
        "name": "counter"
    }


def test_object_state_manager_deletes_many_values_in_one_pass(tmp_path):
    data_dir = tmp_path / "data"
    manager = object_state.ObjectStateManager("basics_counter", base_dir=data_dir)

    manager.set("count", 3)
    manager.set("name", "counter")
    manager.set("status", "ready")

    removed = manager.delete_many(["count", "missing", "name", "count"])

    assert removed == 2
    assert manager.get_all() == {"status": "ready"}
    assert object_state.get_object_state("basics_counter", base_dir=data_dir) == {
        "status": "ready"
    }


def test_object_state_manager_writes_timestamp_format(tmp_path):
    manager = object_state.ObjectStateManager("basics_counter", base_dir=tmp_path / "data")

    manager.set("count", 3)

    state_file = tmp_path / "data" / "state" / "basics_counter" / "state.tsv"
    fields = state_file.read_text().strip().split("\t")
    assert fields[0] == "count"
    assert fields[1] == "3"
    assert float(fields[2]) > 0


def test_object_state_manager_rejects_invalid_keys(tmp_path):
    manager = object_state.ObjectStateManager("basics_counter", base_dir=tmp_path / "data")

    with pytest.raises(ValueError, match="State key must be a non-empty string"):
        manager.set("", 1)

    with pytest.raises(ValueError, match="Invalid state key"):
        manager.set("bad\tkey", 1)


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
def test_get_object_state_rejects_invalid_object_ids(tmp_path, object_id):
    with pytest.raises(object_versions.InvalidObjectIdError):
        object_state.get_object_state(object_id, base_dir=tmp_path / "data")
