import pytest

import object_file_changes
from object_versions import InvalidObjectIdError


def test_append_file_change_writes_jsonl_and_lists_newest_first(tmp_path):
    data_dir = tmp_path / "data"

    first = object_file_changes.append_file_change(
        object_id="site_home",
        action="file_create",
        file_name="assets/report.txt",
        file_size=5,
        actor="admin",
        message="created report",
        correlation_id="first-correlation",
        details={"method": "POST"},
        base_dir=data_dir,
    )
    second = object_file_changes.append_file_change(
        object_id="site_home",
        action="file_update",
        file_name="assets/report.txt",
        file_size=7,
        actor="admin",
        base_dir=data_dir,
    )

    path = data_dir / "file_changes" / "site_home" / "changes.jsonl"
    assert path.exists()

    payload = object_file_changes.list_file_changes("site_home", base_dir=data_dir)

    assert payload["count"] == 2
    assert payload["total"] == 2
    assert payload["changes"][0]["change_id"] == second["change_id"]
    assert payload["changes"][1]["change_id"] == first["change_id"]
    assert payload["changes"][1]["message"] == "created report"
    assert payload["changes"][1]["correlation_id"] == "first-correlation"
    assert payload["changes"][1]["details"] == {"method": "POST"}


def test_list_file_changes_filters_file_and_paginates(tmp_path):
    data_dir = tmp_path / "data"
    for index in range(3):
        object_file_changes.append_file_change(
            object_id="site_home",
            action="file_create",
            file_name=f"assets/{index}.txt",
            file_size=index,
            base_dir=data_dir,
        )

    payload = object_file_changes.list_file_changes(
        "site_home",
        file_name="assets/1.txt",
        base_dir=data_dir,
        limit=1,
        offset=0,
    )

    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["file_name"] == "assets/1.txt"
    assert payload["changes"][0]["file_name"] == "assets/1.txt"


def test_list_file_changes_returns_empty_for_valid_object_without_log(tmp_path):
    payload = object_file_changes.list_file_changes("site_home", base_dir=tmp_path / "data")

    assert payload["count"] == 0
    assert payload["total"] == 0
    assert payload["changes"] == []


def test_file_changes_reject_invalid_inputs(tmp_path):
    with pytest.raises(InvalidObjectIdError):
        object_file_changes.list_file_changes("../bad", base_dir=tmp_path / "data")

    with pytest.raises(object_file_changes.InvalidFileChangeError):
        object_file_changes.append_file_change(
            object_id="site_home",
            action="wrong",
            file_name="assets/report.txt",
            base_dir=tmp_path / "data",
        )

    with pytest.raises(object_file_changes.InvalidFileChangeError):
        object_file_changes.append_file_change(
            object_id="site_home",
            action="file_create",
            file_name="../secret.txt",
            base_dir=tmp_path / "data",
        )

    with pytest.raises(ValueError):
        object_file_changes.list_file_changes(
            "site_home",
            base_dir=tmp_path / "data",
            limit=object_file_changes.MAX_CHANGE_LIMIT + 1,
        )


def test_file_changes_ignore_corrupt_lines(tmp_path):
    data_dir = tmp_path / "data"
    path = data_dir / "file_changes" / "site_home" / "changes.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json}\n")

    object_file_changes.append_file_change(
        object_id="site_home",
        action="file_create",
        file_name="assets/report.txt",
        base_dir=data_dir,
    )

    payload = object_file_changes.list_file_changes("site_home", base_dir=data_dir)

    assert payload["count"] == 1
    assert payload["changes"][0]["file_name"] == "assets/report.txt"
