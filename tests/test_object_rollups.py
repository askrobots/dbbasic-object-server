"""Unit tests for object_rollups.py -- plan/vocabulary/14-rollup-spec.md.

Mirrors the direct schema/record-fixture style tests/test_object_daemon.py
uses for process_compactions/process_stale_transitions: write a schema
straight to data/schemas, create rows through object_records with
base_dir=tmp_path and roots=[] (never touching the real packages/ tree),
then exercise object_rollups directly.

End-to-end package/permission tests (admin-only rollup_definitions, a
target collection carrying its own public-read rule over a private
source, and the worked orders-per-day example run through a real
rollup_definitions row) live in tests/test_app_rollup_package.py instead.
"""
import json

import pytest

import object_records
import object_rollups
import object_schemas


def write_schema(data_dir, collection, fields):
    path = data_dir / "schemas" / f"{collection}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": collection, "fields": fields}))
    return path


ORDERS_FIELDS = [
    {"name": "id"},
    {"name": "channel", "type": "text"},
    {"name": "status", "type": "text"},
    {"name": "total_cents", "type": "integer"},
    {"name": "created_at", "type": "datetime"},
]


def _make_orders(data_dir, rows):
    write_schema(data_dir, "orders", ORDERS_FIELDS)
    for row in rows:
        object_records.create_collection_record("orders", row, base_dir=data_dir, roots=[])


def _definition(**overrides):
    base = {
        "id": "rollup_orders_by_day",
        "name": "Orders per day",
        "source_collection": "orders",
        "filter": json.dumps({"status": "paid"}),
        "group_by": json.dumps(["channel"]),
        "time_bucket": json.dumps({"field": "created_at", "granularity": "day"}),
        "metrics": json.dumps([
            {"op": "count", "as": "order_count"},
            {"op": "sum", "field": "total_cents", "as": "revenue_cents"},
            {"op": "avg", "field": "total_cents", "as": "avg_order_cents"},
        ]),
        "target_collection": "rollup_orders_by_day",
        "min_group_size": "",
        "refresh_mode": "scheduled",
        "refresh_interval_seconds": "3600",
        "last_computed_at": "",
        "enabled": "true",
    }
    base.update(overrides)
    return base


# --- parse_definition -----------------------------------------------------

def test_parse_definition_happy_path(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])

    config = object_rollups.parse_definition(_definition(), base_dir=data_dir, roots=[])

    assert config.definition_id == "rollup_orders_by_day"
    assert config.source_collection == "orders"
    assert config.target_collection == "rollup_orders_by_day"
    assert config.filter == {"status": "paid"}
    assert config.group_by == ("channel",)
    assert config.time_bucket_field == "created_at"
    assert config.time_bucket_granularity == "day"
    assert [m.op for m in config.metrics] == ["count", "sum", "avg"]
    assert config.metrics[0].as_name == "order_count"
    assert config.metrics[1].target_type == "integer"
    assert config.metrics[2].target_type == "number"
    assert config.min_group_size is None
    assert config.refresh_interval_seconds == 3600
    assert config.enabled is True


def test_parse_definition_defaults_group_by_and_filter_to_empty(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])

    config = object_rollups.parse_definition(
        _definition(filter="", group_by="", **{"time_bucket": ""}),
        base_dir=data_dir, roots=[],
    )

    assert config.filter == {}
    assert config.group_by == ()
    assert config.time_bucket_field is None


def test_parse_definition_rejects_missing_source_collection(tmp_path):
    data_dir = tmp_path / "data"
    with pytest.raises(object_rollups.DefinitionError, match="source_collection"):
        object_rollups.parse_definition(_definition(source_collection=""), base_dir=data_dir, roots=[])


