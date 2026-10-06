"""Module routes come from a manifest and nothing else (PRD E3-E5 §11).

`web` composes its route table once, at startup, from the built-ins plus every enabled
module's `di.json`. It must do that WITHOUT bootstrapping and without decrypting anything:
`web` sits on agento-net with `sandbox`, and a config read on this path would put module
secrets in the panel process for no reason.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agento.web import api
from agento.web.routes_registry import RouteDeclarationError, load_module_routes


def _dirs(root: Path) -> list[tuple[str, Path]]:
    """What `module_discovery.module_dirs_by_name` returns for one root: [(name, dir)]."""
    return [(p.name, p) for p in sorted(root.iterdir())
            if p.is_dir() and (p / "module.json").is_file()]

HANDLER = '''
from agento.web.api import Response


def hello(req):
    return Response(200, {"hello": "module"})
'''


def _module(code_dir: Path, name: str, routes: list[dict], *, handler: str = HANDLER) -> Path:
    mod = code_dir / name
    (mod / "src").mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "1.0.0", "description": name}))
    (mod / "di.json").write_text(json.dumps({"routes": routes}))
    (mod / "src" / "routes.py").write_text(handler)
    return mod


@pytest.fixture
def etc(tmp_path) -> Path:
    (tmp_path / "etc").mkdir()
    return tmp_path / "etc"


@pytest.fixture
def code(tmp_path) -> Path:
    (tmp_path / "code").mkdir()
    return tmp_path / "code"


def _status(etc: Path, status: dict) -> None:
    (etc / "modules.json").write_text(json.dumps(status))


ROUTE = {"method": "GET", "path": "/api/demo/hello", "handler": "src.routes.hello"}


def test_an_enabled_modules_route_is_loaded(etc, code):
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": True})

    routes = load_module_routes(etc, _dirs(code))

    assert [r.method for r in routes] == ["GET"]
    assert routes[0].pattern.match("/api/demo/hello")


def test_a_disabled_modules_routes_are_absent(etc, code):
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": False})

    assert load_module_routes(etc, _dirs(code)) == []


def test_a_module_absent_from_the_file_is_enabled(etc, code):
    """Same default as every other module gate: unlisted means enabled."""
    _module(code, "demo", [ROUTE])
    _status(etc, {})

    assert len(load_module_routes(etc, _dirs(code))) == 1


@pytest.mark.parametrize("path", ["/api/session", "/api/other/thing", "/api/demo", "/health"])
def test_a_route_outside_the_modules_own_prefix_is_refused(etc, code, path):
    """A module owns /api/<its name>/ and nothing else — least privilege over the URL space."""
    _module(code, "demo", [{**ROUTE, "path": path}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError) as exc:
        load_module_routes(etc, _dirs(code))
    assert "demo" in str(exc.value)


def test_a_handler_outside_the_module_is_refused(etc, code):
    _module(code, "demo", [{**ROUTE, "handler": "..secrets.read"}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


@pytest.mark.parametrize("bad", [
    {"path": "/api/demo/x", "handler": "src.routes.hello"},               # no method
    {"method": "GET", "handler": "src.routes.hello"},                     # no path
    {"method": "GET", "path": "/api/demo/x"},                             # no handler
    {"method": "TRACE", "path": "/api/demo/x", "handler": "src.routes.hello"},
    {"method": "GET", "path": "api/demo/x", "handler": "src.routes.hello"},
])
def test_an_incomplete_declaration_is_refused(etc, code, bad):
    _module(code, "demo", [bad])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def test_two_modules_cannot_claim_one_path(etc, code):
    _module(code, "demo", [ROUTE])
    _module(code, "demo2", [{**ROUTE, "path": "/api/demo2/hello"}])
    _status(etc, {})
    # demo2 reaching into demo's prefix is the collision that matters.
    (code / "demo2" / "di.json").write_text(json.dumps({"routes": [ROUTE]}))

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def test_loading_routes_never_bootstraps_and_never_decrypts(etc, code, monkeypatch):
    """The panel process has no business holding a module's secrets to serve a URL."""
    import agento.framework.bootstrap as bootstrap_module
    import agento.framework.encryptor as encryptor_module

    def explode(*args, **kwargs):
        raise AssertionError("the route registry resolved configuration")

    monkeypatch.setattr(bootstrap_module, "bootstrap", explode)
    monkeypatch.setattr(encryptor_module, "get_encryptor", explode)
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": True})

    assert len(load_module_routes(etc, _dirs(code))) == 1


