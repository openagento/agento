"""Full end-to-end coverage for the ``app_monitor`` MCP-health telemetry.

These tests drive the real ``Consumer`` against the integration MySQL DB with
a patched runner that:

- Reports rc=0 success (``RunResult`` with ``session_id=<uuid>`` as the
  session_id, ``agent_type`` set to the provider name, and an optional
  ``mcp_init`` self-report).
- Writes a deterministic transcript JSONL at the per-provider production
  layout under the consumer-resolved ``home_dir`` (a real per-agent_view
  build dir under the patched ``BUILD_DIR``).
- Triggers the runner's ``session_id_callback`` so the consumer persists the
  session_id on the job row before ``_finalize_job`` dispatches
  ``job_finalize_before``.

``McpHealthTelemetryObserver`` then records two independent, nullable signals
on the ``job`` row — ``toolbox_mcp_calls`` (from the per-provider
``TranscriptReader``) and ``toolbox_mcp_connected`` (from ``mcp_init``) — and
optionally emails ops. It NEVER sets a verdict: every rc=0 job stays a SUCCESS.

The cases prove:

1. claude connected + toolbox calls → SUCCESS, columns N/TRUE, no alert.
2. claude not-connected + 0 calls + flag on → SUCCESS (no DEAD!), columns
   0/FALSE, ONE combined alert.
3. claude connected + 0 calls + flag on → SUCCESS, columns 0/TRUE, one alert.
4. claude connected + 0 calls + flag off → SUCCESS, columns 0/TRUE, no alert.
5. codex (runner emits no ``mcp_init``) + 0 calls + flag on → SUCCESS, columns
   0/NULL, one alert (the count clause fires; NULL connected does not).
6. unknown provider (no reader, no init) → SUCCESS, columns NULL/NULL, no
   alert (both signals unknown).
7. claude format drift (10 JSON records, none recognized) → SUCCESS (no
   DEAD!), columns NULL/<connected>, drift logged at ERROR, telemetry only.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer
from agento.framework.harness import McpInitReport, McpServerStatus
from agento.modules.app_monitor.src import observers as obs
from agento.modules.app_monitor.src.constants import (
    CFG_ALERT_EMAIL_TO,
    CFG_ALERT_SMTP_FROM,
    CFG_ALERT_SMTP_HOST,
    CFG_ALERT_SMTP_PORT,
    CFG_ALERT_SMTP_TLS,
    CFG_SEND_ALERT_ON_MCP_ISSUES,
)
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import (
    _test_connection,
    fetch_job,
    insert_primary_token,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"
CODEX_FIXTURES = FIXTURES / "codex"

_MCP_CONNECTED = McpInitReport(servers=(McpServerStatus("toolbox", "connected"),))
_MCP_FAILED = McpInitReport(servers=(McpServerStatus("toolbox", "failed"),))
_MCP_PENDING = McpInitReport(servers=(McpServerStatus("toolbox", "pending"),))


# --- DB helpers ---------------------------------------------------------------


def _insert_workspace(code: str = "acme") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace (code, label) VALUES (%s, %s)",
                (code, code),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _insert_agent_view(workspace_id: int, code: str = "developer") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (workspace_id, code, code),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _insert_job_with_agent_view(
    agent_view_id: int,
    *,
    reference_id: str = "AI-1",
    max_attempts: int = 3,
    idempotency_key: str | None = None,
) -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job (type, source, agent_view_id, reference_id,
                                    idempotency_key, status, attempt, max_attempts)
                   VALUES ('cron', 'jira', %s, %s, %s, 'TODO', 0, %s)""",
                (agent_view_id, reference_id,
                 idempotency_key or f"test:app_monitor_e2e:{reference_id}",
                 max_attempts),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _cleanup_test_data() -> None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute(
                "DELETE FROM core_config_data "
                "WHERE scope IN ('workspace','agent_view') OR path = 'agent_view/provider'"
            )
            cur.execute("DELETE FROM workspace_build")
            cur.execute("DELETE FROM job")
            cur.execute("DELETE FROM agent_view")
            cur.execute("DELETE FROM workspace")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


