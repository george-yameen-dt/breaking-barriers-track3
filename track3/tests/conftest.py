"""Offline test setup: track3/ on the path, and a real Checkpoint that never reaches AWS, Arize or Dynatrace."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TRACK3 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRACK3))


@pytest.fixture
def offline_cp(tmp_path, monkeypatch):
    import checkpoint

    for key in ("ARIZE_API_KEY", "DT_TRACES_TOKEN", "DT_PLATFORM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(checkpoint, "ARIZE_KEY_FILE", tmp_path / "no-arize-key")
    monkeypatch.setattr(checkpoint.boto3, "client", lambda *a, **k: MagicMock())
    return checkpoint.Checkpoint("test-run", "0" * 31 + "1", tmp_path / "run")
