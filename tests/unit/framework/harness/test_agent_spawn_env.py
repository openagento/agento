"""The agent's spawn environment must not carry the framework's credential store.

Impl review round 5 (CRITICAL): an agent inherited the consumer's whole environment, so
it could read and decrypt EVERY credential the framework holds — including another
agent_view's SSH private key — either straight from the database or by running
``bin/agento agent_view:prepare-run <peer-view>``, which prints the resolved key.

These tests pin the reduction: whatever the consumer holds, the spawned process does not
get the database credentials, the decryption passphrase, or any ``CONFIG__*`` override.
They do NOT claim isolation — one uid still reads the cron env file (DECISIONS.md D-SSH-1).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.framework.credential_store_env import without_credential_store_env
from agento.framework.harness import RunRequest, clear
from tests.harness_fixtures import make_runner, register_builtin_harnesses

STORE_ENV = {
    "MYSQL_HOST": "mysql",
    "MYSQL_USER": "cron_agent",
    "MYSQL_PASSWORD": "cronagent_pass",
    "AGENTO_ENCRYPTION_KEY": "passphrase-that-decrypts-every-credential",
    "CONFIG__JIRA__API_TOKEN": "plaintext-override",
}


class TestTheAgentDoesNotInheritTheCredentialStore:
    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _spawn_env(self, monkeypatch):
        for name, value in STORE_ENV.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("AGENTO_CONSUMER_MAX_WORKERS", "10")
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._execute_process = MagicMock(
            return_value=MagicMock(
                returncode=0,
                stdout='{"type":"result","result":"ok","usage":{}}\n',
                stderr="",
            ),
        )
        runner._record_usage = MagicMock()
        runner.execute(RunRequest(prompt="x"))
        return runner._execute_process.call_args[0][1]

    def test_no_database_credential_reaches_the_agent(self, monkeypatch):
        env = self._spawn_env(monkeypatch)
        assert not [k for k in env if k.startswith("MYSQL_")]
        assert "cronagent_pass" not in "\n".join(env.values())

    def test_no_decryption_passphrase_reaches_the_agent(self, monkeypatch):
        env = self._spawn_env(monkeypatch)
        assert "AGENTO_ENCRYPTION_KEY" not in env

    def test_no_config_override_reaches_the_agent(self, monkeypatch):
        """A ``CONFIG__*`` value is plaintext config — and config holds credentials."""
        env = self._spawn_env(monkeypatch)
        assert not [k for k in env if k.startswith("CONFIG__")]

    def test_the_ordinary_environment_still_reaches_the_agent(self, monkeypatch):
        """The strip is targeted: an agent with no PATH cannot run anything."""
        env = self._spawn_env(monkeypatch)
        assert env["PATH"] == "/usr/bin"
        assert env["AGENTO_CONSUMER_MAX_WORKERS"] == "10"


class TestTheHelperIsClosedUnderNewNames:
    def test_a_new_variable_under_a_store_prefix_is_covered(self):
        """The set is prefix-based so a knob added later needs no edit here."""
        kept = without_credential_store_env({
            "MYSQL_SSL_CA": "/ca.pem",
            "CONFIG__NEW_MODULE__SECRET": "s",
            "AGENTO_ENCRYPTION_KEY": "k",
            "HOME": "/home/agent",
        })
        assert kept == {"HOME": "/home/agent"}

    def test_the_runs_own_credential_is_not_stripped(self):
        """The provider API key is delivered per run, deliberately — see the runner."""
        assert without_credential_store_env({"ANTHROPIC_API_KEY": "sk-x"}) == {
            "ANTHROPIC_API_KEY": "sk-x",
        }
