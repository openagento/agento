"""Integration: 10 jobs run concurrently through the consumer (max_workers=10),
each resolving a token from the LRU pool and materializing its credentials into
its own per-run artifacts dir. Asserts that concurrency never lets one job's
credentials leak into — or get clobbered by — another job's run directory.

Real MySQL + mocked runner: the agent never actually runs, but every job goes
through the real ``materialize_run_workspace`` + ``ClaudeWorkspaceAdapter`` path
that writes ``.claude/.credentials.json`` before the runner is invoked.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agento.framework.agent_manager.models import encrypt_credentials
from agento.framework.consumer import Consumer
from agento.framework.consumer_config import ConsumerConfig
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import CONCURRENT_WORKERS_STRESS_TEST, _test_connection, fetch_job

WS_CODE = "acme"
AV_CODE = "developer"
N_JOBS = CONCURRENT_WORKERS_STRESS_TEST


def _insert_workspace(code: str = WS_CODE) -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES (%s, %s)", (code, code.title()))
            return cur.lastrowid
    finally:
        conn.close()


def _insert_agent_view(workspace_id: int, code: str = AV_CODE) -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (workspace_id, code, code.title()),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _bind_provider(agent_type: str = "claude") -> None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
                   VALUES ('default', 0, 'agent_view/provider', %s, 0)
                   ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = NOW()""",
                (agent_type,),
            )
    finally:
        conn.close()


def _seed_credential(label: str, access_token: str, email: str) -> int:
    """Seed an enabled, healthy claude oauth token. Credentials mirror a real
    oauth payload so ClaudeWorkspaceAdapter writes ``.claude/.credentials.json``
    with ``claudeAiOauth.accessToken == access_token``."""
    credentials = encrypt_credentials({
        "subscription_key": access_token,
        "refresh_token": f"refresh-{label}",
        "raw_auth": {
            "credentials": {
                "claudeAiOauth": {"accessToken": access_token, "refreshToken": f"refresh-{label}"},
            },
            "claude_json": {
                "oauthAccount": {"emailAddress": email},
                "userID": f"user-{label}",
            },
        },
    })
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO credential
                       (scope, agent_type, type, label, credentials, enabled, status, priority)
                   VALUES ('claude', 'claude', 'oauth', %s, %s, TRUE, 'ok', 0)""",
                (label, credentials),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _insert_job(agent_view_id: int, ref: str) -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job (type, source, agent_view_id, reference_id,
                                    idempotency_key, status, attempt, max_attempts)
                   VALUES ('cron', 'jira', %s, %s, %s, 'TODO', 0, 3)""",
                (agent_view_id, ref, f"concmat:{ref}"),
            )
            return cur.lastrowid
    finally:
        conn.close()


def test_concurrent_jobs_materialize_isolated_credentials_per_run_dir(
    int_db_config, tmp_path,
):
    logger = logging.getLogger("test")
    _bind_provider("claude")
    ws_id = _insert_workspace()
    av_id = _insert_agent_view(ws_id)

    for i in range(N_JOBS):
        _seed_credential(f"tok-{i}", f"sk-oauth-{i}", f"user{i}@example.com")

    # Capture, per run directory, the credential the consumer handed the runner.
    # ``working_dir`` is the per-run artifacts dir where creds were materialized.
    lock = threading.Lock()
    seen: dict[str, str] = {}

    def capturing_run(self_runner, request):
        with lock:
            seen[self_runner.context.working_dir] = self_runner.context.credential.credentials["subscription_key"]
        return ClaudeResult(
            raw_output="ok", input_tokens=100, output_tokens=50,
            duration_ms=1000, session_id="success", harness="claude",
        )

    cfg = ConsumerConfig(max_workers=N_JOBS)

    with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", capturing_run), \
         patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)), \
         patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)), \
         patch("agento.modules.agent_view.src.observers.DatabaseConfig.from_env", return_value=int_db_config), \
         patch("agento.modules.workspace_build.src.observers.DatabaseConfig.from_env", return_value=int_db_config), \
         patch("agento.modules.app_monitor.src.observers.DatabaseConfig.from_env", return_value=int_db_config):
        consumer = Consumer(int_db_config, cfg, logger)

        # Warm-up: one job sequentially so the shared workspace build exists
        # before the concurrent batch. This keeps the test focused on per-run
        # credential isolation, not the (separate) workspace builder's locking.
        warmup_id = _insert_job(av_id, "AI-WARMUP")
        warm = consumer._try_dequeue()
        assert warm is not None and warm.id == warmup_id
        consumer._execute_job(warm)
        assert fetch_job(warmup_id)["status"] == "SUCCESS"

        # Concurrent batch: claim all jobs, then execute them at max_workers width.
        job_ids = [_insert_job(av_id, f"AI-{i}") for i in range(N_JOBS)]
        jobs = [consumer._try_dequeue() for _ in range(N_JOBS)]
        assert all(j is not None for j in jobs)
        assert {j.id for j in jobs} == set(job_ids)

        with ThreadPoolExecutor(max_workers=N_JOBS) as pool:
            list(pool.map(consumer._execute_job, jobs))

    # Every concurrent job succeeded.
    for jid in job_ids:
        assert fetch_job(jid)["status"] == "SUCCESS", f"job {jid} did not succeed"

    # Each job's own run dir holds exactly the credential the consumer resolved
    # for that job — proving concurrency neither mixed nor clobbered files.
    run_dirs = []
    for jid in job_ids:
        run_dir = tmp_path / WS_CODE / AV_CODE / str(jid)
        run_dirs.append(run_dir)
        creds_path = run_dir / ".claude" / ".credentials.json"
        assert creds_path.exists(), f"no credentials materialized in {run_dir}"
        access_token = json.loads(creds_path.read_text())["claudeAiOauth"]["accessToken"]
        expected = seen[str(run_dir)]
        assert access_token == expected, (
            f"job dir {run_dir} holds {access_token!r}, expected {expected!r} "
            f"(credential leak/clobber under concurrency)"
        )
        assert access_token.startswith("sk-oauth-")

    # All run directories are distinct (per-run isolation by construction).
    assert len({str(d) for d in run_dirs}) == N_JOBS

    # And concurrency handed each job a distinct token from the pool — proving
    # the run was genuinely parallel and no two jobs raced onto the same token.
    assert len({seen[str(d)] for d in run_dirs}) == N_JOBS
