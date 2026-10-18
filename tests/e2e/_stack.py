"""Where the e2e stack under test lives.

Default: this checkout, i.e. the dev layout (``docker/docker-compose.dev.yml``, ``bin/agento``).
The release e2e job points ``AGENTO_E2E_PROJECT_ROOT`` at a PyPI-layout deployment instead
(``~/agento-mklauza`` on cyber1: ``docker/docker-compose.yml`` + override, ``.venv/bin/agento``),
after installing the release candidate into it. See docs/deployment/release-e2e.md.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import agento

PROJECT_ROOT = Path(os.environ.get("AGENTO_E2E_PROJECT_ROOT") or Path(__file__).parents[2])

# The code under test, in either layout: src/agento/modules in a checkout, the installed
# package's modules in a deployment (the release job installs the same build into both).
MODULES_DIR = Path(agento.__file__).parent / "modules"

# The stack's own CLI (the deployed build) rather than whichever `agento` `uv run` puts first
# on PATH. In the dev layout both are the checkout's .venv/bin/agento.
_STACK_AGENTO = PROJECT_ROOT / ".venv" / "bin" / "agento"
AGENTO = (
    str(_STACK_AGENTO) if _STACK_AGENTO.is_file()
    else shutil.which("agento") or str(PROJECT_ROOT / "bin" / "agento")
)
