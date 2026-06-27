from pathlib import Path

import pytest

import object_namespace


def write_object(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def GET(request):\n    return {'status': 'ok'}\n")
    return path


def test_get_object_roots_defaults_to_objects(monkeypatch):
    monkeypatch.delenv("DBBASIC_OBJECTS_DIR", raising=False)

    assert object_namespace.get_object_roots() == [Path("objects")]


def test_get_object_roots_honors_env_override(tmp_path, monkeypatch):
    custom_root = tmp_path / "custom_objects"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(custom_root))

    assert object_namespace.get_object_roots() == [custom_root]


def test_missing_source_root_returns_no_objects(tmp_path):
    missing_root = tmp_path / "missing"

    assert object_namespace.iter_object_sources([missing_root]) == []
    assert object_namespace.resolve_object_id("basics_counter", [missing_root]) is None


def test_resolve_system_object_from_category_name(tmp_path):
    root = tmp_path / "objects"
    expected = write_object(root / "basics" / "counter.py")

    found = object_namespace.resolve_object_id("basics_counter", [root])

    assert found == expected


def test_resolve_system_object_with_numbered_filename(tmp_path):
    root = tmp_path / "objects"
    expected = write_object(root / "tutorial" / "01_hello.py")

    found = object_namespace.resolve_object_id("tutorial_01_hello", [root])

    assert found == expected


def test_resolve_user_object(tmp_path):
    root = tmp_path / "objects"
    expected = write_object(root / "users" / "42" / "deals.py")

    found = object_namespace.resolve_object_id("u_42_deals", [root])

    assert found == expected


def test_object_id_from_system_path(tmp_path):
    root = tmp_path / "objects"
    source = write_object(root / "basics" / "counter.py")

    assert object_namespace.object_id_from_path(source, root) == "basics_counter"


def test_object_id_from_user_path(tmp_path):
    root = tmp_path / "objects"
    source = write_object(root / "users" / "42" / "deals.py")

    assert object_namespace.object_id_from_path(source, root) == "u_42_deals"


def test_object_id_from_path_rejects_escaped_path(tmp_path):
    root = tmp_path / "objects"
    outside = write_object(tmp_path / "outside.py")

    with pytest.raises(ValueError):
        object_namespace.object_id_from_path(outside, root)


def test_iter_object_sources_ignores_private_and_cache_files(tmp_path):
    root = tmp_path / "objects"
    public = write_object(root / "basics" / "counter.py")
    write_object(root / "basics" / "__init__.py")
    write_object(root / "basics" / "_private.py")
    write_object(root / "basics" / ".hidden.py")
    write_object(root / "__pycache__" / "cached.py")

    sources = object_namespace.iter_object_sources([root])

    assert sources == [
        object_namespace.ObjectSource(
            object_id="basics_counter",
            path=public,
            relative_path=Path("basics") / "counter.py",
            kind="system",
        )
    ]


def test_iter_object_sources_keeps_user_objects_separate(tmp_path):
    root = tmp_path / "objects"
    write_object(root / "basics" / "counter.py")
    write_object(root / "users" / "42" / "deals.py")

    sources = object_namespace.iter_object_sources([root])

    assert [source.object_id for source in sources] == ["basics_counter", "u_42_deals"]
    assert [source.kind for source in sources] == ["system", "user"]
    assert "users_42_deals" not in [source.object_id for source in sources]


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
def test_invalid_object_ids_are_rejected(object_id):
    assert not object_namespace.validate_object_id(object_id)
    assert object_namespace.resolve_object_id(object_id, [Path("objects")]) is None


def test_duplicate_ids_resolve_from_first_root(tmp_path):
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    first = write_object(root_one / "basics" / "counter.py")
    write_object(root_two / "basics" / "counter.py")

    found = object_namespace.resolve_object_id("basics_counter", [root_one, root_two])

    assert found == first


def test_find_trigger_file_uses_triggers_directory(tmp_path):
    root = tmp_path / "objects"
    expected = write_object(root / "triggers" / "queue.py")

    found = object_namespace.find_trigger_file("queue", [root])

    assert found == expected


def test_find_trigger_file_rejects_unsafe_name(tmp_path):
    root = tmp_path / "objects"
    write_object(root / "triggers" / "queue.py")

    assert object_namespace.find_trigger_file("../queue", [root]) is None
