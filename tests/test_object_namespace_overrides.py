"""Override-object resolution (Phase 3, docs/upgrade-and-customization.md Rule 2).

Overrides are strictly opt-in via DBBASIC_OVERRIDES_DIR. The single most
important guarantee in this module is the backward-compat guard: with the
env var unset, get_object_roots() must be byte-identical to the pre-override
behavior (get_base_object_roots()), and resolution must be unchanged.
"""

from pathlib import Path

import object_namespace
import object_source


def write_object(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def GET(request):\n    return {'status': 'ok'}\n")
    return path


def _clear_env(monkeypatch):
    monkeypatch.delenv("DBBASIC_OBJECTS_DIR", raising=False)
    monkeypatch.delenv("DBBASIC_OVERRIDES_DIR", raising=False)


# --- Backward-compat guard: env unset behaves exactly as before -------------


def test_default_env_unset_get_object_roots_matches_base_roots(monkeypatch):
    _clear_env(monkeypatch)

    assert object_namespace.get_object_roots() == object_namespace.get_base_object_roots()
    assert object_namespace.get_object_roots() == [Path("objects")]


def test_default_env_unset_override_root_is_none(monkeypatch):
    _clear_env(monkeypatch)

    assert object_namespace.get_override_root() is None


def test_default_env_unset_has_override_is_false(monkeypatch):
    _clear_env(monkeypatch)

    assert object_namespace.has_override("basics_counter") is False
    assert object_namespace.override_path("basics_counter") is None


def test_default_env_unset_resolve_object_id_unchanged(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    root = tmp_path / "objects"
    expected = write_object(root / "basics" / "counter.py")
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(root))

    assert object_namespace.resolve_object_id("basics_counter") == expected
    assert object_namespace.get_object_roots() == [root]


# --- Override root enabled ---------------------------------------------------


def test_override_root_sees_env_var(tmp_path, monkeypatch):
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    assert object_namespace.get_override_root() == override_root


def test_get_object_roots_prepends_override_to_base(tmp_path, monkeypatch):
    base_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(base_root))
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    assert object_namespace.get_object_roots() == [override_root, base_root]
    assert object_namespace.get_base_object_roots() == [base_root]


def test_override_relative_path_forms(monkeypatch):
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", "/tmp/overrides-not-used")

    assert object_namespace.override_relative_path("basics_counter") == Path("basics") / "counter.py"
    assert object_namespace.override_relative_path("dashboard") == Path("dashboard.py")
    # User objects are not supported by overrides.
    assert object_namespace.override_relative_path("u_42_deals") is None
    # Invalid ids are rejected outright.
    assert object_namespace.override_relative_path("../escape") is None


def test_override_path_and_has_override(tmp_path, monkeypatch):
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    expected = override_root / "basics" / "counter.py"
    assert object_namespace.override_path("basics_counter") == expected
    assert object_namespace.has_override("basics_counter") is False

    write_object(expected)
    assert object_namespace.has_override("basics_counter") is True


def test_override_path_none_for_user_object(tmp_path, monkeypatch):
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    assert object_namespace.override_path("u_42_deals") is None
    assert object_namespace.has_override("u_42_deals") is False


def test_resolve_object_id_prefers_override_when_present(tmp_path, monkeypatch):
    base_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(base_root))
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    base_path = write_object(base_root / "basics" / "counter.py")
    override_path = override_root / "basics" / "counter.py"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text("def GET(request):\n    return {'status': 'overridden'}\n")

    resolved = object_namespace.resolve_object_id("basics_counter")

    assert resolved == override_path
    assert resolved != base_path


def test_resolve_object_id_falls_back_to_base_when_no_override_file(tmp_path, monkeypatch):
    base_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(base_root))
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    base_path = write_object(base_root / "basics" / "counter.py")

    assert object_namespace.resolve_object_id("basics_counter") == base_path


def test_get_object_source_reads_override_but_base_roots_read_package_copy(tmp_path, monkeypatch):
    base_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(base_root))
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    write_object(base_root / "basics" / "counter.py")
    override_file = override_root / "basics" / "counter.py"
    override_file.parent.mkdir(parents=True, exist_ok=True)
    override_file.write_text("def GET(request):\n    return {'status': 'overridden'}\n")

    # Default (override-aware) resolution reads the override.
    assert object_namespace.resolve_object_id("basics_counter") == override_file
    assert "overridden" in object_source.get_object_source("basics_counter")

    # Explicitly scoping to base roots always reads the pristine package copy.
    base_only = object_source.get_object_source("basics_counter", object_namespace.get_base_object_roots())
    assert "overridden" not in base_only
    assert "'status': 'ok'" in base_only


def test_iter_object_sources_marks_override_kind(tmp_path, monkeypatch):
    base_root = tmp_path / "objects"
    override_root = tmp_path / "overrides"
    monkeypatch.setenv("DBBASIC_OBJECTS_DIR", str(base_root))
    monkeypatch.setenv("DBBASIC_OVERRIDES_DIR", str(override_root))

    write_object(base_root / "basics" / "counter.py")
    write_object(override_root / "dashboard.py")

    sources = {source.object_id: source.kind for source in object_namespace.iter_object_sources()}

    assert sources["basics_counter"] == "system"
    assert sources["dashboard"] == "override"
