"""Unit: the consumer's capability lifecycle — mint under a row lock, revoke on every
terminal transition, and redact the run's own token out of anything it persists.
"""
from __future__ import annotations

import json
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
        """It commits rather than rolls back: the execution row minted just before this
        check describes an attempt that really was claimed and is now abandoned (§5.3), and
        the commit releases the FOR UPDATE lock the rollback used to release. No job row is
        written either way."""
        conn, cur = _conn(status="PAUSED")
        with patch("agento.framework.consumer.issue_capability") as mint:
            assert consumer._issue_run_capabilities(conn, _job()) is None
        mint.assert_not_called()
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        assert not [c for c in cur.execute.call_args_list if "UPDATE job" in c[0][0]]

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
        assert [c.kwargs["allowed_transports"] for c in mint.call_args_list] == [["http"], ["http"]]
        assert mint.call_args_list[1].kwargs["subject_id"] == "service:consumer"

    @pytest.mark.parametrize("test_config", [None, {"stub": ["send"], "real": []}])
    def test_the_run_capability_carries_the_job_test_config(self, consumer, test_config):
        conn, _ = _conn()
        with patch("agento.framework.consumer.issue_capability", return_value="mcp") as mint:
            consumer._issue_run_capabilities(conn, _job(test_config=test_config))
        assert mint.call_args.kwargs["test_config"] == test_config

    @pytest.mark.parametrize("test_config", [None, {"stub": [], "real": []}])
    def test_a_resumed_turn_takes_the_test_config_from_its_own_job(self, consumer, test_config):
        # The line's earlier job (resume_job_id) may differ; only this job counts.
        conn, _ = _conn()
        job = _job(test_config=test_config, resume_job_id=999)
        with patch("agento.framework.consumer.issue_capability", return_value="mcp") as mint:
            consumer._issue_run_capabilities(conn, job)
        assert mint.call_args.kwargs["test_config"] == test_config

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


_V2 = {"v": 2, "stub": ["send"], "real": [], "stub_labels": [], "unstub": [], "known": ["m/send"]}


