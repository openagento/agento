"""The cron image must build its venv on a pinned CPython.

Unpinned, ``uv venv`` takes the newest managed CPython. A new CPython release has no
wheels yet for native deps (``regex``, ``pydantic-core``, ``cffi``), so the build tries
to compile them and fails with ``No such file or directory: 'cc'``.
"""
from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = (
    Path(__file__).resolve().parents[3] / "src/agento/framework/docker/cron/Dockerfile"
).read_text()


def test_uv_python_is_pinned_before_the_venv_is_created():
    pin = re.search(r"^ENV UV_PYTHON=3\.\d+$", DOCKERFILE, re.MULTILINE)
    assert pin, "cron Dockerfile must set ENV UV_PYTHON=3.<minor>"
    assert pin.start() < DOCKERFILE.index("uv venv")
