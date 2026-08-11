"""Event data classes — mutable payloads for the event-observer system."""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .job_models import Job, JobRequester

# --- Consumer lifecycle events ---


@dataclass
class ConsumerStartedEvent:
    """Dispatched when the consumer main loop begins."""


@dataclass
class ConsumerStoppingEvent:
    """Dispatched when the consumer begins graceful shutdown."""


@dataclass
class ConsumerReloadedEvent:
    """Dispatched after consumer hot-reload re-bootstrap succeeds."""

    module_count: int
    elapsed_ms: int


# --- Job lifecycle events ---


@dataclass
class JobClaimedEvent:
    """Dispatched after a job is dequeued and claimed (status → RUNNING)."""

    job: Job


@dataclass
class JobSucceededEvent:
    """Dispatched after a job completes successfully (status → SUCCESS)."""

    job: Job
    summary: str | None = None
    agent_type: str | None = None
    model: str | None = None
    elapsed_ms: int = 0


@dataclass
class JobFailedEvent:
    """Dispatched on any job failure (before retry/dead decision)."""

    job: Job
    error: Exception
    elapsed_ms: int = 0


@dataclass
class JobRetryingEvent:
    """Dispatched when a failed job is scheduled for retry (status → TODO)."""

    job: Job
    error: Exception
    delay_seconds: int = 0
    elapsed_ms: int = 0


@dataclass
class JobDeadEvent:
    """Dispatched when a failed job exhausts retries (status → DEAD)."""

    job: Job
    error: Exception
    elapsed_ms: int = 0


class VerifyReason(StrEnum):
    """Reason for a verification veto on a successful-looking job."""

    NO_MCP_CALLS = "no_mcp_calls"
    TRANSCRIPT_MISSING = "transcript_missing"
    TRANSCRIPT_PARSE_FAILED = "transcript_parse_failed"


@dataclass
class Verdict:
    """Verification verdict produced by ``job_finalize_before`` observers.

    Observers set this on the dispatched ``JobFinalizeEvent`` to veto a
    superficially successful job (rc=0) when channel-agnostic invariants
    are violated (e.g. the agent made zero ``mcp__toolbox__*`` tool calls).
    """

    retryable: bool
    reason: VerifyReason
    fresh_start: bool = False
    detail: str | None = None


@dataclass
class JobFinalizeEvent:
    """Dispatched after rc=0 (before the SUCCESS UPDATE) and again after
    finalization commits, regardless of outcome.

    Observers on ``job_finalize_before`` may mutate ``verdict`` to veto a
    success — the consumer then treats the run as a failure
    (``JobVerificationFailed``) and routes it through the normal retry/dead
    path. ``job_finalize_after`` carries the same event so downstream
    observers can read the final verdict (``None`` if the job committed as
    SUCCESS, otherwise the populated ``Verdict``).

    ``harness`` is the id of the program that drove this job (e.g. ``"claude"``,
    ``"codex"``), taken from the run result. Verification observers use it to resolve
    the right ``TranscriptReader`` from the harness registry — the transcript format
    is a property of the harness, not of the model vendor.
    """

    job: Job
    job_result: Any = None  # _JobResult — Any avoids circular import with consumer
    elapsed_ms: int = 0
    harness: str | None = None
    verdict: Verdict | None = None


