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


def _insert_test_job(db_config: DatabaseConfig, reference_id: str) -> int:
    """Insert a TODO job with type/source='blank' and return its id."""
    conn = get_connection(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job (type, source, reference_id,
                                  idempotency_key, status, attempt, max_attempts)
                VALUES ('blank', 'blank', %s, %s, 'TODO', 0, 1)
                """,
                (reference_id, f"e2e:{reference_id}:{int(time.time())}"),
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


def _run_checks(row: dict) -> list[tuple[str, bool, str]]:
    """Return list of (label, passed, detail) for a finished job row."""
    return [
        ("status=SUCCESS", row["status"] == "SUCCESS", row["status"]),
        ("agent_type set", row["agent_type"] is not None, str(row["agent_type"])),
        ("model set", row["model"] is not None, str(row["model"])),
        ("input_tokens > 0", (row["input_tokens"] or 0) > 0, str(row["input_tokens"])),
        ("prompt saved", bool(row["prompt"]), f"{len(row['prompt'] or '')} chars"),
        ("output saved", bool(row["output"]), f"{len(row['output'] or '')} chars"),
        ("result_summary has stats", "session_id=" in (row["result_summary"] or ""), str(row["result_summary"])),
    ]


def run_scenario(
    credential: CredentialRecord,
    db_config: DatabaseConfig,
    consumer_config: ConsumerConfig,
    logger: logging.Logger,
    *,
    keep: bool = False,
    model: str | None = None,
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
        job_id = _insert_test_job(db_config, ref_id)
        print(f"  Inserted job id={job_id}, reference_id={ref_id}")

        consumer = Consumer(db_config, consumer_config, logger, model_override=model)
        job = consumer._try_dequeue()
        if job is None:
            print("  FAIL: could not dequeue test job")
            if not keep:
                _delete_job(db_config, job_id)
            return False

        assert job.id == job_id, f"Expected job {job_id}, got {job.id}"
        print(f"  Dequeued job {job.id}, executing...")

        consumer._execute_job(job)

        row = _fetch_job(db_config, job_id)
        if row is None:
            print("  FAIL: job row not found after execution")
            return False

        checks = _run_checks(row)
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
    try:
        if args.credential_id:
            credential = get_credential(conn, args.credential_id)
            if credential is None:
                print(f"Credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        else:
            from .harness import list_credential_scopes

            credential = None
            for scope in list_credential_scopes():
                candidate = select_credential(conn, scope)
                if candidate is not None:
                    credential = candidate
                    break
            if credential is None:
                print(
                    "No healthy credentials across any scope. "
                    "Register one: bin/agento credential:register <scope> <label>",
                    file=sys.stderr,
                )
                sys.exit(1)
    finally:
        conn.close()

    try:
        ok = run_scenario(credential, db_config, consumer_config, logger, keep=args.keep, model=args.model)
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
