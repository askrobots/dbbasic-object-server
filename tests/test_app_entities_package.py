"""Structural tests for packages/app-entities (65 multi-entity, slice 1: the
entity concept itself). An entity is a distinct set of books under one login,
owner-scoped -- a faithful port of the predecessor's Entity model (its `user`
FK -> owner_id here). URL-addressable via seeded 59 views (site_view_render),
no bespoke page. Later slices thread entity_id onto the finance/commerce
collections and add the switcher + chart-of-accounts auto-seed.
"""

import json
from pathlib import Path

import object_packages
import object_permissions
import object_schemas

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_ENTITIES_DIR = PACKAGES_ROOT / "app-entities"


def _schema():
    return json.loads((APP_ENTITIES_DIR / "schemas" / "entities.json").read_text())


def _seed_rows(name):
    import csv

    with open(APP_ENTITIES_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_normalizes():
    package = object_packages.get_package("app-entities", root=PACKAGES_ROOT)
    assert package["id"] == "app-entities"
    assert {s["collection"] for s in package["schemas"]} == {"entities"}
    assert package["objects"] == []  # no bespoke page -- pages are seeded views
    assert {e["collection"] for e in package["seed"]} == {"views", "site_routes"}


def test_entities_schema_is_owner_scoped_and_faithful_to_the_predecessor():
    schema = _schema()
    by_name = {f["name"]: f for f in schema["fields"]}
    # owner_id (the predecessor's `user` FK), required.
    assert by_name["owner_id"]["required"] is True
    assert by_name["name"]["required"] is True
    # Faithful enum vocabularies (predecessor MODE/BUSINESS_TYPE/STATUS choices).
    assert by_name["mode"]["enum"] == ["simple", "standard", "double_entry"]
    assert by_name["mode"]["default"] == "simple"
    assert by_name["business_type"]["enum"] == ["freelancer", "small_business", "nonprofit", "other"]
    assert by_name["status"]["enum"] == ["active", "closed", "archived"]
    assert by_name["base_currency"]["default"] == "USD"
    assert by_name["created_at"]["read_only"] is True


def test_pages_are_seeded_views_url_addressable_no_bespoke_object():
    views = {r["id"]: r for r in _seed_rows("views")}
    assert set(views) == {"view_entities_list", "view_entities_detail"}
    # /entities -> a create form + a browsable list (generic blocks).
    list_blocks = json.loads(views["view_entities_list"]["blocks"])
    assert [b["kind"] for b in list_blocks] == ["form", "list"]
    assert views["view_entities_list"]["route"] == "/entities"
    # /entities/{id} -> owner-editable detail (URL-addressable per record).
    detail_blocks = json.loads(views["view_entities_detail"]["blocks"])
    assert len(detail_blocks) == 1 and detail_blocks[0]["kind"] == "detail"
    assert detail_blocks[0]["editable"] is True and detail_blocks[0]["deletable"] is True
    assert views["view_entities_detail"]["route"] == "/entities/{entity_id:uuid}"

    routes = {r["pattern"]: r for r in _seed_rows("site_routes")}
    assert routes["/entities"]["object_id"] == "site_view_render"
    assert routes["/entities/{entity_id:uuid}"]["object_id"] == "site_view_render"


def _policy():
    payload = json.loads((APP_ENTITIES_DIR / "permissions" / "rules.json").read_text())
    return object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )


def test_owner_can_crud_their_own_entities():
    policy = _policy()
    subject = object_permissions.PermissionSubject(user_id="7")
    record = {"owner_id": "7", "name": "Acme Books"}
    for action in (object_permissions.CREATE, object_permissions.READ,
                   object_permissions.UPDATE, object_permissions.DELETE):
        decision = object_permissions.check_permission(
            subject, action, policy=policy, collection="entities", record=record,
        )
        assert decision.allowed is True, action


def test_others_and_anonymous_cannot_read_someone_elses_books():
    policy = _policy()
    record = {"owner_id": "7", "name": "Acme Books"}
    other = object_permissions.PermissionSubject(user_id="8")
    assert object_permissions.check_permission(
        other, object_permissions.READ, policy=policy, collection="entities", record=record,
    ).allowed is False
    assert object_permissions.check_permission(
        None, object_permissions.READ, policy=policy, collection="entities", record=record,
    ).allowed is False


def test_install_loads_the_schema(tmp_path):
    data_dir = tmp_path / "data"
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_packages.install_package(
        "app-entities", root=PACKAGES_ROOT, base_dir=data_dir, object_roots=[object_root],
    )
    schema = object_schemas.get_schema("entities", base_dir=data_dir)
    assert schema["name"] == "entities"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-entities", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []


def test_nav_ships_an_entity_switcher():
    """65 multi-entity: the nav bar (app-theme/nav.py) gains a "Books" switcher
    that lists the user's entities and stores the chosen one in localStorage
    (window.dbbasicEntity) -- the client-held current entity the list/form
    generators scope by. Hidden when the user has no entities."""
    nav = (PACKAGES_ROOT / "app-theme" / "objects" / "site" / "nav.py").read_text()
    assert "nav-books" in nav
    assert "window.dbbasicEntity" in nav
    assert 'ENTITY_KEY = "dbbasic_entity"' in nav
    assert "/collections/entities/records" in nav
    # An "All entities" option clears the scope, and there's a way to manage.
    assert "All entities" in nav
    assert 'href="/entities"' in nav
