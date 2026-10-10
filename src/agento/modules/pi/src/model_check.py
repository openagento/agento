"""Pi's ``check_model``: is the model in Pi's own catalogue for this provider?

``pi --list-models`` prints the catalogue that Pi matches ``--model`` against, as a
table (``provider  model  context …``). It is called with no search argument: a search
that misses prints the same "No models matching" line as a Pi with no provider key,
so only the full table tells "unknown model" from "catalogue not available".
"""
from __future__ import annotations

import difflib
import subprocess
import tempfile
import time

from agento.framework.config_test import ERROR, FAIL, OK, TestResult
from agento.framework.runner import client as runner_client

from .config import PiWorkspaceAdapter
from .runner import PiSubprocessRunner


def parse_models(output: str) -> list[tuple[str, str]]:
    """``(provider, model)`` rows of the ``pi --list-models`` table; ``[]`` for no table."""
    lines = output.splitlines()
    if not lines or lines[0].split()[:2] != ["provider", "model"]:
        return []
    return [tuple(line.split()[:2]) for line in lines[1:] if len(line.split()) >= 2]


def check_model(provider: str, model: str, credential, *, timeout_s: float) -> TestResult | None:
    if credential is None:
        return None  # no hosted catalogue to check (ollama): MODEL_NOT_CHECKED
    deadline = time.monotonic() + timeout_s
    # Created 0700 on the shared /workspace: the CLI runs in a runner.
    with tempfile.TemporaryDirectory(dir=runner_client.shared_tmp()) as home:
        env = {**PiWorkspaceAdapter().credential_env(credential), "HOME": home}
        try:
            proc = runner_client.run(
                ["pi", "--list-models"], cwd=home, env=env,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired:
            return TestResult(ERROR, f"model {model} not checked: pi --list-models timed out",
                              code="MODEL_CHECK_TIMEOUT")
    catalogue = [m for p, m in parse_models(proc.stdout) if p == provider]
    if proc.returncode != 0 or not catalogue:
        # stderr is never shown: only the exit code.
        return TestResult(
            ERROR,
            f"model {model} not checked: Pi lists no models for provider {provider} "
            f"(exit {proc.returncode})",
            code="MODEL_CHECK_FAILED",
        )
    if any(PiSubprocessRunner._same_model(m, model) for m in catalogue):
        return TestResult(OK, f"model {model} found in Pi's catalogue", code="MODEL_OK")
    near = difflib.get_close_matches(model, [m.lstrip("~") for m in catalogue], n=3)
    hint = f"; did you mean: {', '.join(near)}" if near else ""
    return TestResult(FAIL, f"model {model} is not in Pi's catalogue for {provider}{hint}",
                      code="MODEL_UNKNOWN")
