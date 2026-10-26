"""Fixtures shared by the runner tests."""
from __future__ import annotations

import pytest

from agento.framework.runner.client import RemoteRunner


@pytest.fixture(autouse=True)
def _no_usage_db(monkeypatch):
    """A run's usage row needs a database; these tests have none."""
    monkeypatch.setattr(RemoteRunner, "_record_usage", lambda self, event: None)