def test_parse_definition_rejects_nested_filter_value(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(filter=json.dumps({"status": {"op": "eq", "value": "paid"}}))
    with pytest.raises(object_rollups.DefinitionError, match="flat"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_rejects_non_numeric_metric_field(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(metrics=json.dumps([{"op": "sum", "field": "channel", "as": "x"}]))
    with pytest.raises(object_rollups.DefinitionError, match="numeric"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_rejects_sum_without_field(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(metrics=json.dumps([{"op": "sum", "as": "x"}]))
    with pytest.raises(object_rollups.DefinitionError, match="requires a field"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_rejects_time_bucket_on_non_date_field(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(time_bucket=json.dumps({"field": "channel", "granularity": "day"}))
    with pytest.raises(object_rollups.DefinitionError, match="date/datetime"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_requires_positive_refresh_interval(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(refresh_interval_seconds="0")
    with pytest.raises(object_rollups.DefinitionError, match="refresh_interval_seconds"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


def test_parse_definition_rejects_metric_name_colliding_with_group_by(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    bad = _definition(metrics=json.dumps([{"op": "count", "as": "channel"}]))
    with pytest.raises(object_rollups.DefinitionError, match="collides"):
        object_rollups.parse_definition(bad, base_dir=data_dir, roots=[])


# --- derive_target_schema --------------------------------------------------

def test_derive_target_schema_matches_spec_shape(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    config = object_rollups.parse_definition(_definition(), base_dir=data_dir, roots=[])

    schema = object_rollups.derive_target_schema(config)

    field_names = [f["name"] for f in schema["fields"]]
    assert field_names == ["id", "channel", "bucket_start", "order_count", "revenue_cents", "avg_order_cents", "computed_at"]

    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["channel"]["type"] == "text"
    assert "computed" not in by_name["channel"]
    assert by_name["bucket_start"]["type"] == "date"
    assert by_name["order_count"] == {"name": "order_count", "type": "integer", "computed": True}
    assert by_name["revenue_cents"] == {"name": "revenue_cents", "type": "integer", "computed": True}
    assert by_name["avg_order_cents"] == {"name": "avg_order_cents", "type": "number", "computed": True}
    assert by_name["computed_at"] == {"name": "computed_at", "type": "datetime", "computed": True}

    assert schema["storage"] == "classic"
    assert schema["views"]["list_mode"] == "table"
    assert schema["views"]["list_fields"] == ["channel", "bucket_start", "order_count", "revenue_cents", "avg_order_cents"]


def test_derive_target_schema_bucket_start_is_datetime_for_granularity_none_on_datetime_field(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    definition = _definition(
        time_bucket=json.dumps({"field": "created_at", "granularity": "none"}),
        group_by="[]",
        metrics=json.dumps([{"op": "count", "as": "count"}]),
    )
    config = object_rollups.parse_definition(definition, base_dir=data_dir, roots=[])

    schema = object_rollups.derive_target_schema(config)

    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["bucket_start"]["type"] == "datetime"


def test_derive_target_schema_omits_bucket_start_without_time_bucket(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [])
    definition = _definition(time_bucket="", metrics=json.dumps([{"op": "count", "as": "count"}]))
    config = object_rollups.parse_definition(definition, base_dir=data_dir, roots=[])

    schema = object_rollups.derive_target_schema(config)

    assert "bucket_start" not in [f["name"] for f in schema["fields"]]


# --- compute_rollup ---------------------------------------------------

def test_compute_rollup_groups_filters_and_computes_metrics(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "1000", "created_at": "2026-07-01T10:00:00Z"},
        {"id": "o2", "channel": "web", "status": "paid", "total_cents": "2000", "created_at": "2026-07-01T18:00:00Z"},
        {"id": "o3", "channel": "retail", "status": "paid", "total_cents": "500", "created_at": "2026-07-01T09:00:00Z"},
        {"id": "o4", "channel": "web", "status": "draft", "total_cents": "9999", "created_at": "2026-07-01T09:00:00Z"},
    ])

    result = object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])

    assert result["groups"] == 2
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["deleted"] == 0

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    by_channel = {r["channel"]: r for r in rows}

    assert by_channel["web"]["bucket_start"] == "2026-07-01"
    assert by_channel["web"]["order_count"] == "2"
    assert by_channel["web"]["revenue_cents"] == "3000"
    assert by_channel["web"]["avg_order_cents"] == "1500.0"  # exact: 3000 / 2
    assert by_channel["retail"]["order_count"] == "1"
    assert by_channel["retail"]["revenue_cents"] == "500"
    # the draft order (o4) never appears anywhere -- filter excluded it
    assert all(r.get("id") for r in rows)
    assert sum(int(r["order_count"]) for r in rows) == 3


def test_compute_rollup_avg_is_exact_for_a_non_terminating_division(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o2", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o3", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
    ])

    object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    row = rows[0]
    assert row["revenue_cents"] == "300"
    # sum (300) / count (3) computed once from exact integers, not accumulated.
    assert float(row["avg_order_cents"]) == pytest.approx(100.0)


@pytest.mark.parametrize(
    "granularity,created_at,expected_bucket",
    [
        ("day", "2026-07-15T12:00:00Z", "2026-07-15"),
        # Wednesday 2026-07-15 -> ISO week starts Monday 2026-07-13.
        ("week", "2026-07-15T12:00:00Z", "2026-07-13"),
        ("month", "2026-07-15T12:00:00Z", "2026-07-01"),
    ],
)
def test_compute_rollup_time_bucket_granularities(tmp_path, granularity, created_at, expected_bucket):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "100", "created_at": created_at},
    ])
    definition = _definition(time_bucket=json.dumps({"field": "created_at", "granularity": granularity}))

    object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[])

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert rows[0]["bucket_start"] == expected_bucket


def test_compute_rollup_min_and_max_metrics(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "500", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o2", "channel": "web", "status": "paid", "total_cents": "3000", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o3", "channel": "web", "status": "paid", "total_cents": "1200", "created_at": "2026-07-01T00:00:00Z"},
    ])
    definition = _definition(metrics=json.dumps([
        {"op": "min", "field": "total_cents", "as": "min_cents"},
        {"op": "max", "field": "total_cents", "as": "max_cents"},
    ]))

    object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[])

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert rows[0]["min_cents"] == "500"
    assert rows[0]["max_cents"] == "3000"


def test_compute_rollup_deterministic_id_is_stable_across_recomputes(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "1000", "created_at": "2026-07-01T00:00:00Z"},
    ])

    first = object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])
    rows_after_first = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert first["created"] == 1

    second = object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])
    rows_after_second = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])

    assert second["created"] == 0
    assert second["updated"] == 1
    assert {r["id"] for r in rows_after_first} == {r["id"] for r in rows_after_second}
    # computed_at still advances even though nothing else about the row changed.
    assert rows_after_second[0]["computed_at"] >= rows_after_first[0]["computed_at"]


