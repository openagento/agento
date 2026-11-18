"""Workspace directory layout constants.

Defines the three-layer workspace structure:
- theme:     template/scaffolding (base for every build)
- build:     compiled readonly workspace per agent_view
- artifacts: writable per-job directory (agent outputs + toolbox I/O)
"""
from __future__ import annotations

import os

BASE_WORKSPACE_DIR = os.environ.get("AGENTO_WORKSPACE_DIR", "/workspace")

THEME_DIR = os.path.join(BASE_WORKSPACE_DIR, "theme")
BUILD_DIR = os.path.join(BASE_WORKSPACE_DIR, "build")
ARTIFACTS_DIR = os.path.join(BASE_WORKSPACE_DIR, "artifacts")

# Files a panel user handed the agent, before a run copied them into its thread. `web` writes
# here and the sweep removes; a run only ever has the COPY. `agent`-owned 0700 is what keeps
# every run out: a run has a per-run uid (runner/uids.py), never `agent`.
UPLOADS_DIRNAME = "uploads"
UPLOADS_DIR = os.path.join(BASE_WORKSPACE_DIR, UPLOADS_DIRNAME)