class JobVerificationFailed(Exception):
    """Raised internally by the consumer when a ``job_finalize_before``
    observer sets a non-None ``Verdict`` on the event. Carries the verdict
    so the retry policy can honor ``verdict.retryable``."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"job verification veto: {verdict.reason.value}")
        self.verdict = verdict


@dataclass
class JobPausedEvent:
    """Dispatched after a job is paused (status → PAUSED)."""

    job: Job


@dataclass
class JobResumedEvent:
    """Dispatched after a paused job is re-queued (status → TODO)."""

    job: Job


@dataclass
class JobPublishedEvent:
    """Dispatched after a new job is inserted into the queue."""

    type: str  # AgentType value
    source: str
    reference_id: str | None = None
    idempotency_key: str = ""
    agent_view_id: int | None = None
    priority: int = 50
    requester: JobRequester | None = None


# --- Worker pool lifecycle events (Phase 9.5) ---


@dataclass
class WorkerStartedEvent:
    """Dispatched when a worker slot begins processing a job."""

    worker_slot: str
    job_id: int


@dataclass
class WorkerStoppedEvent:
    """Dispatched when a worker slot finishes processing a job."""

    worker_slot: str
    job_id: int
    elapsed_ms: int = 0


@dataclass
class AgentViewRunStartedEvent:
    """Dispatched before agent CLI execution for a job with agent_view context."""

    job: Job
    agent_view_id: int | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    priority: int = 50
    artifacts_dir: str = ""


@dataclass
class AgentViewRunFinishedEvent:
    """Dispatched after agent CLI execution completes (success or failure)."""

    job: Job
    agent_view_id: int | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    elapsed_ms: int = 0
    success: bool = True


# --- Module lifecycle events ---


@dataclass
class ModuleLoadedEvent:
    """Dispatched after a module's capabilities are registered."""

    name: str
    path: Path


@dataclass
class ModuleRegisterEvent:
    """Dispatched when a module is first loaded (before capability registration)."""

    name: str
    path: Path
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModuleReadyEvent:
    """Dispatched after all modules are loaded (safe to query registries)."""

    name: str
    path: Path


@dataclass
class ModuleShutdownEvent:
    """Dispatched during graceful shutdown (reverse dependency order)."""

    name: str
    path: Path


@dataclass
class ModuleReloadEvent:
    """Dispatched on per-tick consumer hot-reload (reverse dependency order)."""

    name: str
    path: Path


# --- Config & setup lifecycle events ---


@dataclass
class ConfigSavedEvent:
    """Dispatched after a config value is set via CLI."""

    path: str
    encrypted: bool = False


@dataclass
class SetupBeforeEvent:
    """Dispatched before setup:upgrade begins work."""

    dry_run: bool = False


@dataclass
class SetupCompleteEvent:
    """Dispatched after setup:upgrade finishes all work."""

    result: Any = None  # SetupResult (avoid circular import with setup.py)
    dry_run: bool = False


@dataclass
class MigrationAppliedEvent:
    """Dispatched after a single SQL migration is applied."""

    version: str
    module: str
    path: Path


@dataclass
class DataPatchAppliedEvent:
    """Dispatched after a data patch is applied."""

    name: str
    module: str


@dataclass
class CrontabInstalledEvent:
    """Dispatched after crontab is updated by setup:upgrade."""

    job_count: int = 0


# --- Routing events ---


@dataclass
class RoutingResolvedEvent:
    """Dispatched after routing successfully resolves to an agent_view."""

    context: Any  # RoutingContext (avoid circular import with router.py)
    agent_view_id: int = 0
    matched_router: str = ""
    reason: str = ""
    candidate_count: int = 0


@dataclass
class RoutingAmbiguousEvent:
    """Dispatched when multiple routers match (first still wins)."""

    context: Any  # RoutingContext
    agent_view_id: int = 0
    matched_router: str = ""
    all_routers: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class RoutingFailedEvent:
    """Dispatched when no router matches the inbound identity."""

    context: Any  # RoutingContext


# --- Security events ---


@dataclass
class SecurityBreachEvent:
    """Dispatched when an inbound channel rejects a message as a probable security breach
    (e.g. an allow-listed sender failing DMARC = spoof). Observers (e.g. app_monitor's
    ``SecurityBreachAlertObserver``) may alert ops; dispatch is fail-closed regardless."""

    channel: str
    reason: str
    sender: str | None = None
    reference_id: str | None = None
    detail: str | None = None


