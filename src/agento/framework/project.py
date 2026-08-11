"""Locating the agento project root — framework-level, not CLI-level.

``module_discovery`` and ``config_dependents`` need this, and a framework contract must not
import from ``framework/cli/`` (that layering is the reason ``module_discovery`` was extracted
from ``cli/_provisioning`` in the first place). ``cli/_project`` re-exports it so existing CLI
callers are unchanged.
"""
from __future__ import annotations

from pathlib import Path

_PROJECT_MARKER = Path(".agento") / "project.json"
_DEV_NAMES = ('name = "agento"', 'name = "agento-core"')


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for an agento project root.

    Detection order:
    1. ``.agento/project.json`` — created by ``agento install``
    2. ``pyproject.toml`` naming agento / agento-core — git clone dev mode
    """
    current = (start or Path.cwd()).resolve()

    for directory in [current, *current.parents]:
        if (directory / _PROJECT_MARKER).is_file():
            return directory
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            try:
                text = pyproject.read_text()
            except OSError:
                continue
            if any(name in text for name in _DEV_NAMES):
                return directory
    return None


__all__ = ["find_project_root"]
