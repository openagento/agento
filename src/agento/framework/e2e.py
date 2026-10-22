"""End-to-end tests using real LLM calls and the prod database.

Exercises the full consumer pipeline: dequeue → channel → workflow → runner → finalize.
The selected credential drives the run: the scenario's consumer claims only that
credential, so the pool and the other credentials are left as they are.
Requires healthy tokens and DISABLE_LLM=0.
"""
from __future__ import annotations

import logging
import secrets
import sys

from .agent_manager.credential_store import get_credential, select_credential
from .agent_manager.models import CredentialRecord
from .channels.registry import register_channel
from .channels.test import TestChannel
from .consumer import Consumer
from .consumer_config import ConsumerConfig
from .database_config import DatabaseConfig
from .db import get_connection
from .harness import find_harness
from .job_models import AgentType
from .publisher import insert_job


def _insert_test_job(
    db_config: DatabaseConfig, reference_id: str, agent_view_id: int | None = None,
    *, harness: str | None = None, model: str | None = None,
) -> int:
    """Insert a TODO job with type/source='blank' and return its id.

    With ``agent_view_id`` the job runs as that agent_view: its harness, its workspace
    build and its toolbox. ``harness`` / ``model`` PIN the job's runtime (see
    docs/cli/job-runtime.md), so a test needs no ``config:set`` on the view.

    ``scheduled_after`` is far in the future so a live stack's consumer never claims the
    row; ``agento e2e`` claims it by id, which ignores the schedule. It is set before the
    commit, so no consumer ever sees the row claimable.
    """
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            job_id = insert_job(
                cur, agent_type=AgentType.BLANK, source="blank", agent_view_id=agent_view_id,
                priority=50, reference_id=reference_id,
                idempotency_key=f"e2e:{reference_id}:{secrets.token_hex(8)}", max_attempts=1,
                requester=None, harness=harness, model=model,
                # Its own thread: a kept or crashed earlier run must not be its line.
                thread=f"e2e:{secrets.token_hex(8)}",
            )
            cur.execute("UPDATE job SET scheduled_after = '2038-01-01 00:00:00' WHERE id = %s",
                        (job_id,))
        conn.commit()
        return job_id
    finally:
        conn.close()


def _fetch_job(db_config: DatabaseConfig, job_id: int) -> dict | None:
    """Fetch a job row by id."""
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _delete_job(db_config: DatabaseConfig, job_id: int) -> None:
    """Delete a test job row and its thread (each e2e run has a thread of its own)."""
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT thread_id FROM job WHERE id = %s", (job_id,))
            row = cur.fetchone()
            cur.execute("DELETE FROM job WHERE id = %s", (job_id,))
            if row is not None and row["thread_id"] is not None:
                cur.execute("DELETE FROM thread WHERE id = %s", (row["thread_id"],))
        conn.commit()
    finally:
        conn.close()


def _run_checks(
    row: dict, *, agent_view: bool = False, mcp_init_report: bool = True,
) -> list[tuple[str, bool, str]]:
    """Return list of (label, passed, detail) for a finished job row.

    ``agent_view``: the job ran with a toolbox, so the telemetry columns that
    ``app_monitor`` writes from the run's init event and transcript must be set.
    ``mcp_init_report``: the harness's capability. Without it the CLI never says
    whether ``toolbox`` connected, so ``toolbox_mcp_connected`` stays NULL by design.
    """
    checks = [
        ("status=SUCCESS", row["status"] == "SUCCESS", row["status"]),
        ("agent_type set", row["agent_type"] is not None, str(row["agent_type"])),
        ("model set", row["model"] is not None, str(row["model"])),
        ("input_tokens > 0", (row["input_tokens"] or 0) > 0, str(row["input_tokens"])),
        ("prompt saved", bool(row["prompt"]), f"{len(row['prompt'] or '')} chars"),
        ("output saved", bool(row["output"]), f"{len(row['output'] or '')} chars"),
        ("result_summary has stats", "session_id=" in (row["result_summary"] or ""), str(row["result_summary"])),
    ]
    if agent_view:
        connected, calls = row.get("toolbox_mcp_connected"), row.get("toolbox_mcp_calls")
        if mcp_init_report:
            checks.append(("toolbox_mcp_connected = 1", connected == 1, str(connected)))
        checks.append(("toolbox_mcp_calls counted", calls is not None, str(calls)))
    return checks


