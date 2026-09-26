"""packages/app-mail-sync: what it declares, and the lines it must not cross.

The declarations are the product here as much as the code: a schedule that
lives only on a running box vanishes with the box, a password field on a
schema ends up in every backup, and a transport field on app-messaging's
schemas is the scope creep app-messaging's own tests refuse. Each is held
by a test, asserted on the manifest and schemas themselves.
"""

import json
import re
from pathlib import Path

import object_packages
import object_permissions
import object_schemas
import object_state

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
PACKAGE_DIR = PACKAGES_ROOT / "app-mail-sync"
COLLECTIONS = ("mail_accounts", "mail_sync_state", "mail_synced_messages")


def _manifest():
    return json.loads((PACKAGE_DIR / "dbbasic-package.json").read_text())


def _schema(name):
    return json.loads((PACKAGE_DIR / "schemas" / f"{name}.json").read_text())


def _policy():
    rules = json.loads((PACKAGE_DIR / "permissions" / "rules.json").read_text())["rules"]
    return object_permissions.policy_from_dict({"access_mode": "role_based", "rules": rules})


def test_the_manifest_normalizes_and_depends_on_app_messaging():
    package = object_packages.get_package("app-mail-sync", root=PACKAGES_ROOT)
    assert {s["collection"] for s in package["schemas"]} == set(COLLECTIONS)
    assert {o["id"] for o in package["objects"]} == {"system_mail_sync", "system_mail_sync_attention",
                                                    "site_mail_accounts"}
    assert package["dependencies"] == [{"id": "app-messaging", "version": None}]


def test_the_sync_is_a_declared_schedule_so_a_rebuilt_box_keeps_it():
    schedules = _manifest()["schedules"]
    assert [(s["id"], s["object_id"], s["schedule"]) for s in schedules] == [
        ("mail_sync_every_two_minutes", "system_mail_sync", "*/2 * * * *")]


def test_failing_accounts_are_a_declared_attention_queue():
    attention = _manifest()["attention"]
    assert [(a["id"], a["object_id"], a["path"], a["severity"]) for a in attention] == [
        ("mail_accounts_failing", "system_mail_sync_attention", "/mail/accounts",
         "warning")]


def test_install_registers_schemas_objects_and_the_schedule(tmp_path):
    data_dir = tmp_path / "data"
    objects = tmp_path / "objects"
    objects.mkdir()
    for dependency in ("app-views", "app-messaging"):
        object_packages.install_package(dependency, root=PACKAGES_ROOT, base_dir=data_dir,
                                        object_roots=[objects])
    plan = object_packages.dry_run_package("app-mail-sync", root=PACKAGES_ROOT,
                                           base_dir=data_dir, object_roots=[objects])
    assert plan["safe_to_install"] is True, plan
    object_packages.install_package("app-mail-sync", root=PACKAGES_ROOT, base_dir=data_dir,
                                    object_roots=[objects])
    for name in COLLECTIONS:
        assert object_schemas.get_schema(name, base_dir=data_dir)["name"] == name
    assert (objects / "system" / "mail_sync.py").is_file()
    assert (objects / "site" / "mail_accounts.py").is_file()
    state = object_state.get_object_state("scheduler", base_dir=data_dir)
    task = json.loads(state["task_mail_sync_every_two_minutes"])
    assert task["object_id"] == "system_mail_sync" and task["status"] == "active"


def test_no_schema_has_a_field_that_could_hold_a_secret():
    """The password lives in the owner's write-only vault. A field here
    would put it in exports, backups and every API read."""
    for name in COLLECTIONS:
        fields = {f["name"] for f in _schema(name)["fields"]}
        assert not {f for f in fields if re.search(r"pass|secret|token|credential|api_key", f)}, name


def test_sync_rows_are_append_and_the_cursor_is_classic():
    assert _schema("mail_synced_messages").get("storage") == "append"
    assert "storage" not in _schema("mail_sync_state")


def test_seeds_are_header_only_and_match_the_schemas():
    for name in COLLECTIONS:
        lines = (PACKAGE_DIR / "seed" / f"{name}.tsv").read_text().splitlines()
        assert lines == ["\t".join(f["name"] for f in _schema(name)["fields"])]


def test_the_transport_facts_live_here_not_on_app_messaging():
    """app-messaging's schemas stay a mailbox; its own test bans imap_uid
    and friends there. This package is where they go instead."""
    fields = {f["name"] for f in _schema("mail_synced_messages")["fields"]}
    assert {"account_id", "folder", "uid", "message_id", "dedupe_key", "references",
            "list_unsubscribe", "list_unsubscribe_post", "list_id", "precedence"} <= fields
    for schema_path in (PACKAGES_ROOT / "app-messaging" / "schemas").glob("*.json"):
        names = {f["name"] for f in json.loads(schema_path.read_text())["fields"]}
        assert not names & {"uid", "folder", "account_id", "dedupe_key", "list_unsubscribe"}


def test_each_person_sees_only_their_own_accounts_and_sync_rows():
    policy = _policy()
    me = object_permissions.PermissionSubject(user_id="7")
    other = object_permissions.PermissionSubject(user_id="8")
    for name in COLLECTIONS:
        mine = {"owner_id": "7", "account_id": "a1", "message_id": "m1"}
        assert object_permissions.check_permission(
            me, object_permissions.READ, policy=policy, collection=name, record=mine).allowed
        assert not object_permissions.check_permission(
            other, object_permissions.READ, policy=policy, collection=name, record=mine).allowed
        assert not object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=name, record=mine).allowed


def test_only_the_sync_writes_the_cursor_and_the_transport_rows():
    """A hand-edited cursor skips or repeats mail."""
    policy = _policy()
    me = object_permissions.PermissionSubject(user_id="7")
    for name in ("mail_sync_state", "mail_synced_messages"):
        for action in (object_permissions.CREATE, object_permissions.UPDATE,
                       object_permissions.DELETE):
            assert not object_permissions.check_permission(
                me, action, policy=policy, collection=name,
                record={"owner_id": "7"}).allowed, (name, action)
    assert object_permissions.check_permission(
        me, object_permissions.UPDATE, policy=policy, collection="mail_accounts",
        record={"owner_id": "7"}).allowed


def test_no_internal_names_leak_into_the_package():
    banned = re.compile("|".join([r"\b" + "q" + "9" + r"\b", "ask" + "robots"]), re.IGNORECASE)
    for path in PACKAGE_DIR.rglob("*"):
        if path.is_file():
            assert not banned.search(path.read_text(errors="ignore")), path
