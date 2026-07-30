from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agento.framework.agent_manager.errors import TransientAuthError, UsageLimitError
from agento.framework.runner import McpInitReport, McpServerStatus
from agento.modules.claude.src.output_parser import (
    AuthenticationError,
    _parse_reset_at,
    parse_claude_output,
)

# ---- Legacy single JSON format (backward compat) ----

def test_parse_output_success(claude_success):
    raw = json.dumps(claude_success)
    result = parse_claude_output(raw)

    assert result.input_tokens == 1500
    assert result.output_tokens == 800
    assert result.cost_usd == 0.0123
    assert result.num_turns == 3
    assert result.duration_ms == 45000
    assert result.subtype == "success"


def test_parse_output_invalid_json():
    result = parse_claude_output("this is not json at all")

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None
    assert result.raw_output == "this is not json at all"


def test_parse_output_partial_data():
    raw = json.dumps({"usage": {"input_tokens": 100}})
    result = parse_claude_output(raw)

    assert result.input_tokens == 100
    assert result.output_tokens is None
    assert result.cost_usd is None


def test_stats_line_full(claude_success):
    raw = json.dumps(claude_success)
    result = parse_claude_output(raw)
    line = result.stats_line
    assert "turns=3" in line
    assert "in=1500" in line
    assert "out=800" in line
    assert "cost_usd=0.0123" in line
    assert "duration_ms=45000" in line


def test_stats_line_missing_data():
    result = parse_claude_output("bad data")
    line = result.stats_line
    assert "turns=?" in line
    assert "in=?" in line


# ---- Stream-JSON format ----

def test_parse_stream_json_result_event():
    raw = (
        '{"type": "init", "session_id": "sess-abc"}\n'
        '{"type": "assistant", "message": "working..."}\n'
        '{"type": "result", "result": "done", "is_error": false, '
        '"usage": {"input_tokens": 200, "output_tokens": 100}, '
        '"total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 3000, '
        '"session_id": "sess-abc"}\n'
    )
    result = parse_claude_output(raw)

    assert result.input_tokens == 200
    assert result.output_tokens == 100
    assert result.cost_usd == 0.01
    assert result.num_turns == 2
    assert result.duration_ms == 3000
    assert result.subtype == "sess-abc"


def test_parse_stream_json_session_id_from_init():
    raw = (
        '{"type": "init", "session_id": "sess-init"}\n'
        '{"type": "result", "result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}\n'
    )
    result = parse_claude_output(raw)

    assert result.subtype == "sess-init"
    assert result.input_tokens == 10


def test_parse_stream_json_error_event():
    raw = (
        '{"type": "init", "session_id": "sess-err"}\n'
        '{"type": "result", "result": "something went wrong", "is_error": true}\n'
    )
    with pytest.raises(RuntimeError, match="something went wrong"):
        parse_claude_output(raw)


def test_parse_stream_json_auth_error():
    raw = (
        '{"type": "result", "result": "authentication_error: invalid token", "is_error": true}\n'
    )
    with pytest.raises(AuthenticationError, match="authentication_error"):
        parse_claude_output(raw)


def test_credential_401_raises_authentication_error():
    raw = (
        '{"type": "result", "is_error": true, '
        '"result": "Failed to authenticate. API Error: 401 Invalid authentication credentials"}\n'
    )
    with pytest.raises(AuthenticationError, match="401 Invalid authentication credentials"):
        parse_claude_output(raw)


def test_session_limit_raises_usage_limit_error_stream_json():
    raw = (
        '{"type": "result", "is_error": true, '
        '"result": "You\'ve hit your session limit \\u00b7 resets 1pm (Europe/Warsaw)"}\n'
    )
    with pytest.raises(UsageLimitError) as exc:
        parse_claude_output(raw)
    # A reset time was parseable → reset_at is set (not the default-throttle fallback).
    assert exc.value.reset_at is not None
    assert not isinstance(exc.value, AuthenticationError)


def test_session_limit_raises_usage_limit_error_single_json():
    raw = json.dumps({"is_error": True, "result": "You've hit your session limit"})
    with pytest.raises(UsageLimitError):
        parse_claude_output(raw)


def test_usage_limit_error_without_reset_has_none_reset_at():
    raw = json.dumps({"is_error": True, "result": "rate_limit_error: slow down"})
    with pytest.raises(UsageLimitError) as exc:
        parse_claude_output(raw)
    assert exc.value.reset_at is None


def test_auth_error_wins_over_limit_phrase():
    # A message containing BOTH an auth phrase and a limit phrase is a permanent
    # auth failure (poison), not a temporary throttle.
    raw = json.dumps({"is_error": True, "result": "authentication_error; usage limit"})
    with pytest.raises(AuthenticationError):
        parse_claude_output(raw)


