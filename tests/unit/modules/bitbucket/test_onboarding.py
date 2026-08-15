import logging
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.config_resolver import path_to_env_key
from agento.modules.bitbucket.src.onboarding import (
    BitbucketOnboarding,
    _evaluate_completeness,
    _normalize_repo_entries,
)

_OB = "agento.modules.bitbucket.src.onboarding"

_ALL = (
    "bitbucket/bitbucket_workspace",
    "bitbucket/bitbucket_email",
    "bitbucket/bitbucket_api_token",
    "bitbucket/bitbucket_account_uuid",
    "bitbucket/repo_allowlist",
)


def _row(scope, scope_id, path, value="x"):
    return {"scope": scope, "scope_id": scope_id, "path": path, "value": value}


# token + account_uuid + repo_allowlist must be at the view's OWN agent_view scope; workspace + email
# may inherit DEFAULT.
_VIEW_SCOPED = (
    "bitbucket/bitbucket_api_token",
    "bitbucket/bitbucket_account_uuid",
    "bitbucket/repo_allowlist",
)
_DEFAULTABLE = ("bitbucket/bitbucket_workspace", "bitbucket/bitbucket_email")
# Onboarding also seeds the agent_view's git commit identity so its commits link to the account.
_IDENTITY = ("agent_view/identity/git_author_email", "agent_view/identity/git_author_name")


def _view_complete_rows(view_id, *, defaultable_at_default=True):
    """Rows that make `view_id` complete: the 3 view-scoped paths at its agent_view scope, and the 2
    non-secret paths at DEFAULT (or also agent_view if defaultable_at_default=False)."""
    rows = [_row("agent_view", view_id, p) for p in _VIEW_SCOPED]
    scope = "default" if defaultable_at_default else "agent_view"
    sid = 0 if defaultable_at_default else view_id
    rows += [_row(scope, sid, p) for p in _DEFAULTABLE]
    return rows


def test_describe_human_readable():
    assert "Bitbucket" in BitbucketOnboarding().describe()


# --- _evaluate_completeness (token/account/repo must be agent_view-scoped) -------------------------

def test_single_view_fully_agent_view_scoped_is_complete():
    assert _evaluate_completeness([(1, 10)], _view_complete_rows(1, defaultable_at_default=False)) is True


def test_single_view_workspace_email_may_inherit_default():
    # token/account/repo at agent_view scope; workspace/email inherited from DEFAULT ⇒ still complete.
    assert _evaluate_completeness([(1, 10)], _view_complete_rows(1)) is True


def test_single_view_all_at_default_not_complete():
    # The token (and account/repo) at DEFAULT is NOT allowed — the token must be agent_view-scoped so
    # bootstrap never decrypts it in cron.
    assert _evaluate_completeness([(1, 10)], [_row("default", 0, p) for p in _ALL]) is False


def test_single_view_token_inherited_from_default_not_complete():
    # account/repo agent_view-scoped, but the token only at DEFAULT ⇒ NOT complete.
    rows = [
        _row("agent_view", 1, "bitbucket/bitbucket_account_uuid"),
        _row("agent_view", 1, "bitbucket/repo_allowlist"),
        _row("default", 0, "bitbucket/bitbucket_workspace"),
        _row("default", 0, "bitbucket/bitbucket_email"),
        _row("default", 0, "bitbucket/bitbucket_api_token"),
    ]
    assert _evaluate_completeness([(1, 10)], rows) is False


def test_zero_views_not_complete():
    assert _evaluate_completeness([], [_row("default", 0, p) for p in _ALL]) is False


def test_single_view_missing_field_not_complete():
    rows = [r for r in _view_complete_rows(1) if r["path"] != "bitbucket/repo_allowlist"]
    assert _evaluate_completeness([(1, 10)], rows) is False


def test_single_view_empty_value_not_complete():
    rows = _view_complete_rows(1)
    for r in rows:
        if r["path"] == "bitbucket/repo_allowlist":
            r["value"] = ""
    assert _evaluate_completeness([(1, 10)], rows) is False


def test_multiview_all_at_default_not_complete():
    assert _evaluate_completeness([(1, 10), (2, 10)], [_row("default", 0, p) for p in _ALL]) is False


def test_multiview_with_one_fully_scoped_view_is_complete():
    # view 1 fully agent_view-scoped (token/account/repo) + workspace/email at default ⇒ complete.
    assert _evaluate_completeness([(1, 10), (2, 10)], _view_complete_rows(1)) is True


