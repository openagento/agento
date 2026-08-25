import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from agento.framework.scoped_config import Scope
from agento.modules.outlook.src.onboarding import (
    OutlookOnboarding,
    _conflicting_mailbox_scopes,
    _pem_has_cert_and_key,
    _print_next_steps,
    _read_gate_disabled_scopes,
    _read_pem_block,
    _warn_read_gate_disabled,
)
from agento.modules.outlook.src.toolbox_client import OutlookToolboxClient

_VALID_PEM_LINES = [
    "-----BEGIN CERTIFICATE-----",
    "MIIBcert",
    "-----END CERTIFICATE-----",
    "-----BEGIN PRIVATE KEY-----",
    "MIIEkey",
    "-----END PRIVATE KEY-----",
]


def _conn_for_is_complete(present_paths, has_mailbox=True, views=((1, 10),)):
    """Mock a conn for is_complete.

    Completeness is checked as ONE chain per active agent_view: identity at DEFAULT scope
    (fetchall), then — per view — the auth material at ITS workspace scope (fetchone) and a
    mailbox at ITS agent_view scope or default (fetchone). ``views`` is a list of
    ``(view_id, workspace_id)`` pairs; ``present_paths``/``has_mailbox`` describe what the
    FIRST view resolves, so a test states which paths exist and nothing else.
    """
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    present = set(present_paths)
    state = {"last_sql": "", "identity_read": False}

    def _execute(sql, params=()):
        state["last_sql"] = sql
        state["last_params"] = params
        return None

    def _fetchone():
        # The mailbox path is a bound PARAMETER, so it never appears in the SQL text.
        if "outlook/outlook_mailbox_user_id" in tuple(state.get("last_params") or ()):
            return {"1": 1} if has_mailbox else None
        # the workspace-scope auth query
        return (
            {"1": 1}
            if present & {"outlook/outlook_client_secret", "outlook/outlook_cert_pem"}
            else None
        )

    def _fetchall():
        # The identity query comes first; every later fetchall is get_active_agent_views.
        if not state["identity_read"]:
            state["identity_read"] = True
            return [
                {"path": p}
                for p in present_paths
                if p in ("outlook/outlook_tenant_id", "outlook/outlook_client_id")
            ]
        return [
            {
                "id": vid, "workspace_id": wid, "code": f"v{vid}", "label": f"v{vid}",
                "is_active": 1, "created_at": None, "updated_at": None,
            }
            for vid, wid in views
        ]

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    return conn


def test_describe_is_human_readable():
    assert "Outlook" in OutlookOnboarding().describe()


def test_is_complete_true_with_client_secret_auth():
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
        "outlook/outlook_client_secret",
    ], has_mailbox=True)
    assert OutlookOnboarding().is_complete(conn) is True


def test_is_complete_true_with_certificate_auth():
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
        "outlook/outlook_cert_pem",
    ], has_mailbox=True)
    assert OutlookOnboarding().is_complete(conn) is True


def test_is_complete_true_when_mailbox_exists_at_any_scope():
    # identity + auth at default, mailbox present somewhere (default OR agent_view scope).
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
        "outlook/outlook_client_secret",
    ], has_mailbox=True)
    assert OutlookOnboarding().is_complete(conn) is True


def test_is_complete_false_without_any_mailbox():
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
        "outlook/outlook_client_secret",
    ], has_mailbox=False)
    assert OutlookOnboarding().is_complete(conn) is False


def test_is_complete_false_when_no_auth_secret_or_cert():
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
    ], has_mailbox=True)
    assert OutlookOnboarding().is_complete(conn) is False


def test_is_complete_false_when_missing_identity_keys():
    conn = _conn_for_is_complete(["outlook/outlook_tenant_id"], has_mailbox=True)
    assert OutlookOnboarding().is_complete(conn) is False


def test_is_complete_mailbox_query_accepts_the_view_scope_and_the_default_scope():
    """The mailbox may sit at the VIEW's own scope or at default — but not at another view's.

    A query restricted to scope='default' would miss a multi-view deployment; an unrestricted
    one would accept a mailbox bound to a different view than the one holding the credential,
    which is the split this check exists to reject.
    """
    conn = _conn_for_is_complete([
        "outlook/outlook_tenant_id",
        "outlook/outlook_client_id",
        "outlook/outlook_client_secret",
    ], has_mailbox=True, views=((5, 7),))
    cur = conn.cursor.return_value.__enter__.return_value
    OutlookOnboarding().is_complete(conn)
    # 0 = identity at default scope, 1 = active views, 2 = auth at the view's workspace,
    # 3 = the mailbox at the view's own scope or default.
    mailbox_sql, mailbox_params = cur.execute.call_args_list[3].args
    assert "outlook/outlook_mailbox_user_id" in mailbox_params
    assert 5 in mailbox_params
    assert "agent_view" in mailbox_sql and "default" in mailbox_sql


