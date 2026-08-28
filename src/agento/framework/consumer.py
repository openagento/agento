from __future__ import annotations

import logging
import os
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .agent_manager.credential_resolver import (
    _DEFAULT_LEASE_TTL_SECONDS,
    CredentialResolver,
)
from .agent_manager.credential_store import (
    clear_auto_credential_error,
    count_credentials_for_scope,
    earliest_throttle_reset_for_scope,
    lease_owner_for_job,
    mark_credential_error,
    release_credential_lease,
    renew_credential_leases,
    throttle_credential,
)
from .agent_manager.errors import AuthenticationError, TransientAuthError, UsageLimitError
from .agent_view_runtime import resolve_agent_view_runtime
from .bootstrap import bootstrap, dispatch_reload, dispatch_shutdown, get_module_config
from .channels.registry import get_channel
from .consumer_config import ConsumerConfig
from .database_config import DatabaseConfig
from .db import get_connection
from .event_manager import get_event_manager
from .events import (
    AgentViewRunFinishedEvent,
    AgentViewRunStartedEvent,
    ConsumerReloadedEvent,
    ConsumerStartedEvent,
    ConsumerStoppingEvent,
    CredentialAuthFailedEvent,
    CredentialAuthThrottledEvent,
    CredentialUsageLimitedEvent,
    JobClaimedEvent,
    JobDeadEvent,
    JobFailedEvent,
    JobFinalizeEvent,
    JobRetryingEvent,
    JobSucceededEvent,
    JobVerificationFailed,
    WorkerStartedEvent,
    WorkerStoppedEvent,
    dispatch_credential_event,
)
from .harness import (
    HarnessRunContext,
    McpInitReport,
    RunRequest,
    RunResult,
    create_runner,
    get_harness,
    get_harness_config,
    resolve_provider,
    workspace_adapter_for,
)
from .job_models import Job, JobStatus
from .retry_policy import evaluate as evaluate_retry
from .run_preparation import materialize_run_workspace
from .workflows import get_workflow_class
from .workflows.base import JobContext

# Fallback throttle window applied to a usage-limited token when the CLI error gave
# no parseable reset time. Session/usage limits typically reset on an hourly/daily
# boundary; 1h keeps the token out of the pool long enough to fail over while still
# recovering on its own.
_DEFAULT_LIMIT_THROTTLE = timedelta(hours=1)

# Transient auth failures (revoked/stale access token) get a SHORT cooldown, not the
# 1h usage-limit window: the credential itself is usually fine and heals on the next
# refresh capture. 15 min outlasts the 60s/300s retry backoffs, so attempts 2 and 3
# are guaranteed to land on a different token, while still auto-recovering fast.
_DEFAULT_TRANSIENT_AUTH_THROTTLE = timedelta(minutes=15)

# When the WHOLE pool is temporarily unavailable — every token throttled by a usage
# limit, or every healthy token busy/held by a refresh lease — the job is rescheduled for
# just after the pool recovers rather than dead-lettered. The extra randomised gap
# (a) clears the ``throttled_until`` / ``leased_until`` boundary so a token is actually
# selectable again, and (b) spreads a thundering herd of waiting jobs so they don't all
# wake and re-hit the same wall in the same instant.
_POOL_RETRY_JITTER_SECONDS = (60, 300)


def _should_resume(
    *, attempt: int, session_id: str | None, pid_alive: bool, can_resume: bool
) -> bool:
    """Whether to resume the stored session instead of starting a fresh run.

    ``can_resume`` is the harness's declared ``capabilities.resume`` and is
    authoritative. A harness that cannot resume must start fresh rather than be
    handed a session id: the consumer resumes with an EMPTY prompt, so a CLI that
    merely re-opens the session would exit having done nothing and be recorded as
    a success. Extracted from ``_run_job`` so the gate is directly testable.
    """
    return attempt > 1 and session_id is not None and not pid_alive and can_resume


@dataclass
class _JobResult:
    """Carries execution metadata from _run_job to _finalize_job."""
    summary: str
    # `job.agent_type` already stores what is now called the harness id ("claude",
    # "codex"), so the harness lands in the existing column and no rename is needed.
    # The provider is recorded per run on `usage_log.provider`.
    agent_type: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt: str | None = None
    output: str | None = None
    session_id: str | None = None
    mcp_init: McpInitReport | None = None

    @classmethod
    def from_run_result(cls, result: RunResult, summary: str) -> _JobResult:
        return cls(
            summary=summary,
            agent_type=result.harness,
            provider=result.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            prompt=result.prompt,
            output=result.raw_output,
            session_id=result.session_id,
            mcp_init=result.mcp_init,
        )

DEQUEUE_SQL = """
    SELECT * FROM job
    WHERE status = 'TODO'
      AND scheduled_after <= NOW()
    ORDER BY priority DESC, created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""

CLAIM_SQL = """
    UPDATE job
    SET status = 'RUNNING', started_at = NOW(), attempt = attempt + 1, updated_at = NOW()
    WHERE id = %s AND status = 'TODO'
