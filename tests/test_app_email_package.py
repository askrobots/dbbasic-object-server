"""Structural tests for packages/app-email (01 email adapter): the email_outbox
queue schema + its permission posture. The delivery engine is object_email +
the daemon pass (see test_object_email); this package ships schema + rules
only, no pages."""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_EMAIL_DIR = PACKAGES_ROOT / "app-email"


def test_manifest_and_schema():
    package = object_packages.get_package("app-email", root=PACKAGES_ROOT)
    assert package["id"] == "app-email"
    assert {s["collection"] for s in package["schemas"]} == {"email_outbox"}
    assert package["objects"] == []  # no pages -- platform enabler
    assert package["seed"] == []     # the queue starts empty
    schema = json.loads((APP_EMAIL_DIR / "schemas" / "email_outbox.json").read_text())
    assert schema["storage"] == "append"  # write-hot: a row per attempt/transition
    names = {f["name"] for f in schema["fields"]}
    assert {"to", "subject", "text_body", "status", "attempts", "max_attempts",
            "last_error", "next_attempt_at", "sent_at", "extra"} <= names


def test_outbox_is_admin_read_only_and_closed_to_writes():
    payload = json.loads((APP_EMAIL_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict({"access_mode": "role_based", "rules": payload["rules"]})
    admin = object_permissions.PermissionSubject(user_id="1", roles=("admin",))
    plain = object_permissions.PermissionSubject(user_id="7")
    # admin may read (bypass also allows it); nobody gets a create/update rule,
    # so the queue can never be misconfigured into a public relay
    assert object_permissions.check_permission(admin, object_permissions.READ, policy=policy, collection="email_outbox").allowed is True
    assert object_permissions.check_permission(plain, object_permissions.READ, policy=policy, collection="email_outbox").allowed is False
    for action in (object_permissions.CREATE, object_permissions.UPDATE, object_permissions.DELETE):
        assert object_permissions.check_permission(plain, action, policy=policy, collection="email_outbox").allowed is False


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-email", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root])
    assert plan["safe_to_install"] is True and plan["warnings"] == []