def test_is_complete_false_when_the_auth_and_the_mailbox_belong_to_different_views():
    """The credential is at view A's workspace, the mailbox at view B — no coherent chain."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    state = {"identity_read": False, "params": ()}

    def _execute(sql, params=()):
        state["params"] = params
        return None

    def _fetchall():
        if not state["identity_read"]:
            state["identity_read"] = True
            return [{"path": "outlook/outlook_tenant_id"}, {"path": "outlook/outlook_client_id"}]
        return [
            {"id": vid, "workspace_id": wid, "code": f"v{vid}", "label": "x",
             "is_active": 1, "created_at": None, "updated_at": None}
            for vid, wid in ((1, 10), (2, 20))
        ]

    def _fetchone():
        params = tuple(state["params"])
        if "outlook/outlook_mailbox_user_id" in params:
            return {"1": 1} if 2 in params else None      # mailbox only for view 2
        return {"1": 1} if 10 in params else None         # auth only for view 1's workspace

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone
    assert OutlookOnboarding().is_complete(conn) is False


# --- PEM reader / validation helpers -------------------------------------------------------------

def test_read_pem_block_joins_lines_until_END(monkeypatch):
    feed = iter([*_VALID_PEM_LINES, "END", "ignored-after"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(feed))
    pem = _read_pem_block("paste:")
    assert pem == "\n".join(_VALID_PEM_LINES)
    assert "ignored-after" not in pem


def test_read_pem_block_stops_at_EOF(monkeypatch):
    feed = iter(_VALID_PEM_LINES)

    def _input(*a, **k):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", _input)
    assert _read_pem_block("paste:") == "\n".join(_VALID_PEM_LINES)


def test_pem_has_cert_and_key_requires_both_markers():
    assert _pem_has_cert_and_key("\n".join(_VALID_PEM_LINES)) is True
    assert _pem_has_cert_and_key(
        "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----"
    ) is False
    assert _pem_has_cert_and_key(
        "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"
    ) is False
    assert _pem_has_cert_and_key(
        "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\ny\n-----END ENCRYPTED PRIVATE KEY-----"
    ) is True


# --- run(): branch-switch cleanup + per-view mailbox + next-steps ---------------------------------

def _patch_run(monkeypatch, *, auth_choice, inputs, getpass_value, views=None, view_choice=0,
               toolbox_url="", workspaces=None):
    """Patch onboarding's run() dependencies; return (conn, calls).

    `calls` records the ORDER of config writes/deletes/selects and conn.commit so tests can assert
    that stale-credential deletes happen in the same transaction (before commit).
    """
    calls = []
    conn = MagicMock()
    # run() now queries core_config_data (cross-scope mailbox collision + read-gate warning); default the
    # cursor's fetchall to empty so those guards see no conflict / no disabled scope unless a test overrides.
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    conn.commit.side_effect = lambda: calls.append(("commit",))

    feed = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(feed))
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: getpass_value)

    def _select(prompt, options, *a, **k):
        calls.append(("select", prompt))
        if "authentication" in prompt.lower():
            return auth_choice
        return view_choice  # agent_view selection

    monkeypatch.setattr("agento.framework.cli.terminal.select", _select)
    monkeypatch.setattr(
        "agento.framework.core_config.config_set",
        lambda conn, path, value, **k: calls.append(("set", path, value)),
    )
    monkeypatch.setattr(
        "agento.framework.core_config.config_set_auto_encrypt",
        lambda conn, path, value, **k: calls.append(
            ("set_enc", path, value, k.get("scope"), k.get("scope_id"))
        ),
    )
    monkeypatch.setattr(
        "agento.framework.core_config.config_delete",
        lambda conn, path, **k: calls.append(("del", path, k.get("scope"), k.get("scope_id"))),
    )
    monkeypatch.setattr(
        "agento.framework.scoped_config.scoped_config_set",
        lambda conn, path, value, **k: calls.append(
            ("scoped_set", path, value, k.get("scope"), k.get("scope_id"), k.get("encrypted"))
        ),
    )
    if views is None:
        views = [SimpleNamespace(id=1, code="dev", workspace_id=7)]
    monkeypatch.setattr("agento.framework.workspace.get_active_agent_views", lambda conn: views)
    if workspaces is None:
        # The destination workspace is DERIVED from the chosen view, never chosen separately.
        workspaces = [SimpleNamespace(id=7, code="dev", label="Dev")]
    by_id = {w.id: w for w in workspaces}
    monkeypatch.setattr(
        "agento.framework.workspace.get_workspace", lambda conn, wid: by_id.get(wid)
    )
    monkeypatch.setattr(
        "agento.framework.bootstrap.get_module_config",
        lambda m: {"toolbox/url": toolbox_url} if toolbox_url else {},
    )
    client = MagicMock(spec=OutlookToolboxClient)
    client.list_delta.return_value = {"mailbox": "agent@example.com", "messages": [], "deltaLink": "L", "resynced": False}
    monkeypatch.setattr(
        "agento.modules.outlook.src.toolbox_client.OutlookToolboxClient",
        lambda *a, **k: client,
    )
    return conn, calls


def _wrote(calls, path, value):
    return any(c[0] == "set_enc" and c[1] == path and c[2] == value for c in calls)


def _deleted(calls):
    return [c[1] for c in calls if c[0] == "del"]


def _assert_all_writes_before_commit(calls):
    commit_idx = next(i for i, c in enumerate(calls) if c[0] == "commit")
    write_idxs = [i for i, c in enumerate(calls) if c[0] in ("set", "set_enc", "del", "scoped_set")]
    assert write_idxs, "expected config writes before commit"
    assert all(i < commit_idx for i in write_idxs)


def test_run_secret_branch_clears_stale_cert_material_before_commit(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="the-secret",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    assert _wrote(calls, "outlook/outlook_client_secret", "the-secret")
    deleted = _deleted(calls)
    assert "outlook/outlook_cert_pem" in deleted
    assert "outlook/outlook_cert_password" in deleted
    assert "outlook/outlook_cert_path" in deleted  # legacy
    _assert_all_writes_before_commit(calls)


def test_run_cert_branch_stores_pem_clears_secret_and_blank_passphrase(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=1,
        inputs=["tid", "cid", "agent@example.com", *_VALID_PEM_LINES, "END"],
        getpass_value="",  # blank passphrase
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    assert _wrote(calls, "outlook/outlook_cert_pem", "\n".join(_VALID_PEM_LINES))
    assert not any(c[0] == "set_enc" and c[1] == "outlook/outlook_cert_password" for c in calls)
    deleted = _deleted(calls)
    assert "outlook/outlook_cert_password" in deleted
    assert "outlook/outlook_client_secret" in deleted
    assert "outlook/outlook_cert_path" in deleted  # legacy
    _assert_all_writes_before_commit(calls)


def test_run_cert_branch_keeps_passphrase_unstripped(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=1,
        inputs=["tid", "cid", "agent@example.com", *_VALID_PEM_LINES, "END"],
        getpass_value="  spaced-pass  ",  # must NOT be stripped
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    assert _wrote(calls, "outlook/outlook_cert_password", "  spaced-pass  ")
    assert "outlook/outlook_cert_password" not in _deleted(calls)


def test_run_cert_branch_reprompts_until_pem_has_both_markers(monkeypatch):
    cert_only = ["-----BEGIN CERTIFICATE-----", "x", "-----END CERTIFICATE-----"]
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=1,
        inputs=["tid", "cid", "agent@example.com", *cert_only, "END", *_VALID_PEM_LINES, "END"],
        getpass_value="",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    assert _wrote(calls, "outlook/outlook_cert_pem", "\n".join(_VALID_PEM_LINES))


def test_single_active_view_saves_mailbox_at_default(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="sec",
        views=[SimpleNamespace(id=1, code="dev", workspace_id=7)],
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    assert ("set", "outlook/outlook_mailbox_user_id", "agent@example.com") in calls
    assert not any(c[0] == "scoped_set" for c in calls)
    # only the auth-method select runs (no agent_view prompt for a single view)
    selects = [c for c in calls if c[0] == "select"]
    assert len(selects) == 1
    assert "authentication" in selects[0][1].lower()


def test_multi_view_selects_and_saves_mailbox_at_agent_view_scope(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="sec",
        views=[SimpleNamespace(id=10, code="dev", workspace_id=7),
               SimpleNamespace(id=20, code="ops", workspace_id=7)],
        view_choice=1,  # pick the second view (id=20)
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    scoped = [c for c in calls if c[0] == "scoped_set" and c[1] == "outlook/outlook_mailbox_user_id"]
    assert len(scoped) == 1
    _, _, value, scope, scope_id, encrypted = scoped[0]
    assert value == "agent@example.com"
    assert scope == Scope.AGENT_VIEW
    assert scope_id == 20
    assert encrypted is False
    # mailbox is NOT also written at the default scope
    assert not any(c[0] == "set" and c[1] == "outlook/outlook_mailbox_user_id" for c in calls)
    # two selects: auth method + agent_view choice
    assert len([c for c in calls if c[0] == "select"]) == 2


def test_next_steps_text_has_no_ingress_bind_and_includes_enable(monkeypatch, capsys):
    conn, _ = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="sec",
        toolbox_url="http://toolbox:3001",  # so run() proceeds past verification to next-steps
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    out = capsys.readouterr().out
    assert "tool:enable" in out
    assert "outlook/allowed_senders" in out
    assert "outlook/enabled" in out
    assert "ingress:bind" not in out


def test_next_steps_printed_even_without_toolbox_url(monkeypatch, capsys):
    # toolbox_url default "" -> Graph verification is skipped, but the operator must STILL see the
    # enable guidance (it was previously lost on the early return).
    conn, _ = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="sec",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    out = capsys.readouterr().out
    assert "core/toolbox/url is not set" in out  # took the no-verify path
    assert "tool:enable" in out
    assert "outlook/enabled" in out
    assert "ingress:bind" not in out


def _conn_with_fetchall(rows):
    """Mock a conn whose `with conn.cursor() as cur: ... cur.fetchall()` yields `rows`."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = rows
    return conn


