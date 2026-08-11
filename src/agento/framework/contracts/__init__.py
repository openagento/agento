"""Public contracts for Agento modules.

Module authors should import from here — these are the stable interfaces.
"""

from __future__ import annotations

from ..channels.base import Channel, DiscoverableChannel, PromptFragments, Publisher, WorkItem
from ..commands import Command
from ..data_patch import DataPatch
from ..encryptor import Encryptor
from ..event_manager import EventManager, Observer, ObserverEntry
from ..events import (
    ConfigSavedEvent,
    ConsumerReloadedEvent,
    ConsumerStartedEvent,
    ConsumerStoppingEvent,
    CredentialRefreshedEvent,
    CredentialRegisteredEvent,
    CrontabInstalledEvent,
    DataPatchAppliedEvent,
    JobClaimedEvent,
    JobDeadEvent,
    JobFailedEvent,
    JobPublishedEvent,
    JobRetryingEvent,
    JobSucceededEvent,
    MigrationAppliedEvent,
    ModuleLoadedEvent,
    ModuleReadyEvent,
    ModuleRegisterEvent,
    ModuleReloadEvent,
    ModuleShutdownEvent,
    RoutingAmbiguousEvent,
    RoutingFailedEvent,
    RoutingResolvedEvent,
    SetupBeforeEvent,
    SetupCompleteEvent,
    TokenRefreshedEvent,
    TokenRegisteredEvent,
)
from ..harness import Runner, RunResult, SubprocessRunner
from ..ingress_identity import IngressIdentity
from ..job_models import AgentType, Job, JobRequester, JobStatus, RequesterTrust
from ..router import Router, RoutingCandidate, RoutingContext, RoutingDecision, RoutingResult
from ..workflows.base import JobContext, Workflow

__all__ = [
    "AgentType",
    "Channel",
    "Command",
    "ConfigSavedEvent",
    "ConsumerReloadedEvent",
    "ConsumerStartedEvent",
    "ConsumerStoppingEvent",
    "CredentialRefreshedEvent",
    "CredentialRegisteredEvent",
    "CrontabInstalledEvent",
    "DataPatch",
    "DataPatchAppliedEvent",
    "DiscoverableChannel",
    "Encryptor",
    "EventManager",
    "IngressIdentity",
    "Job",
    "JobClaimedEvent",
    "JobContext",
    "JobDeadEvent",
    "JobFailedEvent",
    "JobPublishedEvent",
    "JobRequester",
    "JobRetryingEvent",
    "JobStatus",
    "JobSucceededEvent",
    "MigrationAppliedEvent",
    "ModuleLoadedEvent",
    "ModuleReadyEvent",
    "ModuleRegisterEvent",
    "ModuleReloadEvent",
    "ModuleShutdownEvent",
    "Observer",
    "ObserverEntry",
    "PromptFragments",
    "Publisher",
    "RequesterTrust",
    "Router",
    "RoutingAmbiguousEvent",
    "RoutingCandidate",
    "RoutingContext",
    "RoutingDecision",
    "RoutingFailedEvent",
    "RoutingResolvedEvent",
    "RoutingResult",
    "RunResult",
    "Runner",
    "SetupBeforeEvent",
    "SetupCompleteEvent",
    "SubprocessRunner",
    "TokenRefreshedEvent",
    "TokenRegisteredEvent",
    "WorkItem",
    "Workflow",
]
