from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ConsumerConfig:
    """Framework-level consumer tuning config."""

    # Per-run isolation (Phase 9.5) gives each job its own run directory, so the default
    # fans out to 10 workers. Note what that isolates: FILES. It never isolated the shared
    # ROTATING CREDENTIAL — ten workers handed the same near-expiry oauth row each
    # materialize the same single-use refresh token, and the first to rotate invalidates
    # the copy the other nine are about to replay. That is what the refresh lease in
    # select_credential/CredentialResolver exists for.
    max_workers: int = 10
    poll_interval: float = 5.0
    job_timeout_seconds: int = 1200  # 20 minutes
    disable_llm: bool = False

    @property
    def concurrency(self) -> int:
        """Backward-compatible alias for max_workers."""
        return self.max_workers

    @classmethod
    def from_env(cls) -> ConsumerConfig:
        """Build from env vars only."""
        return cls(
            max_workers=int(os.environ.get("AGENTO_CONSUMER_MAX_WORKERS", "10")),
            poll_interval=float(os.environ.get("AGENTO_CONSUMER_POLL_INTERVAL", "5.0")),
            job_timeout_seconds=int(os.environ.get("AGENTO_JOB_TIMEOUT_SECONDS", "1200")),
            disable_llm=os.environ.get("DISABLE_LLM", "0").lower() in ("1", "true", "yes"),
        )