@dataclass
class MailboxStalledEvent:
    """Dispatched when an inbound channel's shared-mailbox poll is skipped or held by a
    MISCONFIGURATION (not a transient fault), so no mail from that mailbox is delivered until an
    operator reconciles it. Observers (e.g. app_monitor's ``MailboxStalledAlertObserver``) may
    alert ops; dispatch is fail-closed regardless. ``reason`` is one of ``policy_divergence``
    (shared members disagree on mailbox-level activation policy — ``activation_modes`` /
    ``summon_token`` / ``direct_requires_sole_recipient`` / ``mailbox_aliases`` /
    ``allow_bot_collaboration``; ``allowed_senders`` is per-view and does NOT stall — group not
    polled), ``no_bindings`` (routed mode with zero active bindings — mail dropped), or
    ``upn_mismatch`` (configured UPN != resolved mailbox — poll held)."""

    channel: str
    mailbox: str
    reason: str
    detail: str | None = None


@dataclass
class InboundRouteDropEvent:
    """Per-poll summary of shared-mailbox messages dropped AFTER admission (admitted through the union
    allow-list, DMARC, AND mailbox-level activation, then dropped downstream by routing/per-view
    refinement). ``channel`` parameterizes it (like MailboxStalledEvent) so it stays
    framework-generic. Counts are per unique sender per poll. Purely observational — NOT a
    misconfiguration alert; no alerting observer is wired by default (drops are normal traffic).
    Dispatched under ``inbound_route_drop_after`` only when a drop occurred, so it never floods on
    clean polls."""

    channel: str
    mailbox: str
    unroutable: int = 0
    ambiguous: int = 0
    per_view_allowlist: int = 0


# --- Workspace build events ---


@dataclass
class WorkspaceBuildStartedEvent:
    """Dispatched when a workspace build begins (status → building)."""

    agent_view_id: int
    build_id: int


@dataclass
class WorkspaceBuildCompletedEvent:
    """Dispatched after a workspace build completes successfully (status → ready)."""

    agent_view_id: int
    build_id: int
    build_dir: str
    checksum: str
    skipped: bool = False


@dataclass
class WorkspaceBuildFailedEvent:
    """Dispatched when a workspace build fails (status → failed)."""

    agent_view_id: int
    build_id: int
    error: str = ""


@dataclass
class WorkspaceBuildCheckEvent:
    """Dispatched by the consumer before a job runs, to give the
    ``workspace_build`` module a chance to rebuild the workspace if the
    resolved scoped config no longer matches the on-disk build. The observer
    (in the workspace_build module) sets ``error`` to surface failures back to
    the consumer — necessary because ``EventManager.dispatch`` swallows
    observer exceptions, and a silent rebuild failure would let the job run
    with a stale build (the exact bug this event was introduced to fix)."""

    agent_view_id: int
    error: Exception | None = None


# --- Skill events ---


@dataclass
class SkillSyncCompletedEvent:
    """Dispatched after skill:sync finishes scanning disk and updating DB."""

    skills_dir: str
    new: int = 0
    updated: int = 0
    unchanged: int = 0


# --- Credential events ---
#
# Names have NO vendor prefix: these are core events, and the convention
# (AGENTS.md / docs/architecture/events.md) reserves `{vendor}_{module}_...` for
# third-party modules. Same shape as job_claim_after, module_register_before.
#
# Dual dispatch: each of these is dispatched under BOTH the new
# `credential_*_after` name (carrying the new payload below) and the legacy
# `token_*_after` name (carrying the legacy `Token*Event` payload further down).
# Renaming the event alone would NOT be backwards compatible — observers bound via
# events.json read `event.agent_type` / `event.token_id`, and because observer
# errors are swallowed by design the AttributeError would be SILENT.


@dataclass
class CredentialRegisteredEvent:
    """Dispatched after ``credential:register`` upserts a credential row."""

    scope: str
    credential_id: int
    label: str
    credentials: dict[str, Any] = field(repr=False, default_factory=dict)
    type: str = "oauth"


@dataclass
class CredentialRefreshedEvent:
    """Dispatched after ``credential:refresh`` re-authenticates and updates the row."""

    scope: str
    credential_id: int
    label: str
    credentials: dict[str, Any] = field(repr=False, default_factory=dict)
    type: str = "oauth"


@dataclass
class CredentialAuthFailedEvent:
    """Dispatched when a runtime auth failure flips a credential to ``status='error'``."""

    scope: str
    credential_id: int
    error_msg: str
    job_id: int | None = None


