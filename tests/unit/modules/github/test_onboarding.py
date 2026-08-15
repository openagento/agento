import logging
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.config_resolver import path_to_env_key
from agento.modules.github.src.env_guard import VIEW_SCOPED_ENV_KEYS
from agento.modules.github.src.onboarding import (
    GitHubOnboarding,
    _evaluate_completeness,
    _normalize_repo_entries,
)

_OB = "agento.modules.github.src.onboarding"

_ALL = (
    "github/github_owner",
    "github/github_login",
    "github/github_token",
    "github/repo_allowlist",
)
# token + login + repo_allowlist must be at the view's OWN agent_view scope; the owner may inherit.
_VIEW_SCOPED = ("github/github_token", "github/github_login", "github/repo_allowlist")
_DEFAULTABLE = ("github/github_owner",)
# Onboarding also seeds the agent_view's git commit identity so its commits link to the account.
_IDENTITY = ("agent_view/identity/git_author_email", "agent_view/identity/git_author_name")


def _row(scope, scope_id, path, value="x"):
    return {"scope": scope, "scope_id": scope_id, "path": path, "value": value}


def _view_complete_rows(view_id, *, owner_at_default=True):
    rows = [_row("agent_view", view_id, p) for p in _VIEW_SCOPED]
    scope = "default" if owner_at_default else "agent_view"
    sid = 0 if owner_at_default else view_id
    rows += [_row(scope, sid, p) for p in _DEFAULTABLE]
    return rows


def test_describe_human_readable():
    assert "GitHub" in GitHubOnboarding().describe()


# --- _evaluate_completeness -----------------------------------------------------------------------

def test_zero_views_not_complete():
    assert _evaluate_completeness([], [_row("default", 0, p) for p in _ALL]) is False


def test_single_view_fully_agent_view_scoped_is_complete():
    assert _evaluate_completeness([(1, 10)], _view_complete_rows(1, owner_at_default=False)) is True


def test_token_at_default_scope_not_complete():
    # The token must be agent_view-scoped so bootstrap (DEFAULT-only) never decrypts it in cron.
    rows = [
        _row("agent_view", 1, "github/github_login"),
        _row("agent_view", 1, "github/repo_allowlist"),
        _row("default", 0, "github/github_owner"),
        _row("default", 0, "github/github_token"),
    ]
    assert _evaluate_completeness([(1, 10)], rows) is False


def test_owner_may_inherit_from_default():
    assert _evaluate_completeness([(1, 10)], _view_complete_rows(1)) is True


def test_empty_values_do_not_count_as_present():
    rows = _view_complete_rows(1)
    for r in rows:
        if r["path"] == "github/repo_allowlist":
            r["value"] = ""
    assert _evaluate_completeness([(1, 10)], rows) is False


def test_two_views_one_complete_is_complete():
    assert _evaluate_completeness([(1, 10), (2, 10)], _view_complete_rows(2)) is True


# --- is_complete(): ENV awareness ------------------------------------------------------------------

def _conn_for_is_complete(view_rows, cfg_rows):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [view_rows, cfg_rows]
    return conn


def test_is_complete_false_when_only_env_supplies_the_token(monkeypatch):
    # ENV is global, never agent_view-scoped — a token that only exists there is not a usable setup.
    for path in _ALL:
        monkeypatch.setenv(path_to_env_key(path), "x")
    conn = _conn_for_is_complete([{"id": 1, "workspace_id": 10}], [])
    assert GitHubOnboarding().is_complete(conn) is False


@pytest.mark.parametrize("key", VIEW_SCOPED_ENV_KEYS)
def test_a_global_env_override_beats_a_perfectly_scoped_db(monkeypatch, capsys, key):
    """The exact rows of the complete case flip to incomplete: the ENV override wins in resolve_field
    for EVERY view, so however right the DB looks, this deployment resolves one shared identity."""
    cfg_rows = _view_complete_rows(1, owner_at_default=False)
    assert _evaluate_completeness([(1, 10)], cfg_rows) is True

    monkeypatch.setenv(key, "x")
    conn = _conn_for_is_complete([{"id": 1, "workspace_id": 10}], cfg_rows)
    assert GitHubOnboarding().is_complete(conn) is False
    assert key in capsys.readouterr().out


