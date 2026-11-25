"""Every panel refusal is built by one serializer and carries a code, and every code has a text
in each shipped language (CLS-1)."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from agento.framework.schedule.tick import FireOutcome
from agento.modules.versioned_artifacts.src import admin as artifact_admin
from agento.web import admin_api

SRC = Path(__file__).parents[2] / "src" / "agento"
SCANNED = sorted({
    *(SRC / "web").rglob("*.py"),
    *(SRC / "framework").rglob("*.py"),
    *(p for src in (SRC / "modules").glob("*/src") for p in src.rglob("*.py")),
})
BODILESS_ANSWERS_OFF_THE_PANEL_API = {("web/server.py", "_not_the_proxy"), ("web/server.py", "_outside_the_api")}
SERIALIZERS = {("web/api.py", "error"), ("web/api.py", "refusal")}
CODED_EXCEPTIONS = {"AccessError", "ConfigWriteError", "AdminError", "ActivationError", "SubmissionError",
                    "UnblockRefused", "UploadError"}


def _with_subclasses(names: set[str]) -> set[str]:
    """A subclass of a coded exception (`CatalogConflict(AccessError)`) is refused with a code too."""
    bases = {node.name: {_name(base) for base in node.bases} for path in SCANNED
             for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ClassDef)}
    found = set(names)
    while grown := {name for name, of in bases.items() if of & found} - found:
        found |= grown
    return found


def _name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_error_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "error"
    return isinstance(func, ast.Attribute) and func.attr == "error" and \
        isinstance(func.value, ast.Name) and func.value.id == "api"


def _has_error_key(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "error" for k in node.keys)


def _body_argument(call: ast.Call) -> ast.expr | None:
    if len(call.args) > 1:
        return call.args[1]
    return next((kw.value for kw in call.keywords if kw.arg == "body"), None)


def _status(call: ast.Call) -> object:
    node = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "status"), None)
    return node.value if isinstance(node, ast.Constant) else None


def _is_hand_built_refusal(call: ast.Call, site: tuple[str, str | None]) -> bool:
    """A `Response` with a literal refusal status. Only the router's two named answers to a
    path outside `/api/` may write it, and only with an empty body."""
    status = _status(call)
    if not isinstance(status, int) or status < 400:
        return False
    body = _body_argument(call)
    bodiless = isinstance(body, ast.Constant) and body.value == b""
    return not (bodiless and site in BODILESS_ANSWERS_OFF_THE_PANEL_API)


def _passes_code(call: ast.Call) -> bool:
    return any(kw.arg == "code" for kw in call.keywords)


def _problems(path: Path) -> list[str]:
    relative = path.relative_to(SRC).as_posix()
    out = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            where = f"{relative}:{node.lineno}"
            name = _name(node.func)
            if name == "Response" and _has_error_key(_body_argument(node)) and (relative, function) not in SERIALIZERS:
                out.append(f"{where}: a refusal body built outside api.error / api.refusal")
            if name == "Response" and _is_hand_built_refusal(node, (relative, function)):
                out.append(f"{where}: a refusal status built outside api.error / api.refusal")
            if _is_error_call(node.func) and not _passes_code(node):
                out.append(f"{where}: error(...) with no code")
            if name == "_refuse" and len(node.args) < 2 and not _passes_code(node):
                out.append(f"{where}: _refuse(...) with no code")
            if name in ALL_CODED_EXCEPTIONS and not _passes_code(node):
                out.append(f"{where}: {name}(...) with no code")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(path.read_text()), None)
    return out


ALL_CODED_EXCEPTIONS = _with_subclasses(CODED_EXCEPTIONS)


def test_the_scan_knows_the_subclasses_of_a_coded_exception():
    assert "CatalogConflict" in ALL_CODED_EXCEPTIONS


def test_the_scan_reaches_the_refusal_sites():
    names = {p.relative_to(SRC).as_posix() for p in SCANNED}

    assert {"web/api.py", "web/server.py", "framework/access/accounts.py", "framework/config_write.py", "framework/tool_catalog.py",
            "modules/conversation/src/routes.py", "modules/miniapps/src/routes.py",
            "modules/versioned_artifacts/src/routes.py", "modules/scheduler/src/routes.py"} <= names


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.relative_to(SRC).as_posix())
def test_every_refusal_goes_through_the_serializer_with_a_code(path):
    assert _problems(path) == []


@pytest.mark.parametrize(("source", "problem"), [
    ('Response(400, {"error": "x"})', "built outside"),
    ('Response(503, b"service unavailable")', "refusal status built outside"),
    ('api.Response(403, b"")', "refusal status built outside"),
    ('Response(status=404, body=None)', "refusal status built outside"),
    ('error(400, "x")', "error(...) with no code"),
    ('api.error(400, "x")', "error(...) with no code"),
    ('raise AccessError("x")', "AccessError(...) with no code"),
    ('refusal = ActivationError(409, "x", current_version="v2")', "ActivationError(...) with no code"),
    ('_refuse(400)', "_refuse(...) with no code"),
])
def test_the_guard_refuses_each_shape(tmp_path, monkeypatch, source, problem):
    path = SRC / "web" / "sample.py"
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: source)

    assert any(problem in found for found in _problems(path))


@pytest.mark.parametrize("source", [
    'error(400, "x", code="INVALID_REQUEST")',
    'logger.error("x")',
    'Response(200, {"error_code": None})',
    'Response(200, b"ok")',
    'Response(204)',
    'Response(answer.status, answer.body)',
    'raise AccessError("x", code="ROLE_NOT_FOUND")',
    '_refuse(400, "bad_rule")',
])
def test_the_guard_accepts_each_shape(monkeypatch, source):
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: source)

    assert _problems(SRC / "web" / "sample.py") == []


CODE_SOURCES = sorted({*SCANNED, *(SRC / "framework" / "schedule").glob("*.py"),
                       *(SRC / "framework" / "config_test").glob("*.py")})
ARTIFACT_STORE_CODES = SRC / "modules" / "versioned_artifacts" / "toolbox" / "errors.js"
POSITION_OF_CODE = {"_refuse": 1, "StoreError": 1, "InvalidSchedule": 0}
CODES_SENT_BEFORE_A_SESSION = {"AUTH_INVALID_CREDENTIALS", "AUTH_THROTTLED"}
LANGUAGES = ("en", "pl")


def _namespace(path: Path) -> str:
    """The catalog of the screen that shows the code: the module's own, or `panel`. Only the
    scheduler screen shows a rule the schedule engine refused."""
    parts = path.relative_to(SRC).parts
    if parts[:2] == ("framework", "schedule"):
        return "scheduler"
    return parts[1] if parts[0] == "modules" else "panel"


def _is_module_tester_result(path: Path, call: ast.Call) -> bool:
    """A module tester's code goes with its probe text, which the panel shows as sent."""
    return _name(call.func) == "TestResult" and _namespace(path) != "panel"


