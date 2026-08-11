from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


class AgentType(Enum):
    CRON = "cron"
    TODO = "todo"
    FOLLOWUP = "followup"
    BLANK = "blank"


class RequesterTrust(Enum):
    CLAIMED = "claimed"
    DOMAIN = "domain"
    ACCOUNT = "account"


@dataclass(frozen=True)
class JobRequester:
    key: str
    email: str | None = None
    trust: RequesterTrust = RequesterTrust.CLAIMED
    meta: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("JobRequester.key must be a non-empty string")
        if not isinstance(self.trust, RequesterTrust):
            raise ValueError(f"JobRequester.trust must be a RequesterTrust, got {self.trust!r}")
        object.__setattr__(self, "key", self.key.strip())
        object.__setattr__(self, "email", normalize_email(self.email))
        if self.meta is not None:
            object.__setattr__(self, "meta", dict(self.meta))  # shallow copy: honor the frozen value object


class JobStatus(Enum):
    TODO = "TODO"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DEAD = "DEAD"
    PAUSED = "PAUSED"


@dataclass
class Job:
    id: int
    schedule_id: int | None
    type: AgentType
    source: str
    agent_view_id: int | None
    priority: int
    reference_id: str | None
    # `agent_type` holds the HARNESS id (pre-0.15 column name, kept to avoid a rename);
    # `provider` is the model/API vendor that served the run. NULL on pre-0.15 rows.
    agent_type: str | None
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    prompt: str | None
    output: str | None
    context: str | None
    idempotency_key: str
    status: JobStatus
    attempt: int
    max_attempts: int
    scheduled_after: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_summary: str | None
    error_message: str | None
    error_class: str | None
    pid: int | None
    session_id: str | None
    created_at: datetime
    updated_at: datetime
    requester_key: str | None = None
    requester_email: str | None = None
    requester_trust: RequesterTrust = RequesterTrust.CLAIMED
    requester_meta: dict[str, Any] | None = None

    @classmethod
    def stub(
        cls,
        *,
        type: AgentType,
        source: str,
        reference_id: str | None = None,
        agent_view_id: int | None = None,
        priority: int = 50,
        context: str | None = None,
    ) -> Job:
        """Create a minimal Job for CLI / testing (no DB row)."""
        now = datetime.now(UTC)
        return cls(
            id=0, schedule_id=None, type=type, source=source,
            agent_view_id=agent_view_id, priority=priority,
            reference_id=reference_id, agent_type=None, provider=None, model=None,
            input_tokens=None, output_tokens=None, prompt=None, output=None,
            context=context, idempotency_key="", status=JobStatus.RUNNING,
            attempt=1, max_attempts=3, scheduled_after=now, started_at=now,
            finished_at=None, result_summary=None, error_message=None,
            error_class=None, pid=None, session_id=None,
            created_at=now, updated_at=now,
        )

    @classmethod
    def from_row(cls, row: dict) -> Job:
        requester_meta = row.get("requester_meta")
        if isinstance(requester_meta, str):
            requester_meta = json.loads(requester_meta)
        # JSON columns can hold arrays/scalars; the contract is dict|None. Coerce any
        # non-dict (only reachable via manual DB tampering/legacy rows - the sole writer,
        # publisher.py, always json.dumps a dict) to None rather than raise: from_row runs
        # in the consumer claim hot path and must not crash on anomalous audit metadata.
        if requester_meta is not None and not isinstance(requester_meta, dict):
            requester_meta = None
        return cls(
            id=row["id"],
            schedule_id=row["schedule_id"],
            type=AgentType(row["type"]),
            source=row["source"],
            agent_view_id=row.get("agent_view_id"),
            priority=row.get("priority", 50),
            reference_id=row["reference_id"],
            agent_type=row.get("agent_type"),
            provider=row.get("provider"),
            model=row.get("model"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            prompt=row.get("prompt"),
            output=row.get("output"),
            context=row.get("context"),
            idempotency_key=row["idempotency_key"],
            status=JobStatus(row["status"]),
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            scheduled_after=row["scheduled_after"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result_summary=row["result_summary"],
            error_message=row["error_message"],
            error_class=row["error_class"],
            pid=row.get("pid"),
            session_id=row.get("session_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            requester_key=row.get("requester_key"),
            requester_email=row.get("requester_email"),
            requester_trust=RequesterTrust(row.get("requester_trust") or "claimed"),
            requester_meta=requester_meta,
        )