# --- runner callbacks: produce rc=0 + write a transcript ---------------------


def _claude_callback(
    transcript_payload: str,
    *,
    captured: list[str] | None = None,
    mcp_init: McpInitReport | None = None,
):
    """Build a ``ClaudeSubprocessRunner.execute`` replacement that writes the given
    transcript content to the production layout under
    ``<home_dir>/.claude/projects/<X>/<session_id>.jsonl``, fires the
    session_id callback, and returns a successful ``ClaudeResult`` carrying the
    given ``mcp_init`` self-report.
    """
    def _run(self_runner, request):
        home = Path(self_runner.context.home_dir)
        sid = str(uuid.uuid4())
        if captured is not None:
            captured.append(sid)
        proj = home / ".claude" / "projects" / "-workspace-test"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text(transcript_payload)
        if self_runner.session_id_callback:
            self_runner.session_id_callback(sid)
        return ClaudeResult(
            raw_output="ok",
            input_tokens=100, output_tokens=50,
            duration_ms=1000,
            session_id=sid,
            harness="claude",
            mcp_init=mcp_init,
        )
    return _run


def _codex_callback(
    transcript_payload: str,
    *,
    captured: list[str] | None = None,
    mcp_init: McpInitReport | None = None,
):
    """Build a ``CodexSubprocessRunner.execute`` replacement that writes the given
    transcript content to ``<home_dir>/.codex/sessions/2026/05/14/rollout-...-<sid>.jsonl``.
    Codex emits no ``mcp_init`` in practice, so it defaults to ``None``.
    """
    def _run(self_runner, request):
        home = Path(self_runner.context.home_dir)
        sid = str(uuid.uuid4())
        if captured is not None:
            captured.append(sid)
        sessions = home / ".codex" / "sessions" / "2026" / "05" / "14"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"rollout-2026-05-14T05-05-33-{sid}.jsonl").write_text(transcript_payload)
        if self_runner.session_id_callback:
            self_runner.session_id_callback(sid)
        return ClaudeResult(
            raw_output="ok",
            input_tokens=100, output_tokens=50,
            duration_ms=1000,
            session_id=sid,
            harness="codex",
            model="o3",
            mcp_init=mcp_init,
        )
    return _run


def _hermes_callback(self_runner, request):
    """Claude runner patched to report ``harness="hermes"`` — a harness
    with no registered ``TranscriptReader`` and no ``mcp_init``. Writes no
    transcript, so both telemetry signals resolve to NULL."""
    sid = str(uuid.uuid4())
    if self_runner.session_id_callback:
        self_runner.session_id_callback(sid)
    return ClaudeResult(
        raw_output="ok",
        input_tokens=10, output_tokens=5,
        duration_ms=100,
        session_id=sid,
        harness="hermes",
    )


# --- common fixtures ---------------------------------------------------------


def _patch_app_monitor(monkeypatch, *, alert_flag: bool, smtp_host: str = "smtp.example.com") -> MagicMock:
    """Configure the MCP-issue alert flag + SMTP and intercept ``send_alert``.
    Reverts automatically at test teardown."""
    monkeypatch.setattr(obs, "_config", lambda: {
        CFG_SEND_ALERT_ON_MCP_ISSUES: alert_flag,
        CFG_ALERT_EMAIL_TO: "ops@example.com",
        CFG_ALERT_SMTP_HOST: smtp_host,
        CFG_ALERT_SMTP_FROM: "agento@example.com",
        CFG_ALERT_SMTP_PORT: 587,
        CFG_ALERT_SMTP_TLS: False,
    })
    sender = MagicMock()
    monkeypatch.setattr(obs, "send_alert", sender)
    return sender