# --- Item A: least-privilege hint in the onboarding checklist ---

def test_print_next_steps_includes_least_privilege_hint(capsys):
    _print_next_steps()
    out = capsys.readouterr().out
    assert "least privilege" in out.lower()
    assert "restrict the Azure app" in out


# --- Item D: cross-scope mailbox collision guard ---

def test_conflicting_mailbox_scopes_detects_default_scope_conflict():
    conn = _conn_with_fetchall([{"scope": "default", "scope_id": 0}])
    conflicts = _conflicting_mailbox_scopes(conn, "Agent@X.com", Scope.AGENT_VIEW, 5)
    assert conflicts == [("default", 0)]


def test_conflicting_mailbox_scopes_detects_other_agent_view_tuple_cursor():
    conn = _conn_with_fetchall([("agent_view", 3)])  # tuple-style cursor
    conflicts = _conflicting_mailbox_scopes(conn, "agent@x.com", Scope.AGENT_VIEW, 5)
    assert conflicts == [("agent_view", 3)]


def test_conflicting_mailbox_scopes_none_when_only_target():
    conn = _conn_with_fetchall([])
    assert _conflicting_mailbox_scopes(conn, "agent@x.com", Scope.DEFAULT, 0) == []


def test_conflicting_mailbox_scopes_empty_mailbox_short_circuits():
    conn = MagicMock()
    assert _conflicting_mailbox_scopes(conn, "   ", Scope.DEFAULT, 0) == []
    conn.cursor.assert_not_called()


