"""Unit: the credential side of a job execution — capture, self-heal and lease release must
run on EVERY exit path, and a held lease must be renewed for as long as this process owns a
worker for that job (main loop AND shutdown drain).
"""
from __future__ import annotations

import logging
import threading
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.errors import (
    AuthenticationError,
    TransientAuthError,
    UsageLimitError,
)
from agento.framework.consumer import Consumer
from agento.framework.consumer_config import ConsumerConfig

_PROVIDER = SimpleNamespace(
    id="anthropic", credential_scope="claude", credential_required=True
)


@pytest.fixture(autouse=True)
def _stub_harness_registry():
    """``_run_job`` looks the harness up in the registry — for its declared
    ``capabilities`` and its allow-listed runtime config. These tests never call
    ``bootstrap()``, so the registry is empty and the lookup would raise before any
    credential wiring runs.
    """
    with (
        patch("agento.framework.consumer.get_harness", return_value=MagicMock()),
        patch("agento.framework.consumer.get_harness_config", return_value={}),
    ):
        yield


def _consumer(db=None) -> Consumer:
    return Consumer(
        db or MagicMock(), ConsumerConfig(poll_interval=0.01), logging.getLogger("test")
    )


def _token(credential_id: int = 7, *, lease_owner: str | None = None):
    return SimpleNamespace(
        id=credential_id,
        credentials={"refresh_token": "R0"},
        lease_owner=lease_owner,
        leased_until=None,
    )


def _drive_run_job(
    consumer: Consumer,
    *,
    token,
    capture: bool = False,
    raise_exc: BaseException | None = None,
):
    """Run ``_run_job`` for real from credential resolution through the lifecycle.

    Everything between is stubbed at its seams (workspace, runner, workflow), so the
    lease/capture/release wiring — including the `leased` state the detector keys off — is
    the code under test rather than a hand-passed argument. ``raise_exc`` is what the
    workflow raises; the default just stops the run after the agent seam.
    Returns the workspace adapter and the ``release_credential_lease`` mock.
    """
    exc = raise_exc or RuntimeError("stop after the agent seam")
    adapter = MagicMock()
    adapter.capture_refreshed_credentials.return_value = capture
    workflow_cls = MagicMock()
    workflow_cls.return_value.execute_job.side_effect = exc
    runtime = SimpleNamespace(harness="claude", provider="anthropic", model=None)
    job = SimpleNamespace(
        id=42, attempt=1, agent_view_id=2, source="jira", type="todo",
        priority=0, session_id=None, pid=None, reference_id="AI-1",
    )
    with (
        patch("agento.framework.consumer.get_channel"),
        patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
        patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime),
        patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER),
        patch.object(consumer._credential_resolver, "resolve", return_value=token),
        patch("agento.framework.config_resolver.ScopedConfigService"),
        patch(
            "agento.framework.consumer.materialize_run_workspace",
            return_value=(Path("/run/42/home"), Path("/run/42/artifacts")),
        ),
        patch("agento.framework.consumer.get_event_manager"),
        patch("agento.framework.consumer.create_runner"),
        patch("agento.framework.consumer.get_workflow_class", return_value=workflow_cls),
        patch("agento.framework.consumer.get_module_config", return_value={}),
        # Auth bookkeeping is not what these tests assert; it needs a real DB.
        patch("agento.framework.consumer.throttle_credential"),
        patch("agento.framework.consumer.mark_credential_error"),
        patch("agento.framework.consumer.count_credentials_for_scope", return_value=(2, 1)),
        patch("agento.framework.consumer.workspace_adapter_for", return_value=adapter),
        patch("agento.framework.consumer.clear_auto_credential_error"),
        patch("agento.framework.consumer.release_credential_lease") as release,
        pytest.raises(type(exc)),
    ):
        consumer._run_job(job)
    return adapter, release


