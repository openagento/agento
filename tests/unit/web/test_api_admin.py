from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from agento.framework import config_test, config_write
from agento.framework.access import accounts, sessions
from agento.framework.admin import data
from agento.web import admin_api, api, toolbox_client

from .conftest import panel_headers

TOKEN = "session-token-value"
ADMIN = accounts.User(id=1, username="root", role="admin", is_active=True)
USER = accounts.User(id=2, username="alice", role="user", is_active=True)
COOKIES = {"__Host-agento-session": TOKEN}


def _as(monkeypatch, user):
    session = sessions.Session(id="sid", user=user, expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: session if t == TOKEN else None)


def _call(web, method, path, body=None):
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    return httpx.request(method, f"{web}{path}", cookies=COOKIES, headers=headers,
                         **({"json": body} if body is not None else {}))


ADMIN_ROUTES = [r for r in api.ROUTES if r.pattern.pattern.startswith("^/api/admin/")]


def _path(route):
    return re.sub(r"\(\?P<\w+>[^)]*\)", "7", route.pattern.pattern.strip("^$"))


def test_admin_routes_exist():
    assert {r.method for r in ADMIN_ROUTES} == {"GET", "POST", "PATCH", "DELETE", "PUT"}


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=lambda r: f"{r.method} {r.pattern.pattern}")
def test_user_role_gets_403_on_every_admin_route(web, monkeypatch, route):
    _as(monkeypatch, USER)
    for name in ("create_user", "update_user", "set_role", "set_active", "set_password", "add_grant", "remove_grant",
                 "list_users", "list_grants"):
        monkeypatch.setattr(accounts, name, MagicMock(side_effect=AssertionError("must not be called")))
    monkeypatch.setattr(config_write, "save_config", MagicMock(side_effect=AssertionError("must not be called")))
    for name in [n for n in vars(data) if n.startswith(("get_", "do_", "delete_"))]:
        monkeypatch.setattr(data, name, MagicMock(side_effect=AssertionError("must not be called")))
    assert _call(web, route.method, _path(route), {} if route.json_body else None).status_code == 403


