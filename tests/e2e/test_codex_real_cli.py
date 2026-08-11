"""Real-codex-CLI smoke tests for the NDJSON parser.

Marked ``@pytest.mark.e2e`` because they:
- invoke the actual ``codex`` binary on the host
- consume real OpenAI tokens (real money)
- take 20-60s each

Run them via the default ``bin/test`` (e2e tests included).
Skip them via ``bin/test --fast`` (passes ``-m "not e2e"`` to pytest).

Each test uses an isolated ``CODEX_HOME`` (a fresh tmp dir) so the
developer's real ``~/.codex`` login is never touched, and the test
result is independent of whether the host is logged in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.cli._env import parse_env_file
from agento.framework.harness import RunRequest
from tests.harness_fixtures import make_runner

_CODEX_PRESENT = shutil.which("codex") is not None
_PROJECT_ROOT = Path(__file__).parents[2]
_SECRETS = parse_env_file(_PROJECT_ROOT / "secrets.env")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.usefixtures("builtin_harnesses"),
    pytest.mark.skipif(not _CODEX_PRESENT, reason="codex CLI not installed on PATH"),
]


def _run_codex(cmd: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, env=env,
    )


def _codex_login(codex_home: str, mode: str, credential: str) -> subprocess.CompletedProcess:
    """Pipe ``credential`` into ``codex login --with-<mode>`` under an
    isolated CODEX_HOME. ``mode`` is ``api-key`` or ``access-token``."""
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    return subprocess.run(
        ["codex", "login", f"--with-{mode}"],
        input=credential, text=True, capture_output=True, env=env, timeout=30,
    )


@pytest.mark.parametrize("auth_mode", ["api-key", "access-token"])
def test_codex_real_unauth_raises_auth_error(tmp_path, auth_mode):
    """Live verification of the production bug fix.

    Logs codex in with a deliberately-bogus credential inside an
    isolated CODEX_HOME, then runs ``codex exec --json``. The server
    rejects the credential, codex emits ``turn.failed`` with a
    structured ``error.message`` containing ``401 Unauthorized``, and
    our parser must raise ``AuthenticationError``.

    Parameterized over both auth flows codex supports
    (``--with-api-key`` and ``--with-access-token``) so we exercise
    the same parser code path against both upstream auth machineries.

    Skips with a clear message if the bogus-login step itself fails
    in some unexpected way on this host (e.g. codex version mismatch).
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    login = _codex_login(str(codex_home), auth_mode, "sk-deliberately-bogus-for-test")
    if auth_mode == "api-key" and login.returncode != 0:
        pytest.skip(
            f"codex login --with-api-key failed in isolated CODEX_HOME: "
            f"rc={login.returncode}, stderr={login.stderr[:300]}"
        )
    # access-token login may itself reject our garbage token; that's fine —
    # codex still tries to use it on `exec` and gets a 401 from the server,
    # which is the canonical unauth path we want to test.

    runner = make_runner("codex", dry_run=True, credential_required=False)
    cmd = runner.command_builder.headless(
        runner.context, RunRequest(prompt="test", model="gpt-5.4-mini")
    )

    proc = _run_codex(cmd, env_extra={"CODEX_HOME": str(codex_home)})

    # codex returns rc=0 even on turn.failed — detection must come from
    # the structured event, not the exit code.
    with pytest.raises(AuthenticationError) as exc_info:
        runner._parse_output(proc.stdout)

    msg = str(exc_info.value).lower()
    assert "401" in msg or "unauthorized" in msg or "missing bearer" in msg


def test_codex_real_simple_prompt_parses(tmp_path):
    """Happy path: real codex run produces NDJSON the runner can parse.

    Reads ``OPENAI_API_KEY`` from the project's ``secrets.env``, logs
    codex into an isolated ``CODEX_HOME`` with that key, then runs a
    real prompt and asserts the parser extracts session id + tokens +
    agent text. The host's real ``~/.codex`` login is never touched,
    so the result is independent of host state.

    Skips with a clear message if ``secrets.env`` lacks
    ``OPENAI_API_KEY`` — that's an environment precondition.
    """
    api_key = _SECRETS.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip(
            f"OPENAI_API_KEY not found in {_PROJECT_ROOT / 'secrets.env'} — "
            "add it to enable this test. The auth-failure path is still "
            "covered by test_codex_real_unauth_raises_auth_error."
        )

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    login = _codex_login(str(codex_home), "api-key", api_key)
    assert login.returncode == 0, f"codex login failed: {login.stderr[:300]}"

    runner = make_runner("codex", dry_run=True, credential_required=False)
    cmd = runner.command_builder.headless(
        runner.context,
        RunRequest(
            prompt="Reply with exactly the word 'pong' and nothing else.",
            model="gpt-5.4-mini",
        ),
    )

    proc = _run_codex(cmd, env_extra={"CODEX_HOME": str(codex_home)})

    assert proc.returncode == 0, f"codex exited {proc.returncode}: {proc.stderr[:500]}"
    result = runner._parse_output(proc.stdout)
    assert result.session_id, "thread_id missing from thread.started"
    assert result.input_tokens and result.input_tokens > 0
    assert result.output_tokens and result.output_tokens > 0
    assert result.raw_output, "no agent_message text extracted"
