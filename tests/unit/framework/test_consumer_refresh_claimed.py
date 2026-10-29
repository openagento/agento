"""Unit: a refresh-imminent credential is refreshed out of band right after the claim, so
the lease lasts for the refresh only (AG-65). Driven through ``_run_job``; the workspace
build is the seam where the run would start, so what reached it is what a run would get.
"""
from __future__ import annotations

import ast
import importlib
import logging
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.credential_resolver import _DEFAULT_LEASE_TTL_SECONDS
from agento.framework.agent_manager.credential_store import CLAIM_LEASE_TTL_SECONDS
from agento.framework.agent_manager.errors import (
    AuthenticationError,
    TransientAuthError,
    UsageLimitError,
)
from agento.framework.agent_manager.models import CredentialStatus
from agento.framework.agent_view_runtime import AgentViewRuntime
from agento.framework.consumer import Consumer
from agento.framework.consumer_config import ConsumerConfig

# The consumer resolves each run's harness from the registry at claim time.
pytestmark = pytest.mark.usefixtures("builtin_harnesses")

_OWNER = "job-42-attempt-1"
_PROVIDER = SimpleNamespace(id="anthropic", credential_scope="claude", credential_required=True)


class _Stop(Exception):
    """Raised at the workspace-build seam: the run would start here."""


def _token(*, lease_owner=_OWNER, enabled=True, status=CredentialStatus.OK, payload="R0"):
    return SimpleNamespace(
        id=7, credentials={"refresh_token": payload}, lease_owner=lease_owner,
        leased_until=None, enabled=enabled, status=status,
    )


_REFRESHED = object()  # default re-read: the row with the payload the refresh persisted


def _drive(*, refresh, reread=_REFRESHED, still_imminent=False):
    """Run ``_run_job`` with ``refresh`` as the adapter's ``refresh_credentials`` (a return
    value or an exception). Returns what the workspace build saw (or ``None`` when it was
    never reached), the release mock, the throttle/poison mocks, and the raised error."""
    consumer = Consumer(MagicMock(), ConsumerConfig(poll_interval=0.01), logging.getLogger("t"))
    adapter = MagicMock()
    if isinstance(refresh, BaseException):
        adapter.refresh_credentials.side_effect = refresh
    elif refresh is AttributeError:
        del adapter.refresh_credentials  # an out-of-tree adapter predating the member
    else:
        adapter.refresh_credentials.return_value = refresh
    adapter.capture_refreshed_credentials.return_value = False
    seen: dict = {}

    def _materialize(*_a, **kw):
        seen.update(
            held=dict(consumer._held_leases),
            released_before_run=release.call_count,
            credential=kw["credential"],
        )
        raise _Stop

    runtime = AgentViewRuntime(harness="claude", provider="anthropic", model=None)
    job = SimpleNamespace(
        id=42, attempt=1, agent_view_id=2, source="jira", type="todo",
        priority=0, session_id=None, pid=None, reference_id="AI-1",
        agent_type=None, provider=None, model=None, thread_key=None, dry_run=False,
    )

    def _mint(*_a, **_kw):
        seen["refreshes_before_mint"] = getattr(adapter, "refresh_credentials", MagicMock()).call_count
        return ("cap-mcp", None)

    consumer._issue_run_capabilities = _mint  # a plain instance attribute: no patch needed
    # An ExitStack: a parenthesized `with` of this many managers exceeds CPython's
    # static block-nesting limit on some 3.12 builds (SyntaxError in CI).
    with ExitStack() as stack:
        stack.enter_context(patch("agento.framework.consumer.get_harness", return_value=MagicMock()))
        stack.enter_context(patch("agento.framework.consumer.get_harness_config", return_value={}))
        stack.enter_context(patch("agento.framework.consumer.get_channel"))
        stack.enter_context(patch("agento.framework.consumer.get_connection", return_value=MagicMock()))
        stack.enter_context(patch("agento.framework.consumer.resolve_agent_view_runtime", return_value=runtime))
        stack.enter_context(patch("agento.framework.consumer.resolve_provider", return_value=_PROVIDER))
        stack.enter_context(patch.object(consumer._credential_resolver, "resolve", return_value=_token()))
        stack.enter_context(patch.object(
            consumer._credential_resolver, "refresh_imminent", return_value=still_imminent
        ))
        stack.enter_context(patch(
            "agento.framework.consumer.get_credential",
            return_value=_token(payload="R1") if reread is _REFRESHED else reread,
        ))
        stack.enter_context(patch("agento.framework.config_resolver.ScopedConfigService"))
        stack.enter_context(patch("agento.framework.consumer.materialize_run_workspace", side_effect=_materialize))
        stack.enter_context(patch("agento.framework.consumer.get_event_manager"))
        stack.enter_context(patch("agento.framework.consumer.workspace_adapter_for", return_value=adapter))
        stack.enter_context(patch("agento.framework.consumer.clear_auto_credential_error"))
        throttle = stack.enter_context(patch("agento.framework.consumer.throttle_credential"))
        poison = stack.enter_context(patch("agento.framework.consumer.mark_credential_error"))
        stack.enter_context(patch("agento.framework.consumer.count_credentials_for_scope", return_value=(2, 1)))
        release = stack.enter_context(patch("agento.framework.consumer.release_credential_lease"))
        try:
            consumer._run_job(job)
        except BaseException as exc:  # the test inspects it
            raised = exc
        else:
            raised = None
    return SimpleNamespace(
        seen=seen or None, release=release, throttle=throttle, poison=poison,
        raised=raised, consumer=consumer,
    )