def run_scenario(
    credential: CredentialRecord,
    db_config: DatabaseConfig,
    consumer_config: ConsumerConfig,
    logger: logging.Logger,
    *,
    keep: bool = False,
    model: str | None = None,
    agent_view_id: int | None = None,
    harness: str | None = None,
) -> bool:
    """Run one e2e scenario for the given credential. True if all checks pass."""
    description = f"{credential.scope} ({credential.label})"
    ref_id = f"E2E-{credential.scope.upper()}-{credential.id}"

    print(f"\n{'='*60}")
    print(f"E2E: {description}")
    print(f"{'='*60}")

    register_channel(TestChannel())

    job_id = _insert_test_job(db_config, ref_id, agent_view_id, harness=harness, model=model)
    print(f"  Inserted job id={job_id}, reference_id={ref_id}")

    consumer = Consumer(db_config, consumer_config, logger, credential_id=credential.id)
    job = consumer._try_dequeue(job_id)
    if job is None:
        # The job is parked, so no consumer polls it; only something that claims by
        # id can have taken it. It is left to that claimant (not deleted).
        print(f"  FAIL: job {job_id} was claimed by someone else — run again")
        return False
    print(f"  Dequeued job {job.id}, executing...")

    consumer._execute_job(job)

    row = _fetch_job(db_config, job_id)
    if row is None:
        print("  FAIL: job row not found after execution")
        return False

    registered = find_harness(row["agent_type"]) if row["agent_type"] else None
    checks = _run_checks(
        row, agent_view=agent_view_id is not None,
        mcp_init_report=bool(registered and registered.descriptor.capabilities.mcp_init_report),
    )
    all_ok = all(ok for _, ok, _ in checks)

    for label, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: {detail}")

    print(f"\n  Model:  {row['model']}")
    print(f"  Tokens: in={row['input_tokens']} out={row['output_tokens']}")
    output_preview = (row["output"] or "")[:120]
    print(f"  Output: {output_preview}")

    if keep:
        print(f"  Keeping job {job_id} (--keep)")
    else:
        _delete_job(db_config, job_id)
        print(f"  Cleaned up job {job_id}")

    return all_ok


def _pick_credential(
    conn, agent_view_id: int | None, credential_id: int | None, harness: str | None = None,
) -> CredentialRecord:
    """The credential the consumer will use for this job — from the pool of the job's
    EFFECTIVE harness/provider (the pinned ``harness``, else the agent_view's, else global
    config), resolved as the consumer resolves it at claim. Raises ``ValueError`` with an
    operator message when there is none.
    """
    from .agent_view_runtime import (
        InvalidJobRuntime,
        resolve_agent_view_runtime,
        resolve_job_runtime,
    )
    from .harness import resolve_provider

    try:
        runtime = resolve_job_runtime(
            resolve_agent_view_runtime(
                conn, agent_view_id, tolerate_pair_error=harness is not None,
            ),
            pinned_harness=harness,
            pinned_model=None,
        )
    except InvalidJobRuntime as exc:
        raise ValueError(str(exc)) from exc
    provider = resolve_provider(runtime.harness, runtime.provider)
    if not provider.credential_required:
        raise ValueError(
            f"{runtime.harness}/{runtime.provider} needs no credential — nothing to test."
        )
    scope = provider.credential_scope
    if credential_id:
        credential = get_credential(conn, credential_id)
        if credential is None:
            raise ValueError(f"Credential not found: id={credential_id}")
        if credential.scope != scope:
            raise ValueError(
                f"Credential id={credential_id} is in scope {credential.scope!r}, but the job "
                f"runs {runtime.harness}/{runtime.provider} (scope {scope!r})."
            )
        return credential
    credential = select_credential(conn, scope)
    if credential is None:
        raise ValueError(
            f"No healthy credential in scope {scope!r}. "
            f"Register one: bin/agento credential:register {scope} <label>"
        )
    return credential


def cmd_e2e(args) -> None:
    """CLI entry point for `agent e2e`."""
    from .bootstrap import bootstrap
    from .cli.runtime import _load_framework_config

    db_config, consumer_config, _ = _load_framework_config()
    conn = get_connection(db_config)
    try:
        bootstrap(db_conn=conn)
    finally:
        conn.close()

    logger = logging.getLogger("e2e")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    conn = get_connection(db_config)
    agent_view_id = None
    try:
        if getattr(args, "agent_view", None):
            from .workspace import get_agent_view_by_code

            view = get_agent_view_by_code(conn, args.agent_view)
            if view is None:
                print(f"Agent view not found: {args.agent_view}", file=sys.stderr)
                sys.exit(1)
            agent_view_id = view.id
        try:
            credential = _pick_credential(
                conn, agent_view_id, args.credential_id, args.harness,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    try:
        ok = run_scenario(
            credential, db_config, consumer_config, logger,
            keep=args.keep, model=args.model, agent_view_id=agent_view_id,
            harness=args.harness,
        )
    except Exception as exc:
        print(f"  ERROR: {exc}")
        logger.exception(f"E2E failed for credential {credential.id}")
        ok = False

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {credential.scope} ({credential.label})")

    print(f"\n{'ALL PASSED' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)
