"""Keep the integration ``bootstrap()`` out of unit tests.

``bootstrap()`` fills process-global registries (module configs, observers, harnesses,
workflows, ...). Under pytest-xdist one worker runs unit and integration tests in any
order, so a unit test can run right after an integration test in the same process and
see a fully bootstrapped framework (e.g. observers that open a real DB connection).
"""
from __future__ import annotations

from agento.framework import bootstrap as _bootstrap

_bootstrapped = False


def is_bootstrapped() -> bool:
    return _bootstrapped


def mark_bootstrapped() -> None:
    global _bootstrapped
    _bootstrapped = True


def clear_registries() -> None:
    """Return every registry ``bootstrap()`` fills to the state of a fresh process."""
    global _bootstrapped
    _bootstrap.clear_registries()
    _bootstrap._MANIFESTS.clear()
    _bootstrapped = False
