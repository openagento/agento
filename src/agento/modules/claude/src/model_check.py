"""Claude's ``check_model``: does the Anthropic API know this model id?

The Claude CLI has no model list; it passes any id to the API. So the check is
``GET /v1/models/{id}``, which costs no tokens: 200 → known, 404 → unknown, anything
else → could not check (an OAuth token that this endpoint does not accept gives
``error``, never a false answer). The request runs in a child process
(``model_probe``) in a runner, so the whole check, DNS included, ends at the deadline.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse

from agento.framework.config_test import ERROR, FAIL, OK, TestResult
from agento.framework.runner import client as runner_client

API_BASE = "https://api.anthropic.com"
PROBE_MODULE = "agento.modules.claude.src.model_probe"

# Aliases the Claude CLI resolves itself. `claude --help` (2.1.165) names `sonnet` and
# `opus` under --model; `haiku` is the third documented alias.
ALIASES = frozenset({"opus", "sonnet", "haiku"})

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _auth_headers(credential) -> dict[str, str] | None:
    payload = credential.credentials or {}
    if credential.type == "anthropic_api_key":
        key = payload.get("api_key")
        return {"x-api-key": key} if key else None
    # OAuth: the same field `fetch_limits` reads (auth.py).
    token = payload.get("subscription_key")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}


def check_model(provider: str, model: str, credential, *, timeout_s: float) -> TestResult | None:
    if model in ALIASES:
        return TestResult(OK, f"model {model} is an alias, resolved by the CLI", code="MODEL_OK")
    if not _MODEL_ID_RE.match(model):
        return TestResult(FAIL, f"{model!r} is not a valid Claude model id", code="MODEL_UNKNOWN")
    if credential is None:
        return None
    headers = _auth_headers(credential)
    if headers is None:
        return TestResult(ERROR, f"model {model} not checked: the credential holds no token",
                          code="MODEL_CHECK_FAILED")
    deadline = time.monotonic() + timeout_s
    request = {
        "url": f"{API_BASE}/v1/models/{urllib.parse.quote(model, safe='')}",
        "headers": {**headers, "anthropic-version": "2023-06-01"},
    }
    try:
        proc = runner_client.run(
            [sys.executable, "-m", PROBE_MODULE], input=json.dumps(request),
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired:
        return TestResult(ERROR, f"model {model} not checked: the API did not answer in time",
                          code="MODEL_CHECK_TIMEOUT")
    status = proc.stdout.strip()  # stderr is discarded (SEC-6)
    if status == "200":
        return TestResult(OK, f"model {model} is known to the Anthropic API", code="MODEL_OK")
    if status == "404":
        return TestResult(FAIL, f"model {model} is not known to the Anthropic API",
                          code="MODEL_UNKNOWN")
    detail = f"HTTP {status}" if status.isdigit() else "no answer"
    return TestResult(ERROR, f"model {model} not checked: the Anthropic API gave {detail}",
                      code="MODEL_CHECK_FAILED")
