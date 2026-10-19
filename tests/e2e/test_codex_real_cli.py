"""Real-CLI contract tests for the Codex harness (``AGENTO_E2E=1``), in the sandbox.

``test_agento_run_real_cli.py`` proves that a run exits 0. This file proves what Agento
READS from the run: the ``codex exec --json`` NDJSON stream (session id, usage, agent
text), and that a rejected credential becomes ``AuthenticationError`` — codex exits 0 on
``turn.failed``, so only the structured event can tell. Both run the codex the sandbox
pins (``sandbox_package`` in the module's ``di.json``), never a binary on the host.

The run is pinned (harness codex, a cheap model for the credential type, a healthy
codex credential) on the discovered agent_view; its config and the pool are never
changed.
"""
from __future__ import annotations

import shlex
import subprocess

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.cli._project import compose_file_flags
from agento.framework.harness import RunRequest
from tests.e2e import test_agento_run_real_cli as base
from tests.harness_fixtures import make_runner

_VIEW = base._AGENT_VIEW

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.usefixtures("builtin_harnesses"),
    pytest.mark.skipif(not base._E2E_ENABLED, reason="set AGENTO_E2E=1 to run"),
    pytest.mark.skipif(base._E2E_ENABLED and not _VIEW, reason="no agent_view built"),
]


def _codex_credential() -> dict | None:
    rows = [c for c in base._healthy_credentials()
            if (c.get("scope") or c.get("agent_type")) == "codex"
            and ("codex", c.get("type")) not in base._SKIPPED_TYPES]
    # oauth first: it is the type whose login state Agento materializes itself.
    rows.sort(key=lambda c: c.get("type") != "oauth")
    return rows[0] if rows else None


def _model_for(credential: dict) -> str:
    return base._MODEL_BY_CREDENTIAL_TYPE.get(
        ("codex", credential.get("type")), base._CHEAP_MODEL_BY_HARNESS["codex"]
    )


@pytest.fixture(scope="module")
def codex_credential():
    credential = _codex_credential()
    if credential is None:
        pytest.skip("no healthy codex credential registered")
    return credential


def test_headless_result_contract(codex_credential):
    res = base._agento(
        base.run_args(_VIEW, "codex", _model_for(codex_credential), codex_credential,
                      "Reply with exactly the word: pong"),
        timeout=300,
    )
    assert res.returncode == 0, f"rc={res.returncode}\n{res.stdout[-800:]}{res.stderr[:800]}"

    runner = make_runner("codex", dry_run=True, credential_required=False)
    result = runner._parse_output(res.stdout)
    assert result.session_id, "thread_id missing from thread.started"
    assert result.input_tokens and result.input_tokens > 0
    assert result.output_tokens and result.output_tokens > 0
    assert "pong" in (result.raw_output or "").lower()


@pytest.mark.parametrize("auth_mode", ["api-key", "access-token"])
def test_rejected_credential_raises_auth_error(auth_mode):
    """A bogus login in a throw-away CODEX_HOME inside the sandbox: the server rejects
    it, codex emits ``turn.failed`` (and exits 0), and the parser must raise
    ``AuthenticationError``. Both auth flows codex supports, so both upstream auth
    machineries reach the same parser path. No real credential is involved."""
    runner = make_runner("codex", dry_run=True, credential_required=False)
    cmd = runner.command_builder.headless(
        runner.context, RunRequest(prompt="test", model=base._CHEAP_MODEL_BY_HARNESS["codex"])
    )
    # An access-token login may itself reject the garbage; codex still sends it on
    # `exec` and gets the 401 we want, so only the exec's stdout is asserted.
    script = (
        'export CODEX_HOME="$(mktemp -d)"; '
        f"printf %s sk-deliberately-bogus-for-test | codex login --with-{auth_mode} "
        ">/dev/null 2>&1; "
        f"exec {shlex.join(cmd)}"
    )
    flags = compose_file_flags(base._PROJECT_ROOT)
    proc = subprocess.run(
        ["docker", "compose", *flags, "exec", "-T", "-u", "agent", "sandbox", "sh", "-c", script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
        cwd=str(base._PROJECT_ROOT / "docker"),
    )

    with pytest.raises(AuthenticationError) as exc_info:
        runner._parse_output(proc.stdout)
    msg = str(exc_info.value).lower()
    assert "401" in msg or "unauthorized" in msg or "missing bearer" in msg
