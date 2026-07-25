"""Value accounts (plan/value-accounts-and-denominations-spec.md §3, §5):
the places value actually sits, and how much a reconciliation of each is
worth.

The schema is doing real work here, so it is worth testing directly: the
difference between a bank account and a box of cash is not cosmetic, and
the assurance ladder is the part a fraud would exploit if we flattened it.
"""

import json
import pathlib

import object_money
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
BANKING = PACKAGES / "app-banking"
FINANCE = PACKAGES / "app-finance"


def _header(pkg_dir, name):
    schema = json.loads((pkg_dir / "schemas" / f"{name}.json").read_text())
    return "\t".join(f["name"] for f in schema["fields"]) + "\n"


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    for pkg_dir, name, seeded in ((BANKING, "value_accounts", False),
                                  (FINANCE, "denominations", True),
                                  (FINANCE, "fin_accounts", False)):
        (schema_dir / f"{name}.json").write_text(
            (pkg_dir / "schemas" / f"{name}.json").read_text())
        coll = data_dir / "collections" / name
        coll.mkdir(parents=True)
        if seeded:
            coll.joinpath("records.tsv").write_text(
                (pkg_dir / "seed" / f"{name}.tsv").read_text())
        else:
            coll.joinpath("records.tsv").write_text(_header(pkg_dir, name))
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    object_records.create_collection_record(
        "fin_accounts", {"id": "acct-cash", "name": "Cash", "account_type": "asset",
                         "owner_id": "dan"}, base_dir=data_dir)
    return data_dir


def make_account(data_dir, aid, **fields):
    record = {"id": aid, "name": fields.pop("name", aid), "owner_id": "dan"}
    record.update(fields)
    return object_records.create_collection_record(
        "value_accounts", record, base_dir=data_dir)


def test_the_four_custody_shapes_all_store(tmp_path, monkeypatch):
    """One collection has to hold a bank account, a hardware wallet, a till
    and a gift card without any of them being a special case."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_account(data_dir, "va-bank", name="Business Checking", kind="bank",
                 denomination_id="den-usd", fin_account_id="acct-cash",
                 custody="institution", custodian="Chase", reference="1004",
                 verification="statement_import")
    make_account(data_dir, "va-btc", name="Cold Wallet", kind="crypto",
                 denomination_id="den-btc", fin_account_id="acct-cash",
                 custody="self", custodian="Ledger in the safe",
                 reference="bc1qexampleaddress", verification="chain_query",
                 last_proven_control_at="2026-07-01")
    make_account(data_dir, "va-till", name="Front Register", kind="cash_box",
                 denomination_id="den-usd", fin_account_id="acct-cash",
                 custody="self", custodian="front register",
                 verification="physical_count", requires_second_attestor="true",
                 managed_by="pat")
    make_account(data_dir, "va-gift", name="Acme Gift Cards", kind="gift_card",
                 denomination_id="den-usd", fin_account_id="acct-cash",
                 custody="third_party", custodian="Acme",
                 verification="issuer_balance")

    rows = {r["id"]: r for r in
            object_records.read_collection_records("value_accounts", base_dir=data_dir)}
    assert len(rows) == 4
    assert rows["va-btc"]["custody"] == "self"
    assert rows["va-btc"]["last_proven_control_at"] == "2026-07-01"
    assert rows["va-till"]["requires_second_attestor"] == "true"
    # managed_by is separate from owner_id on purpose: the person who
    # reconciles the register need not be the person who can move a wire.
    assert rows["va-till"]["managed_by"] == "pat"
    assert rows["va-till"]["owner_id"] == "dan"


def test_assurance_is_not_flat_across_verification_methods():
    """The ladder that stops the system telling a comfortable lie."""
    assert object_money.assurance_for("chain_query") == "strong"
    assert object_money.assurance_for("statement_import") == "strong"
    assert object_money.assurance_for("issuer_balance") == "medium"
    # Self-certification: the counter is usually the person who could take.
    assert object_money.assurance_for("physical_count") == "weak"
    assert object_money.assurance_for("none") == "none"
    assert object_money.assurance_for("") == "none"


def test_a_witness_lifts_a_self_counted_till(tmp_path, monkeypatch):
    """A second attestor is the oldest control there is against the count
    and the custody being the same pair of hands."""
    assert object_money.assurance_for("physical_count", witnessed=True) == "medium"
    # Witnessing does not upgrade evidence that was already independent.
    assert object_money.assurance_for("statement_import", witnessed=True) == "strong"
    assert object_money.assurance_for("none", witnessed=True) == "none"


def test_every_verification_in_the_schema_has_a_ranking(tmp_path, monkeypatch):
    """A verification method the ladder does not know would silently rank as
    no evidence at all, which is exactly the kind of quiet default that
    makes a control useless."""
    schema = json.loads((BANKING / "schemas" / "value_accounts.json").read_text())
    by_name = {f["name"]: f for f in schema["fields"]}
    for method in by_name["verification"]["enum"]:
        assert method in object_money.ASSURANCE_BY_VERIFICATION, method


def test_denomination_drives_how_a_balance_reads(tmp_path, monkeypatch):
    """The same integer means different money in different accounts, which
    is the whole reason denomination lives on the account."""
    data_dir = setup_env(tmp_path, monkeypatch)
    make_account(data_dir, "va-bank", kind="bank", denomination_id="den-usd")
    make_account(data_dir, "va-btc", kind="crypto", denomination_id="den-btc")
    assert object_money.format_amount(5000000, "USD", base_dir=data_dir) == "$50,000.00 USD"
    assert object_money.format_amount(5000000, "BTC", base_dir=data_dir) == "0.05000000 BTC"


def test_schema_declares_the_fields_the_spec_requires(tmp_path, monkeypatch):
    schema = json.loads((BANKING / "schemas" / "value_accounts.json").read_text())
    by_name = {f["name"]: f for f in schema["fields"]}
    for field in ("kind", "denomination_id", "fin_account_id", "custody",
                  "custodian", "verification", "managed_by",
                  "requires_second_attestor", "last_proven_control_at"):
        assert field in by_name, field
    assert by_name["denomination_id"]["relation"]["collection"] == "denominations"
    assert by_name["fin_account_id"]["relation"]["collection"] == "fin_accounts"
    assert "cash_box" in by_name["kind"]["enum"]
    assert "self" in by_name["custody"]["enum"]
    assert "chain_query" in by_name["verification"]["enum"]
