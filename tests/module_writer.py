"""Write a fake module to disk. One helper, so no test copies the module.json boilerplate."""
from __future__ import annotations

import json
from pathlib import Path


def write_module(root: Path, name: str, **files: object) -> Path:
    """Create ``root/name`` with a ``module.json`` plus ``files``; return the module dir.

    A keyword is a file name with its last ``_`` read as ``.`` (``system_json`` ->
    ``system.json``); a name that already holds a ``.`` is kept, so a nested file goes in
    as ``**{"src/wf.py": ...}``. A ``str`` value is written as is, anything else as JSON.
    A dict ``module_json`` is merged over the defaults: ``name``, ``version`` 1.0.0, ``description``.
    """
    mod = root / name
    mod.mkdir(parents=True)
    manifest = files.pop("module_json", {})
    if isinstance(manifest, dict):
        manifest = {"name": name, "version": "1.0.0", "description": name, **manifest}
    for key, content in {"module_json": manifest, **files}.items():
        path = mod / (key if "." in key else ".".join(key.rsplit("_", 1)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return mod