class TestFinishCredentialLifecycle:
    def test_capture_self_heal_and_release_share_one_connection_and_one_commit(self):
        conn = MagicMock()
        adapter = MagicMock()
        adapter.capture_refreshed_credentials.return_value = True
        with (
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch("agento.framework.consumer.workspace_adapter_for", return_value=adapter),
            patch("agento.framework.consumer.clear_auto_credential_error") as clear,
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            _consumer()._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(lease_owner="job-42-attempt-1"),
                Path("/run/42"),
                success=True,
                lease_owner="job-42-attempt-1",
            )

        adapter.capture_refreshed_credentials.assert_called_once()
        clear.assert_called_once()
        assert release.call_args.args[:3] == (conn, 7, "job-42-attempt-1")
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_nothing_selected_is_a_no_op(self):
        with patch("agento.framework.consumer.get_connection") as get_conn:
            _consumer()._finish_credential_lifecycle(
                SimpleNamespace(id=42), None, None, None, success=True
            )
        get_conn.assert_not_called()

    def test_a_failed_run_does_not_clear_the_quarantine_but_still_releases(self):
        conn = MagicMock()
        with (
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch("agento.framework.consumer.clear_auto_credential_error") as clear,
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            _consumer()._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(),
                None,
                success=False,
                lease_owner="job-42-attempt-1",
            )
        # Only a COMPLETED run proves the credential works; a failure must not resurrect it.
        clear.assert_not_called()
        release.assert_called_once()

    def test_a_broken_capture_hook_never_skips_the_release(self):
        conn = MagicMock()
        adapter = MagicMock()
        adapter.capture_refreshed_credentials.side_effect = RuntimeError("adapter exploded")
        with (
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch("agento.framework.consumer.workspace_adapter_for", return_value=adapter),
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            _consumer()._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(),
                Path("/run/42"),
                success=False,
                lease_owner="job-42-attempt-1",
            )
        release.assert_called_once()

    def test_release_is_attempted_even_when_the_credential_carries_no_lease(self):
        # The UPDATE's own WHERE lease_owner = %s is the authority: a no-op costs one
        # statement, a skipped release costs a whole TTL.
        conn = MagicMock()
        with (
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            _consumer()._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(lease_owner=None),
                None,
                success=True,
                lease_owner="job-42-attempt-1",
            )
        release.assert_called_once()

    def test_a_rotation_while_unleased_is_logged_at_error(self, caplog):
        """The falsifiable-assumption detector for the freshness horizon — the one
        instrument that says how often the heuristic was wrong.

        Driven through ``_run_job``, deliberately: passing ``lease_owner=None`` directly
        is an argument combination the real wiring can never produce (an owner is issued
        for EVERY job), and asserting on it is exactly what let the detector regress to
        dead code — it keyed off the issued owner instead of the acquired lease.
        """
        with caplog.at_level(logging.ERROR):
            _drive_run_job(_consumer(), token=_token(lease_owner=None), capture=True)
        assert "WITHOUT holding a refresh lease" in caplog.text

    def test_a_rotation_under_a_held_lease_is_not_logged(self, caplog):
        """The other half of the same instrument: a leased rotation is the DESIGNED path
        and must stay silent, or the signal is worthless."""
        with caplog.at_level(logging.ERROR):
            _drive_run_job(
                _consumer(), token=_token(lease_owner="job-42-attempt-1"), capture=True
            )
        assert "WITHOUT holding a refresh lease" not in caplog.text

    def test_a_failed_release_still_drops_the_held_lease_entry(self):
        """Otherwise renewal would keep the DB row leased forever. Dropping it lets the row
        expire, which is the correct failure direction."""
        consumer = _consumer()
        consumer._held_leases["job-42-attempt-1"] = 7
        with patch(
            "agento.framework.consumer.get_connection", side_effect=RuntimeError("db down")
        ):
            consumer._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(),
                None,
                success=True,
                lease_owner="job-42-attempt-1",
            )
        assert consumer._held_leases == {}

    def test_a_successful_lifecycle_drops_the_held_lease_entry(self):
        consumer = _consumer()
        consumer._held_leases["job-42-attempt-1"] = 7
        with (
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch("agento.framework.consumer.workspace_adapter_for"),
            patch("agento.framework.consumer.clear_auto_credential_error"),
            patch("agento.framework.consumer.release_credential_lease"),
        ):
            consumer._finish_credential_lifecycle(
                SimpleNamespace(id=42),
                "claude",
                _token(),
                None,
                success=True,
                lease_owner="job-42-attempt-1",
            )
        assert consumer._held_leases == {}