class TestTestPolicyAtClaim:
    """A test job's policy is resolved once, before the execution row and the capability; a
    stored policy that is not valid fails the job before any of it (Test-policy v2)."""

    def test_malformed_policy_raises_before_any_mint(self, consumer):
        from agento.framework.test_run import InvalidPolicy, InvalidTestConfig

        with patch("agento.framework.consumer.get_channel") as channel, \
                patch("agento.framework.consumer.mint_execution_id") as mint_id, \
                patch("agento.framework.consumer.issue_capability") as mint, \
                pytest.raises(InvalidTestConfig, match="not an object"):
            consumer._run_job(_job(test_config=InvalidPolicy("stored test policy is not an object")))
        channel.assert_not_called()
        mint_id.assert_not_called()
        mint.assert_not_called()

    def test_the_invalid_policy_check_is_the_first_statement_of_run_job(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(Consumer._run_job)))
        body = tree.body[0].body  # type: ignore[attr-defined]
        first = body[1] if isinstance(body[0], ast.Expr) else body[0]
        assert isinstance(first, ast.If)
        assert "InvalidPolicy" in ast.unparse(first.test)
        assert isinstance(first.body[0], ast.Raise)
        assert "InvalidTestConfig" in ast.unparse(first.body[0])

    def test_malformed_policy_finalized_at_once(self, consumer):
        from agento.framework.retry_policy import evaluate
        from agento.framework.test_run import InvalidPolicy

        events = []
        manager = MagicMock()
        manager.dispatch.side_effect = lambda name, _event: events.append(name)
        with patch("agento.framework.consumer.get_event_manager", return_value=manager), \
                patch.object(consumer, "_finalize_job") as finalize:
            consumer._execute_job(_job(test_config=InvalidPolicy("stored test policy is not an object")))
        error = finalize.call_args.args[1]
        assert type(error).__name__ == "InvalidTestConfig"
        assert evaluate(type(error).__name__, 1, 3, error).should_retry is False
        assert events == ["worker_start_after", "worker_stop_after"]

    def test_a_production_job_reads_one_snapshot_and_is_assessed(self, consumer):
        from agento.framework.test_run import LabelSnapshot

        conn, cur = _conn()
        snapshot = LabelSnapshot(labels=set())
        with patch("agento.framework.consumer.get_manifests", return_value=[]), \
                patch("agento.framework.consumer.load_label_snapshot", return_value=snapshot) as load, \
                patch("agento.framework.consumer.resolve_test_policy") as resolve, \
                patch("agento.framework.consumer.assess_run", return_value={"v": 1}) as assess:
            assert consumer._resolve_tool_policy(conn, _job(test_config=None)) == {"v": 1}
        load.assert_called_once()
        resolve.assert_not_called()
        assert assess.call_args.args[2:4] == (None, snapshot)
        cur.execute.assert_not_called()

    def test_a_production_job_runs_when_the_snapshot_fails(self, consumer):
        conn, _ = _conn()
        with patch("agento.framework.consumer.get_manifests", return_value=[]), \
                patch("agento.framework.consumer.load_label_snapshot", side_effect=RuntimeError("x")):
            risk = consumer._resolve_tool_policy(conn, _job(test_config=None))
        assert (risk["tool_coverage"], risk["coverage"]) == ("error", "error")

    def test_a_test_job_is_refused_when_the_snapshot_fails(self, consumer):
        from agento.framework.test_run import SnapshotTooLarge

        conn, _ = _conn()
        with patch("agento.framework.consumer.get_manifests", return_value=[]), \
                patch("agento.framework.consumer.load_label_snapshot", side_effect=SnapshotTooLarge("over")), \
                pytest.raises(SnapshotTooLarge):
            consumer._resolve_tool_policy(conn, _job(test_config={"stub": [], "real": []}))

    def test_claim_order_resolve_then_capability_with_one_policy(self, consumer):
        from agento.framework.test_run import LabelSnapshot

        conn, cur = _conn()
        job = _job(test_config={"stub": ["send"], "real": []})
        snapshot = LabelSnapshot(labels=set())
        with patch("agento.framework.consumer.get_manifests", return_value=[]), \
                patch("agento.framework.consumer.load_label_snapshot", return_value=snapshot) as load, \
                patch("agento.framework.consumer.resolve_test_policy", return_value=dict(_V2)) as resolve, \
                patch("agento.framework.consumer.assess_run", return_value={"v": 1}) as assess:
            assert consumer._resolve_tool_policy(conn, job) == {"v": 1}
        load.assert_called_once()
        assert resolve.call_args.args[1] is snapshot
        # One snapshot and one policy object reach both the resolution and the assessment.
        assert assess.call_args.args[2] is job.test_config
        assert assess.call_args.args[3] is snapshot
        update = [c for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE job SET test_config")]
        assert [json.loads(c.args[1][0]) for c in update] == [_V2]
        conn.commit.assert_not_called()
        with patch("agento.framework.consumer.issue_capability", return_value="mcp") as mint:
            consumer._issue_run_capabilities(conn, job)
        assert mint.call_args.kwargs["test_config"] is job.test_config
        assert job.test_config == _V2

    def test_a_resolved_policy_is_not_written_again(self, consumer):
        conn, cur = _conn()
        job = _job(test_config=dict(_V2))
        with patch("agento.framework.consumer.get_manifests", return_value=[]), \
                patch("agento.framework.consumer.load_label_snapshot"), \
                patch("agento.framework.consumer.assess_run"):
            consumer._resolve_tool_policy(conn, job)
        cur.execute.assert_not_called()

    def test_run_job_resolves_before_the_execution_row_and_the_capability(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(Consumer._run_job)))
        lines = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                lines.setdefault(name, node.lineno)
        assert lines["_resolve_tool_policy"] < lines["mint_execution_id"] < lines["_issue_run_capabilities"]


