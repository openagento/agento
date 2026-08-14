"""run_lane loop behaviour — ENV guard, fan-out guard, skips, per-repo/per-PR isolation, cleanup."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.github.src.commands._loop import run_lane
from agento.modules.github.src.env_guard import VIEW_SCOPED_ENV_KEYS

_LOOP = "agento.modules.github.src.commands._loop"


def _view(view_id, code):
    return SimpleNamespace(id=view_id, code=code)


def _cfg(enabled="1", repos="api", login="agent-bot", owner="acme", poll_top="20"):
    """Raw per-path config values, as ScopedConfigService.get() would return them.

    run_lane resolves ONLY non-secret paths via .get() (never the token), so the mock exposes .get —
    NOT get_module. A view with no config maps to None (all .get() return None ⇒ disabled ⇒ skipped).
    """
    return {
        "github/enabled": enabled,
        "github/github_owner": owner,
        "github/github_login": login,
        "github/repo_allowlist": repos,
        "github/poll_top": poll_top,
    }


def _run(
    *,
    views,
    cfg_by_view,
    identity_by_view=None,
    open_prs_resp=None,
    publish_results=None,
    agent_view_code=None,
    top_override=None,
):
    """Drive run_lane with all collaborators patched. Returns (published, mocks-dict)."""
    identity_by_view = identity_by_view or {}
    open_prs_resp = open_prs_resp if open_prs_resp is not None else {"pull_requests": [], "errors": []}
    logger = MagicMock()
    asked_paths: list[str] = []

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_LOOP}.get_active_agent_views", return_value=views))
        MockSCS = stack.enter_context(patch(f"{_LOOP}.ScopedConfigService"))
        stack.enter_context(patch(f"{_LOOP}.resolve_publish_priority", return_value=50))
        mock_lso = stack.enter_context(patch(f"{_LOOP}.load_scoped_db_overrides"))
        MockClient = stack.enter_context(patch(f"{_LOOP}.GitHubToolboxClient"))
        MockPub = stack.enter_context(patch(f"{_LOOP}.GitHubPublisher"))

        def _service(conn, scope, scope_id):
            def _get(path):
                asked_paths.append(path)
                return (cfg_by_view.get(scope_id) or {}).get(path)

            return SimpleNamespace(get=_get)

        MockSCS.side_effect = _service
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
            lane="comments", agent_view_code=agent_view_code, top_override=top_override,
        )
        return published, {
            "client": client, "pub": pub, "logger": logger,
            "client_cls": MockClient, "paths": asked_paths,
        }


def test_single_default_view_is_processed():
    """A single-view deployment cannot fan out, so the view-scoped identity guard does not apply."""
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg()},
                        open_prs_resp={"pull_requests": [{"id": 1}, {"id": 2}], "errors": []})
    assert published == 2
    m["client"].open_prs.assert_called_once()
    m["client"].close.assert_called_once()


def test_disabled_view_skipped():
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg(enabled="0")})
    assert published == 0
    m["client"].open_prs.assert_not_called()
    m["client"].close.assert_called_once()


def test_empty_allowlist_skipped():
    published, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg(repos="")})
    assert published == 0
    m["client"].open_prs.assert_not_called()


def test_multiview_default_only_view_skipped_scoped_view_processed():
    views = [_view(1, "dev"), _view(2, "ops")]
    identity = {
        1: {"github/github_login": ("agent-bot", False), "github/repo_allowlist": ("api", False)},
        # view 2: no agent_view-scoped identity → DEFAULT-only → skipped
    }
    published, m = _run(views=views, cfg_by_view={1: _cfg(), 2: _cfg()}, identity_by_view=identity,
                        open_prs_resp={"pull_requests": [{"id": 9}], "errors": []})
    assert published == 1
    m["client"].open_prs.assert_called_once_with(1, lane="comments", top=None)
    m["logger"].info.assert_called()


def test_agent_view_filter_processes_only_matching_view():
    views = [_view(1, "dev"), _view(2, "ops")]
    identity = {2: {"github/github_login": ("agent-bot", False), "github/repo_allowlist": ("api", False)}}
    published, m = _run(views=views, cfg_by_view={1: _cfg(), 2: _cfg()}, identity_by_view=identity,
                        open_prs_resp={"pull_requests": [{"id": 5}], "errors": []}, agent_view_code="ops")
    assert published == 1
    m["client"].open_prs.assert_called_once_with(2, lane="comments", top=None)


def test_top_override_is_forwarded():
    _, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg()}, top_override=5)
    m["client"].open_prs.assert_called_once_with(1, lane="comments", top=5)


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


def test_per_view_exception_isolated_and_remaining_views_run():
    views = [_view(1, "dev"), _view(2, "ops")]
    identity = {
        1: {"github/github_login": ("agent-bot", False), "github/repo_allowlist": ("api", False)},
        2: {"github/github_login": ("agent-bot", False), "github/repo_allowlist": ("api", False)},
    }
    logger = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_LOOP}.get_active_agent_views", return_value=views))
        MockSCS = stack.enter_context(patch(f"{_LOOP}.ScopedConfigService"))
        stack.enter_context(patch(f"{_LOOP}.resolve_publish_priority", return_value=50))
        stack.enter_context(patch(f"{_LOOP}.load_scoped_db_overrides",
                                  side_effect=lambda conn, scope, sid: identity[sid]))
        MockClient = stack.enter_context(patch(f"{_LOOP}.GitHubToolboxClient"))
        MockPub = stack.enter_context(patch(f"{_LOOP}.GitHubPublisher"))
        MockSCS.side_effect = lambda conn, scope, sid: SimpleNamespace(get=lambda path: _cfg().get(path))
        MockClient.return_value.open_prs.side_effect = [
            RuntimeError("toolbox down"), {"pull_requests": [{"id": 3}], "errors": []},
        ]
        MockPub.return_value.publish_pr.return_value = True

        published = run_lane(object(), object(), "http://tb:3001", logger, lane="comments")
    assert published == 1
    logger.exception.assert_called_once()
    MockClient.return_value.close.assert_called_once()


def test_the_token_path_is_never_resolved_by_the_publisher():
    """Token confinement: only the five non-secret paths, and never get_module()."""
    _, m = _run(views=[_view(1, "dev")], cfg_by_view={1: _cfg()})
    assert set(m["paths"]) == {
        "github/enabled", "github/github_owner", "github/github_login",
        "github/repo_allowlist", "github/poll_top",
    }
    assert "github/github_token" not in m["paths"]


@pytest.mark.parametrize("key", VIEW_SCOPED_ENV_KEYS)
def test_a_global_env_override_refuses_the_whole_lane(monkeypatch, key):
    """A global ENV override wins in resolve_field for EVERY view — refuse before any toolbox contact."""
    monkeypatch.setenv(key, "x")
    logger = MagicMock()
    with patch(f"{_LOOP}.GitHubToolboxClient") as MockClient, \
            patch(f"{_LOOP}.get_active_agent_views") as mock_views:
        assert run_lane(object(), object(), "http://tb:3001", logger, lane="comments") == 0
    MockClient.assert_not_called()
    mock_views.assert_not_called()
    assert key in logger.error.call_args.args[1]
