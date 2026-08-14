from __future__ import annotations

import os

# These three MUST come from a view's own agent_view-scoped DB row. A global ENV override would win
# in resolve_field() and apply to EVERY view — defeating both the token boundary and per-view
# attribution — so its presence disables the channel rather than silently changing its meaning.
VIEW_SCOPED_ENV_KEYS = (
    "CONFIG__GITHUB__GITHUB_TOKEN",
    "CONFIG__GITHUB__GITHUB_LOGIN",
    "CONFIG__GITHUB__REPO_ALLOWLIST",
)


def offending_env_keys() -> list[str]:
    """The view-scoped GitHub fields that are (wrongly) set as global ENV overrides."""
    return [key for key in VIEW_SCOPED_ENV_KEYS if os.environ.get(key)]
