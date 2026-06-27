from pathlib import Path

import pytest

import object_execution


class FakeObject:
    def __init__(self, output=None):
        self.output = {"status": "ok"} if output is None else output
        self.calls = []

    def execute(self, method, payload):
        self.calls.append((method, payload))
        return self.output


class FailingObject(FakeObject):
    def execute(self, method, payload):
        self.calls.append((method, payload))
        raise RuntimeError("target failed")


class FakeRuntime:
    def __init__(self, obj=None):
        self.obj = FakeObject() if obj is None else obj
        self.loaded = []

    def load_object(self, path, object_id=None):
        self.loaded.append((Path(path), object_id))
        return self.obj


def write_object(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def POST(request):\n    return {'status': 'ok'}\n")
    return path


def test_execute_object_success_returns_structured_result(tmp_path):
    root = tmp_path / "objects"
    source = write_object(root / "basics" / "counter.py")
    obj = FakeObject(output={"count": 1})
    runtime = FakeRuntime(obj)

    request = object_execution.ObjectExecutionRequest(
        object_id="basics_counter",
        method="post",
        payload={"step": 1},
    )
    result = object_execution.execute_object(runtime, request, roots=[root])

    assert result.ok
    assert result.object_id == "basics_counter"
    assert result.method == "POST"
    assert result.path == source
    assert result.result == {"count": 1}
    assert result.error is None
    assert result.started_at.endswith("Z")
    assert result.finished_at.endswith("Z")
    assert result.duration_ms >= 0
    assert runtime.loaded == [(source, "basics_counter")]
    assert obj.calls == [("POST", {"step": 1})]


def test_execute_object_serializes_result_to_dict(tmp_path):
    root = tmp_path / "objects"
    source = write_object(root / "basics" / "counter.py")
    runtime = FakeRuntime(FakeObject(output={"count": 1}))

    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("basics_counter"),
        roots=[root],
    )

    assert result.to_dict()["path"] == str(source)
    assert result.to_dict()["ok"] is True
    assert result.to_dict()["result"] == {"count": 1}
    assert result.to_dict()["error"] is None


def test_execute_object_missing_source_returns_error_without_loading(tmp_path):
    root = tmp_path / "objects"
    runtime = FakeRuntime()

    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("missing_object"),
        roots=[root],
    )

    assert not result.ok
    assert result.result is None
    assert result.path is None
    assert result.error is not None
    assert result.error.type == "ObjectNotFoundError"
    assert result.error.message == "Object not found: missing_object"
    assert result.error.traceback == ""
    assert runtime.loaded == []


def test_execute_object_exception_captures_error_and_traceback(tmp_path):
    root = tmp_path / "objects"
    source = write_object(root / "basics" / "counter.py")
    runtime = FakeRuntime(FailingObject())

    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("basics_counter", method="GET"),
        roots=[root],
    )

    assert not result.ok
    assert result.path == source
    assert result.error is not None
    assert result.error.type == "RuntimeError"
    assert result.error.message == "target failed"
    assert "RuntimeError: target failed" in result.error.traceback
    assert "test_object_execution.py" in result.error.traceback


def test_execute_object_can_raise_failure_for_existing_daemon_style_calls(tmp_path):
    root = tmp_path / "objects"
    write_object(root / "basics" / "counter.py")
    runtime = FakeRuntime(FailingObject())

    with pytest.raises(object_execution.ObjectExecutionFailure) as exc:
        object_execution.execute_object(
            runtime,
            object_execution.ObjectExecutionRequest("basics_counter"),
            roots=[root],
            raise_on_error=True,
        )

    assert str(exc.value) == "target failed"
    assert exc.value.execution_result.error is not None
    assert exc.value.execution_result.error.type == "RuntimeError"


def test_execution_request_defaults_to_get_with_empty_payload(tmp_path):
    root = tmp_path / "objects"
    write_object(root / "basics" / "counter.py")
    obj = FakeObject()
    runtime = FakeRuntime(obj)

    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("basics_counter", method=""),
        roots=[root],
    )

    assert result.ok
    assert obj.calls == [("GET", {})]


def test_execute_object_can_use_explicit_path(tmp_path):
    source = write_object(tmp_path / "detached" / "counter.py")
    runtime = FakeRuntime()

    result = object_execution.execute_object(
        runtime,
        object_execution.ObjectExecutionRequest("detached_counter", path=source),
        roots=[tmp_path / "objects"],
    )

    assert result.ok
    assert result.path == source
    assert runtime.loaded == [(source, "detached_counter")]