# --- run(): verify-before-save ---------------------------------------------------------------------

def _drive_run(
    *, verify_result=None, verify_raises=False, views, inputs, token, selects, toolbox_url="http://tb:3001",
):
    """Run onboarding with all I/O patched. `inputs` feeds input() in order: owner, repos, email."""
    conn = MagicMock()
    calls = {"set": [], "commit": 0}
    conn.commit.side_effect = lambda: calls.__setitem__("commit", calls["commit"] + 1)

    client = MagicMock()
    if verify_raises:
        client.verify.side_effect = RuntimeError("toolbox down")
    else:
        client.verify.return_value = verify_result

    select_feed = iter(selects)
    input_feed = iter(inputs)

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_OB}.GitHubToolboxClient", return_value=client))
        stack.enter_context(patch(
            f"{_OB}.scoped_config_set",
            side_effect=lambda conn, path, value, **k: calls["set"].append(
                (path, value, k.get("encrypted", False), k.get("scope"), k.get("scope_id"))
            ),
        ))
        stack.enter_context(patch(
            "agento.framework.bootstrap.get_module_config", return_value={"toolbox/url": toolbox_url},
        ))
        stack.enter_context(patch("agento.framework.cli.terminal.select", side_effect=lambda *a, **k: next(select_feed)))
        stack.enter_context(patch("agento.framework.workspace.get_active_agent_views", return_value=views))
        stack.enter_context(patch("builtins.input", side_effect=lambda *a, **k: next(input_feed)))
        stack.enter_context(patch("getpass.getpass", side_effect=lambda *a, **k: token))

        GitHubOnboarding().run(conn, {}, logging.getLogger("t"))
    return calls, client


_ONE_VIEW = [SimpleNamespace(id=1, code="dev", label="Dev")]
_OK = {"ok": True, "login": "agent-bot", "id": 42}


def test_run_without_a_toolbox_url_writes_nothing():
    calls, client = _drive_run(
        verify_result=_OK, views=_ONE_VIEW, inputs=[], token="tok", selects=[], toolbox_url="",
    )
    assert calls["set"] == [] and calls["commit"] == 0
    client.verify.assert_not_called()


def test_run_without_an_active_agent_view_writes_nothing():
    calls, client = _drive_run(verify_result=_OK, views=[], inputs=[], token="tok", selects=[])
    assert calls["set"] == [] and calls["commit"] == 0
    client.verify.assert_not_called()


def test_run_aborting_after_a_failed_verify_writes_nothing():
    calls, client = _drive_run(
        verify_result={"ok": False, "detail": "401 unauthorized"},
        views=_ONE_VIEW, inputs=["acme", "api"], token="tok", selects=[1],  # Abort
    )
    assert calls["set"] == [] and calls["commit"] == 0
    client.close.assert_called_once()


def test_run_aborting_after_an_unreachable_toolbox_writes_nothing():
    calls, _ = _drive_run(
        verify_raises=True, views=_ONE_VIEW, inputs=["acme", "api"], token="tok", selects=[1],
    )
    assert calls["set"] == [] and calls["commit"] == 0


def test_run_on_success_writes_the_whole_set_once_at_the_view_scope():
    calls, client = _drive_run(
        verify_result=_OK, views=_ONE_VIEW, inputs=["acme", "api,web", ""], token="ghp_x", selects=[],
    )
    saved = {path: (value, enc, scope, sid) for path, value, enc, scope, sid in calls["set"]}
    assert set(saved) == set(_ALL) | set(_IDENTITY)
    for path, (_value, _enc, scope, sid) in saved.items():
        assert (scope, sid) == ("agent_view", 1), path
    assert saved["github/github_owner"][:2] == ("acme", False)
    assert saved["github/github_login"][:2] == ("agent-bot", False)  # captured from verify, never typed
    assert saved["github/repo_allowlist"][:2] == ("api,web", False)
    assert saved["github/github_token"][0] == "ghp_x"
    assert saved["github/github_token"][1] is True  # encrypted
    assert calls["commit"] == 1
    client.close.assert_called_once()


