"""Stage-6 retrofit of packages/app-finance: the bespoke site_journal_view.py
page is replaced by a seeded 59 detail view composed of three GENERIC block
renderers -- an editable+deletable `detail` block (fin_journals; the
draft->posted transition becomes a status-field edit in the owner's edit
form, guarded by schemas/fin_journals.json's transitions, not a one-click
Post button -- same make-it-basic trade as app-forum's pin/lock), a
`related` block (fin_journal_lines by journal_id -- an append-only,
RELATIONAL event-log of ledger postings, never embedded), and a `form`
block with journal_id locked to the page's journal (Add Posting).
Debit/credit *_cents render through /form's own cents formatting on the
related list -- no custom code. The bespoke page's client-computed
total-debits/total-credits/is-balanced summary strip has no generic block
equivalent and is intentionally not carried forward (see
dbbasic-package.json's description).
"""

import csv
import json
from pathlib import Path

import object_packages
import object_permissions

PACKAGES_ROOT = Path(__file__).resolve().parents[1] / "packages"
APP_FINANCE_DIR = PACKAGES_ROOT / "app-finance"


def _seed_rows(name):
    with open(APP_FINANCE_DIR / "seed" / f"{name}.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def test_manifest_drops_bespoke_journal_page_and_seeds_the_detail_view():
    package = object_packages.get_package("app-finance", root=PACKAGES_ROOT)
    assert {obj["id"] for obj in package["objects"]} == {
        "site_accounts", "site_journals", "site_trial_balance", "site_setup_accounts",
    }
    assert {entry["collection"] for entry in package["seed"]} == {
        "fin_accounts", "fin_journals", "fin_journal_lines", "fin_recurring",
        "views", "site_routes",
    }
    assert {dep["id"] for dep in package["dependencies"]} == {"app-entities", "app-views"}


def test_journal_view_object_file_is_deleted():
    assert not (APP_FINANCE_DIR / "objects" / "site" / "journal_view.py").exists()


def test_journal_view_composes_detail_related_and_fk_locked_form():
    rows = _seed_rows("views")
    assert len(rows) == 1
    view = rows[0]
    assert view["id"] == "view_journal_detail"
    assert view["route"] == "/journals/{journal_id:uuid}"
    blocks = json.loads(view["blocks"])
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["detail", "related", "form"]

    detail, related, form = blocks

    # 1. editable+deletable journal detail (status edit replaces the
    # bespoke one-click draft->posted Post button)
    assert detail["collection"] == "fin_journals"
    assert detail["record_id"] == "$record_id"
    assert detail["editable"] is True and detail["deletable"] is True
    assert detail["delete_redirect"] == "/journals"

    # 2. postings, relational (fin_journal_lines by journal_id -- not embedded)
    assert related["collection"] == "fin_journal_lines"
    assert related["fk_field"] == "journal_id"
    assert related["match"] == "$record_id"
    assert related["title"] == "Postings"

    # 3. add-posting form, journal_id locked to the page's journal
    assert form["collection"] == "fin_journal_lines"
    assert form["fixed"] == {"journal_id": "$record_id"}
    assert form["title"] == "Add Posting"


def test_journal_route_points_to_view_render_and_setup_accounts_route_survives():
    rows = _seed_rows("site_routes")
    # the setup-accounts route (65 slice 4) must still be first -- this
    # retrofit APPENDS, it never overwrites the existing site_routes.tsv.
    assert rows[0]["pattern"] == "/finance/setup-accounts"
    assert rows[0]["object_id"] == "site_setup_accounts"
    assert len(rows) == 2
    assert rows[1]["id"] == "route_journal_detail"
    assert rows[1]["pattern"] == "/journals/{journal_id:uuid}"
    assert rows[1]["object_id"] == "site_view_render"
    assert rows[1]["priority"] == "10"


def test_permissions_no_longer_reference_the_removed_object():
    payload = json.loads((APP_FINANCE_DIR / "permissions" / "rules.json").read_text())
    object_ids = {rule.get("object_id") for rule in payload["rules"]}
    assert "site_journal_view" not in object_ids
    assert "site_accounts" in object_ids
    assert "site_journals" in object_ids
    assert "site_trial_balance" in object_ids
    assert "site_setup_accounts" in object_ids


def test_no_public_read_survives_the_retrofit():
    """Journals/postings stay owner-private: the retrofit must not have
    added a public read rule on any collection."""
    payload = json.loads((APP_FINANCE_DIR / "permissions" / "rules.json").read_text())
    policy = object_permissions.policy_from_dict(
        {"access_mode": "role_based", "rules": payload["rules"]}
    )
    for collection in ("fin_accounts", "fin_journals", "fin_journal_lines", "fin_recurring"):
        decision = object_permissions.check_permission(
            None, object_permissions.READ, policy=policy, collection=collection,
            record={"owner_id": "7"},
        )
        assert decision.allowed is False, f"anonymous read should be denied on {collection}"


def test_dry_run_is_safe(tmp_path):
    object_root = tmp_path / "objects"
    object_root.mkdir()
    plan = object_packages.dry_run_package(
        "app-finance", root=PACKAGES_ROOT, base_dir=tmp_path / "data", object_roots=[object_root],
    )
    assert plan["safe_to_install"] is True
    assert plan["warnings"] == []
