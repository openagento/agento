"""Admin panel API: what the admin TUI shows, and the writes ``web`` can do without a key.

Every handler checks its operation first. Every response is built from an explicit
allow-list of fields: a row from ``framework.admin.data`` is never sent as it is.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime

from agento.framework.admin import data

from . import api
from .api import Request, Response, Route, _body, _forbidden_unless, _iso, _positive_int, _r, error

_SCOPES = ("default", "workspace", "agent_view")
_SCOPE_TABLES = {"workspace": "workspace", "agent_view": "agent_view"}
_ID = re.compile(r"[0-9]{1,10}")


def _ts(value) -> str | None:
    return _iso(value) if isinstance(value, datetime) else None


def _scope_exists(conn, scope: str, scope_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {_SCOPE_TABLES[scope]} WHERE id = %s", (scope_id,))
        return cur.fetchone() is not None


def config_scope(req: Request, src: dict) -> tuple[str, int] | Response:
    """``(scope, scope_id)`` from a JSON body or a query string; an unknown id is 404 (SEC-7)."""
    scope, scope_id = src.get("scope", "default"), src.get("scope_id", 0)
    if scope not in _SCOPES:
        return error(400, f"scope must be one of {', '.join(_SCOPES)}")
    if scope == "default":
        return scope, 0
    if isinstance(scope_id, str) and _ID.fullmatch(scope_id):
        scope_id = int(scope_id)  # a query string carries text
    if not _positive_int(scope_id):
        return error(400, "scope_id must be a positive integer")
    if not _scope_exists(req.conn, scope, scope_id):
        return error(404, "not found")
    return scope, scope_id


def scopes(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    return Response(200, {
        "workspaces": [{"id": w["id"], "code": w["code"], "label": w["label"], "is_active": bool(w["is_active"])}
                       for w in data.get_workspaces(req.conn)],
        "agent_views": [{"id": v["id"], "code": v["code"], "label": v["label"], "workspace_id": v["workspace_id"],
                         "is_active": bool(v["is_active"])} for v in data.get_agent_views(req.conn)],
    })


def _credential_head(t: dict) -> dict:
    # error_msg is CLI stderr and can quote a prompt or a credential (SEC-6): it is never sent.
    return {"id": t["id"], "scope": t["scope"], "label": t["label"], "status": t["status"],
            "enabled": bool(t["enabled"]),
            "error_source": t.get("error_source"), "used_at": _ts(t.get("used_at")),
            "expires_at": _ts(t.get("expires_at"))}


def dashboard(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    d = data.get_dashboard_data(req.conn)
    return Response(200, {
        "version": d.version, "python_version": d.python_version, "db_connected": d.db_connected,
        "module_count": d.module_count, "running_jobs": d.running_jobs,
        "recent_jobs": [{"id": j["id"], "type": j["type"], "status": j["status"], "reference_id": j["reference_id"],
                         "agent_view_code": j["agent_view_code"], "created_at": _ts(j["created_at"]),
                         "finished_at": _ts(j["finished_at"])} for j in d.recent_jobs],
        "credentials": [_credential_head(t) for t in d.tokens],
        "agent_views": [{"id": v["id"], "code": v["code"], "label": v["label"], "workspace_id": v["workspace_id"]}
                        for v in d.agent_views],
    })


JOB_STATUSES = ("TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD")
_JOB_FIELDS = ("id", "type", "status", "source", "reference_id", "agent_type", "agent_view_code",
               "input_tokens", "output_tokens", "error_class")
_JOB_TIMES = ("created_at", "started_at", "finished_at")
# Job content, cut as the TUI cuts it: the work product an admin inspects (plan: Security notes).
_JOB_CONTENT = ("prompt", "output", "result_summary", "error_message")
_CONTENT_MAX = 500


def _job(row: dict) -> dict:
    return {**{k: row.get(k) for k in _JOB_FIELDS}, **{k: _ts(row.get(k)) for k in _JOB_TIMES}}


def _cut(value):
    if not isinstance(value, str) or len(value) <= _CONTENT_MAX:
        return value
    return value[:_CONTENT_MAX] + "..."


def jobs(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    status = req.query.get("status")
    if status is not None and status not in JOB_STATUSES:
        return error(400, f"status must be one of {', '.join(JOB_STATUSES)}")
    return Response(200, [_job(j) for j in data.get_jobs(req.conn, status=status, strict=True)])


def job_detail(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    row = data.get_job_detail(req.conn, int(req.params["id"]))
    if row is None:
        return error(404, "not found")
    return Response(200, {**_job(row), "model": row.get("model"),
                          **{k: _cut(row.get(k)) for k in _JOB_CONTENT}})


def agents(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    keys = ("id", "code", "label", "workspace_code", "ingress_count", "build_status")
    return Response(200, [{k: a[k] for k in keys} for a in data.get_agents_summary(req.conn)])


def credentials(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    usage = ("token_limit", "tokens_used", "call_count", "pct_free")
    # `limits` is what credential:limits read from the provider: windows and a balance, no secret.
    return Response(200, [{**_credential_head(t), **{k: t[k] for k in usage}, "type": t.get("type"),
                           "limits": t.get("limits"), "limits_at": _ts(t.get("limits_at"))}
                          for t in data.get_credentials_with_usage(req.conn)])


def _credential_exists(conn, credential_id: int) -> bool:
    # Not get_credential(): that reads and decrypts the payload.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM credential WHERE id = %s", (credential_id,))
        return cur.fetchone() is not None


def _credential_write(req: Request, write) -> Response:
    if denied := _forbidden_unless(req, "credentials.manage"):
        return denied
    credential_id = int(req.params["id"])
    if not _credential_exists(req.conn, credential_id):
        return error(404, "not found")
    write(req.conn, credential_id)
    return Response(204)


def clear_credential_error(req: Request) -> Response:
    return _credential_write(req, data.do_reset_credential_error)


def disable_credential(req: Request) -> Response:
    return _credential_write(req, data.do_deregister_credential)


def _interactive_oauth_scopes() -> set[str]:
    # From the di.json declarations, without importing a harness module (web loads none).
    from agento.framework.harness import CredentialRegistrationMode, enumerate_harness_declarations
    from agento.framework.module_discovery import resolve_module_root

    return {p.credential_scope for d in enumerate_harness_declarations(resolve_module_root())
            for p in d.descriptor.providers
            if p.credential_scope and CredentialRegistrationMode.INTERACTIVE_OAUTH in p.registration_modes}


_LOGIN_REFUSED = {
    "not_found": (404, "not found"),
    "unsupported": (400, "this credential cannot sign in from the panel"),
    "disabled": (409, "the credential is disabled"),
    "not_oauth": (409, "only a subscription (OAuth) credential can sign in again"),
    "active": (409, "a sign-in for this credential is already running"),
}
# Printable ASCII with no space; RSA-3072 OAEP-SHA256 seals at most 318 bytes.
_LOGIN_CODE = re.compile(r"[!-~]{1,300}")


def start_credential_login(req: Request) -> Response:
    from agento.framework.agent_manager import credential_login

    if denied := _forbidden_unless(req, "credentials.manage"):
        return denied
    result = credential_login.request_login(req.conn, int(req.params["id"]), req.session.user.id,
                                            _interactive_oauth_scopes())
    if isinstance(result, str):
        return error(*_LOGIN_REFUSED[result])
    return Response(201, {"id": result})


def credential_login_state(req: Request) -> Response:
    from agento.framework.agent_manager import credential_login

    if denied := _forbidden_unless(req, "credentials.manage"):
        return denied
    row = credential_login.get_login(req.conn, int(req.params["id"]))
    if row is None:
        return error(404, "not found")
    return Response(200, {"status": row["status"], "verify_url": row["verify_url"], "user_code": row["user_code"],
                          "needs_code": bool(row["needs_code"]), "error_code": row["error_code"],
                          "expires_at": _ts(row["expires_at"])})


def credential_login_code(req: Request) -> Response:
    from agento.framework.agent_manager import credential_login

    if denied := _forbidden_unless(req, "credentials.manage"):
        return denied
    code = _body(req).get("code")
    if not isinstance(code, str) or not _LOGIN_CODE.fullmatch(code):
        return error(400, "the code must be 1 to 300 printable characters with no space")
    refused = credential_login.put_code(req.conn, int(req.params["id"]), code)
    if refused == "not_found":
        return error(404, "not found")
    if refused:
        return error(409, "this sign-in does not wait for a code")
    return Response(204)


def cancel_credential_login(req: Request) -> Response:
    from agento.framework.agent_manager import credential_login

    if denied := _forbidden_unless(req, "credentials.manage"):
        return denied
    if not credential_login.cancel_login(req.conn, int(req.params["id"])):
        return error(404, "not found")
    return Response(204)


def _scoped(req: Request, operation: str) -> tuple[str, int] | Response:
    return _forbidden_unless(req, operation) or config_scope(req, req.query)


def tools(req: Request) -> Response:
    scope = _scoped(req, "admin.read")
    if isinstance(scope, Response):
        return scope
    return Response(200, [{"toolset": toolset, "tools": [asdict(t) for t in items]}
                          for toolset, items in data.get_tool_states(req.conn, *scope)])


def skills(req: Request) -> Response:
    scope = _scoped(req, "admin.read")
    if isinstance(scope, Response):
        return scope
    keys = ("name", "path", "enabled", "explicit_here")  # a skill has no `requires` master
    return Response(200, [{k: getattr(s, k) for k in keys} for s in data.get_skill_states(req.conn, *scope)])


def config_modules(req: Request) -> Response:
    if denied := _forbidden_unless(req, "admin.read"):
        return denied
    return Response(200, [{"name": m.name, "tools": sorted(m.tools)} for m in data.get_module_schemas()])


def _config_field(f: data.ResolvedField) -> dict:
    out = {"path": f.path, "label": f.label, "description": f.description, "type": f.field_type,
           "source": f.source, "editable": f.editable_at_scope, "allowed_scopes": f.allowed_scopes,
           "options": f.options, "tester": f.tester, "max_length": f.max_length, "secret": f.secret,
           "is_set": f.is_set}
    if not f.secret:
        out["value"] = f.value
    return out


def get_config(req: Request) -> Response:
    scope = _scoped(req, "admin.read")
    if isinstance(scope, Response):
        return scope
    module = req.query.get("module")
    if module not in {m.name for m in data.get_module_schemas()}:
        return error(404, "not found")
    return Response(200, [_config_field(f) for f in data.get_resolved_fields(req.conn, module, *scope)])


def delete_config(req: Request) -> Response:
    from agento.framework import config_write

    if denied := _forbidden_unless(req, "config.write"):
        return denied
    body = _body(req)
    scope = config_scope(req, body)
    if isinstance(scope, Response):
        return scope
    path = body.get("path")
    try:
        config_write.validate_web_path(req.conn, path)  # the check PUT makes: never a secret path
    except config_write.ConfigWriteError as exc:
        return error(400, str(exc))
    if not data.delete_config_override(req.conn, path, *scope):
        return error(404, "not found")
    return Response(204)


# An ERROR's own text can name the toolbox origin or a path inside it (SEC-6), so the browser
# gets a message chosen by code. ok / fail / not_configured text is the probe's, redacted
# where the values live (config_test/runner.py).
TEST_ERROR_MESSAGES = {
    "NO_TESTER": "This field declares no tester.",
    "TESTER_UNAVAILABLE": "The tester of this field cannot load.",
    "BAD_RESULT": "The tester gave a result that is not valid.",
    "SCOPE_ID_INVALID": "The scope id is not valid for this test.",
    "SCOPE_UNSUPPORTED": "This test cannot run at this scope. The toolbox tests the default and agent view scopes only.",
    "TOOLBOX_URL_INVALID": "The core/toolbox/url value is not a usable URL.",
    "TOOLBOX_UNREACHABLE": "The toolbox is not reachable.",
    "CAPABILITY_UNAVAILABLE": "The test cannot get a toolbox capability.",
    "TOOLBOX_HTTP_ERROR": "The toolbox refused the test.",
    "TOOLBOX_BAD_BODY": "The toolbox answer cannot be read.",
}
TEST_ERROR_GENERIC = "The test could not check this value. Run bin/agento config:test for the details."


def run_tester(req: Request) -> Response:
    from agento.framework import config_test

    if denied := _forbidden_unless(req, "config.write"):
        return denied
    body = _body(req)
    scope = config_scope(req, body)
    if isinstance(scope, Response):
        return scope
    path = body.get("path")
    ref = config_test.tester_for_field(path) if isinstance(path, str) else None
    if ref is None:
        return error(400, "this field declares no tester")
    if ref.kind == config_test.KIND_LOCAL:
        # A local tester is module code: web does not load it.
        return error(400, f"run this test in the admin TUI or with bin/agento config:test {path}")
    result = config_test.run_config_test(req.conn, path, scope=scope[0], scope_id=scope[1])
    message = (TEST_ERROR_MESSAGES.get(result.code, TEST_ERROR_GENERIC) if result.status == config_test.ERROR
               else result.message)
    return Response(200, {"status": result.status, "code": result.code, "message": message})


def set_config(req: Request) -> Response:
    from agento.framework.config_write import ConfigWriteError, save_config

    if denied := _forbidden_unless(req, "config.write"):
        return denied
    body = _body(req)
    scope = config_scope(req, body)
    if isinstance(scope, Response):
        return scope
    try:
        # web holds no encryption key: allow_secret=False writes only a provably plain field.
        _encrypted, reset = save_config(
            req.conn, body.get("path"), body.get("value"), scope=scope[0], scope_id=scope[1],
            allow_secret=False, actor_id=req.session.user.id,
        )
    except ConfigWriteError as exc:
        return error(403, "forbidden") if str(exc) == "not allowed" else error(400, str(exc))
    # No admin route returns a secret value; a repaired dependent is named, not shown.
    return Response(200, {"path": body.get("path"), "reset": [p for p, _v in reset]})


ROUTES: list[Route] = [
    _r("GET", "/api/admin/scopes", scopes),
    _r("GET", "/api/admin/dashboard", dashboard),
    _r("GET", "/api/admin/jobs", jobs),
    _r("GET", r"/api/admin/jobs/(?P<id>[0-9]{1,19})", job_detail),
    _r("GET", "/api/admin/agents", agents),
    _r("GET", "/api/admin/credentials", credentials),
    _r("POST", r"/api/admin/credentials/(?P<id>[0-9]{1,10})/clear-error", clear_credential_error),
    _r("POST", r"/api/admin/credentials/(?P<id>[0-9]{1,10})/disable", disable_credential),
    _r("POST", r"/api/admin/credentials/(?P<id>[0-9]{1,10})/login", start_credential_login),
    _r("GET", r"/api/admin/credential-logins/(?P<id>[0-9]{1,19})", credential_login_state),
    _r("POST", r"/api/admin/credential-logins/(?P<id>[0-9]{1,19})/code", credential_login_code, json_body=True),
    _r("POST", r"/api/admin/credential-logins/(?P<id>[0-9]{1,19})/cancel", cancel_credential_login),
    _r("GET", "/api/admin/tools", tools),
    _r("GET", "/api/admin/skills", skills),
    _r("GET", "/api/admin/config/modules", config_modules),
    _r("GET", "/api/admin/config", get_config),
    _r("PUT", "/api/admin/config", set_config, json_body=True),
    _r("DELETE", "/api/admin/config", delete_config, json_body=True),
    _r("POST", "/api/admin/config/test", run_tester, json_body=True),
]
api.ROUTES.extend(ROUTES)
