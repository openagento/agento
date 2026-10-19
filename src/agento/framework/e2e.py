"""End-to-end tests using real LLM calls and the prod database.

Exercises the full consumer pipeline: dequeue → channel → workflow → runner → finalize.
The selected credential drives the run; for deterministic targeting the scenario
temporarily disables every other credential in the same scope so the LRU pool
has only one candidate, then restores their enabled state at teardown.
Requires healthy tokens and DISABLE_LLM=0.
"""
from __future__ import annotations

import logging
import sys
import time

from .agent_manager.credential_store import get_credential, list_credentials, select_credential
from .agent_manager.models import CredentialRecord
from .channels.registry import register_channel
from .channels.test import TestChannel
from .consumer import Consumer
from .consumer_config import ConsumerConfig
from .database_config import DatabaseConfig
from .db import get_connection
from .harness import find_harness


def _insert_test_job(
    db_config: DatabaseConfig, reference_id: str, agent_view_id: int | None = None,
    *, harness: str | None = None, model: str | None = None,
) -> int:
    """Insert a TODO job with type/source='blank' and return its id.

    With ``agent_view_id`` the job runs as that agent_view: its harness, its workspace
    build and its toolbox. ``harness`` / ``model`` PIN the job's runtime (see
    docs/cli/job-runtime.md), so a test needs no ``config:set`` on the view.

    ``scheduled_after`` is far in the future so a live stack's consumer never claims the
    row; ``agento e2e`` claims it by id, which ignores the schedule.
    """
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job (type, source, reference_id, agent_view_id,
                                  idempotency_key, status, attempt, max_attempts,
                                  agent_type, model, scheduled_after)
                VALUES ('blank', 'blank', %s, %s, %s, 'TODO', 0, 1, %s, %s,
                        '2038-01-01 00:00:00')
                """,
                (reference_id, agent_view_id, f"e2e:{reference_id}:{int(time.time())}",
                 harness, model),
            )
            job_id = cur.lastrowid
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
    """Delete a test job row."""
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job WHERE id = %s", (job_id,))
        conn.commit()
    finally:
        conn.close()


def _disable_other_credentials(
    db_config: DatabaseConfig, credential: CredentialRecord
) -> list[int]:
    """Disable every other enabled credential in the same scope; return their ids."""
    conn = get_connection(db_config)
    try:
        peers = [
            c.id for c in list_credentials(conn, scope=credential.scope)
            if c.id != credential.id
        ]
        if peers:
            placeholders = ",".join(["%s"] * len(peers))
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE credential SET enabled = FALSE WHERE id IN ({placeholders})",
                    peers,
                )
            conn.commit()
        return peers
    finally:
        conn.close()


def _restore_credentials(db_config: DatabaseConfig, credential_ids: list[int]) -> None:
    """Re-enable credentials previously disabled by ``_disable_other_credentials``."""
    if not credential_ids:
        return
    conn = get_connection(db_config)
    try:
        placeholders = ",".join(["%s"] * len(credential_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE credential SET enabled = TRUE WHERE id IN ({placeholders})",
                credential_ids,
            )
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
    disabled_peers = _disable_other_credentials(db_config, credential)

    try:
        job_id = _insert_test_job(db_config, ref_id, agent_view_id, harness=harness, model=model)
        print(f"  Inserted job id={job_id}, reference_id={ref_id}")

        consumer = Consumer(db_config, consumer_config, logger)
        job = consumer._try_dequeue(job_id)
        if job is None:
            # A live consumer polling at that instant can claim the job first;
            # it is then left to that consumer (not deleted) and the run reports FAIL.
            print(f"  FAIL: job {job_id} was claimed by another consumer — run again")
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
    finally:
        _restore_credentials(db_config, disabled_peers)


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
