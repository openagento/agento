"""Pi ``check_model`` against a fake ``pi`` on PATH that replays captured output
(tests/fixtures/model_check, captured from pi 0.84.1)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.config_test import ERROR, FAIL, OK
from agento.modules.pi.src.model_check import check_model, parse_models

# The CLI runs in a runner (WS5): an in-process server here.
pytestmark = pytest.mark.usefixtures("runner_server")

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "model_check"
CRED = CredentialRecord(id=1, scope="openrouter", type="openrouter_api_key", label="pi",
                        credentials={"api_key": "sk-or-test"})


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    """Install ``pi`` that prints a fixture (or sleeps); records its env and argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "record"

    def install(fixture: str | None = None, *, sleep: float = 0, rc: int = 0):
        body = f'echo "$@" > {record}.argv; env > {record}.env\n'
        if sleep:
            body += f"sleep {sleep}\n"
        if fixture:
            body += f"cat {FIXTURES / fixture}\n"
        body += "echo 'stderr-marker' >&2\n"
        body += f"exit {rc}\n"
        script = bindir / "pi"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        return record

    return install


def test_parser_reads_provider_and_model_columns():
    rows = parse_models((FIXTURES / "pi_list_models.txt").read_text())
    assert ("openrouter", "deepseek/deepseek-v4-flash") in rows
    assert ("openrouter", "~deepseek/deepseek-v4-flash-latest") in rows
    assert ("openai", "gpt-4") in rows
    assert parse_models((FIXTURES / "pi_list_models_no_key.txt").read_text()) == []


def test_model_found(fake_pi):
    record = fake_pi("pi_list_models.txt")
    result = check_model("openrouter", "deepseek/deepseek-v4-flash", CRED, timeout_s=10)
    assert (result.status, result.code) == (OK, "MODEL_OK")
    assert record.with_suffix(".argv").read_text().strip() == "--list-models"
    env = record.with_suffix(".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-test" in env


def test_alias_row_matches_like_the_run_check(fake_pi):
    fake_pi("pi_list_models.txt")
    result = check_model("openrouter", "deepseek/deepseek-v4-flash-latest", CRED, timeout_s=10)
    assert result.code == "MODEL_OK"


def test_qa_01_model_is_unknown_with_near_match(fake_pi):
    fake_pi("pi_list_models.txt")
    result = check_model("openrouter", "deepseek/deepseek-v4.1-flash", CRED, timeout_s=10)
    assert (result.status, result.code) == (FAIL, "MODEL_UNKNOWN")
    assert "did you mean: deepseek/deepseek-v4-flash" in result.message


def test_no_catalogue_is_not_proof_of_unknown(fake_pi):
    fake_pi("pi_list_models_no_key.txt")
    result = check_model("openrouter", "deepseek/deepseek-v4-flash", CRED, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert "stderr-marker" not in result.message


def test_table_without_this_provider_is_check_failed(fake_pi):
    fake_pi("pi_list_models.txt")
    result = check_model("anthropic", "claude-x", CRED, timeout_s=10)
    assert result.code == "MODEL_CHECK_FAILED"


def test_provider_without_credential_cannot_be_checked(fake_pi):
    assert check_model("ollama", "llama3", None, timeout_s=10) is None


def test_total_deadline(fake_pi):
    fake_pi("pi_list_models.txt", sleep=5)
    started = time.monotonic()
    result = check_model("openrouter", "deepseek/deepseek-v4-flash", CRED, timeout_s=0.5)
    assert time.monotonic() - started < 2.0
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_TIMEOUT")


@pytest.mark.usefixtures("builtin_harnesses")
def test_full_tester_path_for_the_qa_01_config(fake_pi, monkeypatch):
    """agent_view/model = deepseek/deepseek-v4.1-flash on pi + openrouter → MODEL_UNKNOWN."""
    from agento.modules.agent_view.src.testers import harness_chain

    fake_pi("pi_list_models.txt")
    monkeypatch.setattr(harness_chain, "resolve_runtime_for_scope",
                        lambda *_: ("pi", "openrouter", "deepseek/deepseek-v4.1-flash"))
    monkeypatch.setattr(harness_chain, "list_credentials",
                        lambda *_a, **_k: [CredentialRecord(id=1, scope="openrouter",
                                                            type="openrouter_api_key", label="pi")])
    monkeypatch.setattr(harness_chain, "get_credential", lambda *_: CRED)

    result = harness_chain.HarnessChainTester().run(object(), scope="agent_view", scope_id=1)

    assert (result.status, result.code) == (FAIL, "MODEL_UNKNOWN")
    assert "did you mean: deepseek/deepseek-v4-flash" in result.message
    assert "sk-or-test" not in result.message
