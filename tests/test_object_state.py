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
