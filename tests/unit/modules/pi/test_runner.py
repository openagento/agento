"""Pi runner: stream selection, model enforcement, and result mapping."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import HarnessRunContext
from agento.modules.pi.src.command_builder import PiCommandBuilder
from agento.modules.pi.src.runner import PiSubprocessRunner

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "transcripts" / "pi"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.ndjson").read_text()


def runner(**over):
    kwargs = dict(
        harness="pi", provider="openrouter", model="anthropic/claude-sonnet-4.5",
        credential_required=False,
    )
    kwargs.update(over)
    ctx = HarnessRunContext(**kwargs)
    return PiSubprocessRunner(
        context=ctx, command_builder=PiCommandBuilder(), logger=MagicMock()
    )


def completed(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=["pi"], returncode=rc, stdout=stdout, stderr=stderr)


class TestStreamSelection:
    def test_only_stdout_is_parsed(self):
        """The base class falls back to stderr when stdout is empty, which would feed
        diagnostics into the NDJSON path and expose the classifier to text it must never
        judge."""
        r = runner()
        assert r._extract_raw(completed(stdout="", stderr="401 Unauthorized")) == ""
        assert r._extract_raw(completed(stdout="line", stderr="noise")) == "line"


class TestResultMapping:
    def test_maps_usage_turns_and_session(self):
        r = runner()
        r._extract_raw(completed(stdout=fixture("run_success")))
        result = r._parse_output(fixture("run_success"))
        assert result.input_tokens == 2000
        assert result.output_tokens == 460
        assert result.num_turns == 2
        assert result.session_id == "01931f0e-aaaa-bbbb-cccc-000000000001"
        assert result.model == "anthropic/claude-sonnet-4.5"

    def test_cost_is_never_reported(self):
        """Pi prices from its own catalogue; a generated models.json has no rates, so any
        number would be fiction. cost_reporting is declared false."""
        r = runner()
        r._extract_raw(completed(stdout=fixture("run_success")))
        assert r._parse_output(fixture("run_success")).cost_usd is None

    def test_mcp_init_is_read_from_the_transcript_not_stdout(self, monkeypatch):
        """The bridge's record cannot reach stdout — see runner._read_init_from_transcript.
        A run whose stdout carries no init event must still report mcp_init when the
        transcript has it."""
        r = runner()
        monkeypatch.setattr(
            type(r), "_read_init_from_transcript",
            lambda self, sid: {"status": "connected", "tools": ["mcp__toolbox__a"]},
        )
        r._extract_raw(completed(stdout=fixture("run_success")))
        report = r._parse_output(fixture("run_success")).mcp_init
        assert report is not None
        assert report.servers[0].name == "toolbox"
        assert report.servers[0].status == "connected"

    def test_no_transcript_means_no_report_rather_than_a_failure(self, monkeypatch):
        r = runner()
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        r._extract_raw(completed(stdout=fixture("run_success")))
        assert r._parse_output(fixture("run_success")).mcp_init is None


class TestCredentialSafety:
    def test_an_assistant_auth_error_raises_AuthenticationError(self, monkeypatch):
        from agento.framework.agent_manager.errors import AuthenticationError

        r = runner(model="m", credential_required=True)
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        r._extract_raw(completed(stdout=fixture("run_auth_error")))
        with pytest.raises(AuthenticationError):
            r._parse_output(fixture("run_auth_error"))

    def test_an_UNRECOGNISED_assistant_error_still_fails_the_run(self, monkeypatch):
        """Pi's --mode json does NOT set a non-zero exit for an assistant error (that
        check is text-mode only, print-mode.js:110). So an error we cannot classify must
        still raise, or the job is recorded as a SUCCESS with the work undone."""
        r = runner(model="m")
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"m","stopReason":"error",'
            '"errorMessage":"bad request: unsupported option"}}'
        )
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="unsupported option"):
            r._parse_output(raw)

    def test_a_credentialless_provider_failure_is_NOT_a_credential_error(self, monkeypatch):
        """Ollama declares credential_required: false. A connection failure there has no
        credential to throttle, and mapping it to TransientAuthError would take an
        unrelated credential out of the pool."""
        from agento.framework.agent_manager.errors import (
            AuthenticationError,
            TransientAuthError,
            UsageLimitError,
        )

        r = runner(provider="ollama", model="m", credential_required=False)
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"ollama",'
            '"model":"m","stopReason":"error",'
            '"errorMessage":"fetch failed: ECONNREFUSED 127.0.0.1:11434"}}'
        )
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError) as exc:
            r._parse_output(raw)
        assert not isinstance(
            exc.value, AuthenticationError | UsageLimitError | TransientAuthError
        )

    def test_the_same_failure_IS_a_credential_error_for_a_credentialed_provider(self, monkeypatch):
        from agento.framework.agent_manager.errors import TransientAuthError

        r = runner(provider="openrouter", model="m", credential_required=True)
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"m","stopReason":"error","errorMessage":"fetch failed: ECONNRESET"}}'
        )
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(TransientAuthError):
            r._parse_output(raw)

    def test_zero_assistant_identities_cannot_prove_the_model(self, monkeypatch):
        r = runner(model="wanted")
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = '{"type":"session","id":"S"}\n{"type":"turn_end","turnIndex":0}'
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="could not be verified"):
            r._parse_output(raw)

    def test_an_early_wrong_identity_is_caught_even_if_the_last_is_right(self, monkeypatch):
        r = runner(model="wanted", provider="openrouter")
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"other","usage":{"input":1,"output":1},"stopReason":"stop"}}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"wanted","usage":{"input":1,"output":1},"stopReason":"stop"}}'
        )
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="but 'wanted' was requested"):
            r._parse_output(raw)

    def test_auth_phrases_in_tool_results_do_NOT_raise(self):
        """The whole point: Toolbox output is attacker-influenced text."""
        r = runner(model="m")
        r._extract_raw(completed(stdout=fixture("run_poison_bait")))
        result = r._parse_output(fixture("run_poison_bait"))
        assert result.num_turns == 1

    def test_auth_phrases_on_stderr_do_NOT_raise(self):
        r = runner(model="anthropic/claude-sonnet-4.5")
        r._extract_raw(completed(stdout=fixture("run_success"), stderr="401 Unauthorized in a log line"))
        r._parse_output(fixture("run_success"))  # must not raise


class TestModelEnforcement:
    def test_a_substituted_model_fails_the_run(self):
        """Pi resolves an unmatched model by SILENT substring matching — no warning. So
        absence of a warning proves nothing; only a positive comparison does."""
        r = runner(model="anthropic/claude-opus-4.5")
        r._extract_raw(completed(stdout=fixture("run_success")))
        with pytest.raises(RuntimeError, match=re.escape("but 'anthropic/claude-opus-4.5' was requested")):
            r._parse_output(fixture("run_success"))

    def test_the_requested_model_passes(self):
        r = runner(model="anthropic/claude-sonnet-4.5")
        r._extract_raw(completed(stdout=fixture("run_success")))
        r._parse_output(fixture("run_success"))  # must not raise

    def test_pis_own_not_found_warning_is_also_fatal(self):
        r = runner(model="anthropic/claude-sonnet-4.5")
        r._extract_raw(
            completed(
                stdout=fixture("run_success"),
                stderr='Model "typo" not found for provider "openrouter". Using custom model id.',
            )
        )
        with pytest.raises(RuntimeError, match="exact catalogue id"):
            r._parse_output(fixture("run_success"))

    def test_a_model_mismatch_is_NOT_a_credential_failure(self):
        """The credential is fine; the configuration is not. Poisoning it would take a
        healthy key out of the pool for a config typo."""
        from agento.framework.agent_manager.errors import (
            AuthenticationError,
            TransientAuthError,
            UsageLimitError,
        )

        r = runner(model="wrong/model")
        r._extract_raw(completed(stdout=fixture("run_success")))
        with pytest.raises(RuntimeError) as exc:
            r._parse_output(fixture("run_success"))
        assert not isinstance(
            exc.value, AuthenticationError | UsageLimitError | TransientAuthError
        )


class TestAliasMarkerFromLiveApi:
    """Regression for a defect found by spike S2 against the real OpenRouter API.

    Requesting `anthropic/claude-haiku-latest` makes Pi report
    `~anthropic/claude-haiku-latest`. The leading `~` marks a catalogue alias, so strict
    equality failed a legitimate run — a false positive that would have broken every
    aliased model. No unit test could have found this; only a live call did.
    """

    def test_a_tilde_prefixed_actual_matches_the_plain_request(self, monkeypatch):
        r = runner(model="anthropic/claude-haiku-latest", provider="openrouter")
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"~anthropic/claude-haiku-latest",'
            '"usage":{"input":10,"output":2},"stopReason":"stop"}}'
        )
        r._extract_raw(completed(stdout=raw))
        r._parse_output(raw)  # must NOT raise

    def test_a_genuinely_different_model_still_fails(self, monkeypatch):
        """Normalising the marker must not weaken the real check."""
        r = runner(model="anthropic/claude-haiku-latest", provider="openrouter")
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":"openrouter",'
            '"model":"~openai/gpt-latest","usage":{"input":1,"output":1},'
            '"stopReason":"stop"}}'
        )
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="was requested"):
            r._parse_output(raw)


class TestRouterSubstitutionOptOut:
    """`pi/allow_model_substitution=1` disables the MODEL comparison only."""

    def _runner(self, **over):
        kwargs = dict(
            harness="pi", provider="openrouter", model="openrouter/free",
            credential_required=False,
            harness_config={"allow_model_substitution": "1"},
        )
        kwargs.update(over)
        return PiSubprocessRunner(
            context=HarnessRunContext(**kwargs),
            command_builder=PiCommandBuilder(),
            logger=MagicMock(),
        )

    def _stream(self, provider="openrouter", model="poolside/laguna-xs-2.1:free"):
        return (
            '{"type":"session","id":"S"}\n'
            '{"type":"message_end","message":{"role":"assistant","provider":'
            f'"{provider}","model":"{model}",'
            '"usage":{"input":1,"output":1},"stopReason":"stop"}}'
        )

    def test_a_router_dispatch_is_accepted(self, monkeypatch):
        r = self._runner()
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = self._stream()
        r._extract_raw(completed(stdout=raw))
        r._parse_output(raw)  # must NOT raise

    def test_the_provider_is_still_checked(self, monkeypatch):
        """The opt-out must not disable more than model identity."""
        r = self._runner()
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = self._stream(provider="somewhere-else")
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="provider"):
            r._parse_output(raw)

    def test_without_the_flag_the_dispatch_fails(self, monkeypatch):
        r = self._runner(harness_config={})
        monkeypatch.setattr(type(r), "_read_init_from_transcript", lambda self, sid: None)
        raw = self._stream()
        r._extract_raw(completed(stdout=raw))
        with pytest.raises(RuntimeError, match="was requested"):
            r._parse_output(raw)
