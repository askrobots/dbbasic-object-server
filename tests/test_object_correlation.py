from uuid import UUID

import object_correlation


def test_new_correlation_id_returns_uuid4_string():
    correlation_id = object_correlation.new_correlation_id()

    parsed = UUID(correlation_id)
    assert parsed.version == 4
    assert str(parsed) == correlation_id


def test_normalize_correlation_id_accepts_only_uuid4_values():
    valid = "123e4567-e89b-42d3-a456-426614174000"

    assert object_correlation.normalize_correlation_id(valid.upper()) == valid
    assert object_correlation.normalize_correlation_id("") is None
    assert object_correlation.normalize_correlation_id("not-a-uuid") is None
    assert (
        object_correlation.normalize_correlation_id("123e4567-e89b-12d3-a456-426614174000")
        is None
    )


def test_correlation_context_can_be_set_and_reset():
    correlation_id = "123e4567-e89b-42d3-a456-426614174000"

    assert object_correlation.current_correlation_id() is None
    token = object_correlation.set_current_correlation_id(correlation_id)
    try:
        assert object_correlation.current_correlation_id() == correlation_id
    finally:
        object_correlation.reset_current_correlation_id(token)

    assert object_correlation.current_correlation_id() is None
