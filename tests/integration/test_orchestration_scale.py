"""Orchestration benchmark: N jobs through the real `Consumer.run()` (ACC2, RULES.md SCL-1).

Real MySQL, consumer loop, credential pool and a child per run: the stub `claude` in
tests/fixtures/bin answers with the access token it found in its own HOME. One JSON line
per N goes to `logs/bench/<sha>.json` BEFORE the asserts, so a red run still leaves its
measurement. "Spawn" = the worker saved the pid (`Consumer._save_pid`). The asserts are
ratios and counters only, never wall-clock bounds.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pymysql.connections import Connection

from agento.framework import bootstrap
from agento.framework.agent_manager.models import encrypt_credentials
from agento.framework.consumer import Consumer
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.runner import client as runner_client

from .conftest import CONCURRENT_WORKERS_STRESS_TEST, FIXTURES_DIR, _test_connection, fetch_all_jobs

N_VALUES = (CONCURRENT_WORKERS_STRESS_TEST, 2 * CONCURRENT_WORKERS_STRESS_TEST)
CONNECTION_HEADROOM = 30     # fixtures + other clients, as in test_token_selection_concurrency
MAX_USED_CONNECTIONS = 600   # the ceiling the compose template is sized for
RAMP_GROWTH_MAX = 2.2        # ramp@200 / ramp@100
CONNECTS_PER_JOB_MAX = 2.0   # WS1 measured 1.1 with the pool (base: 13)
DEADLINE_S = 300.0
REPO = Path(__file__).resolve().parents[2]
_RAMPS: dict[int, float] = {}
# The runner in its own process, as in a deployment, so its CPU/RSS/threads are its own.
# On SIGTERM it prints them; its children are the CLIs it spawned.
RUNNER = """
import json, os, resource, signal, socket, sys, threading, time
from unittest.mock import patch
from agento.framework.bootstrap import bootstrap
from agento.framework.runner.server import Server
with patch("agento.framework.module_status.read_module_status", return_value={}):
    bootstrap(db_conn=None, quiet=True)
peak = [0]
def report(*_):
    me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({"cpu_s": me.ru_utime + me.ru_stime, "maxrss": me.ru_maxrss, "threads": peak[0],
                      "child_cpu_s": kids.ru_utime + kids.ru_stime, "child_maxrss": kids.ru_maxrss}), flush=True)
    os._exit(0)
signal.signal(signal.SIGTERM, report)
listener = socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[1])
listener.listen(256)
threading.Thread(target=Server(listener, peer=lambda conn: 0).serve_forever, daemon=True).start()
while True:
    peak[0] = max(peak[0], threading.active_count())
    time.sleep(0.05)
