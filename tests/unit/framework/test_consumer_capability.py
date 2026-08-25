"""Unit: the consumer's capability lifecycle — mint under a row lock, revoke on every
terminal transition, and redact the run's own token out of anything it persists.
"""
from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer, _JobResult
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.database_config import DatabaseConfig
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.framework.secret_redaction import redact_exception, redact_secret


@pytest.fixture
def consumer():
    return Consumer(DatabaseConfig(), ConsumerConfig(), logging.getLogger("test"))


def _conn(status="RUNNING"):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = {"status": status} if status else None
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def _job(**overrides) -> Job:
    defaults = dict(
        id=7, schedule_id=None, type=AgentType.CRON, source="jira", agent_view_id=3,
        priority=50, reference_id="AI-1", agent_type=None, provider=None, model=None,
        input_tokens=None, output_tokens=None, prompt=None, output=None, context=None,
        idempotency_key="k", status=JobStatus.RUNNING, attempt=1, max_attempts=3,
        scheduled_after=datetime(2026, 2, 20, 8, 0), started_at=None, finished_at=None,
        result_summary=None, error_message=None, error_class=None, pid=None,
        session_id=None, created_at=datetime(2026, 2, 20, 8, 0),
        updated_at=datetime(2026, 2, 20, 8, 0),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestIssueRunCapabilities:
    def test_the_status_read_takes_a_row_lock(self, consumer):
        conn, cur = _conn()
        with patch("agento.framework.consumer.issue_capability", return_value="t"):
            consumer._issue_run_capabilities(conn, _job())
        sql = cur.execute.call_args_list[0][0][0]
        assert "FOR UPDATE" in sql

    def test_a_paused_job_gets_no_capability(self, consumer):
        conn, _ = _conn(status="PAUSED")
        with patch("agento.framework.consumer.issue_capability") as mint:
            assert consumer._issue_run_capabilities(conn, _job()) is None
        mint.assert_not_called()
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_a_vanished_job_gets_no_capability(self, consumer):
        conn, _ = _conn(status=None)
        with patch("agento.framework.consumer.issue_capability") as mint:
            assert consumer._issue_run_capabilities(conn, _job()) is None
        mint.assert_not_called()

    def test_a_job_without_an_agent_view_mints_nothing_and_still_runs(self, consumer):
        conn, cur = _conn()
        with patch("agento.framework.consumer.issue_capability") as mint:
            assert consumer._issue_run_capabilities(conn, _job(agent_view_id=None)) == (None, None)
        mint.assert_not_called()
        cur.execute.assert_not_called()

    def test_a_referenced_job_gets_no_rest_capability(self, consumer):
        conn, _ = _conn()
        with patch("agento.framework.consumer.issue_capability", return_value="mcp") as mint:
            mcp, rest = consumer._issue_run_capabilities(conn, _job(reference_id="AI-1"))
        assert (mcp, rest) == ("mcp", None)
        assert mint.call_count == 1
        assert mint.call_args.kwargs["kind"] == "mcp_job"

    def test_a_discovery_job_also_gets_a_rest_capability(self, consumer):
        conn, _ = _conn()
        with patch(
            "agento.framework.consumer.issue_capability", side_effect=["mcp", "rest"]
        ) as mint:
            mcp, rest = consumer._issue_run_capabilities(conn, _job(reference_id=None))
        assert (mcp, rest) == ("mcp", "rest")
        kinds = [c.kwargs["kind"] for c in mint.call_args_list]
        assert kinds == ["mcp_job", "internal_rest"]

    def test_both_mints_land_in_one_transaction(self, consumer):
        conn, _ = _conn()
        with patch(
            "agento.framework.consumer.issue_capability", side_effect=["mcp", "rest"]
        ) as mint:
            consumer._issue_run_capabilities(conn, _job(reference_id=None))
        assert all(c.kwargs["commit"] is False for c in mint.call_args_list)
        conn.commit.assert_called_once()


class TestRedaction:
    def test_a_token_echoed_by_the_agent_is_redacted_before_persistence(self):
        token = "s3cr3t-token"
        result = _JobResult(
            summary=f"connected to http://toolbox:3001/mcp?cap={token}",
            prompt=f"cap={token}",
            output=f"my MCP url is http://toolbox:3001/mcp?cap={token}",
        ).redacted(token, None)
        assert token not in result.summary
        assert token not in result.output
        assert token not in result.prompt
        assert "cap=***" in result.output

    def test_a_token_in_a_failure_message_is_redacted(self):
        token = "s3cr3t-token"
        exc = RuntimeError(f"claude failed reaching /mcp?cap={token}")
        exc.agent_output = f"stderr: cap={token}"
        exc.output = f"partial: cap={token}"
        exc.stderr = f"boom cap={token}"
        redact_exception(exc, token, None)
        assert token not in str(exc)
        assert token not in exc.agent_output
        assert token not in exc.output
        assert token not in exc.stderr

    def test_redaction_without_a_secret_changes_nothing(self):
        exc = RuntimeError("plain failure")
        redact_exception(exc, None, None)
        assert str(exc) == "plain failure"
        assert redact_secret("plain failure", None) == "plain failure"

    def test_both_the_mcp_and_the_rest_token_are_redacted(self):
        exc = RuntimeError("mcp=AAA rest=BBB")
        redact_exception(exc, "AAA", "BBB")
        assert str(exc) == "mcp=*** rest=***"


class TestPurgeCadence:
    """The purge runs on the idle tick, so its cost is paid by every deployment. The
    once-per-hour rule is the whole reason that is acceptable — a tick that purged every time
    would run it every 5 seconds."""

    def _consumer_with_idle_tick(self, monkeypatch, consumer):
        monkeypatch.setattr("agento.framework.consumer.get_connection", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr("agento.framework.consumer.dispatch_reload", lambda: None)
        monkeypatch.setattr("agento.framework.consumer.bootstrap", lambda **_k: [])
        purged = []
        monkeypatch.setattr(
            "agento.framework.consumer.purge_expired_capabilities",
            lambda conn: purged.append(1) or 0,
        )
        return purged

    def test_a_tick_inside_the_interval_does_not_purge(self, monkeypatch, consumer):
        purged = self._consumer_with_idle_tick(monkeypatch, consumer)
        clock = [consumer._last_capability_purge + 3599]
        monkeypatch.setattr("agento.framework.consumer.time.monotonic", lambda: clock[0])
        consumer._maybe_reload_bootstrap()
        assert purged == []

    def test_the_first_tick_past_the_interval_purges_exactly_once(self, monkeypatch, consumer):
        purged = self._consumer_with_idle_tick(monkeypatch, consumer)
        clock = [consumer._last_capability_purge + 3601]
        monkeypatch.setattr("agento.framework.consumer.time.monotonic", lambda: clock[0])
        consumer._maybe_reload_bootstrap()
        assert purged == [1]
        # The next tick one second later must NOT purge again: the stamp moved forward.
        clock[0] += 1
        consumer._maybe_reload_bootstrap()
        assert purged == [1]
        # ... and an hour after THAT it purges again.
        clock[0] += 3601
        consumer._maybe_reload_bootstrap()
        assert purged == [1, 1]

    def test_a_purge_failure_does_not_stop_the_tick_or_freeze_the_clock(self, monkeypatch, consumer):
        self._consumer_with_idle_tick(monkeypatch, consumer)
        monkeypatch.setattr(
            "agento.framework.consumer.purge_expired_capabilities",
            MagicMock(side_effect=RuntimeError("db gone")),
        )
        clock = [consumer._last_capability_purge + 3601]
        monkeypatch.setattr("agento.framework.consumer.time.monotonic", lambda: clock[0])
        consumer._maybe_reload_bootstrap()
        assert consumer._last_capability_purge == clock[0]


class TestPostMintBarrier:
    """The mint happens BEFORE the run environment is materialized, and materialization is what
    writes the raw token into the harness MCP config. A failure in that window raises an exception
    whose message can carry the token, and `_execute_job` persists `str(exc)` verbatim — so the
    redaction barrier has to start at the mint, not at the run."""

    def test_materialization_is_inside_a_redacting_handler(self):
        """Structural, not textual: walk the AST of `_run_job` and prove the materialization call
        sits in a `try` whose handler calls `redact_exception`. A grep for the word would pass on
        a comment; this fails if the call is moved out of the block."""
        import ast
        import inspect

        from agento.framework import consumer as consumer_module

        tree = ast.parse(inspect.getsource(consumer_module))
        protected = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body_calls = {
                n.func.id
                for stmt in node.body for n in ast.walk(stmt)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            handler_calls = {
                n.func.id
                for h in node.handlers for n in ast.walk(h)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "materialize_run_workspace" in body_calls and "redact_exception" in handler_calls:
                protected = True
        assert protected, (
            "materialize_run_workspace is not inside a try/except that calls redact_exception — "
            "a failure there would persist the raw capability token"
        )


class TestInteractivePreparationFailure:
    """`agento run` mints a 12-hour interactive capability before materializing the run. If
    preparation then fails, the run never happens but the capability stays valid for 12 hours —
    and the failure message can carry it."""

    def test_a_failure_redacts_the_token_and_revokes_the_capability(self, monkeypatch):
        from agento.modules.agent_view.src.commands import prepare_run as pr

        revoked = []
        monkeypatch.setattr(
            "agento.framework.db.get_connection", lambda *_a, **_k: MagicMock()
        )
        monkeypatch.setattr(
            "agento.framework.toolbox_capability.revoke_capability",
            lambda conn, token, **_k: revoked.append(token) or True,
        )
        token = "s3cr3t-interactive"
        with pytest.raises(RuntimeError) as ei, pr._revoke_on_failure(object(), token):
            raise RuntimeError(f"materialize failed writing cap={token}")
        assert token not in str(ei.value)
        assert revoked == [token]

    def test_a_revoke_failure_prints_the_category_not_the_driver_text(self, monkeypatch, capsys):
        """A PyMySQL connection error carries the DSN it dialled — host, user, sometimes the
        password. This warning goes to an operator terminal, so it prints the class name only."""
        from agento.modules.agent_view.src.commands import prepare_run as pr

        dsn = "mysql://root:hunter2@db.internal:3306/agento"
        monkeypatch.setattr(
            "agento.framework.db.get_connection",
            MagicMock(side_effect=RuntimeError(f"cannot connect to {dsn}")),
        )
        with pytest.raises(ValueError), pr._revoke_on_failure(object(), "tok"):
            raise ValueError("original failure")
        err = capsys.readouterr().err
        assert "hunter2" not in err
        assert dsn not in err
        assert "db.internal" not in err
        assert "RuntimeError" in err

    def test_a_revoke_failure_never_masks_the_original_error(self, monkeypatch):
        from agento.modules.agent_view.src.commands import prepare_run as pr

        monkeypatch.setattr(
            "agento.framework.db.get_connection",
            MagicMock(side_effect=RuntimeError("db unreachable")),
        )
        with pytest.raises(RuntimeError, match="original"), pr._revoke_on_failure(object(), "tok"):
            raise RuntimeError("original failure")
