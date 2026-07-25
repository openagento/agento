# Routing — Ingress Identity Resolution

Maps inbound traffic (Teams, API, and other ingress-routed channels) to the right `agent_view` using a deterministic, module-extensible router chain.

> **Outlook uses this in routed (shared-mailbox) mode.** A mailbox owned by exactly **one**
> agent_view is **direct mode** — the mailbox identifies the view, no routing, no binding required
> (`outlook/allowed_senders` + DMARC remain the inbound security gate). A mailbox **shared** by two
> or more agent_views is **routed mode**: the inbox is polled once and each message is routed to a
> view by matching the normalized sender against `outlook_sender` ingress bindings
> (**regex, case-insensitive `fullmatch`, highest `priority` wins**; a tie between different views is
> ambiguous → no job). See [docs/modules/outlook.md](../modules/outlook.md). The legacy
> `ingress:bind email` type is inert for Outlook.

## How It Works

1. **Inbound request arrives** — a channel/module creates a `RoutingContext` with identity info
2. **Router chain runs** — all registered routers execute in order (not short-circuit, to detect ambiguity)
3. **First match wins** — the first router (by order) with candidates determines the `agent_view`
4. **Events fire** — `agento_routing_resolved`, `agento_routing_ambiguous`, or `agento_routing_failed`

## RoutingContext

The channel/module that triggers routing populates the context:

```python
from agento.framework.contracts import RoutingContext

ctx = RoutingContext(
    channel="teams",
    workflow_type="followup",
    identity_type="teams",
    identity_value="29:1a2b3c…",
    payload={"subject": "Re: Project update", "thread_id": "abc123"},
)
```

| Field | Description |
|-------|-------------|
| `channel` | Channel name (e.g. `jira`, `outlook`, `teams`) |
| `workflow_type` | Workflow type (e.g. `cron`, `todo`, `followup`) |
| `identity_type` | Identity key (e.g. `email`, `teams`, `api_client`) |
| `identity_value` | Identity value (e.g. `user@example.com`) |
| `payload` | Channel-specific data (module populates before calling `resolve_agent_view()`) |

## Router Protocol

Modules contribute routers by implementing the `Router` protocol:

```python
from agento.framework.contracts import Router, RoutingContext, RoutingResult, RoutingCandidate

class MyCustomRouter:
    @property
    def name(self) -> str:
        return "my_custom"

    def resolve(self, conn, context: RoutingContext) -> RoutingResult | None:
        # Return None if no match, or a RoutingResult with candidates
        if context.identity_type == "api_client":
            return RoutingResult(
                router_name=self.name,
                candidates=[RoutingCandidate(
                    agent_view_id=42,
                    confidence=1.0,
                    reason="API client mapping",
                )],
            )
        return None
```

Register in `di.json`:

```json
{
  "routers": [
    {"name": "my_custom", "class": "src.routers.my_custom_router.MyCustomRouter", "order": 200}
  ]
}
```

## Router Chain

`resolve_agent_view(conn, context, *, fail_on_router_error=False)` runs the chain:

1. Iterates all routers sorted by `(order, name)`
2. Calls `resolve()` on each — a per-router exception is swallowed and logged by default; pass
   `fail_on_router_error=True` to re-raise instead (so a caller can distinguish a transient
   router/DB error from a deterministic no-match — the Outlook publisher uses this to hold the
   cursor on a transient error but advance on a clean no-match)
3. Collects all non-empty results
4. First result's first candidate wins — a router returns its candidates in its own preference
   order and `candidates[0]` wins; **multiple candidates alone do NOT imply ambiguity** (a ranked
   router may legitimately return several)
5. The decision is `ambiguous=True` when **more than one router matched OR the winning router
   flagged a genuine tie** (`RoutingResult.ambiguous=True`) → `agento_routing_ambiguous` event
6. If no router matched → returns `None` + `agento_routing_failed` event

Router log statements redact the `identity_value`: an email-shaped value (`@`) is logged
domain-only plus a short sha256 prefix; any other value passes through unchanged.

```python
from agento.framework.router import resolve_agent_view, RoutingContext

decision = resolve_agent_view(conn, ctx)
if decision:
    print(f"Resolved to agent_view {decision.agent_view_id} via {decision.matched_router}")
    if decision.ambiguous:
        print("Warning: multiple routers matched")
```