"""


class Consumer:
    """Long-running consumer that dequeues and executes jobs from MySQL."""

    def __init__(
        self,
        db_config: DatabaseConfig,
        consumer_config: ConsumerConfig,
        logger: logging.Logger,
        *,
        model_override: str | None = None,
    ):
        self.logger = logger
        self.model_override = model_override
        self._shutdown = threading.Event()
        self._db_config = db_config
        self._consumer_config = consumer_config
        # The freshness horizon is derived from job_timeout_seconds rather than a new env
        # var: the entrypoint whitelist only forwards AGENTO_*-prefixed knobs and an AST
        # test guards every from_env() literal against it, so a knob we can derive is pure
        # maintenance cost. The slack is why it is a heuristic — see _refresh_imminent.
        self._credential_resolver = CredentialResolver(
            refresh_horizon_seconds=consumer_config.job_timeout_seconds + 900,
            lease_ttl_seconds=_DEFAULT_LEASE_TTL_SECONDS,
        )
        self._active_jobs = 0
        self._active_jobs_lock = threading.Lock()
        # Refresh leases this process holds: lease_owner -> credential_id. Guarded by
        # _active_jobs_lock. Renewing an entry whose worker has ended would keep a DB
        # lease alive forever, so entries are dropped unconditionally at job end and the
        # row is left to expire — the correct failure direction.
        self._held_leases: dict[str, int] = {}

    def run(self) -> None:
        """Main loop. Blocks until SIGTERM/SIGINT."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        max_workers = self._consumer_config.max_workers
        self.logger.info(
            f"Consumer starting: max_workers={max_workers}, "
            f"poll_interval={self._consumer_config.poll_interval}s, "
            f"job_timeout={self._consumer_config.job_timeout_seconds}s"
        )

        get_event_manager().dispatch("consumer_start_after", ConsumerStartedEvent())

        self._recover_stale_jobs()

        semaphore = threading.Semaphore(max_workers)
        executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="consumer",
        )

        def _run_and_release(job: Job) -> None:
            try:
                self._execute_job(job)
            finally:
                with self._active_jobs_lock:
                    self._active_jobs -= 1
                semaphore.release()

        try:
            while not self._shutdown.is_set():
                self._maybe_reload_bootstrap()
                # NOT gated on _active_jobs == 0 like the reload above: renewal matters
                # precisely while workers are busy holding leases.
                self._renew_leases()

                if not semaphore.acquire(timeout=self._consumer_config.poll_interval):
                    continue  # timed out waiting for a free slot
                if self._shutdown.is_set():
                    semaphore.release()
                    break
                job = self._try_dequeue()
                if job:
                    with self._active_jobs_lock:
                        self._active_jobs += 1
                    executor.submit(_run_and_release, job)
                else:
                    semaphore.release()
                    self._shutdown.wait(timeout=self._consumer_config.poll_interval)
        finally:
            get_event_manager().dispatch("consumer_stop_before", ConsumerStoppingEvent())
            self.logger.info("Consumer shutting down, waiting for running jobs...")
            # Keep renewing while draining. executor.shutdown(wait=True) below is itself
            # unbounded, so a cap here would only re-break the lease for exactly the jobs
            # that outlive it — expiring a lease mid-capture is the failure the lease
            # exists to prevent. The wait doubles as the pacing so this is not a busy loop.
            while True:
                with self._active_jobs_lock:
                    if self._active_jobs <= 0:
                        break
                self._renew_leases()
                time.sleep(self._consumer_config.poll_interval)
            executor.shutdown(wait=True, cancel_futures=False)
            dispatch_shutdown()
            self.logger.info("Consumer stopped.")

    def _maybe_reload_bootstrap(self) -> None:
        """Re-bootstrap from disk + DB when no jobs are active.

        Magento-style live reload: each tick re-reads modules.json, manifests,
        and core_config_data so `mo:en/mo:di` and config changes apply within
        one poll cycle. Skipped while workers are busy to avoid clearing the
        event manager mid-dispatch.
        """
        with self._active_jobs_lock:
            if self._active_jobs > 0:
                return
        try:
            conn = get_connection(self._db_config)
        except Exception as exc:
            self.logger.warning(
                "Re-bootstrap skipped: DB connection error (%s)", type(exc).__name__
            )
            return
        try:
            # Distinct from shutdown: observers expecting graceful-shutdown semantics must NOT subscribe to module_reload_before.
            dispatch_reload()
            start = time.monotonic()
            manifests = bootstrap(db_conn=conn, quiet=True)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            get_event_manager().dispatch(
                "consumer_reload_after",
                ConsumerReloadedEvent(module_count=len(manifests), elapsed_ms=elapsed_ms),
            )
        except Exception:
            self.logger.exception("Re-bootstrap failed — continuing with previous registry")
        finally:
            conn.close()

    def _handle_signal(self, signum: int, frame: object) -> None:
        sig_name = signal.Signals(signum).name
        self.logger.info(f"Received {sig_name}, initiating graceful shutdown")
        self._shutdown.set()

    def _save_pid(self, job_id: int, pid: int) -> None:
        """Best-effort: save subprocess PID to job row."""
        try:
            conn = get_connection(self._db_config)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE job SET pid = %s, updated_at = NOW() WHERE id = %s",
                        (pid, job_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            self.logger.warning(f"Failed to save PID {pid} for job {job_id} (best-effort)")

    def _save_session_id(self, job_id: int, session_id: str) -> None:
        """Best-effort: save session_id to job row."""
        try:
            conn = get_connection(self._db_config)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE job SET session_id = %s, updated_at = NOW() WHERE id = %s",
                        (session_id, job_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            self.logger.warning(f"Failed to save session_id for job {job_id} (best-effort)")

    @staticmethod
    def _is_pid_alive(pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _recover_stale_jobs(self) -> None:
        """Recover RUNNING jobs whose process has died (PID-based check).

        Jobs with a PID are checked via os.kill(pid, 0).  Jobs without a PID
        (callback hasn't fired yet) fall back to the timestamp threshold so we
        don't kill freshly-claimed jobs in multi-worker mode.
        """
        threshold = self._consumer_config.job_timeout_seconds + 60
        try:
            conn = get_connection(self._db_config)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, reference_id, pid, attempt, max_attempts, started_at "
                        "FROM job WHERE status = 'RUNNING'"
                    )
                    running_jobs = cur.fetchall()

                    retried = 0
                    dead = 0
                    for row in running_jobs:
                        job_id = row["id"]
                        ref_id = row["reference_id"]
                        pid = row["pid"]
                        attempt = row["attempt"]
                        max_attempts = row["max_attempts"]

                        if pid is not None:
                            if self._is_pid_alive(int(pid)):
                                continue
                        else:
                            # No PID yet — fall back to timestamp guard
                            started_at = row["started_at"]
                            if started_at is None:
                                continue
                            # PyMySQL returns naive datetimes (UTC assumed)
                            now = datetime.now(UTC).replace(tzinfo=None)
                            elapsed = (now - started_at).total_seconds()
                            if elapsed < threshold:
                                continue

                        if attempt < max_attempts:
                            cur.execute(
                                """
                                UPDATE job
                                SET status = 'TODO', finished_at = NOW(),
                                    error_message = %s,
                                    error_class = 'StaleJobRecovery',
                                    scheduled_after = NOW(), updated_at = NOW()
                                WHERE id = %s AND status = 'RUNNING'
                                """,
                                (f"Recovered: process dead (pid={pid})", job_id),
                            )
                            retried += 1
                            self.logger.warning(
                                f"Recovered stale job -> TODO (retry) | "
                                f"job_id={job_id} reference_id={ref_id} "
                                f"pid={pid} attempt={attempt}/{max_attempts}"
                            )
                        else:
                            cur.execute(
                                """
                                UPDATE job
                                SET status = 'DEAD', finished_at = NOW(),
                                    error_message = %s,
                                    error_class = 'StaleJobRecovery',
                                    updated_at = NOW()
                                WHERE id = %s AND status = 'RUNNING'
                                """,
                                (f"Recovered: process dead (pid={pid}), max attempts reached", job_id),
                            )
                            dead += 1
                            self.logger.warning(
                                f"Recovered stale job -> DEAD | "
                                f"job_id={job_id} reference_id={ref_id} "
                                f"pid={pid} attempt={attempt}/{max_attempts}"
                            )

                conn.commit()
                if retried or dead:
                    self.logger.warning(
                        f"Stale job recovery: {retried} retried, {dead} dead-lettered"
                    )
            finally:
                conn.close()
        except Exception:
            self.logger.exception("Failed to recover stale jobs (non-fatal, continuing)")

    def _try_dequeue(self) -> Job | None:
        """Claim one job from the queue. Returns None if empty."""
        conn = get_connection(self._db_config)
        try:
            with conn.cursor() as cur:
                cur.execute(DEQUEUE_SQL)
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None

                job = Job.from_row(row)
                cur.execute(CLAIM_SQL, (job.id,))
                conn.commit()

                job.status = JobStatus.RUNNING
                job.attempt += 1

                get_event_manager().dispatch("job_claim_after", JobClaimedEvent(job=job))

                return job
        except Exception:
            conn.rollback()
            self.logger.exception("Error during dequeue")
            return None
        finally:
            conn.close()

    def _execute_job(self, job: Job) -> None:
        """Execute a single job. Runs in a thread pool thread."""
        worker_slot = threading.current_thread().name
        em = get_event_manager()

        self.logger.info(
            "Starting job",
            extra={
                "job_id": job.id,
                "type": job.type.value,
                "reference_id": job.reference_id,
                "attempt": job.attempt,
                "agent_view_id": job.agent_view_id,
                "priority": job.priority,
                "worker_slot": worker_slot,
            },
        )
        em.dispatch("worker_start_after", WorkerStartedEvent(
            worker_slot=worker_slot, job_id=job.id,
        ))

        start_time = time.monotonic()
        error: Exception | None = None
        job_result: _JobResult | None = None

        try:
            job_result = self._run_job(job)
        except Exception as exc:
            error = exc
            self.logger.exception(
                "Job failed",
                extra={
                    "job_id": job.id, "reference_id": job.reference_id,
                    "attempt": job.attempt, "worker_slot": worker_slot,
                },
            )

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        self._finalize_job(job, error, job_result, elapsed_ms)

        em.dispatch("worker_stop_after", WorkerStoppedEvent(
            worker_slot=worker_slot, job_id=job.id, elapsed_ms=elapsed_ms,
        ))

    def _run_job(self, job: Job) -> _JobResult:
        """Dispatch to the appropriate workflow with agent_view routing."""
        channel = get_channel(job.source)
        em = get_event_manager()
        artifacts_dir = None
        # Pre-initialised for the outer `finally`: argument evaluation would raise
        # UnboundLocalError before the call if anything above raised, so "the callee
        # tolerates None" is not enough.
        home_dir = credential = harness = lease_owner = None
        # Whether the lease was actually ACQUIRED, which is not the same as having issued an
        # owner: only a refresh-imminent credential is leased. The detector below depends on
        # the difference, so the two must not be conflated.
        leased = False
        success = False
        # Lifecycle try: credential capture, self-heal and lease release must happen on
        # EVERY exit path — including a raise in the post-selection config reads or in
        # materialize_run_workspace, both of which sit outside the inner try below.
        try:
            # Resolve agent_view runtime profile (provider, model, scoped config)
            conn = get_connection(self._db_config)
            try:
                runtime = resolve_agent_view_runtime(conn, job.agent_view_id)

                # agent_view/harness must resolve — the sticky primary-credential
                # fallback is gone (credentials are an LRU pool per scope, not a single
                # globally-preferred license).
                if runtime.harness is None:
                    raise RuntimeError(
                        "No agent_view/harness configured. Set it via: "
                        "bin/agento config:set agent_view/harness <harness> "
                        "--scope=agent_view --scope-id=<id>"
                    )
                harness = runtime.harness
                harness_entry = get_harness(harness)
                provider_desc = resolve_provider(harness, runtime.provider)
                # Explicit --model flag (e2e/replay) wins over config (ENV/DB) model.
                model_override = self.model_override or runtime.model

                # The CALLER claims the credential — exactly once per run. The runner
                # consumes ctx.credential and never touches the pool, so the command and
                # the process can never end up on two different credentials.
                #
                # The lease owner identifies THIS execution (job + attempt), so a late
                # cleanup belonging to attempt 1 can never free the lease attempt 2 holds.
                # resolve() takes an exclusive lease only when the credential is close
                # enough to expiry that this run would likely rotate its single-use
                # refresh token.
                scope = provider_desc.credential_scope
                lease_owner = lease_owner_for_job(job.id, job.attempt)
                credential = (
                    self._credential_resolver.resolve(conn, scope, lease_owner=lease_owner)
                    if provider_desc.credential_required else None
                )
                if credential is not None and credential.lease_owner == lease_owner:
                    leased = True
                    # Register BEFORE anything that can block or raise: a lease that exists
                    # in the DB but not here is a lease nobody renews.
                    with self._active_jobs_lock:
                        self._held_leases[lease_owner] = credential.id

                # Resolve shared toolbox base URL (needed below for writer injection).
                from .config_resolver import ScopedConfigService
                from .scoped_config import Scope
                core_cfg = ScopedConfigService(conn).get_module("core") or {}
                toolbox_url = core_cfg.get("toolbox/url") or "http://toolbox:3001"

                # Agent_view-scoped service for writing workspace config. Built while
                # conn is open; its .get() works off the materialized overrides
                # afterwards (ENV -> DB -> config.json, decrypting as needed).
                agent_config_svc = (
                    ScopedConfigService(conn, Scope.AGENT_VIEW, job.agent_view_id)
                    if job.agent_view_id is not None else None
                )
            finally:
                conn.close()

            # Per-job artifacts directory (only when agent_view is set) — extracted
            # so `agento run` exercises the same pipeline (see run_preparation.py).
            home_dir, artifacts_dir = materialize_run_workspace(
                runtime,
                run_id=job.id,
                agent_config_svc=agent_config_svc,
                toolbox_url=toolbox_url,
                em=em,
                credential=credential,
                # The effective model for THIS run — `--model` (e2e/replay) overrides
                # config. Without it the harness's per-run injection would carry the
                # build-time value and a legitimate override could be rejected by its
                # own model guard.
                effective_model=model_override,
            )

            em.dispatch("agent_view_run_start_before", AgentViewRunStartedEvent(
                job=job,
                agent_view_id=job.agent_view_id,
                harness=harness,
                provider=provider_desc.id,
                model=model_override,
                priority=job.priority,
                artifacts_dir=str(artifacts_dir) if artifacts_dir else "",
            ))

            # Git commit author identity → GIT_AUTHOR_*/GIT_COMMITTER_* env. These override every
            # gitconfig level (incl. a repo-local .git/config), so the agent's commits are authored
            # correctly even in a clone carrying its own stale [user]. Empty config ⇒ no override.
            from .git_identity import (
                GIT_AUTHOR_EMAIL_PATH,
                GIT_AUTHOR_NAME_PATH,
                git_identity_env,
            )
            git_env = (
                git_identity_env(
                    agent_config_svc.get(GIT_AUTHOR_NAME_PATH) or "",
                    agent_config_svc.get(GIT_AUTHOR_EMAIL_PATH) or "",
                )
                if agent_config_svc is not None else {}
            )

            success = True
            try:
                ctx = HarnessRunContext(
                    harness=harness,
                    provider=provider_desc.id,
                    model=model_override,
                    working_dir=str(artifacts_dir) if artifacts_dir else "/workspace",
                    home_dir=str(home_dir) if home_dir else None,
                    timeout_seconds=self._consumer_config.job_timeout_seconds,
                    credential_required=provider_desc.credential_required,
                    credential=credential,
                    extra_env=git_env or {},
                    harness_config=(
                        get_harness_config(agent_config_svc, harness_entry)
                        if agent_config_svc is not None else {}
                    ),
                )
                runner = create_runner(
                    harness,
                    ctx,
                    logger=self.logger,
                    dry_run=self._consumer_config.disable_llm,
                )
                # Persist session_id to both the DB and the in-memory job — the
                # verification observer reads ``event.job.session_id`` to locate
                # the agent's transcript, and would otherwise see ``None`` on the
                # first attempt (the DB write doesn't refresh the local dataclass).
                def _on_session_id(sid: str) -> None:
                    self._save_session_id(job.id, sid)
                    job.session_id = sid

                # Through the protocol method, not by assigning attributes: a third-party
                # runner using __slots__ would raise AttributeError on assignment.
                runner.observe(
                    on_pid=lambda pid: self._save_pid(job.id, pid),
                    on_session_id=_on_session_id,
                )

                should_resume = _should_resume(
                    attempt=job.attempt,
                    session_id=job.session_id,
                    pid_alive=self._is_pid_alive(job.pid),
                    can_resume=harness_entry.descriptor.capabilities.resume,
                )
                if should_resume:
                    self.logger.info(
                        f"Resuming session {job.session_id} for job {job.id} "
                        f"(attempt={job.attempt}, prev_pid={job.pid})"
                    )
                    result = runner.execute(
                        RunRequest(prompt="", session_id=job.session_id, model=model_override)
                    )
                    result.prompt = f"[RESUME] session_id={job.session_id}"
                    summary = f"resumed session_id={job.session_id} {result.stats_line}"
                    return _JobResult.from_run_result(result, summary)

                workflow = get_workflow_class(job.type)(runner, self.logger)

                module_config = get_module_config(job.source) if job.source != "blank" else {}
                context = JobContext(
                    config=module_config,
                    logger=self.logger,
                    update_reference_id=self._update_job_reference_id,
                )
                result = workflow.execute_job(channel, job, context)

                summary = (
                    result.raw_output
                    if result.input_tokens is None and result.raw_output
                    else f"session_id={result.session_id or '?'} {result.stats_line}"
                )
                return _JobResult.from_run_result(result, summary)
            except TransientAuthError as exc:
                success = False
                self._handle_transient_auth(job, credential, scope, exc)
                raise
            except UsageLimitError as exc:
                success = False
                self._handle_usage_limit(job, credential, scope, exc)
                raise
            except AuthenticationError as exc:
                success = False
                self._handle_auth_failure(job, credential, scope, exc)
                raise
            except Exception:
                success = False
                raise
            finally:
                em.dispatch("agent_view_run_finish_after", AgentViewRunFinishedEvent(
                    job=job,
                    agent_view_id=job.agent_view_id,
                    harness=harness,
                    provider=provider_desc.id,
                    model=model_override,
                    success=success,
                ))
        finally:
            self._finish_credential_lifecycle(
                job, harness, credential, home_dir,
                success=success, lease_owner=lease_owner, leased=leased,
            )

    def _renew_leases(self) -> None:
        """Push the deadline forward on every refresh lease this process still holds.

        ``leased_until`` is a liveness deadline, not a duration estimate: a lease lives
        exactly as long as this consumer has a worker for that job (covering the pre-pid
        setup window and the post-exit capture window alike), and a dead consumer's leases
        free themselves within one TTL with no reaper. Deliberately NOT driven from
        ``_recover_stale_jobs``: that runs once at startup and is pid-based, and
        ``os.kill(pid, 0)`` is false between subprocess exit and credential capture — a
        pid-driven reaper could free a lease mid-rotation.

        Best-effort, exactly like ``_maybe_reload_bootstrap``: a DB blip must not kill the
        main loop. The dict is snapshotted under the lock and the lock released before any
        DB I/O, so a slow UPDATE cannot block workers starting or finishing.
        """
        with self._active_jobs_lock:
            owners = list(self._held_leases)
        if not owners:
            return
        try:
            conn = get_connection(self._db_config)
            try:
                renew_credential_leases(
                    conn, owners, _DEFAULT_LEASE_TTL_SECONDS, logger=self.logger
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            self.logger.warning(
                "Refresh lease renewal skipped: %s (%s)", type(exc).__name__, exc
            )

    def _finish_credential_lifecycle(
        self,
        job: Job,
        harness: str | None,
        credential,
        home_dir,
        *,
        success: bool,
        lease_owner: str | None = None,
        leased: bool = False,
    ) -> None:
        """Close out the credential side of one job execution on EVERY exit path.

        Three steps on ONE connection with ONE commit: persist any credential the CLI
        rotated, lift the framework's own quarantine if the run succeeded (a completed run
        proves the credential works — a rotation does not), and release the refresh lease.

        ``lease_owner`` is the owner that was ISSUED for this execution; ``leased`` says
        whether it was actually acquired (only a refresh-imminent credential is leased).
        Conflating the two silently disables the residual-race detector below, because an
        owner is issued for every job.

        The capture's ``except`` is nested so a broken adapter hook can never skip the
        release. Release is attempted whenever a ``lease_owner`` was issued and never gated
        on the in-memory ``credential.lease_owner``: the UPDATE's own
        ``WHERE lease_owner = %s`` is the authority, a no-op costs one statement, and a
        skipped release costs a whole TTL. The ``_held_leases`` entry is dropped in a local
        ``finally`` so a failed connection, release or commit still stops renewal and lets
        the row expire.
        """
        try:
            if credential is None or harness is None:
                return  # nothing claimed -> nothing to capture, clear or release
            try:
                conn = get_connection(self._db_config)
            except Exception:
                self.logger.warning(
                    "credential lifecycle skipped for job_id=%s: DB connection failed",
                    job.id, exc_info=True,
                )
                return
            try:
                rotated = False
                if home_dir is not None:
                    try:
                        # capture_refreshed_credentials is part of the WorkspaceAdapter
                        # protocol now — no getattr probing.
                        rotated = bool(
                            workspace_adapter_for(harness).capture_refreshed_credentials(
                                home_dir, credential, conn
                            )
                        )
                    except Exception:
                        self.logger.warning(
                            "post-run credential capture failed for job_id=%s harness=%s",
                            job.id, harness, exc_info=True,
                        )
                if rotated and not leased:
                    # The falsifiable-assumption detector for the freshness horizon: a
                    # rotation with no lease means a second worker could have been handed
                    # the spent refresh token. Must stay empty in production.
                    self.logger.error(
                        "Credential id=%s rotated its payload WITHOUT holding a refresh "
                        "lease (job_id=%s) — raise the freshness horizon",
                        credential.id, job.id,
                    )
                if success:
                    try:
                        clear_auto_credential_error(conn, credential.id, logger=self.logger)
                    except Exception:
                        # Nested for the same reason as the capture: a failed self-heal must
                        # never cost the release, which would strand the row for a full TTL.
                        self.logger.warning(
                            "self-heal of an automatic quarantine failed for credential id=%s",
                            credential.id, exc_info=True,
                        )
                if lease_owner:
                    release_credential_lease(
                        conn, credential.id, lease_owner, logger=self.logger
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            self.logger.warning(
                "credential lifecycle failed for job_id=%s", job.id, exc_info=True
            )
        finally:
            if lease_owner:
                with self._active_jobs_lock:
                    self._held_leases.pop(lease_owner, None)

    def _handle_auth_failure(
        self,
        job: Job,
        credential,
        scope: str | None,
        exc: AuthenticationError | TransientAuthError,
    ) -> None:
        """Mark the offending token as errored so the pool stops handing it out,
        and dispatch ``token_auth_failed_after`` for observers. Best-effort —
        DB issues here must not mask the original failure about to be re-raised."""
        credential_id = (
            exc.credential_id if exc.credential_id is not None
            else getattr(credential, "id", None)
        )
        try:
            conn = get_connection(self._db_config)
            try:
                mark_credential_error(
                    conn, credential_id, str(exc), logger=self.logger, source="auto"
                )
                conn.commit()
                # Pool-aware retry: now that the offending token is poisoned,
                # let the job retry onto the next token if a healthy one remains
                # for this provider. ``retry_policy.evaluate`` reads this flag to
                # override AuthenticationError's default terminal disposition.
                _total, healthy = count_credentials_for_scope(conn, scope)
                exc.retry_with_other_token = healthy > 0
            finally:
                conn.close()
        except Exception:
            self.logger.exception(
                "Failed to mark token as errored after auth failure",
                extra={"job_id": job.id, "credential_id": credential_id},
            )
        try:
            dispatch_credential_event(
                "credential_auth_failed_after",
                CredentialAuthFailedEvent(
                    scope=scope or "",
                    credential_id=credential_id,
                    error_msg=str(exc),
                    job_id=job.id,
                ),
            )
        except Exception:
            self.logger.exception(
                "credential_auth_failed_after observer failed",
                extra={"job_id": job.id, "credential_id": credential_id},
            )

    def _handle_usage_limit(
        self,
        job: Job,
        credential,
        scope: str | None,
        exc: UsageLimitError,
    ) -> None:
        """Throttle the usage/session-limited token until its reset time (a temporary
        cooldown — NOT a poison) so the pool skips it and the job fails over to a
        healthy token, then dispatch ``token_usage_limited_after``. Best-effort — DB
        issues here must not mask the original failure about to be re-raised."""
        credential_id = (
            exc.credential_id if exc.credential_id is not None
            else getattr(credential, "id", None)
        )
        until = exc.reset_at or (datetime.now(UTC).replace(tzinfo=None) + _DEFAULT_LIMIT_THROTTLE)
        try:
            conn = get_connection(self._db_config)
            try:
                throttle_credential(conn, credential_id, until, str(exc), logger=self.logger)
                conn.commit()
                # Pool-aware retry: with the offending token now throttled (and thus
                # excluded from the healthy count), let the job retry onto the next
                # token if a healthy one remains for this provider.
                _total, healthy = count_credentials_for_scope(conn, scope)
                exc.retry_with_other_token = healthy > 0
                # Whole pool throttled (no healthy token, no failover): don't
                # dead-letter — a usage limit is temporary. Tell ``_finalize_job``
                # when the pool next recovers so it reschedules the job for that time
                # instead. ``earliest_throttle_reset_for_scope`` sees the throttle we
                # just committed, so it is at worst this credential's own reset.
                if healthy == 0:
                    exc.pool_retry_at = (
                        earliest_throttle_reset_for_scope(conn, scope) or until
                    )
            finally:
                conn.close()
        except Exception:
            self.logger.exception(
                "Failed to throttle token after usage limit",
                extra={"job_id": job.id, "credential_id": credential_id},
            )
        try:
            dispatch_credential_event(
                "credential_usage_limited_after",
                CredentialUsageLimitedEvent(
                    scope=scope or "",
                    credential_id=credential_id,
                    error_msg=str(exc),
                    reset_at=until,
                    job_id=job.id,
                ),
            )
        except Exception:
            self.logger.exception(
                "credential_usage_limited_after observer failed",
                extra={"job_id": job.id, "credential_id": credential_id},
            )

    def _update_job_reference_id(self, job_id: int, reference_id: str) -> None:
        conn = get_connection(self._db_config)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job SET reference_id = %s, updated_at = NOW() WHERE id = %s",
                    (reference_id, job_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _finalize_job(
        self,
        job: Job,
        error: Exception | None,
        job_result: _JobResult | None,
        elapsed_ms: int,
    ) -> None:
        """Update job status in MySQL after execution completes.

        Retries DB updates up to 3 times with fresh connections to avoid
        leaving jobs stuck in RUNNING if the DB hiccups.
        """
        max_db_retries = 3
        em = get_event_manager()

        # Build the finalize event once; observers on ``job_finalize_before``
        # may mutate ``.verdict`` to veto an apparent success. We dispatch
        # exactly once across DB retries (using ``verify_dispatched``) and
        # then fire ``job_finalize_after`` after we commit a terminal status.
        # ``provider`` is what the run reported (e.g. "claude") — observers use
        # it to resolve a provider-specific TranscriptReader from the registry.
        finalize_event = JobFinalizeEvent(
            job=job,
            job_result=job_result,
            elapsed_ms=elapsed_ms,
            harness=job_result.agent_type if job_result else None,
            verdict=None,
        )
        verify_dispatched = False
        finalize_after_pending = False

        for db_attempt in range(1, max_db_retries + 1):
            conn = get_connection(self._db_config)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT status FROM job WHERE id = %s", (job.id,))
                    row = cur.fetchone()
                if row is None:
                    current_status = None
                elif isinstance(row, dict):
                    current_status = row.get("status")
                else:
                    current_status = row[0]
                if current_status != "RUNNING":
                    self.logger.info(
                        "Job finalize skipped (status changed during run)",
                        extra={
                            "job_id": job.id,
                            "reference_id": job.reference_id,
                            "current_status": current_status,
                        },
                    )
                    return

                if error is None and not verify_dispatched:
                    em.dispatch("job_finalize_before", finalize_event)
                    verify_dispatched = True
                    if finalize_event.verdict is not None:
                        error = JobVerificationFailed(finalize_event.verdict)

                if error is None:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE job
                            SET status = 'SUCCESS', finished_at = NOW(),
                                result_summary = %s, agent_type = %s, provider = %s,
                                model = %s,
                                input_tokens = %s, output_tokens = %s,
                                prompt = %s, output = %s,
                                updated_at = NOW()
                            WHERE id = %s AND status = 'RUNNING'
                            """,
                            (
                                job_result.summary if job_result else None,
                                job_result.agent_type if job_result else None,
                                job_result.provider if job_result else None,
                                job_result.model if job_result else None,
                                job_result.input_tokens if job_result else None,
                                job_result.output_tokens if job_result else None,
                                job_result.prompt if job_result else None,
                                job_result.output if job_result else None,
                                job.id,
                            ),
                        )
                    conn.commit()
                    self.logger.info(
                        "Job succeeded",
                        extra={
                            "job_id": job.id,
                            "reference_id": job.reference_id,
                            "status": "SUCCESS",
                            "duration_ms": elapsed_ms,
                            "result_summary": job_result.summary if job_result else None,
                        },
                    )
                    em.dispatch(
                        "job_succeed_after",
                        JobSucceededEvent(
                            job=job,
                            summary=job_result.summary if job_result else None,
                            agent_type=job_result.agent_type if job_result else None,
                            model=job_result.model if job_result else None,
                            elapsed_ms=elapsed_ms,
                        ),
                    )
                else:
                    error_class = error.__class__.__name__
                    error_msg = str(error)[:2000]
                    # The agent's own output travels on the exception rather than inside
                    # error_message, so it lands in the column meant for it. Operators lose
                    # nothing — they gain the full output instead of a 500-char excerpt —
                    # and error_message stays free of prompt/customer content.
                    agent_output = getattr(error, "agent_output", None)
                    decision = evaluate_retry(
                        error_class, job.attempt, job.max_attempts, error_obj=error,
                    )

                    em.dispatch(
                        "job_fail_after",
                        JobFailedEvent(job=job, error=error, elapsed_ms=elapsed_ms),
                    )

                    # Extract session_id from result or error (best-effort)
                    session_id = job_result.session_id if job_result else None
                    if session_id is None:
                        session_id = getattr(error, "session_id", None)

                    # Verification veto with ``fresh_start`` → next retry must
                    # spawn a brand-new claude-cli session (incident 3368).
                    fresh_start = (
                        isinstance(error, JobVerificationFailed)
                        and error.verdict.fresh_start
                    )
                    if fresh_start:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE job SET session_id = NULL, updated_at = NOW() WHERE id = %s",
                                (job.id,),
                            )
                        session_id = None

                    # The WHOLE pool is temporarily unavailable — either every token is
                    # usage-limited (``_handle_usage_limit``) or every healthy token is
                    # transiently busy/leased (``CredentialsBusyError`` from ``resolve``).
                    # Both set ``pool_retry_at`` to the naive-UTC time the pool next
                    # recovers. Wait for that instead of dead-lettering: a pool that heals
                    # on its own is not a real failure. Checked BEFORE ``should_retry`` so a
                    # known recovery time wins over blind backoff; ``None`` (no recovery
                    # time, e.g. pure row-lock contention) falls through to ordinary retry.
                    pool_retry_at = getattr(error, "pool_retry_at", None)

                    if pool_retry_at is not None:
                        # The whole pool is temporarily unavailable (all tokens throttled,
                        # or all healthy tokens busy/leased). Reschedule for just after the
                        # pool recovers, plus a randomised gap to clear the boundary and
                        # de-sync waiting jobs. ``pool_retry_at`` is naive UTC (from
                        # ``throttled_until`` or ``leased_until``); the DB session is UTC so
                        # it compares correctly against ``NOW()``.
                        jitter = random.randint(*_POOL_RETRY_JITTER_SECONDS)
                        scheduled_after = pool_retry_at + timedelta(seconds=jitter)
                        # Refund the attempt spent on this run: waiting for the pool is not a
                        # real failure, so it must not march the job toward ``max_attempts``
                        # and reintroduce the dead-letter this fix removes.
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE job
                                SET status = 'TODO', finished_at = NOW(),
                                    attempt = GREATEST(attempt - 1, 0),
                                    error_message = %s, error_class = %s,
                                    output = COALESCE(%s, output),
                                    session_id = COALESCE(%s, session_id),
                                    scheduled_after = %s, updated_at = NOW()
                                WHERE id = %s AND status = 'RUNNING'
                                """,
                                (
                                    error_msg, error_class, agent_output,
                                    session_id, scheduled_after, job.id,
                                ),
                            )
                        conn.commit()
                        self.logger.info(
                            "Job waiting for pool to recover: whole pool throttled or "
                            f"busy, rescheduled for {scheduled_after} (UTC)",
                            extra={
                                "job_id": job.id,
                                "reference_id": job.reference_id,
                                "status": "TODO",
                                "duration_ms": elapsed_ms,
                            },
                        )
                        em.dispatch(
                            "job_retry_after",
                            JobRetryingEvent(
                                job=job,
                                error=error,
                                delay_seconds=max(
                                    int((scheduled_after - datetime.now(UTC).replace(tzinfo=None)).total_seconds()),
                                    0,
                                ),
                                elapsed_ms=elapsed_ms,
                            ),
                        )
                    elif decision.should_retry:
                        scheduled_after = datetime.now(UTC) + timedelta(seconds=decision.delay_seconds)
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE job
                                SET status = 'TODO', finished_at = NOW(),
                                    error_message = %s, error_class = %s,
                                    output = COALESCE(%s, output),
                                    session_id = COALESCE(%s, session_id),
                                    scheduled_after = %s, updated_at = NOW()
                                WHERE id = %s AND status = 'RUNNING'
                                """,
                                (
                                    error_msg, error_class, agent_output,
                                    session_id, scheduled_after, job.id,
                                ),
                            )
                        conn.commit()
                        self.logger.info(
                            f"Job scheduled for retry: {decision.reason}",
                            extra={
                                "job_id": job.id,
                                "reference_id": job.reference_id,
                                "status": "TODO",
                                "duration_ms": elapsed_ms,
                            },
                        )
                        em.dispatch(
                            "job_retry_after",
                            JobRetryingEvent(
                                job=job,
                                error=error,
                                delay_seconds=decision.delay_seconds,
                                elapsed_ms=elapsed_ms,
                            ),
                        )
                    else:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE job
                                SET status = 'DEAD', finished_at = NOW(),
                                    error_message = %s, error_class = %s,
                                    output = COALESCE(%s, output),
                                    session_id = COALESCE(%s, session_id),
                                    updated_at = NOW()
                                WHERE id = %s AND status = 'RUNNING'
                                """,
                                (error_msg, error_class, agent_output, session_id, job.id),
                            )
                        conn.commit()
                        self.logger.warning(
                            f"Job dead-lettered: {decision.reason}",
                            extra={
                                "job_id": job.id,
                                "reference_id": job.reference_id,
                                "status": "DEAD",
                                "duration_ms": elapsed_ms,
                            },
                        )
                        em.dispatch(
                            "job_dead_after",
                            JobDeadEvent(job=job, error=error, elapsed_ms=elapsed_ms),
                        )
                finalize_after_pending = True
                return  # DB update succeeded
            except Exception:
                conn.rollback()
                if db_attempt < max_db_retries:
                    self.logger.warning(
                        f"Failed to finalize job {job.id} "
                        f"(DB attempt {db_attempt}/{max_db_retries}), retrying..."
                    )
                    time.sleep(1)
                else:
                    self.logger.critical(
                        f"FAILED to finalize job {job.id} after {max_db_retries} attempts. "
                        f"Job may be stuck in RUNNING. Manual intervention required."
                    )
            finally:
                conn.close()
                if finalize_after_pending:
                    em.dispatch("job_finalize_after", finalize_event)

    def _handle_transient_auth(
        self,
        job: Job,
        credential,
        scope: str | None,
        exc: TransientAuthError,
    ) -> None:
        """Throttle a credential whose token was rejected in a way that does NOT prove
        it is dead (revoked/stale access token) — a short cooldown via
        ``throttled_until``, NOT ``status='error'``: poisoning would take a token that
        is still serving other jobs out of rotation. Best-effort — DB issues here must
        not mask the original failure about to be re-raised.

        The harness's error classifier cannot make this call — it sees only a message — so
        the credential-aware half lives here. A credential with nothing to rotate has no
        stale copy to blame: its rejection is real, and throttling it forever would trade
        this incident for a silent one, so it is delegated to the poison path instead.
        "Rotatable" is the framework-owned flat ``refresh_token`` field, never a
        harness-specific ``type == 'oauth'`` literal.
        """
        if not (getattr(credential, "credentials", None) or {}).get("refresh_token"):
            self._handle_auth_failure(job, credential, scope, exc)
            return
        credential_id = (
            exc.credential_id if exc.credential_id is not None
            else getattr(credential, "id", None)
        )
        until = datetime.now(UTC).replace(tzinfo=None) + _DEFAULT_TRANSIENT_AUTH_THROTTLE
        try:
            conn = get_connection(self._db_config)
            try:
                throttle_credential(conn, credential_id, until, str(exc), logger=self.logger)
                conn.commit()
                _total, healthy = count_credentials_for_scope(conn, scope)
                exc.retry_with_other_token = healthy > 0
            finally:
                conn.close()
        except Exception:
            self.logger.exception(
                "Failed to throttle token after transient auth failure",
                extra={"job_id": job.id, "credential_id": credential_id},
            )
        try:
            dispatch_credential_event(
                "credential_auth_throttled_after",
                CredentialAuthThrottledEvent(
                    scope=scope or "",
                    credential_id=credential_id,
                    error_msg=str(exc),
                    throttled_until=until,
                    job_id=job.id,
                ),
            )
        except Exception:
            self.logger.exception(
                "credential_auth_throttled_after observer failed",
                extra={"job_id": job.id, "credential_id": credential_id},
            )
