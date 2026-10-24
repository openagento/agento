"""Integration test fixtures — real MySQL, mocked HTTP & Claude."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pymysql
import pytest
from pymysql.cursors import DictCursor

from agento.framework.bootstrap import bootstrap, set_module_config
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.database_config import DatabaseConfig
from agento.framework.migrate import migrate
from agento.modules.claude.src.output_parser import ClaudeResult
from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira_periodic_tasks.src.config import PeriodicTasksConfig
from tests.registry_isolation import is_bootstrapped, mark_bootstrapped

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# One database per pytest-xdist worker (gw0, gw1, ...): workers run in parallel and every
# test truncates the tables, so a shared database would let one worker wipe another's rows.
TEST_DB = "cron_agent_test" + (
    f"_{worker}" if (worker := os.environ.get("PYTEST_XDIST_WORKER")) else ""
)

# Concurrent-claimant fan-out shared by the token-pool / selection / materialization
# stress tests — the general "does the system stay correct under a normal burst" load.
# The high-contention token test deliberately runs hotter (see _HIGH_CONTENTION_WORKERS
# in test_token_selection_concurrency.py, sized to the MySQL connection ceiling to force
# saturation); it does not share this knob on purpose.
CONCURRENT_WORKERS_STRESS_TEST = 100

# Stress tests hold 100-200 MySQL connections each. Under pytest-xdist they share ONE group,
# so they run one after another on a single worker and never stack past max_connections
# (`--dist loadgroup` in pyproject.toml). Mark such a module `pytestmark = DB_STRESS`.
DB_STRESS = pytest.mark.xdist_group("db_stress")

# MySQL `GET_LOCK` names are server-wide, not per database, so the per-worker databases do
# not keep them apart: two workers taking the artifact retention lock (`va_ret:<sha1(code)>`)
# for the same code block each other. Mark such a module `pytestmark = NAMED_LOCKS`.
NAMED_LOCKS = pytest.mark.xdist_group("named_locks")

# Default to this checkout's own MySQL (docker/.env MYSQL_PORT), never a sibling's on 3306.
# Set in os.environ so spawned node helpers (verify_capability.mjs) inherit it.
_env_file = Path(__file__).resolve().parents[2] / "docker" / ".env"
_ports = [
    line.split("=", 1)[1].strip()
    for line in (_env_file.read_text().splitlines() if _env_file.is_file() else [])
    if line.startswith("MYSQL_PORT=")
]
os.environ.setdefault("TEST_MYSQL_PORT", _ports[-1] if _ports else "3306")
os.environ["TEST_MYSQL_DATABASE"] = TEST_DB  # this worker's own database, for node helpers

# Ensure encryption key is available for tests (used for credential.credentials and obscure configs)
os.environ.setdefault("AGENTO_ENCRYPTION_KEY", "test-encryption-key-for-integration")


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _root_connection() -> pymysql.Connection:
    """Connect as root (no database selected) to create/drop test DB."""
    return pymysql.connect(
        host=os.environ.get("TEST_MYSQL_HOST", "localhost"),
        port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
        user=os.environ.get("TEST_MYSQL_USER", "root"),
        password=os.environ.get("TEST_MYSQL_PASSWORD", "cronagent_root"),
        charset="utf8mb4",
        autocommit=True,
    )


def _db_connection(database: str, autocommit: bool = False) -> pymysql.Connection:
    """Connect to ``database`` on the SAME server as every other test connection.

    Every connector in the integration suite goes through this (or `_root_connection`):
    a hardcoded host/port reaches a SIBLING checkout's MySQL, where this run's databases
    do not exist.
    """
    return pymysql.connect(
        host=os.environ.get("TEST_MYSQL_HOST", "localhost"),
        port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
        user=os.environ.get("TEST_MYSQL_USER", "root"),
        password=os.environ.get("TEST_MYSQL_PASSWORD", "cronagent_root"),
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=autocommit,
    )


def _test_connection(autocommit: bool = False) -> pymysql.Connection:
    """Connect to the test database."""
    return _db_connection(TEST_DB, autocommit=autocommit)


# ---------------------------------------------------------------------------
# Session-scoped: create/destroy test database
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _create_test_db():
    """Create test database + apply all migrations once per session, drop on teardown."""
    conn = _root_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            cur.execute(
                f"CREATE DATABASE {TEST_DB} "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()

    # Apply framework migrations
    conn = _test_connection(autocommit=False)
    try:
        migrate(conn)
    finally:
        conn.close()

    # Apply module SQL migrations (same as setup:upgrade does)
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.dependency_resolver import resolve_order
    from agento.framework.module_loader import scan_modules
    # Dependency order, as setup:upgrade: a module's FK may name a table of its `sequence`.
    all_modules = resolve_order(scan_modules(CORE_MODULES_DIR))
    conn = _test_connection(autocommit=False)
    try:
        for m in all_modules:
            sql_dir = m.path / "sql"
            if sql_dir.is_dir():
                migrate(conn, module=m.name, sql_dir=sql_dir)
    finally:
        conn.close()

    yield

    conn = _root_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Function-scoped: config, connection, table cleanup
# ---------------------------------------------------------------------------

@pytest.fixture
def int_db_config() -> DatabaseConfig:
    """DatabaseConfig pointing to the test MySQL database."""
    return DatabaseConfig(
        mysql_host=os.environ.get("TEST_MYSQL_HOST", "localhost"),
        mysql_port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
        mysql_database=TEST_DB,
        mysql_user=os.environ.get("TEST_MYSQL_USER", "root"),
        mysql_password=os.environ.get("TEST_MYSQL_PASSWORD", "cronagent_root"),
    )


@pytest.fixture
def int_consumer_config() -> ConsumerConfig:
    """ConsumerConfig for integration tests."""
    return ConsumerConfig()


@pytest.fixture
def int_config() -> JiraConfig:
    """JiraConfig for integration tests (backward-compat fixture name)."""
    return JiraConfig(
        toolbox_url="http://toolbox:3001",
        user="agenty@example.com",
        jira_projects=["AI"],
        jira_assignee="agenty@example.com",
    )


@pytest.fixture
def int_periodic_config() -> PeriodicTasksConfig:
    """PeriodicTasksConfig for integration tests."""
    return PeriodicTasksConfig(
        jira_status="Cykliczne",
        jira_frequency_field="customfield_10709",
        frequency_map={
            "Co 5min": "*/5 * * * *",
            "Co 30min": "*/30 * * * *",
            "Co 1h": "0 * * * *",
            "Co 4h": "0 */4 * * *",
            "1x dziennie o 8:00": "0 8 * * *",
            "1x dziennie o 1:00 w nocy": "0 1 * * *",
            "2x dziennie o 6:00 i 18:00": "0 6,18 * * *",
            "1x w tygodniu (Pon, 7:00)": "0 7 * * 1",
        },
    )


def bootstrap_for_tests() -> None:
    """Populate the registries the integration suite needs (see below).

    A test that must exercise a real `bootstrap()` - a module reaching a registry
    through its own manifest - calls THIS, not `bootstrap()`: a bare `bootstrap()`
    resets the module-config registry this fills, and the next Jira test then runs
    without its config.

    Stubs ``read_module_status`` so bootstrap ignores the developer's
    ``app/etc/modules.json``. Without this, locally-disabled modules
    (e.g. ``jira_periodic_tasks: false``) would drop their workflow /
    runtime / observer registrations and silently fail integration tests.
    The file is gitignored, so behavior would diverge between developer
    machines and CI.
    """
    with patch(
        "agento.framework.module_status.read_module_status", return_value={},
    ):
        bootstrap()
    # Override module configs with integration test values
    set_module_config("jira", JiraConfig(
        toolbox_url="http://toolbox:3001",
        user="agenty@example.com",
        jira_projects=["AI"],
        jira_assignee="agenty@example.com",
    ))
    # app_monitor: the observer is now telemetry-only — it never sets a verdict,
    # so fake-runner jobs (no real transcript JSONL) are no longer at risk of
    # dead-lettering. Keep the MCP-issue alert flag explicitly off so tests don't
    # depend on a global SMTP send; dedicated coverage lives in
    # tests/unit/modules/app_monitor/ + tests/integration/test_app_monitor_wiring.py.
    from agento.modules.app_monitor.src.constants import CFG_SEND_ALERT_ON_MCP_ISSUES
    set_module_config("app_monitor", {CFG_SEND_ALERT_ON_MCP_ISSUES: False})
    set_module_config("jira_periodic_tasks", PeriodicTasksConfig(
        jira_status="Cykliczne",
        jira_frequency_field="customfield_10709",
        frequency_map={
            "Co 5min": "*/5 * * * *",
            "Co 30min": "*/30 * * * *",
            "Co 1h": "0 * * * *",
            "Co 4h": "0 */4 * * *",
            "1x dziennie o 8:00": "0 8 * * *",
            "1x dziennie o 1:00 w nocy": "0 1 * * *",
            "2x dziennie o 6:00 i 18:00": "0 6,18 * * *",
            "1x w tygodniu (Pon, 7:00)": "0 7 * * 1",
        },
    ))
    mark_bootstrapped()


@pytest.fixture(autouse=True)
def _framework_registries():
    """Populate registries from core modules.

    Overrides the fixture of the same name in tests/conftest.py, which clears them again
    for unit tests. Bootstraps only when needed: once per worker, and again after a unit
    test ran on this worker (pytest-xdist mixes both in one process).
    """
    if not is_bootstrapped():
        bootstrap_for_tests()


@pytest.fixture(autouse=True)
def _thread_group_of_this_host():
    """The thread dirs get group THREAD_GID (artifacts_dir.prepare_thread_dir). A host that
    is not in that group (a developer's machine) gives them its own group instead; root and
    the image's `agent` keep the real one."""
    from agento.framework.runner import uids
    if os.geteuid() == 0 or uids.THREAD_GID in os.getgroups():
        yield
        return
    with patch.object(uids, "THREAD_GID", os.getgid()):
        yield


@pytest.fixture(autouse=True)
def _runner(start_runner):
    """Every agent CLI starts in a runner (WS5): an in-process one per test, so a consumer
    run here goes through the socket, as in a deployment."""
    start_runner()


@pytest.fixture(autouse=True)
def _keep_bootstrapped_registries():
    """`bootstrap_for_tests()` runs ONCE per session, so a test that clears a registry to
    get a clean slate leaves every later test with an empty one. The observed bite: an empty
    event manager means no workspace-build observer, which means a job run resolves its state
    dir under the container's read-only `/workspace`. Every registry bootstrap fills gets the
    same snapshot-and-restore, whatever the test did to it."""
    from agento.framework import event_manager, execution_hooks, job_types
    from agento.framework.workflows import _WORKFLOW_MAP

    manager = event_manager._EVENT_MANAGER
    seams = {name: (slot.get(), slot.module()) for name, slot in execution_hooks.SEAMS.items()}
    workflows = dict(_WORKFLOW_MAP)
    types = dict(job_types._REGISTRY)
    yield
    if event_manager._EVENT_MANAGER is not manager:
        event_manager._EVENT_MANAGER = manager
    for name, (impl, module) in seams.items():
        slot = execution_hooks.SEAMS[name]
        if slot.get() is not impl:
            slot.clear()
            if impl is not None:
                slot.register(impl, module=module)
    if workflows != _WORKFLOW_MAP:
        _WORKFLOW_MAP.clear()
        _WORKFLOW_MAP.update(workflows)
    if types != job_types._REGISTRY:
        job_types._REGISTRY.clear()
        job_types._REGISTRY.update(types)


@pytest.fixture(autouse=True)
def _db_from_env(int_db_config):
    """No integration test may reach the production database host.

    Several paths a job takes open their OWN connection from `DatabaseConfig.from_env()` -
    the workspace-build observer, the conversation workflow, `publish_job`. A test that did
    not patch it passed only when an earlier test's fixture happened to leak the patch, so
    the suite's result depended on file order. One autouse patch removes the whole class.
    A test that needs the real classmethod stops this patch itself.
    """
    with patch.object(DatabaseConfig, "from_env", return_value=int_db_config):
        yield


@pytest.fixture(autouse=True)
def _no_real_cli_refresh():
    """The out-of-band refresh (AG-65) spawns the real agent CLI. Integration tests run
    the consumer against seeded fake credentials, so stub it to "cannot refresh": the job
    keeps its lease, which is the pre-AG-65 path these tests were written for."""
    with patch(
        "agento.modules.claude.src.config.ClaudeWorkspaceAdapter.refresh_credentials",
        return_value=False,
    ), patch(
        "agento.modules.codex.src.config.CodexWorkspaceAdapter.refresh_credentials",
        return_value=False,
    ):
        yield


# Not here, on purpose: `workspace` / `agent_view` (tests reuse the scope rows by code),
# `ingress_identity` (`int_agent_view` owns it), `role` (seeded) and the migration tables.
_WIPED_TABLES = (
    "execution", "execution_delta", "job_event_outbox", "tool_invocation",
    "conversation", "conversation_event", "message", "conversation_prune_watermark",
    "launch", "session", "role_grant", "`user`", "credential_login", "limit_bucket",
    "outlook_poll_cursor", "miniapp", "miniapp_activation", "miniapp_member",
    "versioned_artifact", "versioned_artifact_audit", "thread",
)


@pytest.fixture(autouse=True)
def _truncate_tables():
    """Truncate all tables before each test for isolation."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("TRUNCATE TABLE toolbox_capability")
            cur.execute("TRUNCATE TABLE job")
            # Keyed by job id, which the TRUNCATE above recycles: a stretch a deferring test
            # left open is closed by the next test's claim of job 1, as a `job.deferred` row.
            cur.execute("TRUNCATE TABLE job_defer_stretch")
            # Every other per-test table, so no test file wipes its own subset. An outbox or
            # execution row a test left behind is read as the next test's job 1. DELETE, not
            # TRUNCATE: the tables are almost always empty, and a TRUNCATE costs ~7 ms each.
            for table in _WIPED_TABLES:
                cur.execute(f"DELETE FROM {table}")
            # The seeded roles stay; a test's own role goes.
            cur.execute("DELETE FROM role WHERE code NOT IN ('admin', 'user')")
            cur.execute("TRUNCATE TABLE schedule")
            cur.execute("TRUNCATE TABLE usage_log")
            cur.execute("TRUNCATE TABLE credential")
            cur.execute("TRUNCATE TABLE skill_registry")
            cur.execute("TRUNCATE TABLE workspace_build")
            cur.execute("TRUNCATE TABLE core_config_data")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

# Historical harness → model-vendor pairs, frozen here like the data patch: the
# fixture must not depend on which harness modules happen to be registered.
_HARNESS_PROVIDERS = {"claude": "anthropic", "codex": "openai"}


def insert_primary_token(harness: str = "claude") -> int:
    """Insert an enabled ``credential`` row and bind ``agent_view/harness`` +
    ``agent_view/provider`` at the default scope. Returns the credential id. The name is
    kept for backward-compat with older tests; there is no primary concept anymore —
    selection is LRU over healthy credentials within the scope's pool. The scoped-config
    bind is what tells the consumer which pool to draw from."""
    from agento.framework.agent_manager.models import encrypt_credentials

    encrypted = encrypt_credentials({"subscription_key": f"sk-test-{harness}"})
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO credential
                    (scope, agent_type, label, credentials, enabled, status)
                VALUES (%s, %s, %s, %s, TRUE, 'ok')
                """,
                (harness, harness, f"test-{harness}", encrypted),
            )
            credential_id = cur.lastrowid
            for path, value in (
                ("agent_view/harness", harness),
                ("agent_view/provider", _HARNESS_PROVIDERS[harness]),
            ):
                cur.execute(
                    """
                    INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
                    VALUES ('default', 0, %s, %s, 0)
                    ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = NOW()
                    """,
                    (path, value),
                )
            return credential_id
    finally:
        conn.close()


