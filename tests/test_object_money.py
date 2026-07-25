"""Money representation (plan/value-accounts-and-denominations-spec.md §1):
an amount is an integer in its denomination's smallest unit.

The cases that matter are the ones the predecessor system's fixed
Decimal(28,8) column could not express: yen with no minor unit at all,
ether with eighteen places, and the refusal to silently round away
precision a denomination cannot hold.
"""

import json
import pathlib

import pytest

import object_money
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
FINANCE = PACKAGES / "app-finance"


def setup_env(tmp_path, monkeypatch, *, seed=True):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    (schema_dir / "denominations.json").write_text(
        (FINANCE / "schemas" / "denominations.json").read_text())
    coll = data_dir / "collections" / "denominations"
    coll.mkdir(parents=True)
    seed_text = (FINANCE / "seed" / "denominations.tsv").read_text()
    header = seed_text.splitlines()[0] + "\n"
    (coll / "records.tsv").write_text(seed_text if seed else header)
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


# --- scale resolution ---------------------------------------------------------

def test_seeded_denominations_carry_the_right_scales(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    for code, scale in (("USD", 2), ("JPY", 0), ("BTC", 8), ("ETH", 18),
                        ("USDC", 6), ("XAU", 4)):
        assert object_money.scale_for(code, base_dir=data_dir) == scale, code
    # Case-insensitive, like the schema says.
    assert object_money.scale_for("btc", base_dir=data_dir) == 8


def test_scale_falls_back_without_the_collection_and_never_raises(tmp_path, monkeypatch):
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    assert object_money.scale_for("USD", base_dir=data_dir) == 2
    assert object_money.scale_for("BTC", base_dir=data_dir) == 8
    # An unknown code renders as if it were dollars rather than exploding:
    # a missing denomination is a display problem, not a reason to 500.
    assert object_money.scale_for("WAT", base_dir=data_dir) == 2
    assert object_money.scale_for("", base_dir=data_dir) == 2


def test_operator_defined_denominations_win_over_the_fallback(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    object_records.create_collection_record(
        "denominations",
        {"id": "den-points", "code": "POINTS", "name": "Loyalty Points",
         "kind": "points", "scale": "0", "owner_id": "dan"},
        base_dir=data_dir)
    assert object_money.scale_for("POINTS", base_dir=data_dir) == 0


# --- conversion ---------------------------------------------------------------

def test_to_minor_and_back_across_scales():
    assert object_money.to_minor("1500.00", 2) == 150000
    assert object_money.to_minor("1,500", 2) == 150000       # grouped input
    assert object_money.to_minor("1500", 0) == 1500          # JPY: no minor unit
    assert object_money.to_minor("0.05", 8) == 5000000       # satoshis
    assert object_money.to_minor("", 2) == 0
    assert str(object_money.from_minor(150000, 2)) == "1500.00"
    assert str(object_money.from_minor(5000000, 8)) == "0.05000000"


def test_eighteen_decimals_survive_exactly(tmp_path, monkeypatch):
    """The case the predecessor's fixed 8-decimal column could not hold:
    one wei is a real amount and must round-trip without loss."""
    one_wei = object_money.to_minor("0.000000000000000001", 18)
    assert one_wei == 1
    assert str(object_money.from_minor(one_wei, 18)) == "1E-18"
    big = object_money.to_minor("1234567.123456789012345678", 18)
    assert object_money.from_minor(big, 18) == __import__("decimal").Decimal(
        "1234567.123456789012345678")


def test_precision_that_does_not_fit_is_refused_not_rounded():
    """Silently turning half a cent into a cent is how a rounding error
    becomes a real number at scale, so to_minor refuses and the caller must
    ask for rounding explicitly."""
    with pytest.raises(object_money.MoneyError):
        object_money.to_minor("0.005", 2)
    with pytest.raises(object_money.MoneyError):
        object_money.to_minor("1500.5", 0)          # yen has no half
    assert object_money.quantize_minor("0.005", 2) == 1      # deliberate
    assert object_money.quantize_minor("0.004", 2) == 0
    with pytest.raises(object_money.MoneyError):
        object_money.to_minor("not a number", 2)


def test_negative_amounts_round_trip():
    assert object_money.to_minor("-25.00", 2) == -2500
    assert str(object_money.from_minor(-2500, 2)) == "-25.00"


# --- display and entry --------------------------------------------------------

def test_formatting_uses_the_denominations_scale_and_symbol(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    assert object_money.format_amount(150000, "USD", base_dir=data_dir) == "$1,500.00 USD"
    assert object_money.format_amount(1500, "JPY", base_dir=data_dir) == "¥1,500 JPY"
    # Trailing zeros are kept: "0.05" would hide whether the remaining six
    # places are zero or merely unshown, and for BTC that is real money.
    assert object_money.format_amount(5000000, "BTC", base_dir=data_dir) == "0.05000000 BTC"
    assert object_money.format_amount(150000, "USD", base_dir=data_dir,
                                      with_code=False) == "$1,500.00"


def test_parse_amount_accepts_what_format_produces(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    for minor, code in ((150000, "USD"), (1500, "JPY"), (5000000, "BTC")):
        rendered = object_money.format_amount(minor, code, base_dir=data_dir)
        assert object_money.parse_amount(rendered, code, base_dir=data_dir) == minor


def test_denominations_of_different_kinds_are_never_summed():
    """A total of dollars and satoshis is not a number. Combining them
    requires converting through a rate stamped at the transaction moment
    (docs/logic-decisions.md #1), which is a deliberate act, not an
    accidental addition."""
    assert object_money.same_denomination("USD", "USD", "usd") is True
    assert object_money.same_denomination("USD", "BTC") is False
    assert object_money.same_denomination("USD", "") is True   # blank = unspecified


# --- the collection itself ----------------------------------------------------

def test_seed_matches_the_schema_and_covers_each_kind(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    schema = json.loads((FINANCE / "schemas" / "denominations.json").read_text())
    header = (FINANCE / "seed" / "denominations.tsv").read_text().splitlines()[0].split("\t")
    assert header == [f["name"] for f in schema["fields"]]

    rows = object_records.read_collection_records("denominations", base_dir=data_dir)
    by_code = {r["code"]: r for r in rows}
    assert {"USD", "JPY", "BTC", "ETH", "USDC", "XAU"} <= set(by_code)
    assert by_code["BTC"]["kind"] == "crypto"
    assert by_code["USDC"]["kind"] == "stablecoin"
    assert by_code["XAU"]["kind"] == "metal"
    assert all(r["is_system"] == "true" for r in rows)
    # Every seeded scale must be one object_money can actually use.
    for row in rows:
        assert 0 <= int(row["scale"]) <= object_money.MAX_SCALE


def test_denomination_lookup_returns_the_record(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    btc = object_money.denomination("BTC", base_dir=data_dir)
    assert btc["name"] == "Bitcoin" and btc["scale"] == "8"
    assert object_money.denomination("NOPE", base_dir=data_dir) is None
