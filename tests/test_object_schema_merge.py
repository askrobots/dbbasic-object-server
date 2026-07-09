"""Unit tests for object_schemas.merge_schema_fields (Phase 4a, Rule 3).

Schemas are additive: an operator-added field and a package-added field
should both survive an upgrade. These tests exercise the pure 3-way merge
function directly, independent of the install_package engine wiring (see
tests/test_object_packages.py for the engine-level tests).
"""

import object_schemas


def schema(fields):
    return {"name": "contacts", "fields": fields}


def field(name, type_="text", **extra):
    payload = {"name": name, "type": type_}
    payload.update(extra)
    return payload


def test_operator_and_package_each_add_a_different_field():
    base = schema([field("id"), field("name")])
    mine = schema([field("id"), field("name"), field("x_a")])
    theirs = schema([field("id"), field("name"), field("b")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert [f["name"] for f in merged["fields"]] == ["id", "name", "b", "x_a"]


def test_operator_changes_field_package_leaves_it_alone():
    base = schema([field("email", "text")])
    mine = schema([field("email", "email")])
    theirs = schema([field("email", "text")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert merged["fields"] == [field("email", "email")]


def test_package_changes_field_operator_leaves_it_alone():
    base = schema([field("email", "text")])
    mine = schema([field("email", "text")])
    theirs = schema([field("email", "email")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert merged["fields"] == [field("email", "email")]


def test_both_change_the_same_field_differently_collides():
    base = schema([field("email", "text")])
    mine = schema([field("email", "email")])
    theirs = schema([field("email", "url")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == ["email"]
    # Operator's version is kept pending a resolution.
    assert merged["fields"] == [field("email", "email")]


def test_operator_removes_field_package_leaves_it_unchanged_respects_removal():
    base = schema([field("id"), field("legacy_flag")])
    mine = schema([field("id")])
    theirs = schema([field("id"), field("legacy_flag")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert [f["name"] for f in merged["fields"]] == ["id"]


def test_operator_removes_field_package_changes_it_collides():
    base = schema([field("id"), field("legacy_flag", "text")])
    mine = schema([field("id")])
    theirs = schema([field("id"), field("legacy_flag", "boolean")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == ["legacy_flag"]
    # No shared decision -> the shipped (theirs) version surfaces for review.
    assert [f["name"] for f in merged["fields"]] == ["id", "legacy_flag"]
    assert merged["fields"][1]["type"] == "boolean"


def test_package_removes_field_operator_kept_is_never_silently_dropped():
    base = schema([field("id"), field("legacy_flag")])
    mine = schema([field("id"), field("legacy_flag")])
    theirs = schema([field("id")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert {f["name"] for f in merged["fields"]} == {"id", "legacy_flag"}


def test_package_adds_new_field_with_no_base_ancestor():
    base = schema([field("id")])
    mine = schema([field("id")])
    theirs = schema([field("id"), field("status")])

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert [f["name"] for f in merged["fields"]] == ["id", "status"]


def test_merged_schema_preserves_theirs_metadata_outside_fields():
    base = schema([field("id")])
    mine = schema([field("id"), field("x_a")])
    theirs = {"name": "contacts", "title": "Contacts", "version": 2, "fields": [field("id")]}

    merged, collisions = object_schemas.merge_schema_fields(base, mine, theirs)

    assert collisions == []
    assert merged["title"] == "Contacts"
    assert merged["version"] == 2
    assert [f["name"] for f in merged["fields"]] == ["id", "x_a"]