class TestParseResetAt:
    NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)  # 08:00 UTC

    def test_pm_time_in_warsaw_converts_to_utc(self):
        # 1pm Europe/Warsaw (UTC+2 in July) = 11:00 UTC, later today.
        r = _parse_reset_at("resets 1pm (Europe/Warsaw)", now=self.NOW)
        assert r == datetime(2026, 7, 22, 11, 0)

    def test_past_time_rolls_to_tomorrow(self):
        # 9am Europe/Warsaw = 07:00 UTC, already past 08:00 → next day.
        r = _parse_reset_at("resets 9am (Europe/Warsaw)", now=self.NOW)
        assert r == datetime(2026, 7, 23, 7, 0)

    def test_unparseable_returns_none(self):
        assert _parse_reset_at("resets soon", now=self.NOW) is None
        assert _parse_reset_at("no reset info here", now=self.NOW) is None

    def test_bad_timezone_returns_none(self):
        assert _parse_reset_at("resets 1pm (Not/AZone)", now=self.NOW) is None


def test_transient_401_does_not_raise_authentication_error():
    raw = (
        '{"type": "result", "is_error": true, '
        '"result": "API Error: 401 upstream gateway hiccup"}\n'
    )
    with pytest.raises(RuntimeError) as exc:
        parse_claude_output(raw)
    assert not isinstance(exc.value, AuthenticationError)


def test_parse_stream_json_partial_output_with_session():
    """Partial output from timeout -- only init event, no result."""
    raw = '{"type": "init", "session_id": "sess-partial"}\n'
    result = parse_claude_output(raw)

    assert result.subtype == "sess-partial"
    assert result.input_tokens is None


def test_parse_stream_json_no_result_event():
    """No recognizable events -- fallback."""
    raw = "some random text\nnot json lines\n"
    result = parse_claude_output(raw)

    assert result.raw_output == raw
    assert result.input_tokens is None


# ---- MCP init self-report (system/init line) ----

def _result_line(session_id: str = "sess-mcp") -> str:
    return (
        '{"type": "result", "result": "ok", "is_error": false, '
        '"usage": {"input_tokens": 10, "output_tokens": 5}, '
        f'"session_id": "{session_id}"}}\n'
    )


def test_parse_claude_output_extracts_mcp_init():
    raw = (
        '{"type": "system", "subtype": "init", "session_id": "sess-mcp", '
        '"mcp_servers": [{"name": "toolbox", "status": "connected"}, '
        '{"name": "context7", "status": "failed"}]}\n'
        + _result_line()
    )
    result = parse_claude_output(raw)

    assert result.mcp_init == McpInitReport(
        servers=(
            McpServerStatus("toolbox", "connected"),
            McpServerStatus("context7", "failed"),
        )
    )


def test_parse_claude_output_keeps_pending_status_verbatim():
    """The parser transcribes the CLI's status word; it never interprets it.

    `pending` means the handshake had not finished when init was printed —
    deciding what that implies belongs to the consumer (app_monitor), not here.
    """
    raw = (
        '{"type": "system", "subtype": "init", "session_id": "sess-mcp", '
        '"mcp_servers": [{"name": "toolbox", "status": "pending"}]}\n'
        + _result_line()
    )
    result = parse_claude_output(raw)

    assert result.mcp_init == McpInitReport(
        servers=(McpServerStatus("toolbox", "pending"),)
    )


def test_parse_claude_output_empty_servers_list():
    raw = (
        '{"type": "system", "subtype": "init", "mcp_servers": []}\n'
        + _result_line()
    )
    result = parse_claude_output(raw)

    # Empty list IS a valid init report ("started, no MCP servers visible"),
    # NOT the same as "no init at all".
    assert result.mcp_init == McpInitReport(servers=())
    assert result.mcp_init is not None


def test_parse_claude_output_no_init_event():
    raw = _result_line()
    result = parse_claude_output(raw)

    assert result.mcp_init is None


def test_parse_claude_output_malformed_init_skipped():
    # Server entry missing "status" -> whole report untrusted, no exception.
    raw = (
        '{"type": "system", "subtype": "init", '
        '"mcp_servers": [{"name": "toolbox"}]}\n'
        + _result_line()
    )
    result = parse_claude_output(raw)

    assert result.mcp_init is None


def test_parse_claude_output_only_first_init_wins():
    raw = (
        '{"type": "system", "subtype": "init", '
        '"mcp_servers": [{"name": "toolbox", "status": "connected"}]}\n'
        '{"type": "system", "subtype": "init", '
        '"mcp_servers": [{"name": "context7", "status": "failed"}]}\n'
        + _result_line()
    )
    result = parse_claude_output(raw)

    assert result.mcp_init == McpInitReport(
        servers=(McpServerStatus("toolbox", "connected"),)
    )