"""


def _seed(n: int, agent_view_id: int) -> int:
    """2N claude oauth credentials (odd ones refresh-imminent; each workspace build also
    claims one) and N + 1 TODO jobs (one warm-up). Returns the last execution id."""
    now_ms = int(time.time() * 1000)
    credentials = [
        (f"sk-oauth-{i}", encrypt_credentials({
            "subscription_key": f"sk-oauth-{i}",
            "refresh_token": f"refresh-{i}",
            "raw_auth": {"credentials": {"claudeAiOauth": {
                "accessToken": f"sk-oauth-{i}", "refreshToken": f"refresh-{i}",
                "expiresAt": now_ms + (60_000 if i % 2 else 86_400_000)}}},
        }))
        for i in range(2 * n)
    ]
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            for path, value in (("agent_view/harness", "claude"), ("agent_view/provider", "anthropic")):
                cur.execute(
                    "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
                    "VALUES ('default', 0, %s, %s, 0) "
                    "ON DUPLICATE KEY UPDATE value = VALUES(value)", (path, value))
            cur.executemany(
                "INSERT INTO credential (scope, agent_type, type, label, credentials, enabled, status) "
                "VALUES ('claude', 'claude', 'oauth', %s, %s, TRUE, 'ok')", credentials)
            cur.executemany(
                "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, "
                "status, attempt, max_attempts) VALUES ('cron', 'jira', %s, %s, %s, 'TODO', 0, 3)",
                [(agent_view_id, f"AI-{i}", f"bench:{i}") for i in range(n + 1)])
            cur.execute("SELECT COALESCE(MAX(id), 0) AS id FROM execution")
            return cur.fetchone()["id"]
    finally:
        conn.close()


def _status(cur, name: str) -> int:
    cur.execute("SHOW GLOBAL STATUS LIKE %s", (name,))
    return int(cur.fetchone()["Value"])


def _mib(maxrss: int) -> float:
    return round(maxrss / (2**20 if sys.platform == "darwin" else 2**10), 1)


def _pct(values: list[float], p: float) -> float | None:
    return round(values[int(p * (len(values) - 1))], 4) if values else None


@pytest.mark.e2e
@pytest.mark.parametrize("n", N_VALUES)
def test_orchestration_scale(n, int_agent_view, int_db_config, tmp_path, monkeypatch):
    probe = _test_connection(autocommit=True)
    with probe.cursor() as cur:
        cur.execute("SHOW VARIABLES LIKE 'max_connections'")
        if int(cur.fetchone()["Value"]) < n + CONNECTION_HEADROOM:
            probe.close()
            pytest.skip(f"MySQL max_connections below {n + CONNECTION_HEADROOM}")

    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 8192), hard))  # macOS soft is 256
    monkeypatch.setenv("PATH", f"{FIXTURES_DIR / 'bin'}{os.pathsep}{os.environ['PATH']}")
    last_execution = _seed(n, int_agent_view)
    sock_dir = Path(tempfile.mkdtemp(prefix="rb", dir="/tmp"))  # AF_UNIX paths are short
    monkeypatch.setenv("AGENTO_RUNNER_SOCKET_DIR", str(sock_dir))
    sock = Path(runner_client.socket_path("runner-1.sock"))  # where the consumer connects
    sock.parent.mkdir()
    runner = subprocess.Popen([sys.executable, "-c", RUNNER, str(sock)],
                              stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not sock.exists():
        assert runner.poll() is None and time.monotonic() < deadline, "runner did not start"
        time.sleep(0.05)

    claimed: dict[int, float] = {}
    spawned: dict[int, float] = {}
    connects, commits = itertools.count(), itertools.count()
    real_dequeue, real_save_pid = Consumer._try_dequeue, Consumer._save_pid

    def timed_dequeue(self):
        job = real_dequeue(self)
        if job is not None:
            claimed[job.id] = time.monotonic()
        return job

    def timed_save_pid(self, job_id, pid, runner_ref=None):
        real_save_pid(self, job_id, pid, runner_ref)
        spawned.setdefault(job_id, time.monotonic())

    def counted(real, counter):
        return lambda *a: (next(counter), real(*a))[1]

    consumer = Consumer(int_db_config, ConsumerConfig(max_workers=n), logging.getLogger("bench"))
    # Warm-up: one job alone builds the view's workspace. Cold builds in parallel race on the
    # build's `current` link (FileNotFoundError -> DEAD): the builder's bug, not what this measures.
    consumer._execute_job(consumer._try_dequeue())
    peak_threads, peak_runs, finished = [0], [0], [0.0]

    def watch() -> None:
        deadline = time.monotonic() + DEADLINE_S
        while not consumer._shutdown.is_set():
            peak_threads[0] = max(peak_threads[0], threading.active_count())
            with probe.cursor() as cur:
                cur.execute("SELECT SUM(status NOT IN ('TODO', 'RUNNING')) AS done, "
                            "SUM(status = 'RUNNING' AND pid IS NOT NULL) AS running FROM job")
                row = cur.fetchone()
                peak_runs[0] = max(peak_runs[0], int(row["running"]))
                if int(row["done"]) == n + 1 or time.monotonic() > deadline:
                    finished[0] = time.monotonic()
                    consumer._shutdown.set()
            time.sleep(0.05)

    with probe.cursor() as cur:
        cur.execute("FLUSH STATUS")
        refused_before = _status(cur, "Connection_errors_max_connections")
    handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    self0 = resource.getrusage(resource.RUSAGE_SELF)
    watcher = threading.Thread(target=watch, daemon=True)
    start = time.monotonic()
    try:
        with patch.object(Consumer, "_try_dequeue", timed_dequeue), \
             patch.object(Consumer, "_save_pid", timed_save_pid), \
             patch.object(Consumer, "_maybe_reload_bootstrap", lambda self: None), \
             patch.object(Connection, "connect", counted(Connection.connect, connects)), \
             patch.object(Connection, "commit", counted(Connection.commit, commits)):
            watcher.start()
            consumer.run()  # main thread: run() installs signal handlers
    finally:
        signal.signal(signal.SIGTERM, handlers[0])
        signal.signal(signal.SIGINT, handlers[1])
        bootstrap._sync_delta_worker()  # run() stopped the delta writer; later tests need it
        runner.send_signal(signal.SIGTERM)
        runner_usage = json.loads(runner.communicate(timeout=30)[0])
        shutil.rmtree(sock_dir, ignore_errors=True)
    self1 = resource.getrusage(resource.RUSAGE_SELF)
    with probe.cursor() as cur:
        max_used = _status(cur, "Max_used_connections")
        refused = _status(cur, "Connection_errors_max_connections") - refused_before
        cur.execute(
            "SELECT e.job_id, c.label FROM execution e JOIN credential c ON c.id = e.credential_id "
            "WHERE e.id > %s", (last_execution,))
        resolved = {r["job_id"]: r["label"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM workspace_build")
        builds = cur.fetchone()["n"]
    probe.close()

    waits = sorted(spawned[j] - claimed[j] for j in claimed if j in spawned)
    metrics = {
        "n": n,
        "claim_to_spawn_s": {"p50": _pct(waits, 0.5), "p95": _pct(waits, 0.95), "max": _pct(waits, 1)},
        "ramp_s": round(max(spawned.values()) - min(claimed.values()), 3) if spawned else None,
        "wall_s": round(finished[0] - start, 3),
        "peak_concurrent_runs": peak_runs[0],
        "worker_cpu_s_per_job": round((self1.ru_utime + self1.ru_stime - self0.ru_utime - self0.ru_stime) / n, 4),
        "worker_threads_per_job": round(peak_threads[0] / n, 3),
        "worker_rss_mib": _mib(self1.ru_maxrss),
        "runner_cpu_s_per_job": round(runner_usage["cpu_s"] / n, 4),
        "runner_threads_per_job": round(runner_usage["threads"] / n, 3),
        "runner_rss_mib": _mib(runner_usage["maxrss"]),
        "child_cpu_s_per_job": round(runner_usage["child_cpu_s"] / n, 4),
        "child_rss_mib": _mib(runner_usage["child_maxrss"]),
        "connects_per_job": round(next(connects) / n, 2),
        "commits_per_job": round(next(commits) / n, 2),
        "max_used_connections": max_used,
        "too_many_connections": refused,
        "workspace_builds": builds,
    }
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip() or "unknown"
    out = REPO / "logs" / "bench" / f"{sha}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(metrics) + "\n")

    jobs = fetch_all_jobs()
    assert [j["status"] for j in jobs] == ["SUCCESS"] * (n + 1), metrics
    assert refused == 0 and max_used < MAX_USED_CONNECTIONS, metrics
    assert metrics["connects_per_job"] <= CONNECTS_PER_JOB_MAX, metrics
    # Per-run isolation: each run's CLI saw the credential the consumer resolved, and no
    # two runs shared one.
    for job in jobs:
        run_dir = tmp_path / "artifacts" / "dev" / "developer" / str(job["id"])
        on_disk = json.loads((run_dir / ".claude" / ".credentials.json").read_text())
        assert job["output"] == resolved[job["id"]] == on_disk["claudeAiOauth"]["accessToken"]
    assert len({j["output"] for j in jobs}) == n + 1

    _RAMPS[n] = metrics["ramp_s"]
    if n == N_VALUES[1] and N_VALUES[0] in _RAMPS:
        assert _RAMPS[n] <= RAMP_GROWTH_MAX * _RAMPS[N_VALUES[0]], metrics