class TestToolRiskAtClaim:
    """The assessment rides the execution mint, and the direct MCP servers are recorded once
    after the workspace through the provider's optional `record_tool_risk` (Task 6)."""

    def test_claim_passes_tool_risk_to_execution_row(self):
        from agento.framework.execution_hooks import RunProfile
        from agento.modules.conversation.src.hooks import ConversationExecutionIds

        conn, cur = _conn()
        risk = {"v": 1, "trifecta": True}
        with patch("agento.modules.conversation.src.hooks.service.link_execution"):
            ConversationExecutionIds().mint(conn=conn, job_id=7, attempt=1, profile=RunProfile(
                harness="h", provider="p", model=None, credential_id=None, tool_risk=risk))
        sql, params = cur.execute.call_args_list[0].args
        assert "tool_risk" in sql
        assert json.loads(params[-1]) == risk

    def test_run_job_passes_the_assessment_to_the_mint(self):
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(Consumer._run_job))
        assert "tool_risk = self._resolve_tool_policy(conn, job)" in source
        mint = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "RunProfile")
        assert any(k.arg == "tool_risk" for k in mint.keywords)

    def test_direct_servers_recorded_after_materialize(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(Consumer._run_job)))
        lines = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                lines.setdefault(name, node.lineno)
        assert lines["materialize_run_workspace"] < lines["_record_direct_servers"]

    def _record(self, consumer, provider, *, direct=("github",), artifacts_dir="/w"):
        from agento.framework import execution_hooks

        conn, cur = _conn()
        execution_hooks.clear()
        execution_hooks.SEAMS["execution_id_provider"].register(provider, module="t")
        pooled = MagicMock()
        pooled.__enter__ = MagicMock(return_value=conn)
        pooled.__exit__ = MagicMock(return_value=False)
        try:
            with patch.object(consumer, "_db", return_value=pooled), \
                    patch("agento.framework.consumer.workspace_adapter_for"), \
                    patch("agento.framework.consumer.detect_direct_mcp_servers",
                          return_value=list(direct)) as detect:
                consumer._record_direct_servers(_job(execution_id="e-1"), "h", artifacts_dir,
                                                "http://toolbox:3001", {"v": 1, "tool_coverage": "complete"})
        finally:
            execution_hooks.clear()
        return conn, cur, detect

    def test_record_tool_risk_after_materialize(self, consumer):
        from agento.modules.conversation.src.hooks import ConversationExecutionIds

        conn, cur, _ = self._record(consumer, ConversationExecutionIds())
        updates = [c for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE execution")]
        assert len(updates) == 1
        stored, execution_id = updates[0].args[1]
        assert execution_id == "e-1"
        assert json.loads(stored) == {"v": 1, "tool_coverage": "complete", "direct": "recorded",
                                      "uncovered": ["github"], "coverage": "incomplete"}
        conn.commit.assert_called_once()

    def test_no_update_when_the_provider_lacks_the_member(self, consumer):
        class MintOnly:
            def mint(self, *, conn, job_id, attempt, profile=None):
                return "e-1"

        conn, cur, _ = self._record(consumer, MintOnly())
        cur.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_no_workspace_is_unknown(self, consumer):
        from agento.modules.conversation.src.hooks import ConversationExecutionIds

        _, cur, detect = self._record(consumer, ConversationExecutionIds(), artifacts_dir=None)
        detect.assert_not_called()
        assert json.loads(cur.execute.call_args.args[1][0])["coverage"] == "incomplete"

    def test_a_record_failure_never_fails_the_run(self, consumer, caplog):
        class Failing:
            def mint(self, *, conn, job_id, attempt, profile=None):
                return "e-1"

            def record_tool_risk(self, *, conn, execution_id, tool_risk):
                raise RuntimeError("mysql://u:secret@h")

        self._record(consumer, Failing())
        assert "RuntimeError" in caplog.text
        assert "secret" not in caplog.text
