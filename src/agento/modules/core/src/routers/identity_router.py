"""Default identity router — resolves agent_view from ingress_identity table."""
from __future__ import annotations

from agento.framework.ingress_identity import is_regex_identity_type, match_ingress_identities
from agento.framework.router import RoutingCandidate, RoutingContext, RoutingResult


class IdentityRouter:
    @property
    def name(self) -> str:
        return "identity"

    def resolve(self, conn: object, context: RoutingContext) -> RoutingResult | None:
        matches = match_ingress_identities(conn, context.identity_type, context.identity_value)
        if not matches:
            return None

        if not is_regex_identity_type(context.identity_type):
            # Exact type: match_ingress_identities returns 0/1 active row — today's behavior.
            it = matches[0]
            return RoutingResult(
                router_name=self.name,
                candidates=[
                    RoutingCandidate(
                        agent_view_id=it.agent_view_id,
                        confidence=1.0,
                        reason=f"identity binding: {context.identity_type}={context.identity_value}",
                    )
                ],
            )

        # Regex type: highest priority wins; dedup by agent_view_id (several top-priority bindings
        # to the SAME view = one candidate; DIFFERENT views = a genuine tie → ambiguous, no job).
        # The reason carries binding_ids/priority/agent_view_id ONLY — never the raw patterns, which
        # post-normalization may be an email address (PII, SEC-F3). Operators recover the pattern
        # from the binding id via `ingress:list`.
        highest = max(m.priority for m in matches)
        top = [m for m in matches if m.priority == highest]
        by_view: dict[int, list[int]] = {}
        for m in top:
            by_view.setdefault(m.agent_view_id, []).append(m.id)
        candidates = [
            RoutingCandidate(
                agent_view_id=view_id,
                confidence=1.0,
                reason=f"identity binding_ids={binding_ids} priority={highest} agent_view_id={view_id}",
            )
            for view_id, binding_ids in by_view.items()
        ]
        return RoutingResult(
            router_name=self.name,
            candidates=candidates,
            ambiguous=len(by_view) > 1,
        )
