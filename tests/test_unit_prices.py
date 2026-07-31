"""Media pricing: a lookup, not a rate, and it must exist before the call.

`ai_prices` prices a token and object_ai.compute_cost_cents multiplies.
Media cannot be expressed in that shape at all -- an image has a PRICE
per (model, quality, size), and video has a price per second per (model,
size). Pretending otherwise is how a $6.00 sora-2-pro run gets quoted at
zero and billed at six dollars after the fact.

The refusals in this file matter more than the arithmetic. A media run is
held against the wallet at SUBMISSION, so a price that cannot be computed
in advance is a run that cannot be held -- and the only safe answer is to
refuse it up front rather than discover the number once the provider has
already been paid.
"""

import csv
import pathlib

import object_unit_prices as unit_prices

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SEED = REPO_ROOT / "packages" / "app-settings" / "seed" / "unit_prices.tsv"


def seeded_rows():
    with SEED.open() as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def row(**fields):
    base = {"id": "r1", "kind": "image", "model": "m", "quality": "",
            "size": "", "unit_price_cents": "10"}
    base.update(fields)
    return base


# --- images: a flat price per item ----------------------------------------------

def test_an_image_is_priced_by_model_quality_and_size():
    result = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "gpt-image-2", "quality": "high",
        "size": "1536x1024"})
    assert result["price_cents"] == 30
    assert result["price_row_id"] == "gpt-image-2-high-1536x1024"


def test_the_same_model_at_a_different_size_is_a_different_price():
    """The reason this is a table and not a formula: nothing derives one
    from the other."""
    cheap = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "gpt-image-2", "quality": "low",
        "size": "1024x1024"})["price_cents"]
    dear = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "gpt-image-2", "quality": "high",
        "size": "1536x1024"})["price_cents"]
    assert (cheap, dear) == (2, 30)


def test_a_batch_multiplies_by_quantity():
    result = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "gpt-image-2", "quality": "low",
        "size": "1024x1024", "quantity": "4"})
    assert result["price_cents"] == 8


# --- video: per second, and the number that makes holds necessary ---------------

def test_a_twelve_second_sora_pro_run_costs_six_dollars():
    """The number from the spec, and the reason media needs a hold at
    submission rather than a charge at completion."""
    result = unit_prices.quote(seeded_rows(), {
        "kind": "video", "model": "sora-2-pro", "size": "1792x1024",
        "duration_seconds": "12"})
    assert result["price_cents"] == 600
    assert "50c/s" in result["detail"]


def test_video_without_a_duration_cannot_be_quoted_in_advance():
    """Not a default of one second. A run whose price is unknowable
    before the call is a run that cannot be held, which is exactly the
    case that must be refused."""
    result = unit_prices.quote(seeded_rows(), {
        "kind": "video", "model": "sora-2", "size": "1280x720"})
    assert "duration_seconds is required" in result["error"]
    assert "price_cents" not in result


def test_a_fractional_duration_rounds_half_up_once_at_the_end():
    """Per-second times a fraction must not lose money to truncation, and
    must not round each step."""
    rows = [row(kind="video", model="v", size="", unit_price_cents="10",
                quality="")]
    assert unit_prices.quote(rows, {
        "kind": "video", "model": "v", "duration_seconds": "2.55",
    })["price_cents"] == 26        # 25.5 -> 26, not 25


# --- the refusals ---------------------------------------------------------------

def test_an_unpriced_model_is_refused_and_says_what_to_configure():
    result = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "midjourney-9", "quality": "low",
        "size": "1024x1024"})
    assert "No unit price for image" in result["error"]
    assert "midjourney-9" in result["error"]
    assert "unit_prices" in result["error"]


def test_a_near_miss_never_falls_back_to_a_cheaper_neighbour():
    """THE dangerous convenience. gpt-image-2 at an unlisted size must
    not quietly quote the 1024x1024 price -- that is a number the
    provider will not honour, discovered only on the invoice."""
    result = unit_prices.quote(seeded_rows(), {
        "kind": "image", "model": "gpt-image-2", "quality": "low",
        "size": "4096x4096"})
    assert "error" in result


def test_an_unknown_media_kind_is_refused_naming_the_known_ones():
    result = unit_prices.quote(seeded_rows(), {"kind": "hologram"})
    assert "Known kinds" in result["error"]


def test_a_row_with_no_usable_price_is_refused_rather_than_treated_as_free():
    rows = [row(unit_price_cents="")]
    result = unit_prices.quote(rows, {"kind": "image", "model": "m",
                                      "quality": "", "size": ""})
    assert "no usable unit_price_cents" in result["error"]


def test_zero_is_a_real_price_and_not_a_missing_one():
    """A genuinely free model must quote 0, not refuse -- the free-run
    path through the runner depends on the distinction."""
    rows = [row(unit_price_cents="0")]
    result = unit_prices.quote(rows, {"kind": "image", "model": "m",
                                      "quality": "", "size": ""})
    assert result["price_cents"] == 0


# --- wildcards ------------------------------------------------------------------

def test_a_blank_dimension_is_a_wildcard():
    """A provider charging one price for every size should not have to
    enumerate sizes."""
    rows = [row(id="any-size", model="m", quality="low", size="")]
    result = unit_prices.quote(rows, {
        "kind": "image", "model": "m", "quality": "low", "size": "9999x1"})
    assert result["price_row_id"] == "any-size"


def test_an_exact_row_beats_a_wildcard():
    """So a specific price can be added later without deleting the
    catch-all, which is what makes the table safe to edit."""
    rows = [row(id="any-size", model="m", quality="low", size="",
                unit_price_cents="10"),
            row(id="exact", model="m", quality="low", size="1024x1024",
                unit_price_cents="99")]
    result = unit_prices.quote(rows, {
        "kind": "image", "model": "m", "quality": "low", "size": "1024x1024"})
    assert result["price_row_id"] == "exact"
    assert result["price_cents"] == 99


def test_the_wildcard_still_does_not_match_a_different_model():
    """Wildcards widen a dimension, never the identity of the thing."""
    rows = [row(id="any-size", model="m", quality="low", size="")]
    assert "error" in unit_prices.quote(rows, {
        "kind": "image", "model": "other", "quality": "low", "size": "x"})


# --- the seeded table itself ----------------------------------------------------

def test_every_seeded_row_prices_without_error():
    """A seed that cannot be quoted is a seed that will refuse every run
    it was added to enable."""
    for seeded in seeded_rows():
        spec = {"kind": seeded["kind"], "model": seeded["model"],
                "quality": seeded["quality"], "size": seeded["size"]}
        if seeded["kind"] in unit_prices.PER_SECOND_KINDS:
            spec["duration_seconds"] = "1"
        result = unit_prices.quote(seeded_rows(), spec)
        assert "price_cents" in result, (seeded["id"], result)