def _enter_build_dir_patches(stack: ExitStack, build_root: Path) -> None:
    """Patch every module-level ``BUILD_DIR`` / ``ARTIFACTS_DIR`` reference so
    the consumer + readers + workspace_build all agree on ``tmp_path``. Keep
    this list in sync with new readers / observers."""
    artifacts_root = str(build_root.parent / "artifacts")
    build_str = str(build_root)
    stack.enter_context(patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", artifacts_root))
    stack.enter_context(patch("agento.framework.artifacts_dir.BUILD_DIR", build_str))
    stack.enter_context(patch("agento.modules.workspace_build.src.builder.BUILD_DIR", build_str))
    stack.enter_context(patch("agento.modules.claude.src.transcript_reader.BUILD_DIR", build_str))
    stack.enter_context(patch("agento.modules.codex.src.transcript_reader.BUILD_DIR", build_str))


# --- the suite ---------------------------------------------------------------


class TestAppMonitorE2E:
    """End-to-end telemetry coverage against real MySQL — never DEAD-letters."""

    @pytest.fixture(autouse=True)
    def _patch_observer_db(self, int_db_config):
        """Make observers see the integration test DB (mirrors the recipe in
        ``tests/integration/test_instruction_files.py``).

        ``app_monitor.observers`` is here too because ``_save_mcp_telemetry``
        opens its own connection via ``DatabaseConfig.from_env()``.
        """
        with patch(
            "agento.modules.agent_view.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ), patch(
            "agento.modules.workspace_build.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ), patch(
            "agento.modules.app_monitor.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ):
            yield

    def setup_method(self):
        _cleanup_test_data()

    def teardown_method(self):
        _cleanup_test_data()

    def _run_one(self, int_db_config, int_consumer_config, tmp_path, run_patch):
        with ExitStack() as stack:
            stack.enter_context(run_patch)
            _enter_build_dir_patches(stack, tmp_path / "build")
            consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("e2e"))
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

    # -- 1. connected + toolbox calls --------------------------------------

    def test_claude_connected_with_calls_succeeds_no_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=False)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-201")

        captured: list[str] = []
        payload = (FIXTURES / "good_with_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _claude_callback(payload, captured=captured, mcp_init=_MCP_CONNECTED),
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["session_id"] == captured[0]
        assert row["toolbox_mcp_calls"] == 1
        assert row["toolbox_mcp_connected"] == 1  # TRUE
        sender.assert_not_called()

    # -- 1b. pending init + toolbox calls → NULL column, no alert ------------

    def test_claude_pending_with_calls_null_column_no_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        """The production false positive: claude prints init before the MCP
        handshake finishes, so `pending` is the normal report. It must persist as
        NULL (unknown) and never alert when the toolbox was demonstrably used."""
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-207")

        payload = (FIXTURES / "good_with_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _claude_callback(payload, mcp_init=_MCP_PENDING),
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["toolbox_mcp_calls"] == 1
        assert row["toolbox_mcp_connected"] is None  # NULL, not FALSE
        sender.assert_not_called()

    # -- 2. not connected + 0 calls + flag on → one combined alert, no DEAD --

    def test_claude_not_connected_zero_calls_one_alert_no_dead(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-202")

        payload = (FIXTURES / "bad_no_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _claude_callback(payload, mcp_init=_MCP_FAILED),
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row  # NO DEAD — telemetry only
        assert row["toolbox_mcp_calls"] == 0
        assert row["toolbox_mcp_connected"] == 0  # FALSE

        sender.assert_called_once()
        smtp_cfg, to, subject, body = sender.call_args.args
        assert to == "ops@example.com"
        assert smtp_cfg.host == "smtp.example.com"
        # Combined condition → one email naming both clauses.
        assert "0 toolbox calls" in subject
        assert "toolbox not connected" in subject
        assert "AI-202" in body

    # -- 3. connected + 0 calls + flag on → one alert (count clause) --------

    def test_claude_connected_zero_calls_flag_on_one_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-203")

        payload = (FIXTURES / "bad_no_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _claude_callback(payload, mcp_init=_MCP_CONNECTED),
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["toolbox_mcp_calls"] == 0
        assert row["toolbox_mcp_connected"] == 1  # TRUE
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert "0 toolbox calls" in subject
        assert "toolbox not connected" not in subject  # connected was TRUE

    # -- 4. connected + 0 calls + flag OFF → no alert -----------------------

    def test_claude_zero_calls_flag_off_no_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=False)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-204")

        payload = (FIXTURES / "bad_no_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _claude_callback(payload, mcp_init=_MCP_CONNECTED),
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["toolbox_mcp_calls"] == 0
        assert row["toolbox_mcp_connected"] == 1
        sender.assert_not_called()

    # -- 5. codex (no mcp_init) + 0 calls + flag on → one alert via count ----

    def test_codex_no_init_zero_calls_one_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        insert_primary_token("codex")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-205")

        payload = (CODEX_FIXTURES / "codex_bad_no_mcp.jsonl").read_text()
        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.codex.src.runner.CodexSubprocessRunner.execute",
            _codex_callback(payload),  # mcp_init defaults to None
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["toolbox_mcp_calls"] == 0
        assert row["toolbox_mcp_connected"] is None  # NULL — codex has no init signal
        # The count==0 clause fires; NULL connected does not.
        sender.assert_called_once()
        _, _, subject, body = sender.call_args.args
        assert "0 toolbox calls" in subject
        assert "toolbox not connected" not in subject
        assert "AI-205" in body

    # -- 6. unknown provider → NULL/NULL, no alert --------------------------

    def test_unknown_provider_null_columns_no_alert(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        # claude pool/auth but harness="hermes" — no reader, no init.
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-206")

        self._run_one(int_db_config, int_consumer_config, tmp_path, patch(
            "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
            _hermes_callback,
        ))

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS", row
        assert row["toolbox_mcp_calls"] is None   # no reader → unknown
        assert row["toolbox_mcp_connected"] is None  # no init → unknown
        sender.assert_not_called()  # both signals NULL — neither clause fires

    # -- 7. format drift → no DEAD, telemetry only --------------------------

    def test_claude_format_drift_no_dead_telemetry_only(
        self, int_db_config, int_consumer_config, tmp_path, monkeypatch, caplog,
    ):
        sender = _patch_app_monitor(monkeypatch, alert_flag=True)
        insert_primary_token("claude")
        av_id = _insert_agent_view(_insert_workspace("acme"), "developer")
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-207")

        # 10 JSON-parseable lines whose outer shape doesn't match the Claude
        # ``message.content`` envelope — simulates a silent CLI upgrade.
        drift_payload = "\n".join(
            f'{{"unexpected": "format", "v": {i}}}' for i in range(10)
        ) + "\n"

        with ExitStack() as stack:
            stack.enter_context(patch(
                "agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute",
                _claude_callback(drift_payload, mcp_init=_MCP_CONNECTED),
            ))
            _enter_build_dir_patches(stack, tmp_path / "build")
            consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("e2e"))
            job = consumer._try_dequeue()
            assert job is not None
            with caplog.at_level(logging.ERROR, logger=obs.logger.name):
                consumer._execute_job(job)

        row = fetch_job(job_id)
        # Drift no longer dead-letters — telemetry only.
        assert row["status"] == "SUCCESS", row
        # Parse unreliable → calls NULL; connected resolves independently of the
        # transcript (from mcp_init) → TRUE.
        assert row["toolbox_mcp_calls"] is None
        assert row["toolbox_mcp_connected"] == 1
        # calls NULL + connected TRUE → neither alert clause fires.
        sender.assert_not_called()
        assert any(
            rec.levelno == logging.ERROR and "drift detected" in rec.getMessage()
            for rec in caplog.records
        )