def test_a_registered_route_is_dispatched_and_counted(web, counted, monkeypatch, etc, code):
    """One dispatch path, two sources: a module route is limited like a built-in."""
    from datetime import UTC, datetime, timedelta

    from agento.framework.access import accounts, sessions

    token = "session-token-value"
    session = sessions.Session(
        id="sid", user=accounts.User(id=1, username="root", role="admin", is_active=True),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: session if t == token else None)
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": True})
    extra = load_module_routes(etc, _dirs(code))
    monkeypatch.setattr(api, "ROUTES", api.ROUTES + extra)

    r = httpx.get(f"{web}/api/demo/hello", cookies={"__Host-agento-session": token})

    assert (r.status_code, r.json()) == (200, {"hello": "module"})
    # The SESSION bucket only: an authenticated request never spends the shared address
    # budget, or one signed-in caller could lock out every stranger behind that address,
    # a sign-in included (SEC-12, "the address limit counts failures only").
    assert [b.kind for b in counted[-1]] == ["session"]


def test_an_unauthenticated_call_to_a_module_route_is_refused(web, monkeypatch, etc, code):
    """404 would mean the route never registered; 401 is the shared auth gate doing its job."""
    from agento.framework.access import sessions

    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: None)
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": True})
    monkeypatch.setattr(api, "ROUTES", api.ROUTES + load_module_routes(etc, _dirs(code)))

    assert httpx.get(f"{web}/api/demo/hello").status_code == 401


def test_a_module_route_defaults_to_requiring_a_session(etc, code):
    _module(code, "demo", [ROUTE])
    _status(etc, {"demo": True})

    assert load_module_routes(etc, _dirs(code))[0].auth == "session"


def test_a_module_may_not_declare_an_unauthenticated_route(etc, code):
    """`auth: login` is the sign-in route's own exemption, not a manifest switch."""
    _module(code, "demo", [{**ROUTE, "auth": "login"}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def test_the_shipped_conversation_module_loads_its_own_routes(etc, tmp_path):
    """The real manifest, the real handlers: a typo here is a 404 only at startup."""
    from agento.framework.bootstrap import CORE_MODULES_DIR

    (etc / "modules.json").write_text(json.dumps({"conversation": True}))
    routes = load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))

    assert {f"{r.method} {r.pattern.pattern}" for r in routes} == {
        "POST ^/api/conversation/threads$",
        "GET ^/api/conversation/threads$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})$",
        "DELETE ^/api/conversation/threads/(?P<id>[0-9]{1,19})$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/events$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/timeline$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/events/stream$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages/"
        "(?P<message_id>[0-9]{1,19})/unblock$",
    }
    assert all(r.auth == "session" for r in routes)


