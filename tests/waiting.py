"""Wait for a condition another thread or process sets. One helper, so no test copies it."""
from __future__ import annotations

import time
from collections.abc import Callable


def wait_until(predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.01) -> bool:
    """True as soon as ``predicate()`` is truthy, False when ``timeout`` seconds pass first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