class TestRenewLeases:
    def test_renews_exactly_the_owners_this_process_holds(self):
        consumer = _consumer()
        consumer._held_leases.update({"job-1-attempt-1": 7, "job-2-attempt-1": 8})
        conn = MagicMock()
        with (
            patch("agento.framework.consumer.get_connection", return_value=conn),
            patch("agento.framework.consumer.renew_credential_leases") as renew,
        ):
            consumer._renew_leases()
        assert sorted(renew.call_args.args[1]) == ["job-1-attempt-1", "job-2-attempt-1"]
        conn.commit.assert_called_once()

    def test_no_held_leases_touches_no_connection(self):
        with patch("agento.framework.consumer.get_connection") as get_conn:
            _consumer()._renew_leases()
        get_conn.assert_not_called()

    def test_a_db_error_does_not_propagate(self):
        consumer = _consumer()
        consumer._held_leases["job-1-attempt-1"] = 7
        with patch(
            "agento.framework.consumer.get_connection", side_effect=RuntimeError("db down")
        ):
            consumer._renew_leases()  # must not raise — a blip cannot kill the main loop

    def test_the_lock_is_released_before_db_io(self):
        """A slow or hung UPDATE must not block workers that need _active_jobs_lock to
        start or finish, so the dict is snapshotted and the lock dropped first."""
        consumer = _consumer()
        consumer._held_leases["job-1-attempt-1"] = 7
        acquired_during_io = threading.Event()

        def _renew(*_a, **_kw):
            if consumer._active_jobs_lock.acquire(blocking=False):
                acquired_during_io.set()
                consumer._active_jobs_lock.release()
            return 1

        with (
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch("agento.framework.consumer.renew_credential_leases", side_effect=_renew),
        ):
            consumer._renew_leases()
        assert acquired_during_io.is_set()