def test_run_defaults_the_git_author_email_to_the_noreply_address():
    calls, _ = _drive_run(
        verify_result=_OK, views=_ONE_VIEW, inputs=["acme", "api", ""], token="tok", selects=[],
    )
    saved = {path: value for path, value, *_ in calls["set"]}
    # The noreply form always links the commit to the account — no verified address needed.
    assert saved["agent_view/identity/git_author_email"] == "42+agent-bot@users.noreply.github.com"
    assert saved["agent_view/identity/git_author_name"] == "agent-bot"


def test_run_keeps_an_explicitly_entered_git_author_email():
    calls, _ = _drive_run(
        verify_result=_OK, views=_ONE_VIEW, inputs=["acme", "api", "bot@example.com"], token="tok", selects=[],
    )
    saved = {path: value for path, value, *_ in calls["set"]}
    assert saved["agent_view/identity/git_author_email"] == "bot@example.com"


def test_run_writes_at_the_selected_view_when_several_are_active():
    calls, _ = _drive_run(
        verify_result=_OK,
        views=[SimpleNamespace(id=1, code="dev", label="Dev"), SimpleNamespace(id=2, code="ops", label="Ops")],
        inputs=["acme", "api", ""], token="tok", selects=[1],  # pick ops (id=2)
    )
    for path, _v, _e, scope, sid in calls["set"]:
        assert (scope, sid) == ("agent_view", 2), path
    assert calls["commit"] == 1


# --- repo allow-list normalization ------------------------------------------------------------------
# The allow-list is matched EXACTLY against a tool's `repo` argument and the URL is built as
# repos/{owner}/{repo}, so an "owner/repo" entry can only produce a confusing miss or a 404. The
# normalization lives here, at the input, never in the runtime allow-list check.

def test_normalize_repo_entries_trims_dedupes_and_keeps_order():
    assert _normalize_repo_entries(" api , web ,api, ", "acme") == ("api,web", None)


def test_normalize_repo_entries_strips_the_matching_owner_prefix():
    # Same repository, written the other way — accepted and rewritten to the bare form.
    assert _normalize_repo_entries("acme/api,web", "acme") == ("api,web", None)
    assert _normalize_repo_entries("ACME/api", "acme") == ("api", None)


def test_normalize_repo_entries_refuses_a_foreign_owner_prefix():
    # The owner is fixed by config; an entry cannot widen it here.
    normalized, error = _normalize_repo_entries("other/api", "acme")
    assert normalized == ""
    assert "other/api" in error and "bare repo name" in error


@pytest.mark.parametrize("raw", ["acme/", "acme/api/extra", " , "])
def test_normalize_repo_entries_refuses_malformed_input(raw):
    normalized, error = _normalize_repo_entries(raw, "acme")
    assert normalized == "" and error


def test_run_saves_the_normalized_allow_list():
    calls, _ = _drive_run(
        verify_result=_OK, views=_ONE_VIEW, inputs=["acme", "acme/api, web", ""], token="tok", selects=[],
    )
    saved = {path: value for path, value, *_ in calls["set"]}
    assert saved["github/repo_allowlist"] == "api,web"


def test_run_re_prompts_on_a_bad_allow_list_without_verifying():
    calls, client = _drive_run(
        verify_result=_OK, views=_ONE_VIEW,
        inputs=["acme", "other/api"], token="tok", selects=[1],  # Abort at the retry prompt
    )
    # A bad repo list is caught BEFORE the token leaves the process.
    client.verify.assert_not_called()
    assert calls["set"] == [] and calls["commit"] == 0