@dataclass
class CredentialUsageLimitedEvent:
    """Dispatched when a session/usage/rate limit throttles a credential (a temporary
    cooldown via ``throttled_until`` — the row stays ``status='ok'`` and recovers
    automatically). ``reset_at`` is the naive-UTC time the throttle lifts."""

    scope: str
    credential_id: int
    error_msg: str
    reset_at: datetime | None = None
    job_id: int | None = None


@dataclass
class CredentialAuthThrottledEvent:
    """Dispatched when a TRANSIENT auth failure (e.g. a revoked/stale access token)
    throttles a credential instead of poisoning it — ``throttled_until`` is set,
    ``status`` stays ``'ok'``, and it auto-recovers. Distinct from
    ``CredentialAuthFailedEvent`` (permanent poison) and from
    ``CredentialUsageLimitedEvent`` (a quota limit, not a credential problem)."""

    scope: str
    credential_id: int
    error_msg: str
    throttled_until: datetime | None = None
    job_id: int | None = None


# --- Legacy token events (deprecated, removed next release — see ROADMAP.md) ---
#
# Kept so observers bound to the old `token_*_after` names keep receiving a payload
# with the fields they actually read (`agent_type`, `token_id`).


@dataclass
class TokenRegisteredEvent:
    """Deprecated alias payload for ``token_register_after``."""

    agent_type: str
    token_id: int
    label: str
    credentials: dict[str, Any] = field(repr=False, default_factory=dict)
    type: str = "oauth"


@dataclass
class TokenRefreshedEvent:
    """Deprecated alias payload for ``token_refresh_after``."""

    agent_type: str
    token_id: int
    label: str
    credentials: dict[str, Any] = field(repr=False, default_factory=dict)
    type: str = "oauth"


@dataclass
class TokenAuthFailedEvent:
    """Deprecated alias payload for ``token_auth_failed_after``."""

    agent_type: str
    token_id: int
    error_msg: str
    job_id: int | None = None


@dataclass
class TokenUsageLimitedEvent:
    """Deprecated alias payload for ``token_usage_limited_after``."""

    agent_type: str
    token_id: int
    error_msg: str
    reset_at: datetime | None = None
    job_id: int | None = None


@dataclass
class TokenAuthThrottledEvent:
    """Deprecated alias payload for ``token_auth_throttled_after``."""

    agent_type: str
    token_id: int
    error_msg: str
    throttled_until: datetime | None = None
    job_id: int | None = None


# Maps each new credential event name to its legacy name + legacy payload class.
# ``dispatch_credential_event`` below is the ONE place both are emitted, so the two
# payloads can never drift apart.
_CREDENTIAL_EVENT_ALIASES: dict[str, tuple[str, type]] = {
    "credential_register_after": ("token_register_after", TokenRegisteredEvent),
    "credential_refresh_after": ("token_refresh_after", TokenRefreshedEvent),
    "credential_auth_failed_after": ("token_auth_failed_after", TokenAuthFailedEvent),
    "credential_usage_limited_after": ("token_usage_limited_after", TokenUsageLimitedEvent),
    "credential_auth_throttled_after": (
        "token_auth_throttled_after",
        TokenAuthThrottledEvent,
    ),
}


def dispatch_credential_event(event_name: str, event: object) -> None:
    """Dispatch a credential event under its new name AND its legacy name.

    Both payloads are built from the same data here, so an observer on either name
    sees consistent values. Remove together with the legacy names (ROADMAP.md).
    """
    from .event_manager import get_event_manager

    manager = get_event_manager()
    manager.dispatch(event_name, event)

    alias = _CREDENTIAL_EVENT_ALIASES.get(event_name)
    if alias is None:
        return
    legacy_name, legacy_cls = alias
    fields = {
        f.name: getattr(event, f.name)
        for f in dataclass_fields(event)
        if f.name not in ("scope", "credential_id")
    }
    manager.dispatch(
        legacy_name,
        legacy_cls(agent_type=event.scope, token_id=event.credential_id, **fields),
    )
