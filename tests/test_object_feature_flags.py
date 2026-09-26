"""The shared opt-in and kill-switch gates (object_feature_flags).

These were six copies before; the tests pin the posture every copy had,
so the extraction cannot quietly change what an operator's flag means.
"""

import object_email
import object_feature_flags
import object_records


def _flags(tmp_path, rows):
    data_dir = tmp_path / "data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)
    (schema_dir / "feature_flags.json").write_text(
        '{"name": "feature_flags", "fields": [{"name": "id"}, {"name": "flag"}, {"name": "value"}]}')
    for index, (flag, value) in enumerate(rows):
        object_records.create_collection_record(
            "feature_flags", {"id": f"f{index}", "flag": flag, "value": value},
            base_dir=data_dir, actor="test")
    return data_dir


def test_env_opt_in_is_off_unless_the_operator_wrote_it():
    """Adoption gate: a fresh box talks to nobody."""
    assert object_feature_flags.env_opt_in("X", {}) is False
    assert object_feature_flags.env_opt_in("X", {"X": ""}) is False
    assert object_feature_flags.env_opt_in("X", {"X": "false"}) is False
    assert object_feature_flags.env_opt_in("X", {"X": " TRUE "}) is True
    assert object_feature_flags.env_opt_in("X", {"X": "1"}) is True


def test_kill_switch_is_on_without_a_table_a_row_or_a_value(tmp_path):
    """Brownout lever: only an explicit off turns a pass off. A switch that
    failed closed on a missing table would turn every fresh box's passes
    off for a reason nobody chose."""
    missing = tmp_path / "nothing"
    assert object_feature_flags.kill_switch_on("mail_sync_enabled", base_dir=missing) is True
    data_dir = _flags(tmp_path, [("other_enabled", "off"), ("mail_sync_enabled", "")])
    assert object_feature_flags.kill_switch_on("mail_sync_enabled", base_dir=data_dir) is True
    assert object_feature_flags.kill_switch_on("other_enabled", base_dir=data_dir) is False


def test_the_existing_switches_now_read_through_the_shared_gate(tmp_path):
    """email_pass_enabled keeps its name and its meaning."""
    data_dir = _flags(tmp_path, [("email_enabled", "no")])
    assert object_email.email_pass_enabled(base_dir=data_dir) is False


def test_refusal_names_what_to_set(tmp_path):
    """A refusal that does not name the variable is a refusal nobody can act on."""
    data_dir = _flags(tmp_path, [("mail_sync_enabled", "off")])
    off_env = object_feature_flags.refusal(
        env_name="DBBASIC_ENABLE_MAIL_SYNC", flag="mail_sync_enabled",
        base_dir=data_dir, env={})
    assert "DBBASIC_ENABLE_MAIL_SYNC" in off_env
    off_flag = object_feature_flags.refusal(
        env_name="DBBASIC_ENABLE_MAIL_SYNC", flag="mail_sync_enabled",
        base_dir=data_dir, env={"DBBASIC_ENABLE_MAIL_SYNC": "true"})
    assert "mail_sync_enabled" in off_flag
    assert object_feature_flags.refusal(
        env_name="DBBASIC_ENABLE_MAIL_SYNC", flag="mail_sync_enabled",
        base_dir=tmp_path / "nothing", env={"DBBASIC_ENABLE_MAIL_SYNC": "yes"}) is None
