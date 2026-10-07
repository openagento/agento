"""Codex's ``check_model``: is the model in the account's model list?

``codex debug models`` prints ``{"models": [{"slug": …}, …]}``. With a logged-in HOME
it refreshes the list for that account, which is the list a run is checked against.
Its stdout is no proof of that: when the refresh fails (or there is no login), it
prints the bundled list with exit 0. A successful refresh writes
``$HOME/.codex/models_cache.json`` (``fetched_at``, ``client_version``), so the
account list is read from that file, and no file means "not read".
``--bundled`` prints the list shipped with the CLI; it is only evidence, never proof,
because an account can see models the CLI did not ship with and the reverse.
"""
from __future__ import annotations

import difflib
import json
import subprocess
import tempfile
import time
from pathlib import Path

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.config_test import ERROR, FAIL, OK, TestResult
from agento.framework.harness import harness_base_env

from .config import CodexWorkspaceAdapter


class _Timeout(Exception):
    pass


def parse_slugs(output: str) -> list[str] | None:
    """The ``models[].slug`` list, or ``None`` unless every row has a string slug: an
    unreadable list is no proof that a model is unknown."""
    try:
        models = json.loads(output)["models"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(models, list) or not models:
        return None
    slugs = [m.get("slug") if isinstance(m, dict) else None for m in models]
    if not all(isinstance(slug, str) and slug for slug in slugs):
        return None
    return slugs


def _list(home: str, deadline: float, *extra: str) -> list[str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout
    try:
        proc = subprocess.run(
            ["codex", "debug", "models", *extra],
            cwd=home, env={**harness_base_env(), "HOME": home},
            capture_output=True, text=True, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise _Timeout from e
    return parse_slugs(proc.stdout) if proc.returncode == 0 else None


def _account_list(home: str, deadline: float) -> list[str] | None:
    """The account's list, from the cache that only a successful refresh writes."""
    _list(home, deadline)
    try:
        return parse_slugs((Path(home) / ".codex" / "models_cache.json").read_text())
    except OSError:
        return None


def check_model(provider: str, model: str, credential, *, timeout_s: float) -> TestResult | None:
    deadline = time.monotonic() + timeout_s
    timeout = TestResult(ERROR, f"model {model} not checked: the codex CLI timed out",
                         code="MODEL_CHECK_TIMEOUT")
    with tempfile.TemporaryDirectory() as home:  # created 0700
        try:
            logged_in = True
            if credential is not None:
                try:
                    CodexWorkspaceAdapter().write_credentials(
                        Path(home), credential,
                        timeout_s=max(0.0, deadline - time.monotonic()),
                    )
                except AuthenticationError:
                    logged_in = False  # the live list would not be this account's
            slugs = _account_list(home, deadline) if logged_in else None
            if slugs is not None:
                if model in slugs:
                    return TestResult(OK, f"model {model} is in the account's model list",
                                      code="MODEL_OK")
                near = difflib.get_close_matches(model, slugs, n=3)
                hint = f"; did you mean: {', '.join(near)}" if near else ""
                return TestResult(FAIL, f"model {model} is not in the account's model list{hint}",
                                  code="MODEL_UNKNOWN")
            bundled = _list(home, deadline, "--bundled")
        except _Timeout:
            return timeout
    if bundled is None:
        evidence = "the bundled list could not be read either"
    elif model in bundled:
        evidence = "it is in the bundled catalogue"
    else:
        evidence = "it is not in the bundled catalogue either"
    return TestResult(
        ERROR,
        f"model {model} not checked: the account's model list could not be read"
        f"{'' if logged_in else ' (codex login failed)'}; {evidence}",
        code="MODEL_CHECK_FAILED",
    )