def test_conflicting_mailbox_scopes_query_normalizes_and_excludes_target():
    conn = _conn_with_fetchall([])
    cur = conn.cursor.return_value.__enter__.return_value
    _conflicting_mailbox_scopes(conn, "  Agent@X.COM ", Scope.AGENT_VIEW, 5)
    params = cur.execute.call_args[0][1]
    assert params[1] == "agent@x.com"     # normalized UPN
    assert params[2] == Scope.AGENT_VIEW  # exact target scope excluded
    assert params[3] == 5                 # exact target scope_id excluded


# --- Item F: read-gate-disabled warning ---

def test_read_gate_disabled_scopes_reports_only_falsy():
    conn = _conn_with_fetchall([
        {"scope": "default", "scope_id": 0, "value": "1"},
        {"scope": "agent_view", "scope_id": 2, "value": "0"},
        {"scope": "agent_view", "scope_id": 3, "value": "false"},
        {"scope": "agent_view", "scope_id": 4, "value": None},
    ])
    assert _read_gate_disabled_scopes(conn) == [("agent_view", 2), ("agent_view", 3)]


def test_warn_read_gate_disabled_prints_when_disabled(capsys):
    conn = _conn_with_fetchall([{"scope": "agent_view", "scope_id": 2, "value": "0"}])
    _warn_read_gate_disabled(conn)
    out = capsys.readouterr().out
    assert "DISABLED" in out and "agent_view:2" in out


def test_warn_read_gate_disabled_silent_when_all_enabled(capsys):
    conn = _conn_with_fetchall([{"scope": "default", "scope_id": 0, "value": "1"}])
    _warn_read_gate_disabled(conn)
    assert capsys.readouterr().out == ""