def test_multiview_scattered_across_unrelated_scopes_not_complete():
    rows = [
        # view 1 has workspace + identity but no email/token
        _row("agent_view", 1, "bitbucket/bitbucket_workspace"),
        _row("agent_view", 1, "bitbucket/bitbucket_account_uuid"),
        _row("agent_view", 1, "bitbucket/repo_allowlist"),
        # view 2 has email + token but no workspace/identity
        _row("agent_view", 2, "bitbucket/bitbucket_email"),
        _row("agent_view", 2, "bitbucket/bitbucket_api_token"),
    ]
    assert _evaluate_completeness([(1, 10), (2, 10)], rows) is False


# --- is_complete(): ENV awareness (effective resolution is ENV -> DB -> config.json) ---------------

def _conn_for_is_complete(view_rows, cfg_rows):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [view_rows, cfg_rows]
    return conn


def test_is_complete_env_provides_nonsecret_fields(monkeypatch):
    # workspace + email via ENV; the agent_view-scoped secrets in DB ⇒ complete (ENV fallback works for
    # the non-secret fields).
    for path in _DEFAULTABLE:
        monkeypatch.setenv(path_to_env_key(path), "x")
    cfg_rows = [_row("agent_view", 1, p) for p in _VIEW_SCOPED]
    conn = _conn_for_is_complete([{"id": 1, "workspace_id": 10}], cfg_rows)
    assert BitbucketOnboarding().is_complete(conn) is True


def test_is_complete_env_global_token_not_agent_view_scoped(monkeypatch):
    # Everything via ENV (global) — the token is NOT at agent_view scope ⇒ NOT complete (the token must
    # never be resolvable off agent_view scope).
    for path in _ALL:
        monkeypatch.setenv(path_to_env_key(path), "x")
    conn = _conn_for_is_complete([{"id": 1, "workspace_id": 10}], [])
    assert BitbucketOnboarding().is_complete(conn) is False


# --- run(): verify-before-save --------------------------------------------------------------------

def _drive_run(*, verify_result=None, verify_raises=False, views, inputs, token, selects):
    """Run onboarding with all I/O patched. selects is a list feeding terminal.select calls."""
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
        stack.enter_context(patch(f"{_OB}.BitbucketToolboxClient", return_value=client))
        stack.enter_context(patch(
            f"{_OB}.scoped_config_set",
            side_effect=lambda conn, path, value, **k: calls["set"].append(
                (path, value, k.get("encrypted", False), k.get("scope"), k.get("scope_id"))
            ),
        ))
        stack.enter_context(patch("agento.framework.bootstrap.get_module_config", return_value={"toolbox/url": "http://tb:3001"}))
        stack.enter_context(patch("agento.framework.cli.terminal.select", side_effect=lambda *a, **k: next(select_feed)))
        stack.enter_context(patch("agento.framework.workspace.get_active_agent_views", return_value=views))
        stack.enter_context(patch("builtins.input", side_effect=lambda *a, **k: next(input_feed)))
        stack.enter_context(patch("getpass.getpass", side_effect=lambda *a, **k: token))

        BitbucketOnboarding().run(conn, {}, logging.getLogger("t"))
    return calls, client


def test_run_verify_fail_saves_nothing():
    calls, client = _drive_run(
        verify_result={"ok": False, "detail": "401 unauthorized"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "e@x.com", "api"],
        token="tok",
        selects=[1],  # Abort
    )
    assert calls["set"] == []
    assert calls["commit"] == 0
    client.close.assert_called_once()


def test_run_toolbox_unreachable_saves_nothing():
    calls, _ = _drive_run(
        verify_raises=True,
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "e@x.com", "api"],
        token="tok",
        selects=[1],  # Abort
    )
    assert calls["set"] == []
    assert calls["commit"] == 0


def test_run_verify_ok_writes_atomically_at_sole_agent_view_scope():
    calls, client = _drive_run(
        verify_result={"ok": True, "account_uuid": "{detected-uuid}", "username": "agent"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],  # single view ⇒ auto-select, agent_view scope
        inputs=["acme", "e@x.com", "api,web"],
        token="tok",
        selects=[],  # no select needed (single view, verify ok)
    )
    saved = {path: (value, enc, scope, sid) for path, value, enc, scope, sid in calls["set"]}
    # ALL fields written at the sole view's agent_view scope (never DEFAULT).
    for path, (_value, _enc, scope, sid) in saved.items():
        assert scope == "agent_view" and sid == 1, path
    assert saved["bitbucket/bitbucket_workspace"][:2] == ("acme", False)
    assert saved["bitbucket/bitbucket_account_uuid"][:2] == ("{detected-uuid}", False)  # captured from verify
    assert saved["bitbucket/repo_allowlist"][:2] == ("api,web", False)
    assert saved["bitbucket/bitbucket_api_token"][0] == "tok"
    assert saved["bitbucket/bitbucket_api_token"][1] is True  # encrypted
    assert calls["commit"] == 1
    client.close.assert_called_once()


