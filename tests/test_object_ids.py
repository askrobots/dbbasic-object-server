from uuid import UUID

import object_ids


def test_new_uuid4_returns_canonical_uuid4_string():
    value = object_ids.new_uuid4()

    parsed = UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value


def test_normalize_uuid4_accepts_only_uuid4_values():
    value = object_ids.new_uuid4()

    assert object_ids.normalize_uuid4(value.upper()) == value
    assert object_ids.normalize_uuid4("00000000-0000-0000-0000-000000000000") is None
    assert object_ids.normalize_uuid4("not-a-uuid") is None
    assert object_ids.normalize_uuid4(None) is None
    assert object_ids.is_uuid4(value) is True
    assert object_ids.is_uuid4("not-a-uuid") is False