def test_run_abort_on_mailbox_conflict_rolls_back_and_does_not_commit(monkeypatch):
    # A mailbox already configured at another scope triggers the collision guard; choosing "abort" must
    # roll back the uncommitted identity/auth writes (so setup's is_complete on the same conn can't read
    # the aborted run as complete) and must not commit or write the mailbox.
    conn, calls = _patch_run(
        monkeypatch,
        auth_choice=0,
        inputs=["tid", "cid", "agent@example.com"],
        getpass_value="sec",
        views=[SimpleNamespace(id=1, code="dev", workspace_id=7)],  # single view -> default-scope target
        view_choice=0,  # the conflict confirm prompt -> "No — abort"
    )
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [("agent_view", 7)]

    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))

    conn.rollback.assert_called_once()
    assert not any(c[0] == "commit" for c in calls)
    assert not any(len(c) > 1 and c[1] == "outlook/outlook_mailbox_user_id" for c in calls)


# --- Graph secrets live at WORKSPACE scope --------------------------------------------------------

def test_secret_branch_writes_the_client_secret_at_workspace_scope(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    writes = [c for c in calls if c[0] == "set_enc" and c[1] == "outlook/outlook_client_secret"]
    assert writes, "the client secret must be written"
    assert writes[0][3] == Scope.WORKSPACE
    assert writes[0][4] == 7  # the single active workspace


def test_cert_branch_writes_the_pem_at_workspace_scope(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch, auth_choice=1,
        inputs=["tenant", "client", "agent@example.com", *_VALID_PEM_LINES, "END"],
        getpass_value="pw",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    pem = [c for c in calls if c[0] == "set_enc" and c[1] == "outlook/outlook_cert_pem"]
    assert pem and pem[0][3] == Scope.WORKSPACE


def test_no_secret_is_ever_written_at_default_scope(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    secret_paths = {
        "outlook/outlook_client_secret", "outlook/outlook_cert_pem", "outlook/outlook_cert_password",
    }
    at_default = [
        c for c in calls
        if c[0] == "set_enc" and c[1] in secret_paths and c[3] == Scope.DEFAULT
    ]
    assert at_default == []


def test_a_stale_secret_is_cleared_from_the_legacy_default_scope_too(monkeypatch):
    """A deployment onboarded before the move still has a default-scope row; switching auth
    method must not leave it behind (graph-auth prefers a certificate when both exist)."""
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    cert_deletes = [c for c in calls if c[0] == "del" and c[1] == "outlook/outlook_cert_pem"]
    scopes = {c[2] for c in cert_deletes}
    assert scopes == {Scope.WORKSPACE, Scope.DEFAULT}


def test_the_secret_lands_in_the_chosen_views_own_workspace(monkeypatch):
    """ONE question — which view owns the mailbox — and the workspace follows from it.

    Asking for the workspace separately let an operator store the Graph secret in workspace A
    while binding the mailbox to a view in workspace B; the strict resolver then could not see
    the credential that had just been written.
    """
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
        views=[SimpleNamespace(id=1, code="dev", workspace_id=7),
               SimpleNamespace(id=2, code="prod", workspace_id=9)],
        view_choice=1,
        workspaces=[
            SimpleNamespace(id=7, code="dev", label="Dev"),
            SimpleNamespace(id=9, code="prod", label="Prod"),
        ],
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    assert not any(c[0] == "select" and "workspace" in c[1].lower() for c in calls)
    writes = [c for c in calls if c[0] == "set_enc" and c[1] == "outlook/outlook_client_secret"]
    assert writes[0][4] == 9
    # The mailbox is bound to the SAME view whose workspace now holds the secret.
    assert ("scoped_set", "outlook/outlook_mailbox_user_id", "agent@example.com",
            Scope.AGENT_VIEW, 2, False) in calls


def test_it_aborts_without_any_active_agent_view_and_writes_nothing(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
        views=[],
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    assert [c for c in calls if c[0] in ("set", "set_enc", "scoped_set")] == []
    conn.commit.assert_not_called()


def test_it_aborts_without_any_workspace_and_writes_nothing(monkeypatch):
    conn, calls = _patch_run(
        monkeypatch, auth_choice=0,
        inputs=["tenant", "client", "agent@example.com"], getpass_value="sec",
        workspaces=[],
    )
    OutlookOnboarding().run(conn, {}, logging.getLogger("t"))
    assert [c for c in calls if c[0] in ("set_enc", "scoped_set")] == []
    conn.commit.assert_not_called()