def test_parse_claude_output_mcp_init_survives_missing_result_event():
    # Partial output (timeout): init line present, no result event.
    raw = (
        '{"type": "system", "subtype": "init", "session_id": "sess-x", '
        '"mcp_servers": [{"name": "toolbox", "status": "connected"}]}\n'
    )
    result = parse_claude_output(raw)

    assert result.subtype == "sess-x"
    assert result.mcp_init == McpInitReport(
        servers=(McpServerStatus("toolbox", "connected"),)
    )


# ---- Transient credential rejection (revoked / unrecognised 401) ----
# The exact message reported from production (seen on two separate jobs).
_REVOKED_MSG = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."


def test_revoked_oauth_token_raises_transient_auth_error_stream_json():
    raw = json.dumps({"type": "result", "is_error": True, "result": _REVOKED_MSG})
    with pytest.raises(TransientAuthError, match="has been revoked"):
        parse_claude_output(raw + "\n")


def test_revoked_oauth_token_raises_transient_auth_error_single_json():
    raw = json.dumps({"is_error": True, "result": _REVOKED_MSG})
    with pytest.raises(TransientAuthError):
        parse_claude_output(raw)


def test_revoked_is_not_a_permanent_auth_error():
    # Must NOT poison the token: TransientAuthError is a sibling of
    # AuthenticationError, never a subclass.
    raw = json.dumps({"is_error": True, "result": _REVOKED_MSG})
    with pytest.raises(TransientAuthError) as exc:
        parse_claude_output(raw)
    assert not isinstance(exc.value, AuthenticationError)


def test_revoked_without_401_code_is_still_transient():
    raw = json.dumps({"is_error": True, "result": "OAuth access token has been revoked"})
    with pytest.raises(TransientAuthError):
        parse_claude_output(raw)


@pytest.mark.parametrize("msg", [
    # Auth verb BEFORE the code (the observed shape) ...
    "Failed to authenticate. API Error: 401 some brand new wording",
    # ... and credential words only AFTER it. The matcher must be order-independent.
    "API Error: 401 OAuth access token rejected",
    "API Error: 401 invalid bearer token",
    "API Error: 401 credential rejected",
    "API Error: 401 Unauthorized",
])
def test_unrecognised_401_credential_wording_defaults_to_transient(msg):
    # Any FUTURE 401 wording must fail over, not degrade to a generic in-place retry.
    raw = json.dumps({"is_error": True, "result": msg})
    with pytest.raises(TransientAuthError):
        parse_claude_output(raw)


@pytest.mark.parametrize("msg", [
    # "401" only as a substring of a token count -> \b401\b must not match.
    "Prompt is too long: 401234 tokens > 200000 maximum",
    # A 401 with no credential word at all -> not enough signal to reclassify.
    "API Error: 401",
    # A credential word with no rejection signal -> a local file problem, not a bad
    # pool credential; throttling the token would fix nothing.
    "the access token could not be read from disk",
    # Revocation wording with no credential context -> an authorization failure INSIDE
    # the run (a repo/workspace/permission grant), not a rejected pool credential.
    # These must not cost the token a 15-minute cooldown.
    "workspace access has been revoked",
    "permission has been revoked",
    "your access to the repository has been revoked",
])
def test_matcher_does_not_over_reach(msg):
    # False positives cost a 15-min throttle + failover, so BOTH signals are required:
    # a credential-context word AND a rejection signal (\b401\b or revoked).
    raw = json.dumps({"is_error": True, "result": msg})
    with pytest.raises(RuntimeError) as exc:
        parse_claude_output(raw)
    assert type(exc.value) is RuntimeError


def test_revoked_with_credential_context_is_transient_without_a_401():
    # The rejection signal may be revocation wording rather than a 401 code, as long as
    # credential context is present.
    raw = json.dumps({"is_error": True, "result": "the api key credential was revoked"})
    with pytest.raises(TransientAuthError):
        parse_claude_output(raw)


def test_known_permanent_auth_phrases_still_poison():
    # Regression guard for the four pre-existing AUTH_ERROR_PHRASES: these are
    # checked FIRST, so they stay permanent even though they also mention 401/auth.
    for msg in (
        "authentication_error: invalid token",
        "OAuth token has expired",
        "Not logged in",
        "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    ):
        raw = json.dumps({"is_error": True, "result": msg})
        with pytest.raises(AuthenticationError):
            parse_claude_output(raw)


def test_limit_phrase_wins_over_generic_401_matcher():
    # A limit message that happens to carry a 401 must stay a UsageLimitError so its
    # reset_at (and the 1h throttle default) still apply.
    raw = json.dumps({
        "is_error": True,
        "result": "401 authentication: you've hit your session limit · resets 1pm (Europe/Warsaw)",
    })
    with pytest.raises(UsageLimitError) as exc:
        parse_claude_output(raw)
    assert exc.value.reset_at is not None


def test_plain_runtime_error_is_unchanged():
    raw = json.dumps({"is_error": True, "result": "something went wrong"})
    with pytest.raises(RuntimeError) as exc:
        parse_claude_output(raw)
    assert type(exc.value) is RuntimeError
