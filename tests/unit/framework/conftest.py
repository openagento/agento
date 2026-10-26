"""Fixtures shared by the framework unit tests."""
from __future__ import annotations

import pytest


@pytest.fixture
def reset_deltas():
    """No delta worker thread, queue or generation leaks into or out of the test."""
    from agento.framework import execution_deltas

    execution_deltas._reset_for_tests()
    yield
    execution_deltas._reset_for_tests()
