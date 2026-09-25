from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework import config_write
from agento.framework.access import accounts, sessions
from agento.web import api, toolbox_client

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
    for name in ("create_user", "set_role", "set_active", "set_password", "add_grant", "remove_grant",
                 "list_users", "list_grants"):
        monkeypatch.setattr(accounts, name, MagicMock(side_effect=AssertionError("must not be called")))
    monkeypatch.setattr(config_write, "save_config", MagicMock(side_effect=AssertionError("must not be called")))
    assert _call(web, route.method, _path(route), {} if route.json_body else None).status_code == 403


def test_admin_user_management(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    created = accounts.User(id=9, username="bob", role="user", is_active=True)
    create = MagicMock(return_value=created)
    set_role = MagicMock()
    monkeypatch.setattr(accounts, "create_user", create)
    monkeypatch.setattr(accounts, "set_role", set_role)
    monkeypatch.setattr(accounts, "get_user", lambda conn, uid: created)
    monkeypatch.setattr(accounts, "list_users", lambda conn: [ADMIN, created])
    r = _call(web, "POST", "/api/admin/users", {"username": "bob", "role": "user", "password": "correct horse battery"})
    assert r.status_code == 201 and r.json()["username"] == "bob"
    assert create.call_args.kwargs["actor_id"] == ADMIN.id
    assert _call(web, "PATCH", "/api/admin/users/9", {"role": "admin"}).status_code == 200
    assert set_role.call_args.kwargs["actor_id"] == ADMIN.id
    assert [u["username"] for u in _call(web, "GET", "/api/admin/users").json()] == ["root", "bob"]


def test_admin_write_refused_by_the_service_is_403(web, monkeypatch):
    _as(monkeypatch, ADMIN)
    monkeypatch.setattr(accounts, "set_role", MagicMock(side_effect=accounts.AccessError("not allowed")))
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