class TestRenewalCallSites:
    def test_the_main_loop_renews_held_leases_while_workers_are_busy(self):
        """A test that called _renew_leases directly would not have caught the real defect,
        which was that nothing in the loop called it. Also unlike _maybe_reload_bootstrap,
        renewal must NOT be skipped while jobs are active — that is when it matters."""
        consumer = _consumer()
        consumer._active_jobs = 1  # busy
        ticks = []

        def _renew():
            # Record the worker count AS OBSERVED at renewal time; resetting it to zero
            # before run() would have proven nothing about the busy case.
            with consumer._active_jobs_lock:
                ticks.append(consumer._active_jobs)
            if len(ticks) >= 2:
                consumer._shutdown.set()
                with consumer._active_jobs_lock:
                    consumer._active_jobs = 0  # let the drain below exit immediately

        with (
            patch.object(consumer, "_recover_stale_jobs"),
            patch.object(consumer, "_maybe_reload_bootstrap"),
            patch.object(consumer, "_try_dequeue", return_value=None),
            patch.object(consumer, "_renew_leases", side_effect=_renew),
            patch("agento.framework.consumer.get_event_manager"),
            patch("agento.framework.consumer.dispatch_shutdown"),
            patch("signal.signal"),
        ):
            consumer.run()

        assert len(ticks) >= 2
        assert ticks[0] > 0, "renewal must run while a worker still holds a lease"

    def test_leases_are_renewed_while_draining_past_the_ttl_on_sigterm(self):
        """SIGTERM: the loop exits but executor.shutdown(wait=True) blocks — itself
        UNBOUNDED — so renewal must continue with no cap, or a graceful restart would expire
        a lease mid-capture.

        The proof uses the REAL ``_renew_leases`` with a small TTL and an injected clock
        (the drain's own ``time.sleep`` is the clock): counting mocked calls proved only
        that a stub was called N times, never that renewal outlived the deadline it
        renews. Here simulated time passes many TTLs and every one is still renewed.
        """
        ttl = 2
        consumer = _consumer()
        consumer._shutdown.set()  # skip the main loop entirely; go straight to the drain
        consumer._held_leases["job-42-attempt-1"] = 7
        with consumer._active_jobs_lock:
            consumer._active_jobs = 1  # one worker still running
        now = 0.0
        renewals: list[tuple[float, list[str], int]] = []

        def _advance(seconds):
            nonlocal now
            now += seconds

        def _renew_sql(_conn, owners, ttl_seconds, logger=None):
            renewals.append((now, list(owners), ttl_seconds))
            if now > ttl * 10:  # far past any plausible cap, in simulated time
                with consumer._active_jobs_lock:
                    consumer._active_jobs = 0
            return len(owners)

        with (
            patch.object(consumer, "_recover_stale_jobs"),
            patch("agento.framework.consumer._DEFAULT_LEASE_TTL_SECONDS", ttl),
            patch("agento.framework.consumer.time.sleep", side_effect=_advance),
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch("agento.framework.consumer.renew_credential_leases", side_effect=_renew_sql),
            patch("agento.framework.consumer.get_event_manager"),
            patch("agento.framework.consumer.dispatch_shutdown"),
            patch("signal.signal"),
        ):
            consumer.run()

        assert renewals[-1][0] > ttl * 10, "renewal stopped at a bound the drain does not have"
        assert all(r[1] == ["job-42-attempt-1"] for r in renewals)
        assert all(r[2] == ttl for r in renewals), "the renewed deadline must be the TTL"
        # Every deadline is pushed forward before the previous one could lapse.
        gaps = [b[0] - a[0] for a, b in pairwise(renewals)]
        assert gaps and max(gaps) < ttl


class TestLeaseIsNotLeakedOnSetupFailures:
    """Both leak paths sit OUTSIDE the inner try that wraps the agent run, so only a
    lifecycle try enclosing the whole method covers them."""

    def test_a_raise_in_the_post_selection_config_read_still_releases(self):
        consumer = _consumer()
        finish = MagicMock()
        runtime = SimpleNamespace(harness="claude", provider="anthropic", model=None)
        with (
            patch.object(consumer, "_finish_credential_lifecycle", finish),
            patch("agento.framework.consumer.get_channel"),
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch(
                "agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime
            ),
            patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER),
            patch.object(
                consumer._credential_resolver, "resolve",
                return_value=_token(lease_owner="job-42-attempt-1"),
            ),
            patch(
                "agento.framework.config_resolver.ScopedConfigService",
                side_effect=RuntimeError("config blew up"),
            ),pytest.raises(RuntimeError, match="config blew up")
        ):
            consumer._run_job(
                SimpleNamespace(
                    id=42, attempt=1, agent_view_id=2, source="jira", type="todo",
                    priority=0, session_id=None, pid=None,
                )
            )

        assert finish.call_args.kwargs["lease_owner"] == "job-42-attempt-1"
        assert finish.call_args.kwargs["success"] is False

    def test_a_raise_in_materialize_run_workspace_still_releases(self):
        consumer = _consumer()
        finish = MagicMock()
        runtime = SimpleNamespace(harness="claude", provider="anthropic", model=None)
        with (
            patch.object(consumer, "_finish_credential_lifecycle", finish),
            patch("agento.framework.consumer.get_channel"),
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch(
                "agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime
            ),
            patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER),
            patch.object(
                consumer._credential_resolver, "resolve",
                return_value=_token(lease_owner="job-42-attempt-1"),
            ),
            patch("agento.framework.config_resolver.ScopedConfigService"),
            patch(
                "agento.framework.consumer.materialize_run_workspace",
                side_effect=RuntimeError("workspace blew up"),
            ),pytest.raises(RuntimeError, match="workspace blew up")
        ):
            consumer._run_job(
                SimpleNamespace(
                    id=42, attempt=1, agent_view_id=2, source="jira", type="todo",
                    priority=0, session_id=None, pid=None,
                )
            )

        assert finish.call_args.kwargs["lease_owner"] == "job-42-attempt-1"

    def test_the_lease_is_registered_for_renewal_before_any_blocking_setup(self):
        """A lease that exists in the DB but not in _held_leases is a lease nobody renews."""
        consumer = _consumer()
        seen: list[dict] = []
        runtime = SimpleNamespace(harness="claude", provider="anthropic", model=None)

        def _capture_state(*_a, **_kw):
            seen.append(dict(consumer._held_leases))
            raise RuntimeError("stop here")

        with (
            patch.object(consumer, "_finish_credential_lifecycle"),
            patch("agento.framework.consumer.get_channel"),
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch(
                "agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime
            ),
            patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER),
            patch.object(
                consumer._credential_resolver, "resolve",
                return_value=_token(lease_owner="job-42-attempt-1"),
            ),
            patch(
                "agento.framework.config_resolver.ScopedConfigService",
                side_effect=_capture_state,
            ),pytest.raises(RuntimeError)
        ):
            consumer._run_job(
                SimpleNamespace(
                    id=42, attempt=1, agent_view_id=2, source="jira", type="todo",
                    priority=0, session_id=None, pid=None,
                )
            )

        assert seen == [{"job-42-attempt-1": 7}]


