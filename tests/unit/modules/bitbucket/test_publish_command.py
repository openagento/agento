"""run_lane loop behaviour — fan-out guard, skips, per-repo/per-PR isolation, client cleanup."""
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agento.modules.bitbucket.src.commands._loop import run_lane

_LOOP = "agento.modules.bitbucket.src.commands._loop"


def _view(view_id, code):
    return SimpleNamespace(id=view_id, code=code)


def _cfg(enabled="1", repos="api", account_uuid="{a}", workspace="acme", poll_top="20"):
    """Raw per-path config values, as ScopedConfigService.get() would return them.

    run_lane resolves ONLY non-secret paths via .get() (never the token), so the mock exposes .get —
    NOT get_module. A view with no config maps to None (all .get() return None ⇒ disabled ⇒ skipped).
    """
    return {
        "bitbucket/enabled": enabled,
        "bitbucket/bitbucket_workspace": workspace,
        "bitbucket/bitbucket_account_uuid": account_uuid,
        "bitbucket/repo_allowlist": repos,
        "bitbucket/poll_top": poll_top,
    }


def _run(
    *,
    views,
    cfg_by_view,
    identity_by_view=None,
    open_prs_resp=None,
    publish_results=None,
    agent_view_code=None,
):
    """Drive run_lane with all collaborators patched. Returns (published, mocks-dict)."""
    identity_by_view = identity_by_view or {}
    open_prs_resp = open_prs_resp if open_prs_resp is not None else {"pull_requests": [], "errors": []}
    logger = MagicMock()

    with ExitStack() as stack:
        mock_views = stack.enter_context(patch(f"{_LOOP}.get_active_agent_views", return_value=views))
        # The loop mints one capability per view; these tests pass a stub connection.
        stack.enter_context(
            patch(f"{_LOOP}.rest_capability", lambda *a, **k: nullcontext("cap"))
        )
        MockSCS = stack.enter_context(patch(f"{_LOOP}.ScopedConfigService"))
        stack.enter_context(patch(f"{_LOOP}.resolve_publish_priority", return_value=50))
        mock_lso = stack.enter_context(patch(f"{_LOOP}.load_scoped_db_overrides"))
        MockClient = stack.enter_context(patch(f"{_LOOP}.BitbucketToolboxClient"))
        MockPub = stack.enter_context(patch(f"{_LOOP}.BitbucketPublisher"))

        MockSCS.side_effect = lambda conn, scope, scope_id: SimpleNamespace(
            get=lambda path: (cfg_by_view.get(scope_id) or {}).get(path)
        )
        mock_lso.side_effect = lambda conn, scope, scope_id: identity_by_view.get(scope_id, {})
        client = MockClient.return_value
        client.open_prs.return_value = open_prs_resp
        pub = MockPub.return_value
        if publish_results is not None:
            pub.publish_pr.side_effect = publish_results
        else:
            pub.publish_pr.return_value = True

        published = run_lane(
            object(), object(), "http://tb:3001", logger,
            lane="comments", agent_view_code=agent_view_code,
        )
        return published, {"client": client, "pub": pub, "logger": logger, "mock_views": mock_views}


def test_single_default_view_is_processed():
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg()},
                        open_prs_resp={"pull_requests": [{"id": 1}, {"id": 2}], "errors": []})
    assert published == 2
    m["client"].open_prs.assert_called_once()
    m["client"].close.assert_called_once()


def test_disabled_view_skipped():
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg(enabled="0")})
    assert published == 0
    m["client"].open_prs.assert_not_called()
    # The client is built per view, AFTER the enabled/allow-list gates, so a skipped
    # view builds none — and there is nothing to close.
    m["client"].close.assert_not_called()


def test_empty_allowlist_skipped():
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg(repos="")})
    assert published == 0
    m["client"].open_prs.assert_not_called()


def test_unconfigured_view_skipped():
    # No config rows for the view ⇒ every .get() returns None ⇒ enabled falsey ⇒ skipped.
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: None})
    assert published == 0
    m["client"].open_prs.assert_not_called()


def test_multiview_default_only_view_skipped_scoped_view_processed():
    # R4-1: with >1 view, only a view with its OWN agent_view-scoped identity is processed.
    views = [_view(1, "dev"), _view(2, "ops")]
    cfg_by_view = {1: _cfg(), 2: _cfg()}
    identity = {
        1: {"bitbucket/bitbucket_account_uuid": ("{a}", False), "bitbucket/repo_allowlist": ("api", False)},
        # view 2: no agent_view-scoped identity → DEFAULT-only → skipped
    }
    published, m = _run(views=views, cfg_by_view=cfg_by_view, identity_by_view=identity,
                        open_prs_resp={"pull_requests": [{"id": 9}], "errors": []})
    assert published == 1
    m["client"].open_prs.assert_called_once_with(1, lane="comments", top=None)


def test_per_repo_errors_logged_and_run_continues():
    published, m = _run(
        views=[_view(1, "dev")], cfg_by_view={1: _cfg()},
        open_prs_resp={"pull_requests": [{"id": 1}], "errors": [{"repo": "x", "error": "HTTP 500"}]},
    )
    assert published == 1
    m["logger"].warning.assert_called()


def test_per_pr_exception_isolated():
    published, m = _run(
        views=[_view(1, "dev")], cfg_by_view={1: _cfg()},
        open_prs_resp={"pull_requests": [{"id": 1}, {"id": 2}], "errors": []},
        publish_results=[RuntimeError("boom"), True],
    )
    assert published == 1
    m["logger"].exception.assert_called_once()
    m["client"].close.assert_called_once()


def test_agent_view_filter_processes_only_matching_view():
    views = [_view(1, "dev"), _view(2, "ops")]
    cfg_by_view = {1: _cfg(), 2: _cfg()}
    identity = {
        2: {"bitbucket/bitbucket_account_uuid": ("{a}", False), "bitbucket/repo_allowlist": ("api", False)},
    }
    published, m = _run(
        views=views, cfg_by_view=cfg_by_view, identity_by_view=identity,
        open_prs_resp={"pull_requests": [{"id": 5}], "errors": []}, agent_view_code="ops",
    )
    assert published == 1
    m["client"].open_prs.assert_called_once_with(2, lane="comments", top=None)


def test_client_closed_even_when_open_prs_raises():
    logger = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_LOOP}.get_active_agent_views", return_value=[_view(1, "dev")]))
        # The loop mints one capability per view; these tests pass a stub connection.
        stack.enter_context(
            patch(f"{_LOOP}.rest_capability", lambda *a, **k: nullcontext("cap"))
        )
        MockSCS = stack.enter_context(patch(f"{_LOOP}.ScopedConfigService"))
        stack.enter_context(patch(f"{_LOOP}.resolve_publish_priority", return_value=50))
        stack.enter_context(patch(f"{_LOOP}.load_scoped_db_overrides", return_value={}))
        MockClient = stack.enter_context(patch(f"{_LOOP}.BitbucketToolboxClient"))
        stack.enter_context(patch(f"{_LOOP}.BitbucketPublisher"))
        MockSCS.side_effect = lambda conn, scope, scope_id: SimpleNamespace(get=lambda path: _cfg().get(path))
        MockClient.return_value.open_prs.side_effect = RuntimeError("toolbox down")

        published = run_lane(object(), object(), "http://tb:3001", logger, lane="comments")
        assert published == 0
        MockClient.return_value.close.assert_called_once()
        logger.exception.assert_called_once()
