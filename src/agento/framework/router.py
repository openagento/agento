"""Router protocol, types, and routing chain for ingress identity resolution."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .event_manager import get_event_manager
from .events import RoutingAmbiguousEvent, RoutingFailedEvent, RoutingResolvedEvent
from .router_registry import get_routers
from .workspace import AgentView, get_agent_view

logger = logging.getLogger(__name__)


@dataclass
class RoutingContext:
    """Rich inbound request context, populated by the channel/module that triggers routing."""

    channel: str
    workflow_type: str
    identity_type: str
    identity_value: str
    payload: dict = field(default_factory=dict)


@dataclass
class RoutingCandidate:
    """Single candidate from a router."""

    agent_view_id: int
    confidence: float
    reason: str


@dataclass
class RoutingResult:
    """What a router returns.

    ``candidates`` is the router's OWN preference order; ``candidates[0]`` wins. Multiple
    candidates alone do NOT imply ambiguity — a ranked router may legitimately return several.
    A router that detects a genuine tie (e.g. distinct agent_views at the same priority) sets
    ``ambiguous=True`` to signal that no single winner should be chosen.
    """

    router_name: str
    candidates: list[RoutingCandidate]
    ambiguous: bool = False


@dataclass
class RoutingDecision:
    """Final output of the routing chain."""

    agent_view_id: int
    agent_view: AgentView | None
    matched_router: str
    all_results: list[RoutingResult]
    reason: str
    ambiguous: bool = False


@runtime_checkable
class Router(Protocol):
    @property
    def name(self) -> str: ...

    def resolve(self, conn: object, context: RoutingContext) -> RoutingResult | None: ...


def _redact_identity_value(value: str) -> str:
    """Log-safe rendering of an identity value (SEC-F3).

    Email-shaped values (containing ``@``) are PII, so log the domain only plus a short sha256
    prefix for correlation; any other value (e.g. jira's constant ``"jira"``) passes through
    byte-for-byte. Generic email-shape redaction, not a per-channel branch.
    """
    if "@" in value:
        domain = value.rsplit("@", 1)[-1]
        digest = hashlib.sha256(value.encode()).hexdigest()[:8]
        return f"<redacted>@{domain}#{digest}"
    return value


def resolve_agent_view(
    conn: object, context: RoutingContext, *, fail_on_router_error: bool = False
) -> RoutingDecision | None:
    """Run all registered routers and return the routing decision.

    Runs ALL routers (not short-circuit) to detect ambiguity. A router returns its candidate list
    in its OWN preference order; ``candidates[0]`` wins. Multiple candidates alone do NOT imply
    ambiguity — a ranked router may legitimately return several. A router that detects a genuine
    tie sets ``RoutingResult.ambiguous=True``. The decision is ambiguous when more than one router
    matched OR the winning router flagged a tie.

    ``fail_on_router_error``: by default a per-router exception is swallowed and logged (a broken
    router must not take down routing). When True the exception is re-raised — for callers (e.g.
    the Outlook publisher) that must distinguish a transient router/DB error (hold the cursor) from
    a deterministic no-match (advance).
    """
    routers = get_routers()
    all_results: list[RoutingResult] = []

    for router in routers:
        try:
            result = router.resolve(conn, context)
            if result and result.candidates:
                all_results.append(result)
        except Exception:
            if fail_on_router_error:
                raise
            logger.exception("Router %r raised an exception", router.name)

    em = get_event_manager()

    if not all_results:
        logger.info(
            "Routing failed: no router matched for %s/%s",
            context.identity_type, _redact_identity_value(context.identity_value),
        )
        em.dispatch("routing_fail_after", RoutingFailedEvent(context=context))
        return None

    winner = all_results[0]
    candidate = winner.candidates[0]
    agent_view = get_agent_view(conn, candidate.agent_view_id)
    ambiguous = len(all_results) > 1 or winner.ambiguous

    decision = RoutingDecision(
        agent_view_id=candidate.agent_view_id,
        agent_view=agent_view,
        matched_router=winner.router_name,
        all_results=all_results,
        reason=candidate.reason,
        ambiguous=ambiguous,
    )

    if ambiguous:
        logger.warning(
            "Routing ambiguous: %d routers matched for %s/%s, using %r",
            len(all_results), context.identity_type, _redact_identity_value(context.identity_value),
            winner.router_name,
        )
        em.dispatch(
            "routing_ambiguous_after",
            RoutingAmbiguousEvent(
                context=context,
                agent_view_id=candidate.agent_view_id,
                matched_router=winner.router_name,
                all_routers=[r.router_name for r in all_results],
                reason=candidate.reason,
            ),
        )
    else:
        logger.info(
            "Routing resolved: %s/%s → agent_view %d via %r",
            context.identity_type, _redact_identity_value(context.identity_value),
            candidate.agent_view_id, winner.router_name,
        )
        em.dispatch(
            "routing_resolve_after",
            RoutingResolvedEvent(
                context=context,
                agent_view_id=candidate.agent_view_id,
                matched_router=winner.router_name,
                reason=candidate.reason,
                candidate_count=sum(len(r.candidates) for r in all_results),
            ),
        )

    return decision