def test_compute_rollup_drops_stale_group_on_rewrite(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "1000", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o2", "channel": "retail", "status": "paid", "total_cents": "500", "created_at": "2026-07-01T00:00:00Z"},
    ])
    object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])
    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert {r["channel"] for r in rows} == {"web", "retail"}

    # The retail order un-pays (no longer matches the filter) -- its whole
    # group should vanish from the target on the next recompute, not be
    # left behind as a stale row.
    object_records.update_collection_record("orders", "o2", {"status": "refunded"}, base_dir=data_dir, roots=[])

    result = object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])
    assert result["deleted"] == 1

    rows_after = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert {r["channel"] for r in rows_after} == {"web"}


def test_compute_rollup_min_group_size_suppresses_small_groups(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o2", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
        # "referral" has exactly one paid order today -- the textbook
        # group-of-1 disclosure case 14's Permissions Posture names.
        {"id": "o3", "channel": "referral", "status": "paid", "total_cents": "9999", "created_at": "2026-07-01T00:00:00Z"},
    ])
    definition = _definition(min_group_size="2")

    result = object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[])

    assert result["suppressed"] == 1
    assert result["groups"] == 1
    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert {r["channel"] for r in rows} == {"web"}
    # Suppressed, not merged: no "referral" identity or its $99.99 value
    # leaks into any row, including as part of an "other" bucket.
    assert not any("referral" in json.dumps(r) for r in rows)


