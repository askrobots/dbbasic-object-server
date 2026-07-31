"""The capability layer, actually used.

`capabilities` exists so a collection gains a comment thread, an
attachment list or record sharing by declaring a flag -- no per-app code,
no per-app table, one line in the detail renderer. It shipped with
`tasks` as its only user, and stayed that way: ONE collection out of 98,
which makes a uniform layer look like a feature of one app.

This file is the layer being used, and every choice in it is a judgement
about where the widget earns its place -- not a sweep. Two kinds of
restraint matter as much as the additions:

**Never beside a layer that already owns the job.** projects get
attachments and comments but NOT `shareable`, because project_access
already grants project sharing and two grant paths on one record is how
"who can see this" stops having an answer. contacts get attachments but
NOT comments, because `interactions` already records what was said to
this person and a second thread would split that history in half.

**Never where the audience is different.** articles could take comments,
but public commentary is a different permission model than an internal
thread, and quietly mounting one where the other is meant is worse than
having neither.
"""

import json
import pathlib

import pytest

PACKAGES = pathlib.Path(__file__).resolve().parents[1] / "packages"


def schema(package, collection):
    return json.loads(
        (PACKAGES / package / "schemas" / f"{collection}.json").read_text())


EXPECTED = {
    ("app-invoices", "invoices"): {"comments", "attachments"},
    ("app-orders", "orders"): {"comments", "attachments"},
    ("app-projects", "projects"): {"comments", "attachments"},
    ("app-finance", "fin_journals"): {"comments", "attachments"},
    ("app-notes", "notes"): {"attachments", "shareable"},
    ("app-contacts", "contacts"): {"attachments"},
    ("app-tasks", "tasks"): {"comments", "attachments", "shareable"},
}


@pytest.mark.parametrize("package,collection", sorted(EXPECTED))
def test_the_declared_capabilities_are_what_was_decided(package, collection):
    caps = schema(package, collection).get("capabilities") or {}
    assert {name for name, on in caps.items() if on} == EXPECTED[(package, collection)]
    assert all(value is True for value in caps.values()), \
        "a capability is present-and-true or absent; false is a third state " \
        "the renderer does not read and nobody can see"


def test_projects_do_not_gain_a_second_sharing_path():
    """project_access already grants project sharing. Two grant paths on
    one record is how an access question becomes unanswerable."""
    assert "shareable" not in (schema("app-projects", "projects")
                               .get("capabilities") or {})


def test_contacts_do_not_gain_a_second_history():
    """`interactions` records what was said to this person; a comment
    thread beside it splits that history in half."""
    assert "comments" not in (schema("app-contacts", "contacts")
                              .get("capabilities") or {})


@pytest.mark.parametrize("package,collection", sorted(EXPECTED))
def test_every_capability_collection_has_a_detail_page_to_mount_into(
        package, collection):
    """A flag on a collection with no generative detail view mounts
    nowhere: `maybeMountCapabilities` runs from the detail block. A
    capability that renders nowhere is worse than an absent one, because
    the schema claims a behaviour the product does not have."""
    seeds = list((PACKAGES / package / "seed").glob("views.tsv"))
    assert seeds, f"{package} ships no views seed"
    text = seeds[0].read_text()
    assert '"detail"' in text and collection in text, \
        f"{collection} declares capabilities but seeds no detail view"


@pytest.mark.parametrize("package,collection", sorted(EXPECTED))
def test_the_schema_version_was_bumped_with_the_change(package, collection):
    """Capabilities are an additive schema change and the version is how a
    deployed box knows it changed at all."""
    assert int(schema(package, collection).get("version", 0)) >= 2


def test_the_documented_set_is_the_implemented_set():
    """docs/capabilities.md is a contract: a fourth flag documented but
    unmounted, or mounted but undocumented, is the drift this catches."""
    doc = (PACKAGES.parent / "docs" / "capabilities.md").read_text()
    renderer = (PACKAGES / "app-views" / "objects" / "site"
                / "view_render.py").read_text()
    for flag in ("comments", "attachments", "shareable"):
        assert f"`{flag}`" in doc, flag
        assert f"caps.{flag}" in renderer, flag

    declared = {name for caps in
                (schema(p, c).get("capabilities") or {} for p, c in EXPECTED)
                for name in caps}
    assert declared == {"comments", "attachments", "shareable"}