def test_the_shipped_conversation_module_registers_its_job_type(etc):
    """`web` never bootstraps, so a message submit resolved `conversation` to JobTypeUnknown."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.job_types import JobTypeUnknown, clear_job_types, resolve_job_type

    clear_job_types()
    with pytest.raises(JobTypeUnknown):
        resolve_job_type("conversation")
    (etc / "modules.json").write_text(json.dumps({"conversation": True}))
    try:
        load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))
        assert resolve_job_type("conversation").module == "conversation"
    finally:
        clear_job_types()


def test_a_disabled_conversation_module_serves_nothing(etc):
    from agento.framework.bootstrap import CORE_MODULES_DIR

    (etc / "modules.json").write_text(json.dumps({"conversation": False}))

    assert [r for r in load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))
            if "conversation" in r.pattern.pattern] == []


# --- the capture grammar (SEC-5) -------------------------------------------

@pytest.mark.parametrize("capture", [
    "(?P<id>(a+)+)",          # nested quantifier: catastrophic backtracking
    "(?P<id>(a|a)*)",         # alternation under a star: the same shape, spelled out
    "(?P<id>.*)",             # unbounded, and it eats the path separator
    "(?P<id>[0-9]+)",         # a character class, but with no ceiling
    "(?P<id>[0-9]{1,19})x",   # a capture with something welded onto it
    "(?P<Id>[0-9]{1,4})",     # a name outside the grammar
])
def test_a_capture_outside_the_bounded_grammar_is_refused(etc, code, capture):
    """A declaration is written by an operator and matched against a caller-supplied path.

    The grammar is closed - one named group, one character class, one bounded repeat - so
    that no declaration can compile to an expression a URL makes backtrack. The assertion
    is on the SHAPE being refused, not on any one pathological expression: a grammar that
    admitted `(?P<id>(a+)+)` would admit the next one nobody thought of.
    """
    _module(code, "demo", [{**ROUTE, "path": f"/api/demo/{capture}"}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError) as exc:
        load_module_routes(etc, _dirs(code))
    assert "capture" in str(exc.value)


@pytest.mark.parametrize("capture", ["(?P<id>[0-9]{1,19})", "(?P<message_id>[a-f0-9]{32})"])
def test_a_bounded_capture_is_accepted(etc, code, capture):
    _module(code, "demo", [{**ROUTE, "path": f"/api/demo/{capture}"}])
    _status(etc, {"demo": True})

    assert len(load_module_routes(etc, _dirs(code))) == 1


@pytest.mark.parametrize("capture", [
    "(?P<id>[z-a]{1,2})",       # a character class whose range runs backwards
    "(?P<id>[0-9]{19,1})",      # a repeat whose bounds run backwards
])
def test_a_capture_the_grammar_admits_but_re_cannot_compile_is_refused(etc, code, capture):
    """The grammar bounds the SHAPE; compiling is what proves the shape is a pattern.

    Both of these match the capture grammar character for character and both make `re`
    raise. Without the compile, `module:validate` passed them and web's startup died on a
    raw `re.error` instead — a manifest check that accepts what the thing it guards cannot
    load is not a check.
    """
    _module(code, "demo", [{**ROUTE, "path": f"/api/demo/{capture}"}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def test_two_segments_cannot_claim_one_group_name(etc, code):
    _module(code, "demo", [{**ROUTE,
                            "path": "/api/demo/(?P<id>[0-9]{1,9})/x/(?P<id>[0-9]{1,9})"}])
    _status(etc, {"demo": True})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def test_the_validator_and_the_registry_compile_the_same_expression(etc, code):
    """One definition of the anchored pattern, so a declaration cannot pass the validator
    and then fail to compile in the registry — which is the whole bug above."""
    from agento.framework.route_rules import compiled_pattern

    _module(code, "demo", [{**ROUTE, "path": "/api/demo/(?P<id>[0-9]{1,19})"}])
    _status(etc, {"demo": True})

    route = load_module_routes(etc, _dirs(code))[0]

    assert route.pattern.pattern == compiled_pattern("/api/demo/(?P<id>[0-9]{1,19})")


def test_an_installed_extensions_routes_are_composed(etc, tmp_path, monkeypatch):
    """PLN-3. `web` must see the module set `bootstrap`, `setup:upgrade` and
    `module:validate` see - the container extension mount included. Listing the roots here by
    hand meant an installed PyPI extension could be enabled and loaded by the framework while
    every route it declares answered 404.

    Composed through the SHARED discovery, so this also pins its shadowing: a local
    `app/code` module of the same name wins, and the extension never displaces it.
    """
    from agento.framework import module_discovery
    from agento.web import server

    core = tmp_path / "core"
    core.mkdir()
    user = tmp_path / "code"
    user.mkdir()
    ext = tmp_path / "opt"
    ext.mkdir()
    _module(ext, "demo", [ROUTE])
    _status(etc, {"demo": True})
    monkeypatch.setattr(module_discovery, "CONTAINER_EXTENSION_DIR", str(ext))

    dirs = module_discovery.module_dirs_by_name(str(core), str(user))
    assert [name for name, _ in dirs] == ["demo"]
    routes = load_module_routes(etc, dirs)
    assert len(routes) == 1
    assert routes[0].pattern.match("/api/demo/hello")

    # `compose_routes` must go through that same call - not a root list of its own.
    seen: list = []
    monkeypatch.setattr(server, "connect", lambda: None, raising=False)
    monkeypatch.setattr(module_discovery, "module_dirs_by_name",
                        lambda c, u: seen.append((c, u)) or dirs)
    monkeypatch.setattr(api, "ROUTES", list(api.ROUTES))
    server.compose_routes()
    assert seen, "compose_routes() derives its own roots instead of using module discovery"
    assert any(r.pattern.match("/api/demo/hello") for r in api.ROUTES)
