"""Derived stock levels, folded from the immutable stock_moves append log.

This module belongs to the STOCK slice of packages/app-catalog (locations +
stock_moves), but lives here at the repo root next to the other shared
object_*.py modules (object_records.py, object_record_changes.py, ...)
rather than inside packages/app-catalog/ itself. That placement is
deliberate, not a convenience shortcut: package installs only ever copy the
artifacts a manifest declares (objects/schemas/permissions/seed -- see
object_packages.install_package) into the runtime's object/data roots, so a
plain Python module sitting inside a package directory would never actually
land anywhere importable at runtime. Every object in this codebase that
needs shared logic imports a bare top-level module the same way
packages/app-orders/objects/system/order_totals.py imports object_records
and object_record_changes; this module follows that exact precedent so
packages/app-catalog/objects/site/stock.py can `import object_stock` and
have it resolve.

Source model (reconciled against a private predecessor-system catalog
audit, not part of this repo): a StockMove is immutable -- product_id moves
quantity from from_location_id to to_location_id, once, forever. Current
stock is never a stored column anywhere; it is always the fold of every
move for a product (docs/append-only-storage-design.md's own "stock_moves
is the textbook append-mode collection" framing, and the same source
audit's own note: "StockMove (immutable, levels DERIVED not stored)").
This module is that fold, nothing more.

Quantity discipline: every quantity here is a Decimal, never a bare float.
quantity is a count/measure, not currency -- may be fractional (e.g. 2.5
kg) -- but Decimal arithmetic is still required so a fractional quantity
can never introduce binary-float rounding error, the same "count/measure,
not money" exception app-orders' order_lines.quantity documents.

Blank from_location_id/to_location_id ("external origin"/"external
destination" in the source model -- e.g. a purchase with no supplier
location on file, or a sale with no customer location on file) simply have
nothing to accumulate at: the fold below only touches a (product,
location) key when the id is non-empty. The paired real location's balance
still moves normally either way.

Virtual locations (location_type customer/supplier/virtual) hold balances
like any other location -- a stock move into a virtual "customer" location
models stock that left the business, for instance -- but they are excluded
from total_quantity's on-hand figure, matching the source model's own
total_quantity semantics: only real, physical locations count as "on
hand."

Negative levels: no floor or clamp is applied anywhere in this module. A
location that has shipped out more than it ever received folds to a
negative Decimal and is left visible rather than floored to zero. The
predecessor audit's own business-records notes admit stock/balance
integrity is enforced nowhere at write time ("WEAK: ... balance NOT
enforced at post" -- said there of the ledger, but the same honesty
applies here); inventing a clamp the append log itself does not assert
would hide a real data-quality signal (overselling) rather than surface
it. This is the documented choice where the source audit does not spell
out clamp-vs-negative behavior explicitly.

Scale note: every function here is a pure O(len(stock_moves)) fold over
the full log on every call -- no materialized rollup, no cache. That is
the right, honest trade for a first slice at this collection's expected
scale (append-mode collections read fine up to the thresholds documented
in docs/storage-modes.md). A materialized/incremental rollup -- recomputed
only from moves newer than the last snapshot, or a per-(product,location)
sidecar -- is future work, the same trade docs/append-only-storage-design.md
already calls out for any derived index over an append log ("Secondary
sidecar indexes are future work, only when a named workload demands one").
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import object_records
import object_schemas

STOCK_MOVES_COLLECTION = "stock_moves"
LOCATIONS_COLLECTION = "locations"

# Location types that are counterparties, not physical inventory --
# excluded from total_quantity's on-hand figure. Matches locations.json's
# location_type enum tail (customer, supplier, virtual).
VIRTUAL_LOCATION_TYPES = frozenset({"customer", "supplier", "virtual"})


def _decimal(value: Any) -> Decimal:
    """Parse a stored quantity as Decimal; blank/invalid -> 0."""
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _stock_moves(
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, str]]:
    moves = object_records.read_collection_records(
        STOCK_MOVES_COLLECTION, base_dir=base_dir, roots=roots
    )
    if owner is not None:
        moves = [move for move in moves if move.get("owner_id") == owner]
    return moves


def _locations(
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> list[dict[str, str]]:
    """Return locations records, or [] when the collection isn't known yet.

    Defensive only: in every real install locations and stock_moves ship
    together in the same package, but a caller could point base_dir at a
    data dir where locations was never installed (e.g. an isolated test of
    stock_moves alone) -- rather than raising, virtual-location resolution
    degrades to "nothing is virtual" instead of failing the whole fold.
    """
    try:
        locations = object_records.read_collection_records(
            LOCATIONS_COLLECTION, base_dir=base_dir, roots=roots
        )
    except (object_schemas.SchemaNotFoundError, LookupError, OSError):
        return []
    if owner is not None:
        locations = [loc for loc in locations if loc.get("owner_id") == owner]
    return locations


def virtual_location_ids(
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> set[str]:
    """Return the set of location ids whose location_type is virtual
    (customer, supplier, or virtual) -- the ids total_quantity excludes.
    """
    return {
        loc["id"]
        for loc in _locations(base_dir=base_dir, owner=owner, roots=roots)
        if loc.get("id") and loc.get("location_type") in VIRTUAL_LOCATION_TYPES
    }


def _fold_levels(moves: Iterable[dict[str, str]]) -> dict[tuple[str, str], Decimal]:
    """Fold immutable stock_moves rows into net (product_id, location_id) deltas.

    A move INTO a location adds quantity; a move OUT OF a location
    subtracts it. A blank from/to id has no location to accumulate at and
    is simply skipped for that side of the move.
    """
    levels: dict[tuple[str, str], Decimal] = {}
    for move in moves:
        product_id = move.get("product_id")
        if not product_id:
            continue
        quantity = _decimal(move.get("quantity"))

        to_location_id = move.get("to_location_id")
        if to_location_id:
            key = (product_id, to_location_id)
            levels[key] = levels.get(key, Decimal("0")) + quantity

        from_location_id = move.get("from_location_id")
        if from_location_id:
            key = (product_id, from_location_id)
            levels[key] = levels.get(key, Decimal("0")) - quantity

    return levels


def quantity_at_location(
    product_id: str,
    location_id: str,
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> Decimal:
    """Return the net on-hand quantity of one product at one location.

    Sum of quantity for every move INTO location_id minus every move OUT
    OF it, for product_id only. A blank location_id (the "external
    origin/destination" sentinel -- see module docstring) always returns 0:
    it names no real location to hold a balance at.
    """
    if not location_id:
        return Decimal("0")
    levels = _fold_levels(_stock_moves(base_dir=base_dir, owner=owner, roots=roots))
    return levels.get((product_id, location_id), Decimal("0"))


def total_quantity(
    product_id: str,
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> Decimal:
    """Return one product's total real on-hand quantity.

    Sums net quantity across every location the product's moves touch,
    EXCLUDING virtual location types (customer/supplier/virtual) -- matches
    the source model's total_quantity semantics: only physical locations
    count as "on hand." A location id referenced by a move but no longer
    present in the locations collection (e.g. deleted) is treated as real
    (included), the conservative default when its type can no longer be
    resolved.
    """
    levels = _fold_levels(_stock_moves(base_dir=base_dir, owner=owner, roots=roots))
    virtual_ids = virtual_location_ids(base_dir=base_dir, owner=owner, roots=roots)

    total = Decimal("0")
    for (moved_product_id, location_id), quantity in levels.items():
        if moved_product_id != product_id:
            continue
        if location_id in virtual_ids:
            continue
        total += quantity
    return total


def stock_levels(
    *,
    base_dir: Path | str,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Return a page-ready summary: per-(product, location) net quantity,
    and per-product on-hand totals (virtual locations excluded).

    One fold over stock_moves serves both halves of the summary, so this
    is cheaper than calling quantity_at_location/total_quantity in a loop
    per product. Quantities are returned as strings (str(Decimal(...))) --
    JSON- and TSV-safe, exact, never a float.
    """
    moves = _stock_moves(base_dir=base_dir, owner=owner, roots=roots)
    levels = _fold_levels(moves)
    virtual_ids = virtual_location_ids(base_dir=base_dir, owner=owner, roots=roots)

    totals: dict[str, Decimal] = {}
    level_rows = []
    for (product_id, location_id), quantity in sorted(levels.items()):
        level_rows.append({
            "product_id": product_id,
            "location_id": location_id,
            "quantity": str(quantity),
        })
        if location_id not in virtual_ids:
            totals[product_id] = totals.get(product_id, Decimal("0")) + quantity

    total_rows = [
        {"product_id": product_id, "quantity": str(quantity)}
        for product_id, quantity in sorted(totals.items())
    ]

    return {"levels": level_rows, "totals": total_rows}


# === valuation: what the goods cost, for COGS ==================================
#
# The stock fold above answers "how many"; this answers "at what cost",
# which is the prerequisite COGS-on-sale was blocked on. The method is
# WEIGHTED AVERAGE, and it is a decision rather than a default:
#
# FIFO would be more precise, and it is what a lot-tracked warehouse
# should use -- but it requires cost LAYERS (which units, from which
# receipt, are still on the shelf), and stock_moves has no lot dimension
# at all. Bolting layers on to satisfy one journal entry would be a
# storage change driven by an accounting report. LIFO is not permitted
# under IFRS and is a bad fit besides. Weighted average needs nothing the
# append log does not already carry, is accepted under both GAAP and
# IFRS, and is what most small shops actually run.
#
# MOVING average, specifically, folded in move order -- not the simpler
# "total spent / total ever bought". The two genuinely differ: buy 10 at
# 100, sell 10, buy 1 at 300, and the lifetime average says 118 while the
# moving average says 300, which is what the next sale actually costs.
# The lifetime version quietly reprices goods you no longer own.
#
# A depletion at an unknown average (nothing on hand) consumes nothing and
# says so by leaving the value alone; the caller sees None and must skip
# rather than guess, which is the whole point of the exercise.

ACQUISITION_REASONS = frozenset({"purchase", "return"})


def _move_order_key(move: dict) -> tuple:
    """Chronological order, falling back to insertion order.

    occurred_at is the business date and may be blank or coarse (a plain
    date), so created_at breaks ties -- two receipts on the same day must
    still fold in the order they were recorded.
    """
    return (str(move.get("occurred_at") or move.get("created_at") or ""),
            str(move.get("created_at") or ""))


def weighted_average_cost_cents(
    moves: Iterable[dict[str, str]],
    product_id: str,
) -> int | None:
    """The moving weighted-average unit cost of one product, in cents.

    Returns None when there is nothing on hand or no acquisition ever
    carried a cost -- there is no honest average to report, and a caller
    that stamps one anyway has invented a valuation method by accident.

    Pure: takes rows, returns a number. No I/O, no clock.
    """
    on_hand = Decimal("0")
    value = Decimal("0")

    for move in sorted((m for m in moves if m.get("product_id") == product_id),
                       key=_move_order_key):
        quantity = _decimal(move.get("quantity"))
        if quantity <= 0:
            continue
        reason = str(move.get("reason") or "").strip()
        cost = _decimal(move.get("unit_cost_cents"))
        into = str(move.get("to_location_id") or "").strip()
        out_of = str(move.get("from_location_id") or "").strip()

        # An acquisition is stock arriving from outside with a price on
        # it. A transfer (both ends set) moves goods between shelves and
        # must not re-average anything.
        acquiring = (reason in ACQUISITION_REASONS or (into and not out_of))
        if acquiring and into and not out_of:
            if cost > 0:
                value += quantity * cost
                on_hand += quantity
            else:
                # Free/uncosted stock still occupies the shelf, and
                # pretending it does not would inflate the average for
                # everything else on it.
                on_hand += quantity
            continue

        if out_of and not into:
            # A depletion consumes stock at the average it was carried at,
            # which leaves the average itself unchanged -- the defining
            # property of weighted average.
            if on_hand <= 0:
                continue
            unit = value / on_hand
            consumed = min(quantity, on_hand)
            value -= consumed * unit
            on_hand -= consumed

    if on_hand <= 0 or value <= 0:
        return None
    return int((value / on_hand).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def average_cost_for_product(
    product_id: str,
    *,
    base_dir: Path | str = object_records.DEFAULT_DATA_DIR,
    owner: str | None = None,
    roots: Iterable[Path] | None = None,
) -> int | None:
    """weighted_average_cost_cents against stored moves."""
    return weighted_average_cost_cents(
        _stock_moves(base_dir=base_dir, owner=owner, roots=roots), product_id)
