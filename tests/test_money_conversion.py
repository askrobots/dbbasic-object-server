"""Conversion between denominations (plan/value-accounts-and-denominations-
spec.md §7): stored rates, and the rules for applying them.

The two that matter: a rate dated after the moment being valued is never
used (valuing yesterday with today's price silently restates history), and
a conversion returns its rate alongside its result so the caller can stamp
it -- a conversion nobody stamped is one that can quietly change later.
"""

import json
import pathlib
from decimal import Decimal

import pytest

import object_money
import object_records

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FINANCE = REPO_ROOT / "packages" / "app-finance"


def setup_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    for name in ("denominations", "rates"):
        (schema_dir / f"{name}.json").write_text(
            (FINANCE / "schemas" / f"{name}.json").read_text())
        coll = data_dir / "collections" / name
        coll.mkdir(parents=True)
        (coll / "records.tsv").write_text(
            (FINANCE / "seed" / f"{name}.tsv").read_text())
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(data_dir))
    return data_dir


_COUNTER = iter(range(1, 10_000))


def add_rate(data_dir, base, quote, rate, as_of, *, kind="spot", source="test",
             rid=None, period=""):
    return object_records.create_collection_record(
        "rates",
        {"id": rid or f"r-{base}-{quote}-{kind}-{next(_COUNTER)}", "base_code": base,
         "quote_code": quote, "rate": rate, "as_of": as_of, "kind": kind,
         "period": period, "source": source, "owner_id": "dan"},
        base_dir=data_dir)


# --- applying a rate ----------------------------------------------------------

