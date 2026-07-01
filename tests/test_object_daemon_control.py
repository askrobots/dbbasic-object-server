import json

import object_daemon_control
import object_ids
import object_state


def test_scheduler_task_controls_use_daemon_state_shape(tmp_path):
    data_dir = tmp_path / "data"

    task = object_daemon_control.create_scheduler_task(
        {
            "object_id": "system_dashboard",
            "method": "POST",
            "type": "onetime",
            "schedule": "2026-07-01T12:00:00Z",
            "payload": {"refresh": True},
        },
        actor="admin",
        base_dir=data_dir,
        now=100,
    )

    assert object_ids.is_uuid4(task["id"])
    assert task["status"] == "active"
    assert task["payload_present"] is True
    assert "payload" not in task

    state = object_state.get_object_state("scheduler", data_dir)
    assert list(state) == [f"task_{task['id']}"]
    stored = json.loads(state[f"task_{task['id']}"])
    assert stored["payload"] == {"refresh": True}

    listed = object_daemon_control.list_scheduler_tasks(base_dir=data_dir)
    assert listed["tasks"] == [task]

    updated = object_daemon_control.update_scheduler_task(
        task["id"],
        {"status": "paused"},
        actor="admin",
        base_dir=data_dir,
        now=200,
    )
    assert updated["status"] == "paused"
    assert updated["updated_at"] == 200

    deleted = object_daemon_control.delete_scheduler_task(task["id"], base_dir=data_dir)
    assert deleted["id"] == task["id"]
    assert object_daemon_control.list_scheduler_tasks(base_dir=data_dir)["tasks"] == []


def test_queue_message_controls_use_daemon_state_shape(tmp_path):
    data_dir = tmp_path / "data"

    message = object_daemon_control.enqueue_message(
        {
            "object_id": "system_dashboard",
            "method": "POST",
            "queue_name": "default",
            "priority_level": 5,
            "payload": {"refresh": True},
        },
        actor="admin",
        base_dir=data_dir,
        now=100,
    )

    assert object_ids.is_uuid4(message["id"])
    assert message["status"] == "pending"
    assert message["message"]["object_id"] == "system_dashboard"
    assert message["message"]["payload_present"] is True
    assert "payload" not in message["message"]

    state = object_state.get_object_state("queue", data_dir)
    assert list(state) == [f"msg_default_5_100_{message['id']}"]
    stored = json.loads(state[f"msg_default_5_100_{message['id']}"])
    assert stored["message"]["payload"] == {"refresh": True}

    listed = object_daemon_control.list_queue_messages(base_dir=data_dir)
    assert listed["messages"] == [message]

    cancelled = object_daemon_control.update_queue_message(
        message["id"],
        {"action": "cancel"},
        actor="admin",
        base_dir=data_dir,
        now=200,
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] == 200

    retried = object_daemon_control.update_queue_message(
        message["id"],
        {"action": "retry"},
        actor="admin",
        base_dir=data_dir,
        now=300,
    )
    assert retried["status"] == "pending"
    assert retried["visible_after"] == 300

    deleted = object_daemon_control.delete_queue_message(message["id"], base_dir=data_dir)
    assert deleted["id"] == message["id"]
    assert object_daemon_control.list_queue_messages(base_dir=data_dir)["messages"] == []


def test_control_lists_keep_payloads_redacted_unless_requested(tmp_path):
    data_dir = tmp_path / "data"
    task = object_daemon_control.create_scheduler_task(
        {
            "object_id": "system_dashboard",
            "schedule": "2026-07-01T12:00:00Z",
            "payload": {"secret": "not-for-list"},
        },
        base_dir=data_dir,
    )
    message = object_daemon_control.enqueue_message(
        {
            "object_id": "system_dashboard",
            "payload": {"secret": "not-for-list"},
        },
        base_dir=data_dir,
    )

    task_list = object_daemon_control.list_scheduler_tasks(base_dir=data_dir)
    message_list = object_daemon_control.list_queue_messages(base_dir=data_dir)

    assert "payload" not in task_list["tasks"][0]
    assert "payload" not in message_list["messages"][0]["message"]

    task_list = object_daemon_control.list_scheduler_tasks(base_dir=data_dir, include_payload=True)
    message_list = object_daemon_control.list_queue_messages(base_dir=data_dir, include_payload=True)

    assert task_list["tasks"][0]["id"] == task["id"]
    assert task_list["tasks"][0]["payload"] == {"secret": "not-for-list"}
    assert message_list["messages"][0]["id"] == message["id"]
    assert message_list["messages"][0]["message"]["payload"] == {"secret": "not-for-list"}
