"""Tests for the routing chain (resolve_agent_view)."""
import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.router import (
    RoutingCandidate,
    RoutingContext,
    RoutingResult,
    _redact_identity_value,
    resolve_agent_view,
)
from agento.framework.router_registry import clear, register_router
from agento.framework.workspace import AgentView


class _StubRouter:
    def __init__(self, name: str, result: RoutingResult | None = None, *, raise_error: bool = False):
        self._name = name
        self._result = result
        self._raise_error = raise_error

    @property
    def name(self) -> str:
        return self._name

    def resolve(self, conn, context: RoutingContext) -> RoutingResult | None:
        if self._raise_error:
            raise RuntimeError("router exploded")
        return self._result


def _make_agent_view(agent_view_id=1):
    return AgentView(
        id=agent_view_id, workspace_id=1, code="default",
        label="Default", is_active=True,
        created_at=datetime(2025, 1, 1), updated_at=datetime(2025, 1, 1),
    )


def _ctx():
    return RoutingContext(
        channel="jira", workflow_type="cron",
        identity_type="email", identity_value="user@example.com",
    )


class TestResolveAgentView:
    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_single_match(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        em = MagicMock()
        mock_em.return_value = em

        result = RoutingResult(
            router_name="identity",
            candidates=[RoutingCandidate(agent_view_id=10, confidence=1.0, reason="bound")],
        )
        register_router(_StubRouter("identity", result), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is not None
        assert decision.agent_view_id == 10
        assert decision.matched_router == "identity"
        assert decision.ambiguous is False
        em.dispatch.assert_called_once()
        assert em.dispatch.call_args[0][0] == "routing_resolve_after"

    @patch("agento.framework.router.get_event_manager")
    def test_no_match_fires_failed_event(self, mock_em):
        em = MagicMock()
        mock_em.return_value = em

        register_router(_StubRouter("identity", None), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is None
        em.dispatch.assert_called_once()
        assert em.dispatch.call_args[0][0] == "routing_fail_after"

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_multiple_matches_ambiguous(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        em = MagicMock()
        mock_em.return_value = em

        result1 = RoutingResult(
            router_name="identity",
            candidates=[RoutingCandidate(agent_view_id=10, confidence=1.0, reason="bound")],
        )
        result2 = RoutingResult(
            router_name="custom",
            candidates=[RoutingCandidate(agent_view_id=20, confidence=0.8, reason="custom match")],
        )
        register_router(_StubRouter("identity", result1), order=100)
        register_router(_StubRouter("custom", result2), order=200)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is not None
        assert decision.agent_view_id == 10  # first router wins
        assert decision.ambiguous is True
        assert len(decision.all_results) == 2
        em.dispatch.assert_called_once()
        assert em.dispatch.call_args[0][0] == "routing_ambiguous_after"

    @patch("agento.framework.router.get_event_manager")
    def test_router_exception_swallowed(self, mock_em):
        em = MagicMock()
        mock_em.return_value = em

        register_router(_StubRouter("broken", raise_error=True), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is None
        em.dispatch.assert_called_once()
        assert em.dispatch.call_args[0][0] == "routing_fail_after"

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_exception_router_skipped_good_router_wins(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        em = MagicMock()
        mock_em.return_value = em

        result = RoutingResult(
            router_name="good",
            candidates=[RoutingCandidate(agent_view_id=10, confidence=1.0, reason="ok")],
        )
        register_router(_StubRouter("broken", raise_error=True), order=50)
        register_router(_StubRouter("good", result), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is not None
        assert decision.matched_router == "good"
        assert decision.ambiguous is False

    @patch("agento.framework.router.get_event_manager")
    def test_empty_candidates_treated_as_no_match(self, mock_em):
        em = MagicMock()
        mock_em.return_value = em

        result = RoutingResult(router_name="empty", candidates=[])
        register_router(_StubRouter("empty", result), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is None

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_resolved_event_carries_context(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        em = MagicMock()
        mock_em.return_value = em

        result = RoutingResult(
            router_name="identity",
            candidates=[RoutingCandidate(agent_view_id=10, confidence=1.0, reason="bound")],
        )
        register_router(_StubRouter("identity", result), order=100)

        ctx = _ctx()
        resolve_agent_view(MagicMock(), ctx)

        event = em.dispatch.call_args[0][1]
        assert event.context is ctx
        assert event.agent_view_id == 10

    @patch("agento.framework.router.get_event_manager")
    def test_no_routers_registered(self, mock_em):
        em = MagicMock()
        mock_em.return_value = em

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is None
        em.dispatch.assert_called_once()
        assert em.dispatch.call_args[0][0] == "routing_fail_after"


class TestRouterGenericContract:
    """C4: ambiguity is an EXPLICIT signal (RoutingResult.ambiguous) OR >1 router matched — never
    candidate-count alone. A single ranked router may legitimately return several candidates."""

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @patch("agento.framework.router.get_event_manager")
    def test_fail_on_router_error_reraises(self, mock_em):
        mock_em.return_value = MagicMock()
        register_router(_StubRouter("broken", raise_error=True), order=100)
        with pytest.raises(RuntimeError, match="router exploded"):
            resolve_agent_view(MagicMock(), _ctx(), fail_on_router_error=True)

    @patch("agento.framework.router.get_event_manager")
    def test_fail_on_router_error_default_swallows(self, mock_em):
        mock_em.return_value = MagicMock()
        register_router(_StubRouter("broken", raise_error=True), order=100)
        # default (fail_on_router_error=False) swallows -> no match, no raise
        assert resolve_agent_view(MagicMock(), _ctx()) is None

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_multi_candidate_ranked_router_not_ambiguous(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        mock_em.return_value = MagicMock()
        result = RoutingResult(
            router_name="ranked",
            candidates=[
                RoutingCandidate(agent_view_id=10, confidence=0.9, reason="best"),
                RoutingCandidate(agent_view_id=20, confidence=0.5, reason="runner-up"),
            ],
            ambiguous=False,
        )
        register_router(_StubRouter("ranked", result), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision is not None
        assert decision.ambiguous is False       # >1 candidate alone is NOT ambiguity
        assert decision.agent_view_id == 10       # candidates[0] wins

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_router_flagged_tie_is_ambiguous(self, mock_em, mock_get_av):
        mock_get_av.return_value = _make_agent_view(10)
        em = MagicMock()
        mock_em.return_value = em
        result = RoutingResult(
            router_name="identity",
            candidates=[
                RoutingCandidate(agent_view_id=10, confidence=1.0, reason="a"),
                RoutingCandidate(agent_view_id=20, confidence=1.0, reason="b"),
            ],
            ambiguous=True,
        )
        register_router(_StubRouter("identity", result), order=100)

        decision = resolve_agent_view(MagicMock(), _ctx())
        assert decision.ambiguous is True
        assert em.dispatch.call_args[0][0] == "routing_ambiguous_after"


class TestRedaction:
    """SEC-F3: email-shaped identity values are PII — the three routing log sites must log the
    domain only; a non-@ value (jira's constant "jira") passes through byte-for-byte."""

    def test_redact_helper_email_is_domain_only(self):
        out = _redact_identity_value("secret-user@corp.example.com")
        assert "secret-user" not in out
        assert out.startswith("<redacted>@corp.example.com#")

    def test_redact_helper_non_email_passes_through(self):
        assert _redact_identity_value("jira") == "jira"

    def test_redact_helper_is_deterministic(self):
        assert _redact_identity_value("a@b.com") == _redact_identity_value("a@b.com")

    def setup_method(self):
        clear()

    def teardown_method(self):
        clear()

    @patch("agento.framework.router.get_agent_view")
    @patch("agento.framework.router.get_event_manager")
    def test_resolved_log_redacts_email_identity(self, mock_em, mock_get_av, caplog):
        mock_get_av.return_value = _make_agent_view(10)
        mock_em.return_value = MagicMock()
        result = RoutingResult(
            router_name="identity",
            candidates=[RoutingCandidate(agent_view_id=10, confidence=1.0, reason="bound")],
        )
        register_router(_StubRouter("identity", result), order=100)
        ctx = RoutingContext(channel="outlook", workflow_type="todo",
                             identity_type="outlook_sender", identity_value="secret-user@corp.com")
        with caplog.at_level(logging.INFO, logger="agento.framework.router"):
            resolve_agent_view(MagicMock(), ctx)
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "secret-user" not in blob      # local part never logged
        assert "corp.com" in blob             # domain retained for correlation

    @patch("agento.framework.router.get_event_manager")
    def test_no_match_log_keeps_non_email_value(self, mock_em, caplog):
        mock_em.return_value = MagicMock()
        register_router(_StubRouter("identity", None), order=100)
        ctx = RoutingContext(channel="jira", workflow_type="cron",
                             identity_type="jira", identity_value="jira")
        with caplog.at_level(logging.INFO, logger="agento.framework.router"):
            resolve_agent_view(MagicMock(), ctx)
        assert any("jira" in r.getMessage() for r in caplog.records)