def test_run_multiview_writes_at_selected_agent_view_scope():
    calls, _ = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": "agent"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev"), SimpleNamespace(id=2, code="ops", label="Ops")],
        inputs=["acme", "e@x.com", "api"],
        token="tok",
        selects=[1],  # pick the second view (ops, id=2)
    )
    saved = {path: (scope, sid) for path, _v, _e, scope, sid in calls["set"]}
    assert set(saved) == set(_ALL) | set(_IDENTITY)  # bitbucket config + seeded git identity
    for path, (scope, sid) in saved.items():
        assert scope == "agent_view" and sid == 2, path  # the selected view
    assert calls["commit"] == 1


def test_run_seeds_git_author_identity_from_verified_account():
    # Onboarding seeds the agent_view's git commit identity: email = the entered Atlassian email,
    # name = the verify-result username — so the agent's commits link to this Bitbucket account.
    calls, _ = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": "Agent Smith"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "agent@example.com", "api"],
        token="tok",
        selects=[],
    )
    saved = {path: (value, scope, sid) for path, value, _e, scope, sid in calls["set"]}
    assert saved["agent_view/identity/git_author_email"] == ("agent@example.com", "agent_view", 1)
    assert saved["agent_view/identity/git_author_name"] == ("Agent Smith", "agent_view", 1)


def test_run_skips_git_author_name_when_username_empty():
    # No nickname/display name/account_id ⇒ skip the name seed (email alone still links the commit).
    calls, _ = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": ""},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "agent@example.com", "api"],
        token="tok",
        selects=[],
    )
    saved_paths = {path for path, *_ in calls["set"]}
    assert "agent_view/identity/git_author_email" in saved_paths
    assert "agent_view/identity/git_author_name" not in saved_paths


def test_run_no_active_views_saves_nothing():
    calls, client = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": "agent"},
        views=[],  # no active agent_view ⇒ refuse before prompting for credentials
        inputs=[],
        token="tok",
        selects=[],
    )
    assert calls["set"] == []
    assert calls["commit"] == 0
    client.verify.assert_not_called()


# --- repo allow-list normalization ------------------------------------------------------------------
# The allow-list is matched EXACTLY against a tool's `repo` argument and the URL is built as
# repositories/{workspace}/{repo}, so a "workspace/repo" entry can only produce a confusing miss or a
# 404. The normalization lives here, at the input, never in the runtime allow-list check.

def test_normalize_repo_entries_trims_dedupes_and_keeps_order():
    assert _normalize_repo_entries(" api , web ,api, ", "acme") == ("api,web", None)


def test_normalize_repo_entries_strips_the_matching_workspace_prefix():
    # Same repository, written the other way — accepted and rewritten to the bare form.
    assert _normalize_repo_entries("acme/api,web", "acme") == ("api,web", None)
    assert _normalize_repo_entries("ACME/api", "acme") == ("api", None)


def test_normalize_repo_entries_refuses_a_foreign_workspace_prefix():
    # The workspace is fixed by config; an entry cannot widen it here.
    normalized, error = _normalize_repo_entries("other/api", "acme")
    assert normalized == ""
    assert "other/api" in error and "bare repo slug" in error


@pytest.mark.parametrize("raw", ["acme/", "acme/api/extra", " , "])
def test_normalize_repo_entries_refuses_malformed_input(raw):
    normalized, error = _normalize_repo_entries(raw, "acme")
    assert normalized == "" and error


def test_run_saves_the_normalized_allow_list():
    calls, _ = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": "agent"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "e@x.com", "acme/api, web"],
        token="tok",
        selects=[],
    )
    saved = {path: value for path, value, *_ in calls["set"]}
    assert saved["bitbucket/repo_allowlist"] == "api,web"


def test_run_re_prompts_on_a_bad_allow_list_without_verifying():
    calls, client = _drive_run(
        verify_result={"ok": True, "account_uuid": "{a}", "username": "agent"},
        views=[SimpleNamespace(id=1, code="dev", label="Dev")],
        inputs=["acme", "e@x.com", "other/api"],
        token="tok",
        selects=[1],  # Abort at the retry prompt
    )
    # A bad repo list is caught BEFORE the credentials leave the process.
    client.verify.assert_not_called()
    assert calls["set"] == [] and calls["commit"] == 0