@pytest.fixture
def int_agent_view(tmp_path, int_db_config, monkeypatch):
    """An active workspace + agent_view, the jira ingress binding that routes jobs to it, and
    the build-path redirection a view-scoped run needs.

    Every toolbox REST call is scoped to a capability, and a capability needs a view — a
    discovery job with no agent_view has nothing to mint against and cannot reach the toolbox.
    Production reaches the same conclusion earlier: ``todo:publish`` refuses to run without a
    view. These flows therefore need one.
    """
    # A view-scoped job also runs the workspace-build freshness observer, which builds under
    # BUILD_DIR and opens its OWN connection from env. Same redirection the other build-touching
    # integration tests use (test_orchestration_scale, test_app_monitor_e2e).
    build_root = str(tmp_path / "build")
    artifacts_root = str(tmp_path / "artifacts")
    patches = [
        patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", artifacts_root),
        patch("agento.framework.artifacts_dir.BUILD_DIR", build_root),
        patch("agento.modules.workspace_build.src.builder.BUILD_DIR", build_root),
        # ONE patch, on the class itself. Both observers import the SAME DatabaseConfig, so
        # patching two module paths would patch one attribute twice — and stopping them in start
        # order then restores the first mock instead of the real classmethod, leaking a localhost
        # config into every later test that calls from_env().
        patch.object(DatabaseConfig, "from_env", return_value=int_db_config),
    ]
    for p_ in patches:
        p_.start()
    # A view-scoped job resolves its module config at the view's scope (ENV -> DB ->
    # config.json), not from the bootstrap registry `set_module_config` fills, so the
    # integration jira identity has to arrive through a source that resolver reads.
    monkeypatch.setenv("CONFIG__JIRA__USER", "agenty@example.com")
    monkeypatch.setenv("CONFIG__JIRA__JIRA_ASSIGNEE", "agenty@example.com")
    monkeypatch.setenv("CONFIG__JIRA__JIRA_PROJECTS", '["AI"]')

    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            # workspace / agent_view are NOT truncated between tests — reuse the rows.
            cur.execute("SELECT id FROM workspace WHERE code = %s", ("dev",))
            row = cur.fetchone()
            if row:
                workspace_id = row["id"]
            else:
                cur.execute("INSERT INTO workspace (code, label) VALUES (%s, %s)", ("dev", "dev"))
                workspace_id = cur.lastrowid

            cur.execute("SELECT id FROM agent_view WHERE code = %s", ("developer",))
            row = cur.fetchone()
            if row:
                agent_view_id = row["id"]
            else:
                cur.execute(
                    "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                    (workspace_id, "developer", "developer"),
                )
                agent_view_id = cur.lastrowid

            # Jira publishing routes through an ingress identity; with no binding
            # `_resolve_routing` returns (None, 50) and the job is published viewless.
            # ON DUPLICATE KEY UPDATE, not INSERT IGNORE: the row caches an agent_view id,
            # and a stale one points the router at a view that is gone - the publish then
            # fails the foreign key and the jira flows fail with no explanation.
            cur.execute(
                "INSERT INTO ingress_identity "
                "(identity_type, identity_value, agent_view_id) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE agent_view_id = VALUES(agent_view_id)",
                ("jira", "jira", agent_view_id),
            )
    finally:
        conn.close()

    try:
        yield agent_view_id
    finally:
        # Remove the routing before the BUILD_DIR patches stop. The binding is global: a
        # later test that publishes a jira job would otherwise resolve this view and run the
        # workspace-build observer against the REAL build root, which is read-only here.
        conn = _test_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ingress_identity WHERE identity_type = 'jira' "
                            "AND identity_value = 'jira'")
        finally:
            conn.close()
        for p_ in patches:
            p_.stop()


