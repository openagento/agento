"""Pi NDJSON parsing and — above all — credential-classification safety.

The negative matrix here is the point of the file. Both shipped harnesses were burned by
scanning raw output for auth strings: an order number "401" inside an MCP payload used to
poison a healthy credential. Toolbox tool results carry Jira comments, mail bodies and SQL
rows, i.e. text that whoever files a ticket controls. So the classifier reads exactly one
field — the ``errorMessage`` of an assistant message whose ``stopReason`` is ``"error"`` —
and every other channel must be provably inert.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agento.framework.agent_manager.errors import (
    AuthenticationError,
    TransientAuthError,
    UsageLimitError,
)
from agento.modules.pi.src.output_parser import (
    classify_error,
    parse_session_id,
    parse_stream,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "transcripts" / "pi"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.ndjson").read_text()


class TestSessionHeader:
    def test_first_line_yields_the_session_id(self):
        first = fixture("run_success").splitlines()[0]
        assert parse_session_id(first) == "01931f0e-aaaa-bbbb-cccc-000000000001"

    def test_non_json_and_other_events_yield_nothing(self):
        assert parse_session_id("Warning: something on stderr") is None
        assert parse_session_id('{"type":"turn_end"}') is None


class TestStreamFolding:
    def test_usage_turns_and_text(self):
        parsed = parse_stream(fixture("run_success"))
        assert parsed.session_id == "01931f0e-aaaa-bbbb-cccc-000000000001"
        assert parsed.input_tokens == 2000        # 1200 + 800
        assert parsed.output_tokens == 460        # 340 + 120
        assert parsed.num_turns == 2              # turn_end events, not messages
        assert "Done." in parsed.text
        assert parsed.provider == "openrouter"
        assert parsed.model == "anthropic/claude-sonnet-4.5"

    def test_an_entry_appended_init_record_is_read_when_present(self):
        """The parser still honours a stream-borne record, but production does NOT emit
        one: the bridge appends during `session_start`, which Pi fires from
        `bindExtensions()` BEFORE attaching the JSON subscriber
        (print-mode.js:53 vs :84). So mcp_init is read from the transcript instead —
        this only proves the parser would cope if a future Pi emitted it later."""
        raw = (
            '{"type":"entry_appended","entry":{"type":"custom",'
            '"customType":"agento-toolbox-init",'
            '"data":{"status":"connected","tools":["a","b"]}}}'
        )
        parsed = parse_stream(raw)
        assert parsed.mcp_init_raw is not None
        assert len(parsed.mcp_init_raw["tools"]) == 2

    def test_the_success_fixture_contains_no_impossible_stdout_event(self):
        """Guards against re-adding a fixture line the real lifecycle cannot produce."""
        assert "entry_appended" not in fixture("run_success")

    def test_unparseable_lines_are_skipped_not_fatal(self):
        raw = "not json at all\n" + fixture("run_success") + "\n{broken\n"
        parsed = parse_stream(raw)
        assert parsed.num_turns == 2

    def test_an_empty_stream_folds_to_empty(self):
        parsed = parse_stream("")
        assert parsed.session_id is None
        assert parsed.num_turns == 0
        assert parsed.error_message is None


class TestCredentialClassification:
    """One channel decides credential state. Everything else must be inert."""

    def test_a_real_assistant_error_is_classified(self):
        parsed = parse_stream(fixture("run_auth_error"))
        assert parsed.error_message == "401 Unauthorized: invalid api key"
        assert isinstance(classify_error(parsed.error_message), AuthenticationError)

    def test_poison_bait_stream_classifies_as_nothing(self):
        """A stream stuffed with auth/limit phrases in EVERY other channel.

        The fixture puts "401 Unauthorized", "quota exceeded", "429 Too Many Requests"
        and "invalid api key" into a user message, a successful tool result, a FAILED
        tool result, and assistant text — the exact shapes a hostile or careless Jira
        ticket would produce. None of them is the classifier's channel.
        """
        parsed = parse_stream(fixture("run_poison_bait"))
        assert parsed.error_message is None
        assert classify_error(parsed.error_message) is None

    def test_a_stopreason_that_is_not_error_is_ignored(self):
        raw = (
            '{"type":"message_end","message":{"role":"assistant","stopReason":"stop",'
            '"errorMessage":"401 Unauthorized","content":[]}}'
        )
        assert parse_stream(raw).error_message is None

    def test_a_user_message_carrying_an_errormessage_is_ignored(self):
        raw = (
            '{"type":"message_end","message":{"role":"user","stopReason":"error",'
            '"errorMessage":"401 Unauthorized","content":[]}}'
        )
        assert parse_stream(raw).error_message is None

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("401 Unauthorized", AuthenticationError),
            ("Invalid API key provided", AuthenticationError),
            ("authentication failed for this request", AuthenticationError),
            ("429 Too Many Requests", UsageLimitError),
            ("quota exceeded for this key", UsageLimitError),
            ("402 Payment Required", UsageLimitError),
            ("insufficient credits", UsageLimitError),
            ("fetch failed: ECONNRESET", TransientAuthError),
            ("503 Service Unavailable", TransientAuthError),
        ],
    )
    def test_phrase_mapping(self, message, expected):
        assert isinstance(classify_error(message), expected)

    def test_a_limit_message_mentioning_401_stays_a_limit(self):
        """Order matters: throttling a good credential beats poisoning it."""
        exc = classify_error("429 Too Many Requests (was previously 401 Unauthorized)")
        assert isinstance(exc, UsageLimitError)

    @pytest.mark.parametrize(
        "message",
        [
            "",
            None,
            "Order 401123 could not be found",       # digits, not the anchored phrase
            "the file api_key.txt is missing",       # substring without the phrase
            "HTTP 404 Not Found",
            "the model produced 4012 tokens",
        ],
    )
    def test_innocuous_messages_classify_as_nothing(self, message):
        assert classify_error(message) is None
