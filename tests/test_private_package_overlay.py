"""Closed-source package overlay (packages-private/).

The server searches a private package root ahead of the open `packages/` dir,
so a proprietary package (e.g. Mailcow email hosting) is discovered, listed,
and resolved through the same flow as an open one -- and shadows an open
package that shares its id. See packages-private/README.md.
"""

import json

import object_server


def _write_pkg(root, package_id, *, name):
    pkg_dir = root / package_id
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "dbbasic-package.json").write_text(json.dumps({
        "id": package_id,
        "name": name,
        "version": "0.1.0",
        "compatibility": {"dbbasic_object_server": ">=0.1.0"},
        "objects": [], "schemas": [], "permissions": [], "seed": [], "migrations": [],
    }))
    return pkg_dir


def _point_env(monkeypatch, public, private):
    monkeypatch.setenv(object_server.PACKAGES_DIR_ENV, str(public))
    monkeypatch.setenv(object_server.PRIVATE_PACKAGES_DIR_ENV, str(private))


def test_private_root_included_only_when_it_exists(tmp_path, monkeypatch):
    public = tmp_path / "packages"; public.mkdir()
    private = tmp_path / "packages-private"  # does NOT exist yet
    _point_env(monkeypatch, public, private)
    # absent private root -> behaves exactly like the open-only checkout
    assert object_server._package_roots() == [str(public)]
    private.mkdir()
    assert object_server._package_roots() == [str(private), str(public)]


def test_list_merges_both_roots(tmp_path, monkeypatch):
    public = tmp_path / "packages"; public.mkdir()
    private = tmp_path / "packages-private"; private.mkdir()
    _point_env(monkeypatch, public, private)
    _write_pkg(public, "app-open-only", name="Open Only")
    _write_pkg(private, "app-mailcow", name="Mailcow Hosting")

    ids = {p["id"] for p in object_server._list_all_packages()}
    assert ids == {"app-open-only", "app-mailcow"}


def test_private_shadows_open_on_id_collision(tmp_path, monkeypatch):
    public = tmp_path / "packages"; public.mkdir()
    private = tmp_path / "packages-private"; private.mkdir()
    _point_env(monkeypatch, public, private)
    _write_pkg(public, "app-email", name="Open Email Adapter")
    _write_pkg(private, "app-email", name="Private Email Override")

    # private wins in both the merged listing and single-id resolution
    listed = {p["id"]: p["name"] for p in object_server._list_all_packages()}
    assert listed["app-email"] == "Private Email Override"
    assert object_server._root_for_package("app-email") == str(private)


def test_private_root_defaults_to_sibling_of_packages_dir(tmp_path, monkeypatch):
    # With only the packages dir overridden, the private overlay must derive as
    # a SIBLING of it -- never a fixed cwd-relative path -- so a hermetic test
    # can't accidentally pick up a developer's real packages-private/ contents.
    pkgs = tmp_path / "pkgs"; pkgs.mkdir()
    monkeypatch.setenv(object_server.PACKAGES_DIR_ENV, str(pkgs))
    monkeypatch.delenv(object_server.PRIVATE_PACKAGES_DIR_ENV, raising=False)
    assert object_server._private_packages_dir() == str(tmp_path / "packages-private")
    # the sibling doesn't exist -> overlay empty -> only the open root is searched
    assert object_server._package_roots() == [str(pkgs)]


def test_resolve_falls_back_to_open_root_for_unknown_id(tmp_path, monkeypatch):
    public = tmp_path / "packages"; public.mkdir()
    private = tmp_path / "packages-private"; private.mkdir()
    _point_env(monkeypatch, public, private)
    _write_pkg(public, "app-open-only", name="Open Only")

    assert object_server._root_for_package("app-open-only") == str(public)
    # unknown id -> open root, so downstream raises the normal not-found
    assert object_server._root_for_package("app-nope") == str(public)