@pytest.fixture
def mock_claude():
    """Patch ClaudeSubprocessRunner.execute to return a successful result + insert primary token."""
    insert_primary_token("claude")
    result = ClaudeResult(
        raw_output="ok",
        input_tokens=1500,
        output_tokens=800,
        cost_usd=0.0123,
        num_turns=3,
        duration_ms=45000,
        session_id="success",
        harness="claude",
    )
    with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", return_value=result):
        yield result




@pytest.fixture
def mock_codex():
    """Patch CodexSubprocessRunner.execute to return a successful result + insert primary token."""
    insert_primary_token("codex")
    result = ClaudeResult(
        raw_output="Cześć! W czym mogę pomóc?",
        input_tokens=6374,
        output_tokens=None,
        num_turns=1,
        duration_ms=3200,
        session_id="019cbcfa-837a-7130-b776-15ac3d39b1ad",
        harness="codex",
        model="o3",
    )
    with patch(
        "agento.modules.codex.src.runner.CodexSubprocessRunner.execute",
        return_value=result,
    ):
        yield result


@pytest.fixture
def jira_todo_fixture() -> dict:
    return _load_fixture("jira_search_todo.json")


@pytest.fixture
def jira_cykliczne_fixture() -> dict:
    return _load_fixture("jira_search_cykliczne.json")