def test_compute_rollup_empty_group_by_yields_one_row_per_bucket(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "o2", "channel": "retail", "status": "paid", "total_cents": "200", "created_at": "2026-07-01T00:00:00Z"},
    ])
    definition = _definition(group_by="[]")

    object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[])

    rows = object_records.read_collection_records("rollup_orders_by_day", base_dir=data_dir, roots=[])
    assert len(rows) == 1
    assert rows[0]["order_count"] == "2"
    assert rows[0]["revenue_cents"] == "300"


def test_compute_rollup_regenerates_target_schema_with_computed_fields(tmp_path):
    data_dir = tmp_path / "data"
    _make_orders(data_dir, [
        {"id": "o1", "channel": "web", "status": "paid", "total_cents": "100", "created_at": "2026-07-01T00:00:00Z"},
    ])

    object_rollups.compute_rollup(_definition(), base_dir=data_dir, roots=[])

    schema = object_schemas.get_schema("rollup_orders_by_day", base_dir=data_dir)
    by_name = {f["name"]: f for f in schema["fields"]}
    assert by_name["order_count"].get("computed") is True
    assert by_name["computed_at"].get("computed") is True

    target_row_id = object_records.read_collection_records(
        "rollup_orders_by_day", base_dir=data_dir, roots=[]
    )[0]["id"]
    # A client (ordinary create/update, no bypass flag) must still be
    # unable to write a computed field -- this is what makes "the rollup
    # pass is the only writer" a real guarantee, not just a convention.
    with pytest.raises(object_records.InvalidRecordPayloadError):
        object_records.update_collection_record(
            "rollup_orders_by_day", target_row_id,
            {"order_count": "999999"},
            base_dir=data_dir, roots=[],
        )


def test_compute_rollup_missing_source_collection_raises(tmp_path):
    data_dir = tmp_path / "data"
    definition = _definition(source_collection="does_not_exist")
    with pytest.raises(object_schemas.SchemaNotFoundError):
        object_rollups.compute_rollup(definition, base_dir=data_dir, roots=[])


# --- flags / due gate -------------------------------------------------

def test_rollup_pass_enabled_defaults_true_with_no_feature_flags_collection(tmp_path):
    assert object_rollups.rollup_pass_enabled(base_dir=tmp_path / "data") is True


def test_rollup_pass_enabled_false_when_flag_explicitly_off(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "feature_flags", [
        {"name": "id"}, {"name": "flag", "type": "text"}, {"name": "value", "type": "text"},
    ])
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "rollup_enabled", "value": "off"}, base_dir=data_dir, roots=[],
    )
    assert object_rollups.rollup_pass_enabled(base_dir=data_dir) is False


def test_rollup_pass_enabled_true_when_flag_explicitly_on(tmp_path):
    data_dir = tmp_path / "data"
    write_schema(data_dir, "feature_flags", [
        {"name": "id"}, {"name": "flag", "type": "text"}, {"name": "value", "type": "text"},
    ])
    object_records.create_collection_record(
        "feature_flags", {"id": "f1", "flag": "rollup_enabled", "value": "on"}, base_dir=data_dir, roots=[],
    )
    assert object_rollups.rollup_pass_enabled(base_dir=data_dir) is True


def test_is_definition_due_true_when_never_computed():
    assert object_rollups.is_definition_due({"last_computed_at": "", "refresh_interval_seconds": "3600"}) is True


def test_is_definition_due_false_within_interval():
    import datetime as dt
    now = dt.datetime(2026, 7, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    record = {"last_computed_at": "2026-07-18T11:30:00Z", "refresh_interval_seconds": "3600"}
    assert object_rollups.is_definition_due(record, now=now) is False


def test_is_definition_due_true_after_interval_elapses():
    import datetime as dt
    now = dt.datetime(2026, 7, 18, 13, 0, 1, tzinfo=dt.timezone.utc)
    record = {"last_computed_at": "2026-07-18T11:30:00Z", "refresh_interval_seconds": "3600"}
    assert object_rollups.is_definition_due(record, now=now) is True


def test_is_definition_enabled_defaults_true():
    assert object_rollups.is_definition_enabled({}) is True


def test_is_definition_enabled_false_when_explicitly_disabled():
    assert object_rollups.is_definition_enabled({"enabled": "false"}) is False
