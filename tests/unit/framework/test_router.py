"""Tests for router types and IdentityRouter."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agento.framework import ingress_identity as ii
from agento.framework.ingress_identity import IngressIdentity, register_regex_identity_type
from agento.framework.router import RoutingCandidate, RoutingContext, RoutingDecision, RoutingResult
from agento.modules.core.src.routers.identity_router import IdentityRouter

REGEX_TYPE = "outlook_sender"


@pytest.fixture
def regex_type():
    saved = set(ii._REGEX_IDENTITY_TYPES)
    register_regex_identity_type(REGEX_TYPE)
    yield
    ii._REGEX_IDENTITY_TYPES.clear()
    ii._REGEX_IDENTITY_TYPES.update(saved)


def _ri(id, agent_view_id, priority, value="pattern"):
    return IngressIdentity(
        id=id, identity_type=REGEX_TYPE, identity_value=value, agent_view_id=agent_view_id,
        priority=priority, is_active=True,
        created_at=datetime(2025, 1, 1), updated_at=datetime(2025, 1, 1),
    )


class TestRoutingDataclasses:
    def test_routing_context(self):
        ctx = RoutingContext(
            channel="jira", workflow_type="cron",
            identity_type="email", identity_value="user@example.com",
        )
        assert ctx.channel == "jira"
        assert ctx.payload == {}

    def test_routing_context_with_payload(self):
        ctx = RoutingContext(
            channel="outlook", workflow_type="followup",
            identity_type="email", identity_value="user@example.com",
            payload={"subject": "Test"},
        )
        assert ctx.payload["subject"] == "Test"

    def test_routing_candidate(self):
        c = RoutingCandidate(agent_view_id=1, confidence=1.0, reason="test")
        assert c.agent_view_id == 1
        assert c.confidence == 1.0

    def test_routing_result(self):
        r = RoutingResult(
            router_name="identity",
            candidates=[RoutingCandidate(agent_view_id=1, confidence=1.0, reason="test")],
        )
        assert r.router_name == "identity"
        assert len(r.candidates) == 1

    def test_routing_decision(self):
        d = RoutingDecision(
            agent_view_id=1, agent_view=None,
            matched_router="identity", all_results=[], reason="test",
        )
        assert d.ambiguous is False


class TestIdentityRouter:
    def _make_identity(self, *, is_active=True, **overrides):
        base = dict(
            id=1, identity_type="email", identity_value="user@example.com",
            agent_view_id=10, priority=0, is_active=is_active,
            created_at=datetime(2025, 1, 1), updated_at=datetime(2025, 1, 1),
        )
        base.update(overrides)
        return IngressIdentity(**base)

    def _patch_matches(self, monkeypatch, matches):
        # The router now resolves via match_ingress_identities (which itself filters is_active and,
        # for regex types, applies the fullmatch + priority logic). Exact types return 0/1 rows.
        monkeypatch.setattr(
            "agento.modules.core.src.routers.identity_router.match_ingress_identities",
            lambda conn, t, v: matches,
        )

    def test_name(self):
        assert IdentityRouter().name == "identity"

    def test_resolve_match(self, monkeypatch):
        self._patch_matches(monkeypatch, [self._make_identity()])
        router = IdentityRouter()
        ctx = RoutingContext(channel="jira", workflow_type="cron", identity_type="email", identity_value="user@example.com")
        result = router.resolve(MagicMock(), ctx)
        assert result is not None
        assert result.candidates[0].agent_view_id == 10
        assert result.candidates[0].confidence == 1.0
        assert result.ambiguous is False

    def test_resolve_no_match(self, monkeypatch):
        self._patch_matches(monkeypatch, [])
        router = IdentityRouter()
        ctx = RoutingContext(channel="jira", workflow_type="cron", identity_type="email", identity_value="nobody@example.com")
        result = router.resolve(MagicMock(), ctx)
        assert result is None

    def test_resolve_inactive(self, monkeypatch):
        # An inactive binding is filtered out by match_ingress_identities → no matches → no result.
        self._patch_matches(monkeypatch, [])
        router = IdentityRouter()
        ctx = RoutingContext(channel="jira", workflow_type="cron", identity_type="email", identity_value="user@example.com")
        result = router.resolve(MagicMock(), ctx)
        assert result is None


class TestIdentityRouterRegex:
    """Regex-type routing (C4): highest priority wins, dedup by agent_view_id; a distinct-view tie
    sets RoutingResult.ambiguous=True. The reason never carries the raw pattern (SEC-F3)."""

    def _resolve(self, monkeypatch, matches):
        monkeypatch.setattr(
            "agento.modules.core.src.routers.identity_router.match_ingress_identities",
            lambda conn, t, v: matches,
        )
        ctx = RoutingContext(channel="outlook", workflow_type="todo",
                             identity_type=REGEX_TYPE, identity_value="sender@x.com")
        return IdentityRouter().resolve(MagicMock(), ctx)

    def test_no_matches_returns_none(self, regex_type, monkeypatch):
        assert self._resolve(monkeypatch, []) is None

    def test_multi_binding_same_view_is_one_candidate(self, regex_type, monkeypatch):
        # two top-priority bindings to the SAME view collapse to one candidate — not a tie
        result = self._resolve(monkeypatch, [_ri(1, 5, 10), _ri(2, 5, 10)])
        assert len(result.candidates) == 1
        assert result.candidates[0].agent_view_id == 5
        assert result.ambiguous is False

    def test_multi_binding_different_views_is_ambiguous(self, regex_type, monkeypatch):
        result = self._resolve(monkeypatch, [_ri(1, 5, 10), _ri(2, 6, 10)])
        assert result.ambiguous is True
        assert {c.agent_view_id for c in result.candidates} == {5, 6}

    def test_top_priority_wins(self, regex_type, monkeypatch):
        # a higher-priority binding to view 5 beats a lower-priority binding to view 6 -> not a tie
        result = self._resolve(monkeypatch, [_ri(1, 5, 10), _ri(2, 6, 3)])
        assert result.ambiguous is False
        assert [c.agent_view_id for c in result.candidates] == [5]

    def test_negative_priority_selected_when_only_option(self, regex_type, monkeypatch):
        result = self._resolve(monkeypatch, [_ri(1, 5, -5)])
        assert result.candidates[0].agent_view_id == 5
        assert result.ambiguous is False

    def test_reason_carries_binding_ids_not_raw_pattern(self, regex_type, monkeypatch):
        secret = r"secret\.person@corp\.com"
        result = self._resolve(monkeypatch, [_ri(1, 5, 10, value=secret), _ri(2, 5, 10, value=secret)])
        reason = result.candidates[0].reason
        assert "binding_ids=[1, 2]" in reason
        assert "priority=10" in reason
        assert "agent_view_id=5" in reason
        assert secret not in reason  # never the raw pattern (may be PII post-normalization)