def test_convert_produces_exact_minor_units_of_the_target(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    # 0.05 BTC at 67,432.10 USD/BTC = 3,371.605 USD -> 337161 cents (half-up)
    assert object_money.convert(5000000, "BTC", "USD", "67432.10",
                                base_dir=data_dir) == 337161
    # USD -> JPY, and JPY has no minor unit at all
    assert object_money.convert(150000, "USD", "JPY", "157.25",
                                base_dir=data_dir) == 235875
    # An amount of the same denomination is unchanged at rate 1.
    assert object_money.convert(150000, "USD", "USD", "1", base_dir=data_dir) == 150000


def test_conversion_refuses_a_nonsense_rate(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    for bad in ("0", "-1", "not a rate"):
        with pytest.raises(object_money.MoneyError):
            object_money.convert(150000, "USD", "EUR", bad, base_dir=data_dir)


def test_rounding_happens_once_at_the_end(tmp_path, monkeypatch):
    """Converting then rounding per line is how a total stops matching the
    sum of its parts; the rounding here is a single deliberate step."""
    data_dir = setup_env(tmp_path, monkeypatch)
    rate = "0.911"
    lines = [3333, 3333, 3334]                      # 100.00 USD in three parts
    converted = [object_money.convert(c, "USD", "EUR", rate, base_dir=data_dir)
                 for c in lines]
    total_converted = object_money.convert(sum(lines), "USD", "EUR", rate,
                                           base_dir=data_dir)
    # Per-line rounding may differ from the whole by at most a minor unit,
    # and the test states which number is authoritative: the one the books use.
    assert abs(sum(converted) - total_converted) <= 1


# --- finding the right rate ---------------------------------------------------

def test_lookup_never_uses_a_rate_from_the_future(tmp_path, monkeypatch):
    """The rule that keeps history from being restated: valuing a July
    transaction with an August price is time travel."""
    data_dir = setup_env(tmp_path, monkeypatch)
    add_rate(data_dir, "BTC", "USD", "60000.00", "2026-07-01T00:00:00Z")
    add_rate(data_dir, "BTC", "USD", "67432.10", "2026-07-20T00:00:00Z")
    add_rate(data_dir, "BTC", "USD", "99999.00", "2026-08-01T00:00:00Z")

    found = object_money.find_rate("BTC", "USD", base_dir=data_dir,
                                   as_of="2026-07-24T00:00:00Z")
    assert found["rate"] == "67432.10"          # newest at-or-before, not the August one

    early = object_money.find_rate("BTC", "USD", base_dir=data_dir,
                                   as_of="2026-06-01T00:00:00Z")
    assert early is None                        # nothing yet: say so, do not guess


def test_kind_and_source_narrow_the_lookup(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_rate(data_dir, "EUR", "USD", "1.0850", "2026-07-31T23:59:59Z",
             kind="spot", source="coinbase")
    add_rate(data_dir, "EUR", "USD", "1.0902", "2026-07-31T23:59:59Z",
             kind="close", period="2026-07", source="ecb", rid="r-eur-close")
    add_rate(data_dir, "EUR", "USD", "1.0788", "2026-07-31T23:59:59Z",
             kind="average", period="2026-07", source="ecb", rid="r-eur-avg")

    close = object_money.find_rate("EUR", "USD", base_dir=data_dir, kind="close")
    assert close["rate"] == "1.0902"            # revaluation uses the close
    average = object_money.find_rate("EUR", "USD", base_dir=data_dir, kind="average")
    assert average["rate"] == "1.0788"          # translating a period uses the average
    by_source = object_money.find_rate("EUR", "USD", base_dir=data_dir, source="coinbase")
    assert by_source["rate"] == "1.0850"        # sources disagree; never silently averaged


def test_the_inverse_pair_is_derived_and_marked(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_rate(data_dir, "BTC", "USD", "50000.00", "2026-07-01T00:00:00Z")
    inverse = object_money.find_rate("USD", "BTC", base_dir=data_dir)
    assert inverse["inverted"] is True          # computed, not quoted
    assert Decimal(inverse["rate"]) == Decimal(1) / Decimal("50000.00")
    assert object_money.find_rate("USD", "BTC", base_dir=data_dir,
                                  allow_inverse=False) is None


def test_identity_and_corrupt_rows_are_handled(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    same = object_money.find_rate("USD", "USD", base_dir=data_dir)
    assert same["rate"] == "1" and same["source"] == "identity"
    add_rate(data_dir, "XAU", "USD", "0", "2026-07-01T00:00:00Z")       # corrupt
    add_rate(data_dir, "XAU", "USD", "abc", "2026-07-02T00:00:00Z",
             rid="r-bad-2")                                             # corrupt
    add_rate(data_dir, "XAU", "USD", "2400.00", "2026-07-03T00:00:00Z",
             rid="r-good")
    good = object_money.find_rate("XAU", "USD", base_dir=data_dir)
    assert good["rate"] == "2400.00"            # unusable rows skipped, not fatal


# --- convert_at: the stamping contract ----------------------------------------

def test_convert_at_returns_the_rate_so_the_caller_can_stamp_it(tmp_path, monkeypatch):
    data_dir = setup_env(tmp_path, monkeypatch)
    add_rate(data_dir, "BTC", "USD", "67432.10", "2026-07-20T00:00:00Z",
             source="coinbase")
    result = object_money.convert_at(5000000, "BTC", "USD", base_dir=data_dir,
                                     as_of="2026-07-24T00:00:00Z")
    assert result["amount_minor"] == 337161
    # The rate travels with the result precisely so the record that lands
    # carries the number it was valued at (docs/logic-decisions.md #1).
    assert result["rate"] == "67432.10"
    assert result["source"] == "coinbase"
    assert result["as_of"] == "2026-07-20T00:00:00Z"
    assert result["inverted"] is False


def test_a_conversion_with_no_rate_behind_it_refuses(tmp_path, monkeypatch):
    """Better to fail than to invent a number nobody can defend later."""
    data_dir = setup_env(tmp_path, monkeypatch)
    with pytest.raises(object_money.RateNotFound):
        object_money.convert_at(5000000, "BTC", "USD", base_dir=data_dir,
                                as_of="2026-07-24T00:00:00Z")


def test_a_later_restatement_does_not_erase_what_we_recorded(tmp_path, monkeypatch):
    """Rates are append-only observations: a source correcting itself is a
    new row, and the earlier observation stays visible."""
    data_dir = setup_env(tmp_path, monkeypatch)
    add_rate(data_dir, "EUR", "USD", "1.0850", "2026-07-20T00:00:00Z",
             source="ecb", rid="r-first")
    add_rate(data_dir, "EUR", "USD", "1.0855", "2026-07-20T00:00:00Z",
             source="ecb", rid="r-restated")
    rows = object_money.rate_records("EUR", "USD", base_dir=data_dir)
    assert len(rows) == 2
    assert {r["rate"] for r in rows} == {"1.0850", "1.0855"}


def test_rates_schema_is_append_only_and_seed_is_header_only():
    schema = json.loads((FINANCE / "schemas" / "rates.json").read_text())
    assert schema["storage"] == "append"
    lines = (FINANCE / "seed" / "rates.tsv").read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].split("\t") == [f["name"] for f in schema["fields"]]
