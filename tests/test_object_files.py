from pathlib import Path

import pytest

import object_files
from object_versions import InvalidObjectIdError


def write_file(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_list_object_files_returns_metadata_for_nested_files(tmp_path):
    data_dir = tmp_path / "data"
    write_file(data_dir / "files" / "site_home" / "image.png", b"png")
    write_file(data_dir / "files" / "site_home" / "nested" / "note.txt", b"hello")

    files = object_files.list_object_files("site_home", base_dir=data_dir)

    assert [item["name"] for item in files] == ["image.png", "nested/note.txt"]
    assert files[0]["size"] == 3
    assert isinstance(files[0]["modified"], float)


def test_list_object_files_returns_empty_for_missing_directory(tmp_path):
    assert object_files.list_object_files("site_home", base_dir=tmp_path / "data") == []


def test_list_object_files_skips_symlink_outside_object_directory(tmp_path):
    data_dir = tmp_path / "data"
    outside = write_file(tmp_path / "outside.txt", b"secret")
    link = data_dir / "files" / "site_home" / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    assert object_files.list_object_files("site_home", base_dir=data_dir) == []


def test_read_object_file_returns_bytes_and_metadata(tmp_path):
    data_dir = tmp_path / "data"
    write_file(data_dir / "files" / "site_home" / "report.txt", b"hello")

    content, metadata = object_files.read_object_file(
        "site_home",
        "report.txt",
        base_dir=data_dir,
    )

    assert content == b"hello"
    assert metadata["name"] == "report.txt"
    assert metadata["size"] == 5


@pytest.mark.parametrize(
    "filename",
    ["", "../secret.txt", "nested/../../secret.txt", "/etc/passwd", "bad\x00name"],
)
def test_read_object_file_rejects_unsafe_filenames(tmp_path, filename):
    with pytest.raises(object_files.InvalidObjectFilenameError):
        object_files.read_object_file("site_home", filename, base_dir=tmp_path / "data")


def test_read_object_file_rejects_invalid_object_id(tmp_path):
    with pytest.raises(InvalidObjectIdError):
        object_files.read_object_file("../bad", "file.txt", base_dir=tmp_path / "data")


def test_read_object_file_blocks_symlink_outside_object_directory(tmp_path):
    data_dir = tmp_path / "data"
    outside = write_file(tmp_path / "outside.txt", b"secret")
    link = data_dir / "files" / "site_home" / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with pytest.raises(object_files.InvalidObjectFilenameError):
        object_files.read_object_file("site_home", "link.txt", base_dir=data_dir)


def test_read_object_file_raises_for_missing_file(tmp_path):
    with pytest.raises(object_files.ObjectFileNotFoundError):
        object_files.read_object_file("site_home", "missing.txt", base_dir=tmp_path / "data")


def test_write_object_file_creates_nested_file_and_metadata(tmp_path):
    data_dir = tmp_path / "data"

    metadata = object_files.write_object_file(
        "site_home",
        "assets/report.txt",
        b"hello",
        base_dir=data_dir,
    )

    assert metadata["name"] == "assets/report.txt"
    assert metadata["size"] == 5
    assert (data_dir / "files" / "site_home" / "assets" / "report.txt").read_bytes() == b"hello"


def test_write_object_file_rejects_duplicate_without_overwrite(tmp_path):
    data_dir = tmp_path / "data"
    write_file(data_dir / "files" / "site_home" / "assets" / "report.txt", b"old")

    with pytest.raises(object_files.ObjectFileExistsError):
        object_files.write_object_file(
            "site_home",
            "assets/report.txt",
            b"new",
            base_dir=data_dir,
        )

    assert (data_dir / "files" / "site_home" / "assets" / "report.txt").read_bytes() == b"old"


def test_write_object_file_overwrites_when_requested(tmp_path):
    data_dir = tmp_path / "data"
    write_file(data_dir / "files" / "site_home" / "assets" / "report.txt", b"old")

    metadata = object_files.write_object_file(
        "site_home",
        "assets/report.txt",
        b"new",
        base_dir=data_dir,
        overwrite=True,
    )

    assert metadata["name"] == "assets/report.txt"
    assert metadata["size"] == 3
    assert (data_dir / "files" / "site_home" / "assets" / "report.txt").read_bytes() == b"new"


def test_write_object_file_enforces_max_bytes(tmp_path):
    with pytest.raises(object_files.ObjectFileTooLargeError):
        object_files.write_object_file(
            "site_home",
            "report.txt",
            b"hello",
            base_dir=tmp_path / "data",
            max_bytes=4,
        )


def test_write_object_file_rejects_unsafe_filename(tmp_path):
    with pytest.raises(object_files.InvalidObjectFilenameError):
        object_files.write_object_file(
            "site_home",
            "../secret.txt",
            b"secret",
            base_dir=tmp_path / "data",
        )


def test_delete_object_file_removes_file_and_prunes_empty_dirs(tmp_path):
    data_dir = tmp_path / "data"
    file_path = write_file(data_dir / "files" / "site_home" / "assets" / "report.txt", b"old")

    metadata = object_files.delete_object_file(
        "site_home",
        "assets/report.txt",
        base_dir=data_dir,
    )

    assert metadata["name"] == "assets/report.txt"
    assert metadata["size"] == 3
    assert not file_path.exists()
    assert not file_path.parent.exists()


def test_delete_object_file_raises_for_missing_file(tmp_path):
    with pytest.raises(object_files.ObjectFileNotFoundError):
        object_files.delete_object_file("site_home", "missing.txt", base_dir=tmp_path / "data")
