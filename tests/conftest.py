import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGES = ROOT / "packages"


@pytest.fixture(autouse=True)
def isolated_default_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))


# --- shared TSV fixture staging ---------------------------------------------
#
# A cluster of tests build a data_dir by hand: copy a real package schema
# into schemas/, then write collections/<name>/records.tsv themselves. Many
# of them used to hand-type that header as a literal tab-separated string.
# That string has no connection to the schema it claims to describe, so
# when a field is added to the real schema, the hand-typed header silently
# stops matching field order -- the test keeps passing while exercising a
# column layout that no longer exists in production. schema_header() and
# stage_collection() derive the header from the schema itself so drift is
# structurally impossible.
#
# These are plain functions, not fixtures: `from conftest import
# schema_header, stage_collection` at each call site, so a test's fixture
# setup still reads as ordinary, narrative code rather than disappearing
# behind fixture injection.

def schema_header(package_id, collection):
    """The TSV header line (with trailing newline) for one package
    collection, in the exact field order its real schema declares."""
    schema = json.loads(
        (PACKAGES / package_id / "schemas" / f"{collection}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def stage_collection(data_dir, package_id, collection, *, rows="", seed=False):
    """Stage one real package collection under `data_dir`: copy its schema
    JSON into schemas/, then create collections/<collection>/records.tsv.

    seed=True copies the package's seed/<collection>.tsv verbatim -- for
    tests that need the seeded reference rows themselves (e.g.
    denominations), not just an empty, correctly-shaped collection.
    Otherwise the file is schema_header() plus `rows`, a caller-supplied
    TSV body already in schema field order (empty for a collection a test
    only needs to exist).
    """
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / f"{collection}.json").write_text(
        (PACKAGES / package_id / "schemas" / f"{collection}.json").read_text())

    coll_dir = data_dir / "collections" / collection
    coll_dir.mkdir(parents=True, exist_ok=True)
    records_path = coll_dir / "records.tsv"
    if seed:
        records_path.write_text(
            (PACKAGES / package_id / "seed" / f"{collection}.tsv").read_text())
    else:
        records_path.write_text(schema_header(package_id, collection) + rows)
    return records_path
