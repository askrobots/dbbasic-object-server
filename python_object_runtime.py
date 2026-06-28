"""Minimal Python object runtime adapter.

This loader is intentionally small and direct so the public ASGI server can
execute simple objects while the hardened runtime is extracted. It is not a
production sandbox.
"""

from __future__ import annotations

import os
import re
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

import object_logs
import object_state
from object_versions import DEFAULT_DATA_DIR


DATA_DIR_ENV = "DBBASIC_DATA_DIR"


class PythonObjectRuntimeError(Exception):
    """Base exception for the minimal Python object runtime."""


class ObjectLoadError(PythonObjectRuntimeError):
    """Raised when an object source file cannot be loaded."""


class MethodNotSupportedError(PythonObjectRuntimeError):
    """Raised when an object does not expose the requested method."""


class ObjectMethodExecutionError(PythonObjectRuntimeError):
    """Raised when an object method fails."""


class PythonObjectRuntime:
    """Load Python object files for direct execution."""

    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir is not None else None

    def load_object(self, path: Path, object_id: str | None = None) -> "PythonObject":
        return PythonObject(path=path, object_id=object_id, base_dir=self._base_dir())

    def _base_dir(self) -> Path:
        if self.base_dir is not None:
            return self.base_dir
        return Path(os.environ.get(DATA_DIR_ENV, DEFAULT_DATA_DIR))


class PythonObject:
    """Executable wrapper around a loaded Python object module."""

    def __init__(self, path: Path, object_id: str | None = None, base_dir: Path | str = DEFAULT_DATA_DIR):
        self.path = Path(path)
        self.object_id = object_id or self.path.stem
        self.state_manager = object_state.ObjectStateManager(self.object_id, base_dir=base_dir)
        self.logger = object_logs.ObjectLogger(self.object_id, base_dir=base_dir)
        self.module = _load_module(self.path, self.object_id)
        self.module._logger = self.logger
        self.module._state_manager = self.state_manager

    def execute(self, method: str, payload: dict[str, Any]) -> Any:
        method_name = method.upper()
        method_func = getattr(self.module, method_name, None)

        if not callable(method_func):
            raise MethodNotSupportedError(
                f"Method {method_name} not supported by object {self.object_id}. "
                f"Available methods: {_available_methods(self.module)}"
            )

        try:
            return method_func(payload)
        except Exception as exc:
            raise ObjectMethodExecutionError(
                f"{method_name} failed for object {self.object_id}: "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            ) from exc


def _load_module(path: Path, object_id: str) -> ModuleType:
    if not path.exists() or not path.is_file():
        raise ObjectLoadError(f"Object source not found: {path}")

    module_name = f"_dbbasic_object_{_safe_module_part(object_id)}"
    module = ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""

    try:
        source = path.read_text()
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as exc:
        raise ObjectLoadError(
            f"Failed to load object {object_id} from {path}: "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        ) from exc

    return module


def _available_methods(module: ModuleType) -> list[str]:
    methods = []
    for name in ["GET", "POST", "PUT", "DELETE"]:
        if callable(getattr(module, name, None)):
            methods.append(name)
    return methods


def _safe_module_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)
