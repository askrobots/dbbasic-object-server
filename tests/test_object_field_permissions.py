import object_field_permissions
import object_permissions


def test_field_access_uses_role_map_and_most_restrictive_match():
    policy = object_permissions.PermissionPolicy(
        roles={"admin": {}, "sales": {}},
        user_roles={"7": ("sales", "admin")},
    )
    subject = object_permissions.PermissionSubject(user_id="7")
    field = {
        "name": "margin",
        "permissions": {
            "admin": "edit",
            "sales": "hidden",
        },
    }

    access = object_field_permissions.field_access(field, subject=subject, policy=policy)

    assert access == object_field_permissions.HIDDEN


def test_field_access_supports_grouped_access_principals_and_default():
    policy = object_permissions.PermissionPolicy()
    owner = object_permissions.PermissionSubject(user_id="7")
    viewer = object_permissions.PermissionSubject(user_id="8", roles=("viewer",))
    field = {
        "name": "notes",
        "permissions": {
            "edit": ["owner"],
            "read": ["role:viewer"],
            "default": "hidden",
        },
    }

    assert object_field_permissions.field_access(
        field,
        subject=owner,
        policy=policy,
        record={"owner_id": "7"},
    ) == object_field_permissions.EDIT
    assert object_field_permissions.field_access(
        field,
        subject=viewer,
        policy=policy,
        record={"owner_id": "7"},
    ) == object_field_permissions.READ
    assert object_field_permissions.field_access(
        field,
        subject=object_permissions.PermissionSubject.anonymous(),
        policy=policy,
        record={"owner_id": "7"},
    ) == object_field_permissions.HIDDEN


def test_denied_write_fields_only_checks_known_schema_fields(tmp_path):
    data_dir = tmp_path / "data"
    schema = data_dir / "schemas" / "invoices.json"
    schema.parent.mkdir(parents=True)
    schema.write_text(
        """
        {
          "fields": [
            {"name": "id"},
            {"name": "memo", "permissions": {"sales": "edit"}},
            {"name": "total", "permissions": {"sales": "read"}},
            {"name": "margin", "permissions": {"sales": "hidden"}}
          ]
        }
        """
    )
    subject = object_permissions.PermissionSubject(user_id="7", roles=("sales",))
    policy = object_permissions.PermissionPolicy()

    denied = object_field_permissions.denied_write_fields(
        "invoices",
        ["id", "memo", "total", "margin", "custom"],
        subject=subject,
        policy=policy,
        base_dir=data_dir,
    )

    assert denied == ["margin", "total"]