@pytest.fixture
def jira_empty_fixture() -> dict:
    return _load_fixture("jira_search_empty.json")


# ---------------------------------------------------------------------------
# Helpers — each read uses a fresh autocommit connection to avoid
# transaction isolation issues (consumer/publisher commit on separate conns).
# ---------------------------------------------------------------------------

def fetch_job(job_id: int) -> dict | None:
    """Fetch a job row by id (fresh connection, sees latest committed data)."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
            return cur.fetchone()
    finally:
        conn.close()


def fetch_all_jobs() -> list[dict]:
    """Fetch all job rows."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def fetch_all_schedules() -> list[dict]:
    """Fetch all schedule rows."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM schedule ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def insert_queued_job(
    *,
    job_type: str = "cron",
    reference_id: str = "AI-1",
    idempotency_key: str = "test:key:1",
    max_attempts: int = 3,
    source: str = "jira",
    context: str | None = None,
    agent_view_id: int | None = None,
) -> int:
    """Insert a TODO job and return its id."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job (type, source, agent_view_id, reference_id, context,
                                  idempotency_key, status, attempt, max_attempts)
                VALUES (%s, %s, %s, %s, %s, %s, 'TODO', 0, %s)
                """,
                (job_type, source, agent_view_id, reference_id, context,
                 idempotency_key, max_attempts),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_job(job_id: int, **fields) -> None:
    """Update arbitrary fields on a job row."""
    conn = _test_connection(autocommit=True)
    try:
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE job SET {set_clause} WHERE id = %s",
                (*fields.values(), job_id),
            )
    finally:
        conn.close()


# --- the conversation world (PRD E3-E5) ------------------------------------
#
# These live here, not in a test module, so the fourteen conversation test files can take
# them as arguments without importing them: an imported fixture is a module binding that
# every test's parameter then shadows, which is 382 ruff F811 errors for one pattern. A test
# file that defines its own `conn` still overrides this one.

def _clean(c):
    """The scopes: the autouse wipe keeps them, and a test that deactivates the view must not
    hand the next test a deactivated one (INSERT IGNORE would keep it)."""
    with c.cursor() as cur:
        cur.execute("DELETE FROM agent_view WHERE code IN ('c-av', 'c-av2')")
        cur.execute("DELETE FROM workspace WHERE code IN ('c-ws', 'c-ws2')")
    c.commit()


@pytest.fixture
def conn(int_db_config, monkeypatch):
    from agento.framework import publish_service
    from agento.modules.conversation.src import service

    # publish_job opens its own connection; point it at the test database.
    real = publish_service.publish_job
    monkeypatch.setattr(
        publish_service, "publish_job",
        lambda **kw: real(**{**kw, "config": int_db_config}),
    )
    monkeypatch.setattr(service, "publish_job", publish_service.publish_job)

    c = _test_connection(autocommit=False)
    _clean(c)
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """One workspace, one view, an owner who reaches it, and a stranger who does not."""
    from agento.framework.access import accounts

    ids = {}
    with conn.cursor() as cur:
        for ws, av, key in (("c-ws", "c-av", "view"), ("c-ws2", "c-av2", "other_view")):
            cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES (%s, %s)", (ws, ws))
            cur.execute("SELECT id FROM workspace WHERE code = %s", (ws,))
            ids[f"workspace{'' if key == 'view' else '2'}"] = cur.fetchone()["id"]
            cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                        (ids[f"workspace{'' if key == 'view' else '2'}"], av, av))
            cur.execute("SELECT id FROM agent_view WHERE code = %s", (av,))
            ids[key] = cur.fetchone()["id"]
    conn.commit()
    owner = accounts.create_user(conn, "c-owner", "user", "pw-owner-123")
    stranger = accounts.create_user(conn, "c-stranger", "user", "pw-stranger-123")
    admin = accounts.create_user(conn, "c-admin", "admin", "pw-admin-1234")
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=ids["view"])
    ids.update(owner=owner, stranger=stranger, admin=admin)
    return ids


@pytest.fixture
def selects(monkeypatch) -> list[str]:
    """Every SELECT any connection runs while this is installed."""
    seen: list[str] = []
    real = DictCursor.execute

    def spy(self, query, args=None):
        if query.lstrip().upper().startswith("SELECT"):
            seen.append(query)
        return real(self, query, args)

    monkeypatch.setattr(DictCursor, "execute", spy)
    return seen


@pytest.fixture
def thread(conn, world):
    """One conversation of the owner's in the view. A test file that needs a turn in it
    defines its own."""
    from agento.modules.conversation.src import service

    return service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")


def routed(module_name: str, handlers):
    """``handlers`` (a module's ``src/routes.py``) behind web's route check: each name calls the
    handler only when ``server.acl_allows`` passes for its ``di.json`` ``acl``, else 403, as
    ``server._run`` does (D-ADMIN-ACL-2). A handler checks no panel leaf of its own."""
    from types import SimpleNamespace

    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.web import api, server

    decls = json.loads((Path(CORE_MODULES_DIR) / module_name / "di.json").read_text())["routes"]

    def guard(handler, acl):
        def call(req):
            if not server.acl_allows(req.conn, req.session.user, acl):
                return api.error(403, "forbidden")
            return handler(req)
        return call

    return SimpleNamespace(**{
        d["handler"].rsplit(".", 1)[1]: guard(getattr(handlers, d["handler"].rsplit(".", 1)[1]),
                                              d["acl"] if isinstance(d["acl"], str) else tuple(d["acl"]))
        for d in decls})
