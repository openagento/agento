"""The consumer resolves and persists each job's runtime at claim time."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_view_runtime import AgentViewRuntime, InvalidJobRuntime
from agento.framework.consumer import Consumer
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.job_models import AgentType, Job

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _consumer():
    return Consumer(MagicMock(), ConsumerConfig(), logging.getLogger("test"))


def _job(**overrides) -> Job:
    job = Job.stub(type=AgentType.TODO, source="test", agent_view_id=1)
    job.id = 7
    job.attempt = 1
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: cursor
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _view(harness="claude", provider="anthropic", model="view-model"):
    return AgentViewRuntime(harness=harness, provider=provider, model=model)


def _written(cursor) -> tuple:
    """The (agent_type, provider, model) the claim persisted."""
    sql, params = cursor.execute.call_args_list[-1][0]
    assert "UPDATE job" in sql and "agent_type" in sql
    return params[:3]


def test_unpinned_job_persists_the_resolved_runtime():
    conn, cursor = _conn()
    job = _job()
    with patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=_view()):
        runtime = _consumer()._resolve_run_runtime(conn, job)
    assert (runtime.harness, runtime.provider, runtime.model) == (
        "claude", "anthropic", "view-model",
    )
    assert _written(cursor) == ("claude", "anthropic", "view-model")
    assert (job.agent_type, job.provider, job.model) == ("claude", "anthropic", "view-model")


def test_pinned_model_is_persisted_and_config_is_not_used_for_it():
    conn, cursor = _conn()
    with patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=_view()):
        runtime = _consumer()._resolve_run_runtime(conn, _job(model="pinned"))
    assert runtime.model == "pinned"
    assert _written(cursor)[2] == "pinned"


def test_a_committed_snapshot_is_reused_verbatim_and_not_rewritten():
    conn, cursor = _conn()
    job = _job(attempt=2, agent_type="claude", provider="anthropic", model=None)
    view = _view(model="changed-since-attempt-1")
    # The runtime config must not even be READ for a committed snapshot: the context
    # function is the one that loads no harness/provider/model.
    with patch(
        "agento.framework.consumer.resolve_agent_view_context",
        return_value=AgentViewRuntime(),
    ), patch(
        "agento.framework.consumer.resolve_agent_view_runtime", return_value=view,
    ) as full:
        runtime = _consumer()._resolve_run_runtime(conn, job)
    # A NULL model inside a committed snapshot means the harness default — it must not
    # pick up a config change made between attempts.
    assert (runtime.harness, runtime.provider, runtime.model) == ("claude", "anthropic", None)
    cursor.execute.assert_not_called()
    full.assert_not_called()


def test_an_interrupted_claim_resolves_again_instead_of_running_half_written():
    # attempt 2, pin written, provider never committed -> NOT a snapshot.
    conn, cursor = _conn()
    job = _job(attempt=2, agent_type="claude", provider=None, model=None)
    with patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=_view()):
        runtime = _consumer()._resolve_run_runtime(conn, job)
    assert runtime.provider == "anthropic"
    assert _written(cursor) == ("claude", "anthropic", "view-model")


def test_a_cleared_job_resolves_from_config_on_any_attempt():
    conn, cursor = _conn()
    job = _job(attempt=3, agent_type=None, provider=None, model=None)
    with patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=_view()):
        runtime = _consumer()._resolve_run_runtime(conn, job)
    assert (runtime.harness, runtime.model) == ("claude", "view-model")
    assert _written(cursor) == ("claude", "anthropic", "view-model")


def test_a_harness_swap_on_a_retried_job_rederives_the_provider():
    from agento.framework.harness import list_harnesses

    other = next(
        r.descriptor for r in list_harnesses() if r.descriptor.id != "claude"
    )
    conn, cursor = _conn()
    # job:set-runtime --harness <other> nulled provider, so this is not a snapshot.
    job = _job(attempt=2, agent_type=str(other.id), provider=None, model=None)
    with patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=_view()):
        runtime = _consumer()._resolve_run_runtime(conn, job)
    assert runtime.harness == str(other.id)
    assert runtime.provider == str(other.default_provider)
    assert _written(cursor)[1] == str(other.default_provider)


def test_an_unknown_pinned_harness_fails_before_the_view_is_read():
    conn, _cursor = _conn()
    with patch("agento.framework.consumer.resolve_agent_view_runtime") as view, pytest.raises(InvalidJobRuntime):
        _consumer()._resolve_run_runtime(conn, _job(agent_type="no-such-harness"))
    view.assert_not_called()


def test_a_pinned_job_tolerates_a_broken_agent_view_pair():
    conn, _cursor = _conn()
    with patch("agento.framework.consumer.resolve_agent_view_runtime") as view:
        view.return_value = _view()
        _consumer()._resolve_run_runtime(conn, _job(agent_type="claude"))
    assert view.call_args.kwargs["tolerate_pair_error"] is True


def test_an_unpinned_job_does_not_tolerate_a_broken_agent_view_pair():
    conn, _cursor = _conn()
    with patch("agento.framework.consumer.resolve_agent_view_runtime") as view:
        view.return_value = _view()
        _consumer()._resolve_run_runtime(conn, _job())
    assert view.call_args.kwargs["tolerate_pair_error"] is False


class TestRunPath:
    """The effective runtime reaches the things that actually run the job.

    These drive ``_run_job`` itself — credential scope, workspace preparation and the
    runner all see the EFFECTIVE values, not the agent_view's configured ones.
    """

    def _drive(self, job, *, view=None, run_error=None):
        from pathlib import Path
        from types import SimpleNamespace

        from agento.framework.harness import list_harnesses

        captured: dict = {}
        provider_desc = SimpleNamespace(
            id="prov", credential_scope="scope-for-the-effective-harness",
            credential_required=True,
        )

        def _resolve_provider(harness, provider):
            captured["resolve_provider"] = (harness, provider)
            return provider_desc

        def _materialize(runtime, **kwargs):
            captured["order"].append("workspace")
            captured["workspace_runtime"] = runtime
            captured["effective_model"] = kwargs.get("effective_model")
            return Path("/run/7/home"), Path("/run/7/artifacts")

        def _create_runner(*args, **kwargs):
            captured["order"].append("runner")
            captured["runner"] = {"harness": args[0], "ctx": args[1]}
            runner = MagicMock()
            # DISTINCT failures: the message names which path ran, so a test that means
            # "the fresh-prompt path" cannot be satisfied by the resume path.
            runner.execute.side_effect = run_error or RuntimeError("resume path")
            return runner

        workflow_cls = MagicMock()
        workflow_cls.return_value.execute_job.side_effect = (
            run_error or RuntimeError("prompt path")
        )
        harness_entry = MagicMock()
        harness_entry.descriptor.capabilities.resume = True

        consumer = _consumer()
        captured["order"] = []
        conn = MagicMock()
        conn.commit.side_effect = lambda: captured["order"].append("commit")
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        with (
            patch("agento.framework.consumer.get_channel"),
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch(
                "agento.framework.consumer.resolve_agent_view_runtime",
                return_value=view if view is not None else _view(),
            ),
            patch(
                "agento.framework.consumer.resolve_agent_view_context",
                return_value=AgentViewRuntime(),
            ),
            patch("agento.framework.consumer.resolve_provider", _resolve_provider),
            patch("agento.framework.consumer.get_harness", return_value=harness_entry),
            patch("agento.framework.consumer.get_harness_config", return_value={}),
            patch.object(
                consumer._credential_resolver, "resolve",
                side_effect=lambda c, scope, **kw: (
                    captured.setdefault("scope", scope),
                    SimpleNamespace(id=1, lease_owner=None, credentials={}),
                )[1],
            ),
            patch("agento.framework.config_resolver.ScopedConfigService"),
            patch("agento.framework.consumer.materialize_run_workspace", _materialize),
            patch("agento.framework.consumer.get_event_manager"),
            patch("agento.framework.consumer.RemoteRunner", _create_runner),
            patch(
                "agento.framework.consumer.get_workflow_class", return_value=workflow_cls
            ),
            patch("agento.framework.consumer.get_module_config", return_value={}),
            patch("agento.framework.consumer.workspace_adapter_for"),
            patch.object(
                Consumer, "_issue_run_capabilities", return_value=("cap-mcp", None),
            ),
            patch("agento.framework.consumer.release_credential_lease"),
            pytest.raises(RuntimeError) as excinfo,
        ):
            consumer._run_job(job)
        captured["error"] = str(excinfo.value)
        captured["cursor"] = cursor
        captured["harnesses"] = list_harnesses()
        return captured

    def _other(self):
        from agento.framework.harness import list_harnesses

        return next(
            r.descriptor for r in list_harnesses() if r.descriptor.id != "claude"
        )

    def test_pinned_values_reach_credential_scope_workspace_and_runner(self):
        other = self._other()
        cap = self._drive(_job(agent_type=str(other.id), model="pinned-model"))
        # credential scope comes from the provider of the PINNED harness
        assert cap["resolve_provider"] == (str(other.id), str(other.default_provider))
        assert cap["scope"] == "scope-for-the-effective-harness"
        # workspace preparation is handed the EFFECTIVE runtime, not the view's
        assert cap["workspace_runtime"].harness == str(other.id)
        assert cap["workspace_runtime"].model == "pinned-model"
        assert cap["effective_model"] == "pinned-model"
        # and so is the runner
        assert cap["runner"]["harness"] == str(other.id)
        assert cap["runner"]["ctx"].model == "pinned-model"

    def test_the_snapshot_is_committed_before_the_run_starts(self):
        cap = self._drive(_job())
        sql = next(c[0][0] for c in cap["cursor"].execute.call_args_list
                   if "UPDATE job" in c[0][0])
        assert "UPDATE job" in sql and "agent_type" in sql
        # COMMITTED — and committed before anything the run does with it. A crash
        # between the claim and the first run step must not leave the row unpinned.
        assert cap["order"][:3] == ["commit", "workspace", "runner"]
        assert cap["workspace_runtime"].harness == "claude"

    def test_a_dropped_session_makes_the_run_start_from_the_prompt(self):
        # job:set-runtime nulled session_id when the harness changed; the run must not
        # resume (an empty-prompt resume would record a no-op as a success).
        other = self._other()
        job = _job(attempt=2, agent_type=str(other.id), provider=None, session_id=None)
        cap = self._drive(job)
        assert cap["runner"]["harness"] == str(other.id)
        # the resume path never ran — the workflow did
        assert cap["error"] == "prompt path"

    def test_a_kept_session_still_resumes(self):
        """The guard above is real: with the session intact, the resume path DOES run."""
        cap = self._drive(_job(attempt=2, session_id="sess-1"))
        assert cap["error"] == "resume path"

    def test_a_retry_keeps_a_changed_provider_and_a_non_null_model(self):
        other = self._other()
        job = _job(
            attempt=2, agent_type=str(other.id), provider="snapshot-provider",
            model="snapshot-model",
        )
        # the view now says something completely different
        cap = self._drive(job, view=_view(harness="claude", provider="anthropic",
                                          model="changed-after-attempt-1"))
        assert cap["resolve_provider"] == (str(other.id), "snapshot-provider")
        assert cap["workspace_runtime"].model == "snapshot-model"
        # snapshot is not rewritten (the pool's isolation SET is not a write)
        assert not [c for c in cap["cursor"].execute.call_args_list if "UPDATE job" in c[0][0]]

    def test_a_pinned_job_runs_over_a_broken_agent_view_pair(self):
        """The view's bad harness/provider pair is tolerated, the pin is used."""
        broken = _view(harness=None, provider=None, model=None)  # what tolerance leaves
        other = self._other()
        cap = self._drive(_job(agent_type=str(other.id)), view=broken)
        assert cap["resolve_provider"] == (str(other.id), str(other.default_provider))
        assert cap["runner"]["harness"] == str(other.id)
