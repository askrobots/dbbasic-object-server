"""Stage-6 retrofit of packages/app-messaging: the bespoke
site_message_thread.py page is replaced by a seeded 59 detail view composed
of four GENERIC block renderers -- an editable+deletable `detail` block (the
thread; star/archive/trash/read toggles become boolean edits in the owner's
edit form, not one-click buttons), a `related` block (FLAT messages by
thread_id -- messages were never nested/self-threaded, unlike app-forum's
replies), a `markdown` block carrying the "nothing is sent" note, and a
`form` block with a FK locked to the page's thread (Save Draft). A mailbox
stays owner-private throughout: the row-filtered collection API only ever
returns real data to the signed-in owner, same as the page it replaces.
"""

import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_MESSAGING_DIR = PACKAGES_ROOT / "app-messaging"


def _seed_rows(name):
    import csv

    with open(APP_MESSAGING_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_bespoke_thread_page_and_seeds_the_detail_view():
    package = object_packages.get_package("app-messaging", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {"site_inbox"}
    assert {entry["collection"] for entry in package["seed"]} == {
        "message_threads", "messages", "message_recipients", "message_drafts",
        "views", "site_routes",
    }
    assert {dep["id"] for dep in package["dependencies"]} == {"app-views"}


def test_message_thread_object_file_is_deleted():
    assert not (APP_MESSAGING_DIR / "objects" / "site" / "message_thread.py").exists()


def test_thread_view_composes_detail_flat_related_markdown_and_fk_locked_form():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_inbox_thread"
    assert view["route"] == "/inbox/{thread_id}"
    blocks = json.loads(view["blocks"])
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["detail", "related", "markdown", "form"]

    detail, related, markdown, form = blocks

    # 1. editable+deletable thread detail
    assert detail["collection"] == "message_threads"
    assert detail["record_id"] == "$record_id"
    assert detail["editable"] is True and detail["deletable"] is True
    assert detail["delete_redirect"] == "/inbox"

    # 2. FLAT messages (a plain related list by thread_id -- no nesting)
    assert related["collection"] == "messages"
    assert related["fk_field"] == "thread_id"
    assert related["match"] == "$record_id"

    # 3. the "nothing is sent" note preserved as a markdown block
    assert "nothing is sent" in markdown["text"]
    assert "IMAP" in markdown["text"] and "SMTP" in markdown["text"]

    # 4. draft compose with thread_id locked to the page's thread
    assert form["collection"] == "message_drafts"
    assert form["fixed"] == {"thread_id": "$record_id"}
    assert form["title"] == "Save Draft"


def test_permalink_route_points_to_view_render():
    rows = _seed_rows("site_routes")
    assert len(rows) == 1
    assert rows[0]["pattern"] == "/inbox/{thread_id}"
    assert rows[0]["object_id"] == "site_view_render"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_MESSAGING_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_message_thread" not in object_ids
    assert "site_inbox" in object_ids


def test_no_public_read_survives_the_retrofit():
    """A mailbox stays private: the retrofit must not have added a public
    read rule on any collection, and the removed object's public-execute
    rule must be gone too (already covered above)."""
    payload = json.loads((APP_MESSAGING_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )
    for collection in ("message_threads", "messages", "message_recipients", "message_drafts"):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection,
            record={"owner_id": "7"},
        )
        assert decision.allowed is False, f"anonymous read should be denied on {collection}"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-messaging", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
