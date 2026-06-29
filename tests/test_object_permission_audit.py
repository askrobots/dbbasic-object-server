import object_permission_audit


def test_append_and_read_permission_audit_entries(tmp_path):
    data_dir = tmp_path / "data"

    path = object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:00Z",
            "action": "execute",
            "object_id": "site_home",
            "decision": {"allowed": True},
        },
        data_dir,
    )
    object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:01Z",
            "action": "source",
            "object_id": "site_home",
            "decision": {"allowed": False},
        },
        data_dir,
    )

    assert path == data_dir / "permissions" / "audit.jsonl"
    assert object_permission_audit.get_permission_audit(data_dir, limit=1) == [
        {
            "timestamp": "2026-06-29T00:00:01Z",
            "action": "source",
            "object_id": "site_home",
            "decision": {"allowed": False},
        }
    ]


def test_missing_permission_audit_returns_empty_list(tmp_path):
    assert object_permission_audit.get_permission_audit(tmp_path / "data") == []


def test_permission_audit_rejects_bad_limit(tmp_path):
    try:
        object_permission_audit.get_permission_audit(tmp_path / "data", limit=0)
    except ValueError as exc:
        assert str(exc) == "Permission audit limit must be at least 1"
    else:
        raise AssertionError("Expected bad audit limit to fail")


def test_permission_audit_filters_entries(tmp_path):
    data_dir = tmp_path / "data"
    object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:00Z",
            "action": "execute",
            "object_id": "site_home",
            "collection": "site",
            "enforced": False,
            "decision": {"allowed": False},
        },
        data_dir,
    )
    object_permission_audit.append_permission_audit(
        {
            "timestamp": "2026-06-29T00:00:01Z",
            "action": "source",
            "object_id": "basics_counter",
            "collection": "basics",
            "enforced": True,
            "decision": {"allowed": True},
        },
        data_dir,
    )

    assert object_permission_audit.get_permission_audit(
        data_dir,
        action="source",
        allowed=True,
        enforced=True,
    ) == [
        {
            "timestamp": "2026-06-29T00:00:01Z",
            "action": "source",
            "object_id": "basics_counter",
            "collection": "basics",
            "enforced": True,
            "decision": {"allowed": True},
        }
    ]
    assert object_permission_audit.get_permission_audit(data_dir, collection="missing") == []


def test_permission_audit_skips_malformed_lines(tmp_path):
    audit_file = tmp_path / "data" / "permissions" / "audit.jsonl"
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text(
        "{bad json\n"
        '{"timestamp":"2026-06-29T00:00:00Z","action":"execute"}\n',
        encoding="utf-8",
    )

    assert object_permission_audit.get_permission_audit(tmp_path / "data") == [
        {"timestamp": "2026-06-29T00:00:00Z", "action": "execute"}
    ]