def _literals(node: ast.expr) -> set[str]:
    """The code a site names: a literal, or each side of `"A" if … else "B"`."""
    if isinstance(node, ast.IfExp):
        return _literals(node.body) | _literals(node.orelse)
    return {node.value} if isinstance(node, ast.Constant) and isinstance(node.value, str) else set()


def _codes_in(path: Path) -> set[str]:
    codes: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call) or _is_module_tester_result(path, node):
            continue
        for kw in node.keywords:
            if kw.arg == "code":
                codes |= _literals(kw.value)
        position = POSITION_OF_CODE.get(_name(node.func) or "")
        if position is not None and len(node.args) > position:
            codes |= _literals(node.args[position])
    return codes


def _artifact_store_codes() -> set[str]:
    """`versioned_artifacts` passes the store's own code through; the store names them in one table."""
    table = re.search(r"ERROR_CODES = Object\.freeze\(\{(.*?)\}\);", ARTIFACT_STORE_CODES.read_text(), re.S)
    return {*re.findall(r"(\w+): '\1'", table.group(1)), artifact_admin._code(None)}


def _codes() -> set[tuple[str, str]]:
    found = {(_namespace(path), code) for path in CODE_SOURCES for code in _codes_in(path)}
    found |= {("panel", code) for _, _, code in admin_api._LOGIN_REFUSED.values()}
    found |= {("panel", code) for code in admin_api.TEST_ERROR_MESSAGES}
    found |= {("scheduler", outcome.value) for outcome in FireOutcome if outcome is not FireOutcome.PUBLISHED}
    found |= {("versioned_artifacts", code) for code in _artifact_store_codes()}
    return found


def _catalog(namespace: str, language: str) -> dict:
    directory = SRC / "framework" / "i18n" / "panel" if namespace == "panel" else SRC / "modules" / namespace / "i18n"
    file = directory / f"{language}.json"
    return json.loads(file.read_text()) if file.exists() else {}


def _has_text(namespace: str, code: str, language: str) -> bool:
    if code in CODES_SENT_BEFORE_A_SESSION:
        return f"login.error.{code}" in _catalog("panel", language)
    return any(f"error.{code}" in _catalog(holder, language) for holder in (namespace, "panel"))


def test_the_scan_finds_the_codes_of_each_shape():
    assert {("panel", "FORBIDDEN"), ("panel", "METHOD_NOT_ALLOWED"), ("panel", "LOGIN_NOT_OAUTH"),
            ("panel", "SERVICE_UNAVAILABLE"), ("panel", "TOOLBOX_UNREACHABLE"), ("panel", "TESTER_RAISED"), ("panel", "UPLOAD_TOO_LARGE"),
            ("scheduler", "bad_rule"), ("scheduler", "too_complex"), ("scheduler", "limit"), ("scheduler", "too_soon"),
            ("conversation", "UPLOAD_BUSY"), ("miniapps", "TITLE_INVALID"),
            ("versioned_artifacts", "DRAFT_LOCKED"), ("versioned_artifacts", "ARTIFACT_REFUSED")} <= _codes()


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_code_has_a_text_in_each_language(language):
    assert sorted(f"{namespace}:{code}" for namespace, code in _codes() if not _has_text(namespace, code, language)) == []
