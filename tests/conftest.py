import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_default_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DBBASIC_DATA_DIR", str(tmp_path / "data"))