## Default Router: Identity

The core module ships an `IdentityRouter` (order=100) that looks up the `ingress_identity` table:

- Maps `(identity_type, identity_value)` → `agent_view_id`
- Returns `None` for unknown or inactive identities
- Confidence is always 1.0 (deterministic binding)

**Exact vs regex identity types.** Most types match **exactly** (the unique `(type, value)` row).
A module can declare a type as **regex-matched** via the generic `di.json` capability
`regex_identity_types` (an array of type names matching `^[a-z][a-z0-9_]{0,31}$`, populated into a
framework registry at bootstrap; the framework hardcodes no channel string). For a regex type, each
active binding's `identity_value` is a **case-insensitive `fullmatch`** pattern; the highest
`priority` wins, and a tie between **different** agent_views sets `RoutingResult.ambiguous=True`
(no job). The reason string carries `binding_ids`/`priority`/`agent_view_id` only — never the raw
pattern (which post-normalization may be PII). The Outlook module contributes `outlook_sender`.

> **Regex dialect + ReDoS bound.** Matching uses the [`regex`](https://pypi.org/project/regex/)
> engine pinned to the **`regex.VERSION0`** dialect — used identically by the `ingress:bind`
> validator and the runtime matcher so a pattern accepted at bind time behaves the same at match
> time. Because admin-authored patterns are matched against attacker-influenced senders, matching is
> bounded by a **dual wall-clock budget** — a per-pattern limit (~0.1s) AND a total per-lookup
> deadline (~0.5s) via the engine's in-process `timeout` — the only reliable in-process bound on
> catastrophic backtracking. A timed-out or invalid pattern is skipped (rate-limited WARN by binding
> id, never the raw pattern) and, once the total budget is spent, remaining bindings fail closed
> (no match). The decision stays deterministic, so cursor discipline is unaffected. See
> [DECISIONS.md](../../DECISIONS.md).

> **`ingress_identity` vs `requester_*` — distinct concerns.** `ingress_identity` is routing
> input: it maps an inbound identity to an `agent_view_id`. The `requester_*` columns on the
> `job` row (`requester_key`/`requester_email`/`requester_trust`/`requester_meta`) are a
> separate, channel-agnostic snapshot of *who triggered the job* — **audit / future-policy
> metadata only**. They never participate in dedupe (`idempotency_key`/`skip_if_active`), auth,
> or routing, and are never fed into `RoutingContext.payload` or the routing events.

### CLI Commands

```bash
# Bind an identity to an agent_view (exact type)
bin/agento ingress:bind jira jira developer

# Bind a regex sender pattern for a shared Outlook mailbox (routed mode); higher priority wins
bin/agento ingress:bind outlook_sender '[^@]+@company\.com' sales --priority 10
bin/agento ingress:bind outlook_sender 'vip@company\.com' vip --priority 50

# List all bindings (shows priority)
bin/agento ingress:list
bin/agento ingress:list --type outlook_sender --json

# Remove a binding
bin/agento ingress:unbind outlook_sender '[^@]+@company\.com'
```

## Routing Events

| Event | When |
|-------|------|
| `agento_routing_resolved` | Successful resolution to an agent_view |
| `agento_routing_ambiguous` | Multiple routers matched (first still wins) |
| `agento_routing_failed` | No router matched |

All events carry the full `RoutingContext` for observability.

## Post-MVP: Semantic Router

A future semantic router could use LLM-based matching to route requests based on agent competence descriptions rather than explicit identity bindings. This is documented as post-MVP — the identity router covers deterministic use cases first.

## Source Files

| Component | File |
|-----------|------|
| Router protocol & chain | [src/agento/framework/router.py](../../src/agento/framework/router.py) |
| Router registry | [src/agento/framework/router_registry.py](../../src/agento/framework/router_registry.py) |
| Ingress identity model | [src/agento/framework/ingress_identity.py](../../src/agento/framework/ingress_identity.py) |
| Identity router | [src/agento/modules/core/src/routers/identity_router.py](../../src/agento/modules/core/src/routers/identity_router.py) |
| Routing events | [src/agento/framework/events.py](../../src/agento/framework/events.py) |
| DB migration | [src/agento/framework/sql/015_ingress_identity.sql](../../src/agento/framework/sql/015_ingress_identity.sql) |
