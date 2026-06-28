import pytest

import object_state
import python_object_runtime


def write_source(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_python_object_runtime_loads_and_executes_method(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "counter.py",
        "def GET(request):\n    return {'count': int(request['count']) + 1}\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    obj = runtime.load_object(source, object_id="basics_counter")

    assert obj.execute("GET", {"count": "1"}) == {"count": 2}


def test_python_object_runtime_normalizes_method_name(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "counter.py",
        "def GET(request):\n    return {'ok': True}\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    obj = runtime.load_object(source, object_id="basics_counter")

    assert obj.execute("get", {}) == {"ok": True}


def test_python_object_runtime_reports_missing_method(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "counter.py",
        "def POST(request):\n    return {'ok': True}\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    obj = runtime.load_object(source, object_id="basics_counter")

    with pytest.raises(python_object_runtime.MethodNotSupportedError) as exc:
        obj.execute("GET", {})

    assert "Method GET not supported by object basics_counter" in str(exc.value)
    assert "Available methods: ['POST']" in str(exc.value)


def test_python_object_runtime_wraps_method_exception(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "broken.py",
        "def GET(request):\n    raise RuntimeError('boom')\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    obj = runtime.load_object(source, object_id="basics_broken")

    with pytest.raises(python_object_runtime.ObjectMethodExecutionError) as exc:
        obj.execute("GET", {})

    assert "GET failed for object basics_broken: RuntimeError: boom" in str(exc.value)
    assert "Traceback" in str(exc.value)


def test_python_object_runtime_wraps_load_errors(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "bad_syntax.py",
        "def GET(request):\n    return {\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    with pytest.raises(python_object_runtime.ObjectLoadError) as exc:
        runtime.load_object(source, object_id="basics_bad_syntax")

    assert "Failed to load object basics_bad_syntax" in str(exc.value)
    assert "SyntaxError" in str(exc.value)


def test_python_object_runtime_loads_fresh_source_each_time(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "counter.py",
        "def GET(request):\n    return {'version': 1}\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime()

    first = runtime.load_object(source, object_id="basics_counter")
    source.write_text("def GET(request):\n    return {'version': 2}\n")
    second = runtime.load_object(source, object_id="basics_counter")

    assert first.execute("GET", {}) == {"version": 1}
    assert second.execute("GET", {}) == {"version": 2}


def test_python_object_runtime_injects_state_manager(tmp_path):
    source = write_source(
        tmp_path / "objects" / "basics" / "counter.py",
        "_state_manager = None\n"
        "def GET(request):\n"
        "    count = _state_manager.get('count', 0) + 1\n"
        "    _state_manager.set('count', count)\n"
        "    return {'count': count}\n",
    )
    runtime = python_object_runtime.PythonObjectRuntime(base_dir=tmp_path / "data")

    first = runtime.load_object(source, object_id="basics_counter")
    assert first.execute("GET", {}) == {"count": 1}

    second = runtime.load_object(source, object_id="basics_counter")
    assert second.execute("GET", {}) == {"count": 2}
    assert object_state.get_object_state("basics_counter", base_dir=tmp_path / "data") == {
        "count": 2
    }