def test_capabilities_are_minted_after_the_refresh():
    """The refresh can take up to 60s; the REST capability lives 120s. Minting first
    would spend half its life before the run starts."""
    r = _drive(refresh=True)
    assert r.seen["refreshes_before_mint"] == 1


def test_a_refreshed_credential_is_released_before_the_run_and_never_renewed():
    r = _drive(refresh=True)
    assert isinstance(r.raised, _Stop)
    assert r.seen["released_before_run"] == 1
    assert r.release.call_args_list[0].args[1:3] == (7, _OWNER)
    assert r.seen["held"] == {}  # never registered for renewal
    # The run gets the re-read row: the payload the refresh persisted.
    assert r.seen["credential"].credentials == {"refresh_token": "R1"}


@pytest.mark.parametrize(
    "refresh",
    [False, AttributeError, RuntimeError("claude refresh failed: TimeoutExpired")],
    ids=["cannot-refresh", "adapter-without-member", "unexpected-error"],
)
def test_a_harness_that_cannot_refresh_keeps_todays_job_lease(refresh):
    r = _drive(refresh=refresh)
    assert isinstance(r.raised, _Stop)
    assert r.seen["released_before_run"] == 0
    assert r.seen["held"] == {_OWNER: 7}  # renewed for the run
    # Released once, by the lifecycle, after the run.
    assert [c.args[2] for c in r.release.call_args_list] == [_OWNER]
    assert r.consumer._held_leases == {}


def test_a_refresh_that_leaves_the_token_near_expiry_keeps_the_lease():
    """Persisted is not the same as safe to share (a codex ``last_refresh``-only change):
    a leaseless run could rotate it while the next claimant does the same."""
    r = _drive(refresh=True, still_imminent=True)
    assert r.seen["released_before_run"] == 0
    assert r.seen["held"] == {_OWNER: 7}
    assert r.seen["credential"].credentials == {"refresh_token": "R1"}


def test_a_dead_refresh_token_is_poisoned_and_the_run_never_starts():
    r = _drive(refresh=AuthenticationError("OAuth token has expired"))
    assert isinstance(r.raised, AuthenticationError)
    assert r.seen is None
    assert r.poison.call_args.kwargs["source"] == "auto"
    assert r.raised.retry_with_other_token is True
    assert [c.args[2] for c in r.release.call_args_list] == [_OWNER]


@pytest.mark.parametrize(
    "exc", [TransientAuthError("401 revoked oauth token"), UsageLimitError("usage limit")]
)
def test_a_temporary_failure_throttles_and_the_run_never_starts(exc):
    r = _drive(refresh=exc)
    assert r.raised is exc
    assert r.seen is None
    r.throttle.assert_called_once()
    r.poison.assert_not_called()
    assert [c.args[2] for c in r.release.call_args_list] == [_OWNER]


@pytest.mark.parametrize("refresh", [True, False], ids=["refreshed", "fallback"])
@pytest.mark.parametrize(
    "reread",
    [None, _token(enabled=False), _token(status=CredentialStatus.ERROR)],
    ids=["deleted", "disabled", "quarantined"],
)
def test_a_credential_made_ineligible_during_the_refresh_never_reaches_a_run(refresh, reread):
    r = _drive(refresh=refresh, reread=reread)
    assert isinstance(r.raised, RuntimeError)
    assert "became ineligible during refresh" in str(r.raised)
    assert r.seen is None
    assert r.consumer._held_leases == {}
    # The lease is still released, by the lifecycle.
    assert [c.args[2] for c in r.release.call_args_list] == [_OWNER]


def test_the_claim_lease_is_short_and_not_renewed():
    consumer = Consumer(MagicMock(), ConsumerConfig(poll_interval=0.01), logging.getLogger("t"))
    assert consumer._credential_resolver._lease_ttl_seconds == CLAIM_LEASE_TTL_SECONDS == 120


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_refresh_timeout_fits_inside_the_claim_lease(harness):
    """The claim lease is not renewed while the refresh runs, so the refresh must end
    first, with the runner client's 30 s transport slack (``runner/client.py`` ``run``);
    the renewed fallback lease is the longest of the three."""
    module = importlib.import_module(f"agento.modules.{harness}.src.config")
    assert module._REFRESH_TIMEOUT_SECONDS + 30 < CLAIM_LEASE_TTL_SECONDS < _DEFAULT_LEASE_TTL_SECONDS


def test_every_in_tree_adapter_implements_the_protocol_signature():
    """Protocols do not check signatures; a mismatch would only surface as a TypeError in
    the fallback branch, silently costing every refresh."""
    import inspect

    from agento.framework.harness.protocols import WorkspaceAdapter

    expected = list(inspect.signature(WorkspaceAdapter.refresh_credentials).parameters)
    modules = Path("src/agento/modules")
    found = 0
    for path in sorted(modules.glob("*/src/config.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("WorkspaceAdapter"):
                method = next(
                    (n for n in node.body
                     if isinstance(n, ast.FunctionDef) and n.name == "refresh_credentials"),
                    None,
                )
                assert method is not None, f"{path}: {node.name} lacks refresh_credentials"
                assert [a.arg for a in method.args.args] == expected, path
                found += 1
    assert found >= 3