def test_admin_user_management(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    created = accounts.User(id=9, username="bob", role="user", is_active=True)
    create = MagicMock(return_value=created)
    update = MagicMock()
    monkeypatch.setattr(accounts, "create_user", create)
    monkeypatch.setattr(accounts, "update_user", update)
    monkeypatch.setattr(accounts, "get_user", lambda conn, uid: created)
    monkeypatch.setattr(accounts, "list_users", lambda conn: [ADMIN, created])
    r = _call(web, "POST", "/api/admin/users", {"username": "bob", "role": "user", "password": "correct horse battery"})
    assert r.status_code == 201 and r.json()["username"] == "bob"
    assert create.call_args.kwargs["actor_id"] == ADMIN.id
    assert _call(web, "PATCH", "/api/admin/users/9", {"role": "admin", "password": "x" * 12}).status_code == 200
    assert update.call_count == 1  # every field in one service call: one transaction
    assert update.call_args.kwargs == {"role": "admin", "active": None, "password": "x" * 12, "actor_id": ADMIN.id}
    assert [u["username"] for u in _call(web, "GET", "/api/admin/users").json()] == ["root", "bob"]


def test_admin_write_refused_by_the_service_is_403(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(accounts, "update_user", MagicMock(side_effect=accounts.AccessError("not allowed")))
    assert _call(web, "PATCH", "/api/admin/users/9", {"role": "user"}).status_code == 403


def test_admin_grants(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    add = MagicMock(return_value=5)
    monkeypatch.setattr(accounts, "add_grant", add)
    monkeypatch.setattr(accounts, "remove_grant", MagicMock())
    r = _call(web, "POST", "/api/admin/grants", {"role": "user", "kind": "operation", "name": "artifact.launch",
                                                 "agent_view_id": 3})
    assert r.status_code == 201 and r.json() == {"id": 5}
    assert add.call_args.kwargs["agent_view_id"] == 3
    assert _call(web, "DELETE", "/api/admin/grants/5").status_code == 204
    monkeypatch.setattr(accounts, "add_grant", MagicMock(side_effect=accounts.AccessError("no module declares tool 'x'")))
    r = _call(web, "POST", "/api/admin/grants", {"role": "user", "kind": "tool", "name": "x", "agent_view_id": 3})
    assert r.status_code == 400 and "declares" in r.json()["error"]


def test_admin_grant_options(web, monkeypatch):
    """One source for roles and grantable operations: the panel hardcodes neither."""
    _as(monkeypatch, ADMIN)
    r = _call(web, "GET", "/api/admin/grants/options")
    assert r.status_code == 200
    assert r.json() == {"roles": list(accounts.ROLES), "operations": ["artifact.launch", "conversation.run_details"]}
    _as(monkeypatch, USER)
    assert _call(web, "GET", "/api/admin/grants/options").status_code == 403


def test_admin_config_uses_the_web_form(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    save = MagicMock(return_value=(False, [("agent_view/provider", "openai")]))
    monkeypatch.setattr(config_write, "save_config", save)
    r = _call(web, "PUT", "/api/admin/config", {"path": "web/launch/max_concurrent", "value": "3",
                                                "scope": "workspace", "scope_id": 2})
    assert r.status_code == 200 and r.json() == {"path": "web/launch/max_concurrent", "reset": ["agent_view/provider"]}
    assert save.call_args.kwargs == {"scope": "workspace", "scope_id": 2, "allow_secret": False, "actor_id": ADMIN.id}


def test_admin_config_refusal_text_reaches_the_admin(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(config_write, "save_config",
                        MagicMock(side_effect=config_write.ConfigWriteError("set this field with bin/agento config:set")))
    r = _call(web, "PUT", "/api/admin/config", {"path": "jira/jira_token", "value": "x"})
    assert r.status_code == 400 and "config:set" in r.json()["error"]


def test_agent_views_lists_only_visible(web, monkeypatch):
    _as(monkeypatch, USER)
    monkeypatch.setattr(accounts, "visible_agent_views",
                        lambda conn, user: [{"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1}])
    assert _call(web, "GET", "/api/agent-views").json() == [{"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1}]


def test_invoke_in_an_unreachable_view_is_404(web, monkeypatch):
    _as(monkeypatch, USER)
    monkeypatch.setattr(accounts, "can_reach", lambda conn, user, **kw: False)
    monkeypatch.setattr(toolbox_client, "invoke_tool", MagicMock(side_effect=AssertionError("must not be called")))
    r = _call(web, "POST", "/api/tools/versioned_artifact_get_current:invoke", {"agent_view_id": 3, "arguments": {}})
    assert r.status_code == 404


def test_invoke_needs_exactly_one_scope(web, monkeypatch):
    _as(monkeypatch, USER)
    for body in ({"arguments": {}}, {"agent_view_id": 3, "workspace_id": 1}):
        assert _call(web, "POST", "/api/tools/t:invoke", body).status_code == 400


def test_invoke_passes_the_toolbox_answer_through(web, monkeypatch):
    """admin + grant + is_enabled=0: the toolbox dispatcher answers not_found and web relays it."""
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(accounts, "can_reach", lambda conn, user, **kw: True)
    body = {"ok": False, "error": {"code": "not_found", "message": "tool not found"}, "execution_id": "e"}
    invoke = MagicMock(return_value=toolbox_client.InvokeResult(404, body))
    monkeypatch.setattr(toolbox_client, "invoke_tool", invoke)
    r = _call(web, "POST", "/api/tools/versioned_artifact_get_current:invoke", {"workspace_id": 1, "arguments": {"a": 1}})
    assert (r.status_code, r.json()) == (404, body)
    assert invoke.call_args.args[2:] == ("versioned_artifact_get_current", {"a": 1})
    assert invoke.call_args.kwargs == {"workspace_id": 1, "agent_view_id": None}


# --- /api/admin/* reads (the admin TUI's screens) ---------------------------------------

T = datetime(2026, 10, 2, 12, 0, 0)


def test_scope_id_that_does_not_exist_is_404(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(admin_api, "_scope_exists", lambda conn, scope, scope_id: False)
    monkeypatch.setattr(config_write, "save_config", MagicMock(side_effect=AssertionError("must not be called")))
    r = _call(web, "PUT", "/api/admin/config", {"path": "a/b", "value": "1", "scope": "agent_view", "scope_id": 9})
    assert r.status_code == 404


@pytest.mark.parametrize(("scope", "scope_id"), [("global", "1"), ("workspace", "0"), ("agent_view", "x"),
                                                 ("agent_view", "")])
def test_bad_scope_in_the_query_is_400(web, monkeypatch, scope, scope_id):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_tool_states", MagicMock(side_effect=AssertionError("must not be called")))
    assert _call(web, "GET", f"/api/admin/tools?scope={scope}&scope_id={scope_id}").status_code == 400


def test_scopes(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_workspaces", lambda conn: [{"id": 1, "code": "ws", "label": "WS", "is_active": 1}])
    monkeypatch.setattr(data, "get_agent_views", lambda conn: [
        {"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1, "is_active": 0}])
    assert _call(web, "GET", "/api/admin/scopes").json() == {
        "workspaces": [{"id": 1, "code": "ws", "label": "WS", "is_active": True}],
        "agent_views": [{"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1, "is_active": False}],
    }


def test_dashboard_sends_no_error_message(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_dashboard_data", lambda conn: data.DashboardData(
        version="0.17.0", python_version="3.12.1", db_connected=True, module_count=12, running_jobs=2,
        recent_jobs=[{"id": 5, "type": "todo", "status": "RUNNING", "reference_id": "AI-1", "created_at": T,
                      "finished_at": None, "agent_view_code": "dev", "prompt": "secret work"}],
        tokens=[{"id": 1, "scope": "claude", "label": "main", "status": "error", "error_msg": "sk-live-123 leaked",
                 "error_source": "auto", "used_at": T, "expires_at": None, "enabled": True}],
        agent_views=[{"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1}],
    ))
    body = _call(web, "GET", "/api/admin/dashboard").json()
    assert "sk-live-123" not in str(body)
    assert body == {
        "version": "0.17.0", "python_version": "3.12.1", "db_connected": True, "module_count": 12,
        "running_jobs": 2,
        "recent_jobs": [{"id": 5, "type": "todo", "status": "RUNNING", "reference_id": "AI-1",
                         "agent_view_code": "dev", "created_at": "2026-10-02T12:00:00Z", "finished_at": None}],
        "credentials": [{"id": 1, "scope": "claude", "label": "main", "status": "error", "enabled": True,
                         "error_source": "auto", "used_at": "2026-10-02T12:00:00Z",
                         "expires_at": None}],
        "agent_views": [{"id": 3, "code": "dev", "label": "Dev", "workspace_id": 1}],
    }


JOB_ROW = {"id": 5, "type": "todo", "status": "FAILED", "source": "jira", "reference_id": "AI-1",
           "agent_type": "claude", "created_at": T, "started_at": T, "finished_at": T, "input_tokens": 10,
           "output_tokens": 20, "error_class": "Boom", "error_message": "x" * 600, "agent_view_code": "dev"}


def test_jobs_filters_by_status_strictly(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    get_jobs = MagicMock(return_value=[JOB_ROW])
    monkeypatch.setattr(data, "get_jobs", get_jobs)
    body = _call(web, "GET", "/api/admin/jobs?status=FAILED").json()
    assert get_jobs.call_args.kwargs == {"status": "FAILED", "strict": True}
    assert body == [{"id": 5, "type": "todo", "status": "FAILED", "source": "jira", "reference_id": "AI-1",
                     "agent_type": "claude", "agent_view_code": "dev", "created_at": "2026-10-02T12:00:00Z",
                     "started_at": "2026-10-02T12:00:00Z", "finished_at": "2026-10-02T12:00:00Z",
                     "input_tokens": 10, "output_tokens": 20, "error_class": "Boom"}]
    _call(web, "GET", "/api/admin/jobs")
    assert get_jobs.call_args.kwargs == {"status": None, "strict": True}


@pytest.mark.parametrize("status", ["done", "", "failed"])
def test_jobs_unknown_status_is_400(web, monkeypatch, status):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_jobs", MagicMock(side_effect=AssertionError("must not be called")))
    assert _call(web, "GET", f"/api/admin/jobs?status={status}").status_code == 400


def test_job_detail_cuts_job_content_to_500_chars(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    detail = {**JOB_ROW, "model": "opus", "prompt": "p" * 501, "output": "short", "result_summary": None,
              "agent_view_id": 3, "attempt": 1}
    monkeypatch.setattr(data, "get_job_detail", lambda conn, job_id: detail if job_id == 5 else None)
    body = _call(web, "GET", "/api/admin/jobs/5").json()
    assert body["prompt"] == "p" * 500 + "..." and body["error_message"] == "x" * 500 + "..."
    assert body["output"] == "short" and body["result_summary"] is None and body["model"] == "opus"
    assert "attempt" not in body
    assert _call(web, "GET", "/api/admin/jobs/6").status_code == 404


def test_agents(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    row = {"id": 3, "code": "dev", "label": "Dev", "workspace_code": "ws", "ingress_count": 2, "build_status": "none"}
    monkeypatch.setattr(data, "get_agents_summary", lambda conn: [row])
    assert _call(web, "GET", "/api/admin/agents").json() == [row]


def test_credentials_send_only_the_allow_list(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    row = {"id": 1, "scope": "claude", "label": "main", "status": "error", "enabled": True,
           "error_msg": "refresh failed: sk-ant-oat01-SECRET", "error_source": "operator", "used_at": T,
           "expires_at": T, "token_limit": 1000, "tokens_used": 250, "call_count": 3, "pct_free": 75.0,
           "token": "t", "access_token": "a", "refresh_token": "r", "credentials": {"x": 1}, "type": "oauth",
           "limits": {"windows": [{"label": "5h", "used_pct": 20.0, "resets_at": None}], "balance_usd": None},
           "limits_at": T}
    monkeypatch.setattr(data, "get_credentials_with_usage", lambda conn: [row])
    body = _call(web, "GET", "/api/admin/credentials").json()
    assert "SECRET" not in str(body)
    assert body == [{"id": 1, "scope": "claude", "label": "main", "status": "error", "enabled": True,
                     "error_source": "operator", "used_at": "2026-10-02T12:00:00Z",
                     "expires_at": "2026-10-02T12:00:00Z", "token_limit": 1000, "tokens_used": 250,
                     "call_count": 3, "pct_free": 75.0, "type": "oauth", "limits": row["limits"],
                     "limits_at": "2026-10-02T12:00:00Z"}]


@pytest.mark.parametrize(("action", "fn"), [("clear-error", "do_reset_credential_error"),
                                            ("disable", "do_deregister_credential")])
def test_credential_writes(web, monkeypatch, action, fn):
    _as(monkeypatch, ADMIN)
    write = MagicMock(return_value=True)
    monkeypatch.setattr(data, fn, write)
    monkeypatch.setattr(admin_api, "_credential_exists", lambda conn, cid: cid == 4)
    assert _call(web, "POST", f"/api/admin/credentials/4/{action}").status_code == 204
    assert write.call_args.args[1] == 4
    write.reset_mock()
    assert _call(web, "POST", f"/api/admin/credentials/5/{action}").status_code == 404
    write.assert_not_called()


def test_tools_by_toolset_at_the_query_scope(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(admin_api, "_scope_exists", lambda conn, scope, scope_id: True)
    states = MagicMock(return_value=[("fake", [
        data.EnablementItem("fake_tool", "tools/fake_tool/is_enabled", True, False, blocked_by="fake_switch")])])
    monkeypatch.setattr(data, "get_tool_states", states)
    body = _call(web, "GET", "/api/admin/tools?scope=agent_view&scope_id=3").json()
    assert states.call_args.args[1:] == ("agent_view", 3)
    assert body == [{"toolset": "fake", "tools": [{"name": "fake_tool", "path": "tools/fake_tool/is_enabled",
                                                   "enabled": True, "explicit_here": False,
                                                   "blocked_by": "fake_switch"}]}]


def test_skills_at_the_default_scope(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    states = MagicMock(return_value=[data.EnablementItem("git-workflow", "skill/git-workflow/is_enabled", False, True)])
    monkeypatch.setattr(data, "get_skill_states", states)
    body = _call(web, "GET", "/api/admin/skills").json()
    assert states.call_args.args[1:] == ("default", 0)
    assert body == [{"name": "git-workflow", "path": "skill/git-workflow/is_enabled", "enabled": False,
                     "explicit_here": True}]


def test_config_modules_lists_names_and_tool_groups(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_module_schemas", lambda: [
        data.ModuleSchema("jira", {"jira_token": {"type": "obscure"}}, {"jira_search": {"limit": {}}})])
    assert _call(web, "GET", "/api/admin/config/modules").json() == [{"name": "jira", "tools": ["jira_search"]}]


def _rf(path, value, secret, **kw):
    return data.ResolvedField(path=path, field_name=path.rsplit("/", 1)[-1], value=value, display_value="****",
                              source="db", field_type="obscure" if secret else "string", label="L", obscure=secret,
                              secret=secret, is_set=True, **kw)


def test_config_fields_never_carry_a_secret_value(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(admin_api, "_scope_exists", lambda conn, scope, scope_id: True)
    monkeypatch.setattr(data, "get_module_schemas", lambda: [data.ModuleSchema("jira", {}, {})])
    fields = MagicMock(return_value=[
        _rf("jira/jira_token", "would-be-ciphertext", True),
        _rf("jira/jira_host", "https://example.atlassian.net", False, tester="http", max_length=200,
            options=None, description="Host"),
    ])
    monkeypatch.setattr(data, "get_resolved_fields", fields)
    body = _call(web, "GET", "/api/admin/config?module=jira&scope=workspace&scope_id=2").json()
    assert fields.call_args.args[1:] == ("jira", "workspace", 2)
    assert "would-be-ciphertext" not in str(body)
    assert body[0] == {"path": "jira/jira_token", "label": "L", "description": "", "type": "obscure",
                       "source": "db", "editable": True, "allowed_scopes": ["default", "workspace", "agent_view"],
                       "options": None, "tester": "", "max_length": None, "secret": True, "is_set": True}
    assert body[1]["value"] == "https://example.atlassian.net" and body[1]["secret"] is False
    assert (body[1]["tester"], body[1]["max_length"], body[1]["description"]) == ("http", 200, "Host")


def test_config_for_an_unknown_module_is_404(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(data, "get_module_schemas", lambda: [])
    assert _call(web, "GET", "/api/admin/config?module=nope").status_code == 404
    assert _call(web, "GET", "/api/admin/config").status_code == 404


def test_delete_config_refuses_a_secret_path(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(config_write, "validate_web_path",
                        MagicMock(side_effect=config_write.ConfigWriteError("set this field with bin/agento config:set")))
    monkeypatch.setattr(data, "delete_config_override", MagicMock(side_effect=AssertionError("must not be called")))
    r = _call(web, "DELETE", "/api/admin/config", {"path": "jira/jira_token"})
    assert r.status_code == 400 and "config:set" in r.json()["error"]


def test_delete_config(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(admin_api, "_scope_exists", lambda conn, scope, scope_id: True)
    check = MagicMock(return_value=False)
    monkeypatch.setattr(config_write, "validate_web_path", check)
    delete = MagicMock(return_value=True)
    monkeypatch.setattr(data, "delete_config_override", delete)
    assert _call(web, "DELETE", "/api/admin/config",
                 {"path": "jira/jira_host", "scope": "agent_view", "scope_id": 3}).status_code == 204
    assert check.call_args.args[1] == "jira/jira_host"
    assert delete.call_args.args[1:] == ("jira/jira_host", "agent_view", 3)
    delete.return_value = False
    assert _call(web, "DELETE", "/api/admin/config", {"path": "jira/jira_host"}).status_code == 404


# --- POST /api/admin/config/test ---------------------------------------------------------

SMTP = "app_monitor/alerts/smtp_host"


@pytest.fixture
def toolbox(monkeypatch):
    """The real runner and toolbox arm; only the capability mint and the toolbox are doubles."""
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(admin_api, "_scope_exists", lambda conn, scope, scope_id: True)
    monkeypatch.setattr("agento.framework.config_test.toolbox.rest_capability",
                        lambda **kw: contextlib.nullcontext("tok-rest"))
    monkeypatch.setattr("agento.framework.config_test.toolbox.resolve_toolbox_url", lambda conn: "http://toolbox:3001")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="127.0.0.1").pass_through()
        yield mock.post("http://toolbox:3001/config-test")


def _test(web, path=SMTP, scope="agent_view", scope_id=3):
    return _call(web, "POST", "/api/admin/config/test", {"path": path, "scope": scope, "scope_id": scope_id})


@pytest.mark.parametrize(("answer", "expected"), [
    ({"status": "ok", "code": "OK", "ms": 12}, {"status": "ok", "code": "OK", "message": "ok (12 ms)"}),
    ({"status": "fail", "code": "AUTH_FAILED", "detail": "535 authentication failed"},
     {"status": "fail", "code": "AUTH_FAILED", "message": "535 authentication failed"}),
    ({"status": "not_configured", "detail": "smtp_host is empty"},
     {"status": "not_configured", "code": "", "message": "smtp_host is empty"}),
])
def test_config_test_relays_a_probe_verdict(web, toolbox, answer, expected):
    toolbox.respond(200, json=answer)
    r = _test(web)
    assert (r.status_code, r.json()) == (200, expected)
    assert toolbox.calls.last.request.url.params["agent_view_id"] == "3"


@pytest.mark.parametrize(("respond", "code"), [
    ({"status_code": 200, "json": {"status": "error", "code": "PROBE_CRASHED",
                                   "detail": "connect http://toolbox:3001 /app/src/agento/toolbox/probes/smtp.js"}},
     "PROBE_CRASHED"),
    ({"status_code": 502}, "TOOLBOX_HTTP_ERROR"),
    ({"status_code": 200, "text": "<html>"}, "TOOLBOX_BAD_BODY"),
])
def test_config_test_error_is_a_fixed_message_never_fail(web, toolbox, respond, code):
    toolbox.respond(**respond)
    body = _test(web).json()
    assert (body["status"], body["code"]) == ("error", code)
    assert body["message"] == admin_api.TEST_ERROR_MESSAGES.get(code, admin_api.TEST_ERROR_GENERIC)
    assert "toolbox:3001" not in body["message"] and "/app/" not in body["message"]


def test_config_test_unreachable_toolbox_names_no_origin(web, toolbox):
    toolbox.side_effect = httpx.ConnectError("boom")
    body = _test(web).json()
    assert body == {"status": "error", "code": "TOOLBOX_UNREACHABLE",
                    "message": admin_api.TEST_ERROR_MESSAGES["TOOLBOX_UNREACHABLE"]}


def test_config_test_refuses_a_local_tester(web, toolbox, monkeypatch):
    monkeypatch.setattr(config_test, "tester_for_field", lambda path: config_test.TesterRef(
        kind=config_test.KIND_LOCAL, label="pair", module="m", module_dir=Path("/m"), class_path="src.t:T"))
    r = _test(web, "m/key")
    assert r.status_code == 400 and "config:test" in r.json()["error"]
    assert not toolbox.called


def test_config_test_refuses_a_field_with_no_tester(web, toolbox):
    r = _test(web, "app_monitor/alerts/no_such_field")
    assert r.status_code == 400 and not toolbox.called


@pytest.mark.parametrize(("code", "status"), [("x" * 300, 204), ("x" * 301, 400), ("", 400), ("a b", 400),
                                              ("ż", 400), (7, 400)])
def test_login_code_is_1_to_300_printable_characters(web, monkeypatch, code, status):
    from agento.framework.agent_manager import credential_login

    _as(monkeypatch, ADMIN)
    put = MagicMock(return_value=None)
    monkeypatch.setattr(credential_login, "put_code", put)
    assert _call(web, "POST", "/api/admin/credential-logins/3/code", {"code": code}).status_code == status
    assert put.called == (status == 204)


@pytest.mark.parametrize(("refused", "status"), [("not_found", 404), ("unsupported", 400), ("disabled", 409),
                                                 ("not_oauth", 409), ("active", 409)])
def test_login_request_refusals(web, monkeypatch, refused, status):
    from agento.framework.agent_manager import credential_login

    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(credential_login, "request_login", lambda *a: refused)
    r = _call(web, "POST", "/api/admin/credentials/4/login")
    assert r.status_code == status and set(r.json()) == {"error"}
