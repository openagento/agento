"""Reuses the fake harness's behaviour; only its di.json differs (it claims a
credential scope that another module already owns)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_FAKE = Path(__file__).resolve().parents[2] / "fake_harness" / "src" / "adapter.py"
_spec = importlib.util.spec_from_file_location("_fake_harness_adapter", _FAKE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

FakeHarnessAdapter = _mod.FakeHarnessAdapter