class TestResumedSessionPath:
    """The resumed-session branch returns EARLY from the inner try, which is how it used to
    bypass the success-only capture block entirely — a rotation on a resumed attempt was
    silently lost and its lease leaked for a whole TTL. It is the one success path that never
    reaches the bottom of the method, so it needs its own case."""

    def test_a_resumed_run_still_captures_self_heals_and_releases(self):
        from agento.framework.harness import RunResult

        consumer = _consumer()
        adapter = MagicMock()
        adapter.capture_refreshed_credentials.return_value = True
        runner = MagicMock()
        runner.execute.return_value = RunResult(
            raw_output="ok", input_tokens=10, output_tokens=5, cost_usd=0.0,
            num_turns=1, duration_ms=1, harness="claude", provider="anthropic",
        )
        runtime = SimpleNamespace(harness="claude", provider="anthropic", model=None)
        job = SimpleNamespace(
            id=42, attempt=2, agent_view_id=2, source="jira", type="todo",
            priority=0, session_id="sess-1", pid=None, reference_id="AI-1",
        )
        with (
            patch("agento.framework.consumer.get_channel"),
            patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
            patch(
                "agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime
            ),
            patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER),
            patch.object(
                consumer._credential_resolver, "resolve",
                return_value=_token(lease_owner="job-42-attempt-2"),
            ),
            patch("agento.framework.config_resolver.ScopedConfigService"),
            patch(
                "agento.framework.consumer.materialize_run_workspace",
                return_value=(Path("/run/42/home"), Path("/run/42/artifacts")),
            ),
            patch("agento.framework.consumer.get_event_manager"),
            patch("agento.framework.consumer.create_runner", return_value=runner),
            patch.object(consumer, "_save_session_id"),
            patch("agento.framework.consumer.workspace_adapter_for", return_value=adapter),
            patch("agento.framework.consumer.clear_auto_credential_error") as heal,
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            result = consumer._run_job(job)

        assert result.summary.startswith("resumed session_id=sess-1")
        runner.execute.assert_called_once()
        adapter.capture_refreshed_credentials.assert_called_once()
        heal.assert_called_once()  # a completed run proves the credential works
        release.assert_called_once()
        assert release.call_args.args[2] == "job-42-attempt-2"
        assert consumer._held_leases == {}


