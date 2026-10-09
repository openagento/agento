"""Codex ``check_model`` against a fake ``codex`` on PATH (fixture captured from
``codex debug models --bundled``, codex-cli 0.137.0)."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.config_test import ERROR, FAIL, OK
from agento.modules.codex.src.config import CodexWorkspaceAdapter
from agento.modules.codex.src.model_check import check_model, parse_slugs

# The CLI runs in a runner (WS5): an in-process server here.
pytestmark = pytest.mark.usefixtures("runner_server")

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "model_check" / "codex_debug_models.json"
CRED = CredentialRecord(id=3, scope="codex", type="openai_api_key", label="codex",
                        credentials={"api_key": "sk-codex-test"})


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """``codex`` whose ``login`` and ``debug models`` [--bundled] behave as told."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def install(*, login_rc=0, live="fixture", bundled="fixture", sleep=0, refreshed=True):
        """``refreshed``: the live call writes $HOME/.codex/models_cache.json, as a
        successful account refresh does. Without it the CLI prints its bundled list
        with exit 0 (verified on codex-cli 0.137.0 with no login)."""
        def answer(kind, *, cache=False):
            if kind == "fixture":
                out = f"cat {FIXTURE}"
                if cache:
                    out = f'mkdir -p "$HOME/.codex"; cp {FIXTURE} "$HOME/.codex/models_cache.json"; ' + out
                return out + "; exit 0"
            if kind == "fail":
                return "echo 'live list failed: stderr-marker' >&2; exit 1"
            if cache:
                return (f"mkdir -p \"$HOME/.codex\"; echo '{kind}' > \"$HOME/.codex/models_cache.json\"; "
                        f"echo '{kind}'; exit 0")
            return f"echo '{kind}'; exit 0"

        script = bindir / "codex"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = login ]; then cat >/dev/null; echo stderr-marker >&2; exit {login_rc}; fi\n'
            f"sleep {sleep}\n"
            f'if [ "$3" = --bundled ]; then {answer(bundled)}; fi\n'
            f"{answer(live, cache=refreshed)}\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    return install


def test_parser():
    slugs = parse_slugs(FIXTURE.read_text())
    assert "gpt-5.4-mini" in slugs and "codex-auto-review" in slugs
    for bad in ("not json", '{"nope": []}', '{"models": {}}', '{"models": ""}',
                '{"models": []}', '{"models": [{"id": "gpt-5.4"}]}', '{"models": ["gpt-5.4"]}'):
        assert parse_slugs(bad) is None, bad


def test_a_row_without_a_slug_is_unreadable_not_unknown(fake_codex):
    fake_codex(live='{"models": [{"id": "gpt-5.4"}]}', bundled='{"models": [{"id": "x"}]}')
    result = check_model("openai", "gpt-5.4", CRED, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")


def test_stdout_without_a_refresh_is_not_the_account_list(fake_codex):
    """codex prints its bundled list with exit 0 when the refresh fails: no proof."""
    fake_codex(refreshed=False)
    result = check_model("openai", "gpt-5.4-mini", CRED, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert "it is in the bundled catalogue" in result.message


def test_failed_login_never_trusts_a_live_list(fake_codex):
    """The live list would be an anonymous one: no proof for this account."""
    fake_codex(login_rc=1)
    result = check_model("openai", "gpt-5.4", CRED, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert "codex login failed" in result.message
    assert "it is in the bundled catalogue" in result.message


def test_model_in_live_list(fake_codex):
    fake_codex()
    result = check_model("openai", "gpt-5.4-mini", CRED, timeout_s=10)
    assert (result.status, result.code) == (OK, "MODEL_OK")


def test_unknown_model_with_near_matches(fake_codex):
    fake_codex()
    result = check_model("openai", "gpt-5.4-mni", CRED, timeout_s=10)
    assert (result.status, result.code) == (FAIL, "MODEL_UNKNOWN")
    assert "did you mean: gpt-5.4-mini" in result.message


@pytest.mark.parametrize("model, evidence", [
    ("gpt-5.4", "it is in the bundled catalogue"),
    ("gpt-9", "it is not in the bundled catalogue either"),
])
def test_live_failure_falls_back_to_bundled_and_is_always_error(fake_codex, model, evidence):
    fake_codex(live="fail")
    result = check_model("openai", model, CRED, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert evidence in result.message
    assert "stderr-marker" not in result.message


def test_bad_json_is_a_live_failure(fake_codex):
    fake_codex(live="not json", bundled="also not json")
    result = check_model("openai", "gpt-5.4", CRED, timeout_s=10)
    assert result.code == "MODEL_CHECK_FAILED"
    assert "could not be read either" in result.message


def test_total_deadline(fake_codex):
    fake_codex(sleep=5)
    started = time.monotonic()
    result = check_model("openai", "gpt-5.4", CRED, timeout_s=0.5)
    assert time.monotonic() - started < 2.0
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_TIMEOUT")


def test_codex_login_logs_and_raises_no_cli_output(fake_codex, tmp_path, caplog):
    """Class guard: a credential helper never logs or raises the CLI's raw output."""
    fake_codex(login_rc=1)
    for cred in (CRED, CredentialRecord(id=4, scope="codex", type="codex_access_token",
                                        label="t", credentials={"access_token": "eyJ.x.y"})):
        with caplog.at_level(logging.DEBUG), pytest.raises(AuthenticationError) as exc:
            CodexWorkspaceAdapter().write_credentials(tmp_path / "home", cred)
        assert "stderr-marker" not in str(exc.value)
    assert "stderr-marker" not in caplog.text
    assert "exited 1" in caplog.text


def test_codex_login_gets_its_home(fake_codex, tmp_path, monkeypatch):
    seen = {}
    real_popen = subprocess.Popen

    def spy(args, **kwargs):
        seen.update(kwargs["env"])
        return real_popen(args, **kwargs)

    fake_codex()
    monkeypatch.setattr("agento.framework.runner.server.subprocess.Popen", spy)
    CodexWorkspaceAdapter().write_credentials(tmp_path / "home", CRED)
    assert seen["HOME"] == str(tmp_path / "home")