class TestLeaseLivesUntilCaptureIsDone:
    def test_the_lease_stays_registered_while_capture_is_still_writing(self):
        """The window between subprocess exit and the credential write is exactly where a
        pid-based reaper would free the lease mid-rotation. There is no live pid here, so
        the only thing keeping the row leased is the ``_held_leases`` entry — it must
        survive until the capture returns, and be gone immediately after.
        """
        consumer = _consumer()
        consumer._held_leases["job-42-attempt-1"] = 7
        in_capture = threading.Event()
        may_finish = threading.Event()
        seen: list[dict] = []

        def _slow_capture(*_a, **_kw):
            seen.append(dict(consumer._held_leases))
            in_capture.set()
            may_finish.wait(timeout=5)
            return True

        adapter = MagicMock()
        adapter.capture_refreshed_credentials.side_effect = _slow_capture

        def _finish():
            with (
                patch("agento.framework.consumer.get_connection", return_value=MagicMock()),
                patch(
                    "agento.framework.consumer.workspace_adapter_for", return_value=adapter
                ),
                patch("agento.framework.consumer.clear_auto_credential_error"),
                patch("agento.framework.consumer.release_credential_lease"),
            ):
                consumer._finish_credential_lifecycle(
                    SimpleNamespace(id=42),
                    "claude",
                    _token(lease_owner="job-42-attempt-1"),
                    Path("/run/42"),
                    success=True,
                    lease_owner="job-42-attempt-1",
                    leased=True,
                )

        worker = threading.Thread(target=_finish)
        worker.start()
        assert in_capture.wait(timeout=5)
        # Renewal is driven off this dict, so this is what keeps the DB row leased.
        assert consumer._held_leases == {"job-42-attempt-1": 7}
        may_finish.set()
        worker.join(timeout=5)

        assert seen == [{"job-42-attempt-1": 7}]
        assert consumer._held_leases == {}, "and dropped as soon as the capture is done"


class TestAutoProvenanceIsFrameworkOnly:
    def test_only_the_consumer_quarantines_with_auto_provenance(self):
        """An operator's decision must never be self-cleared, so the CLI and admin paths
        keep mark_credential_error's fail-closed 'operator' default."""
        for path in (
            "src/agento/framework/cli/credential.py",
            "src/agento/framework/admin/data.py",
        ):
            assert 'source="auto"' not in Path(path).read_text(), path
        assert 'source="auto"' in Path("src/agento/framework/consumer.py").read_text()


class TestTransientAuthDelegation:
    @pytest.mark.parametrize(
        "exc",
        [
            AuthenticationError("dead"),
            TransientAuthError("401 Invalid authentication credentials"),
            UsageLimitError("usage limit"),
        ],
    )
    def test_every_auth_outcome_still_captures_and_releases(self, exc):
        """Each of the three auth exits raises past the old success-only capture block, so
        each is a path on which the credential could previously be lost and the lease
        leaked for a whole TTL. Asserted through ``_run_job``, not by inspecting the
        exception object."""
        adapter, release = _drive_run_job(
            _consumer(), token=_token(lease_owner="job-42-attempt-1"), raise_exc=exc
        )
        adapter.capture_refreshed_credentials.assert_called_once()
        release.assert_called_once()
        assert release.call_args.args[2] == "job-42-attempt-1"

    @pytest.mark.parametrize(
        "exc",
        [
            AuthenticationError("dead"),
            TransientAuthError("401 Invalid authentication credentials"),
            UsageLimitError("usage limit"),
        ],
    )
    def test_every_auth_outcome_keeps_the_widened_handler_signature(self, exc):
        # TransientAuthError is deliberately NOT a subclass of AuthenticationError, and the
        # non-rotatable branch delegates to _handle_auth_failure with one — hence the
        # widened annotation. Both carry credential_id/retry_with_other_token.
        assert hasattr(exc, "credential_id")
        assert hasattr(exc, "retry_with_other_token")
