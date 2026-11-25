"""Module routes come from a manifest and nothing else (PRD E3-E5 §11).

`web` composes its route table once, at startup, from the built-ins plus every enabled
module's `di.json`. It must do that WITHOUT bootstrapping and without decrypting anything:
`web` is reachable from `toolbox` and `cron` (db-net), and a config read on this path would put module
secrets in the panel process for no reason.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from pathlib import Path

import httpx
import pytest

from agento.framework import i18n, job_types
from agento.framework.access import acl
from agento.web import api, routes_registry
from agento.web.routes_registry import RouteDeclarationError, load_module_routes
from tests.module_writer import write_module


@pytest.fixture(autouse=True)
def _job_types(monkeypatch):
    """Loading the real core modules registers their job types, channels and schedule
    sources: keep that in this test."""
    from agento.framework.channels import registry
    from agento.framework.schedule import sources

    monkeypatch.setattr(job_types, "_REGISTRY", dict(job_types._REGISTRY))
    monkeypatch.setattr(registry, "_CHANNELS", dict(registry._CHANNELS))
    monkeypatch.setattr(sources, "_REGISTERED", dict(sources._REGISTERED))


def _dirs(root: Path) -> list[tuple[str, Path]]:
    """What `module_discovery.module_dirs_by_name` returns for one root: [(name, dir)]."""
    return [(p.name, p) for p in sorted(root.iterdir())
            if p.is_dir() and (p / "module.json").is_file()]

HANDLER = '''
from agento.web.api import Response


def hello(req):
    return Response(200, {"hello": "module"})
'''


def _module(code_dir: Path, name: str, routes: list[dict], *, handler: str = HANDLER,
            sequence: list[str] | None = None, acl: dict | None = None) -> Path:
    mod = code_dir / name
    (mod / "src").mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "1.0.0", "description": name,
                                                 "sequence": sequence or []}))
    if acl is not None:
        (mod / "acl.json").write_text(json.dumps(acl))
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


ROUTE = {"method": "GET", "path": "/api/demo/hello", "handler": "src.routes.hello", "acl": "self"}


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
    routes = [r for r in load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))
              if r.pattern.pattern.startswith("^/api/conversation/")]

    assert {f"{r.method} {r.pattern.pattern}" for r in routes} == {
        "POST ^/api/conversation/threads$",
        "GET ^/api/conversation/threads$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})$",
        "PATCH ^/api/conversation/threads/(?P<id>[0-9]{1,19})$",
        "DELETE ^/api/conversation/threads/(?P<id>[0-9]{1,19})$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/events$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/timeline$",
        "GET ^/api/conversation/threads/(?P<id>[0-9]{1,19})/events/stream$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/uploads$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/regenerate$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/stop$",
        "POST ^/api/conversation/threads/(?P<id>[0-9]{1,19})/messages/"
        "(?P<message_id>[0-9]{1,19})/unblock$",
        "GET ^/api/conversation/jobs/(?P<id>[0-9]{1,19})$",
    }
    assert all(r.auth == "session" for r in routes)


def test_the_shipped_miniapps_module_loads_its_own_routes(etc):
    from agento.framework.bootstrap import CORE_MODULES_DIR

    (etc / "modules.json").write_text(json.dumps({"miniapps": True}))
    app = "/api/miniapps/apps/(?P<code>[a-z0-9-]{1,64})"
    approval = f"{app}/views/(?P<view>[0-9]{{1,10}})/approval"
    routes = [r for r in load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))
              if r.pattern.pattern.startswith("^/api/miniapps/")]
    assert {f"{r.method} {r.pattern.pattern}" for r in routes} == {
        "GET ^/api/miniapps/apps$",
        "POST ^/api/miniapps/launches$",
        "GET ^/api/miniapps/agent-views$",
        f"GET ^{app}$",
        f"PATCH ^{app}$",
        f"DELETE ^{app}$",
        f"PUT ^{app}/members$",
        f"DELETE ^{app}/members/(?P<id>[0-9]{{1,10}})$",
        f"GET ^{app}/member-options$",
        f"GET ^{approval}$",
        f"POST ^{approval}$",
        f"DELETE ^{approval}$",
        f"GET ^{app}/approvals$",
        "POST ^/api/miniapps/previews$",
    }
    assert {r.acl for r in routes} == {"self"}  # per-app rights, checked by the handler (D-MINIAPP-3)


def test_the_shipped_conversation_module_registers_its_job_type(etc):
    """`web` never bootstraps, so a message submit resolved `conversation` to JobTypeUnknown."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.job_types import JobTypeUnknown, clear_job_types, resolve_job_type

    clear_job_types()
    with pytest.raises(JobTypeUnknown):
        resolve_job_type("conversation")
    (etc / "modules.json").write_text(json.dumps({"conversation": True}))
    load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))
    assert resolve_job_type("conversation").module == "conversation"


def test_web_registers_the_enabled_modules_channels(etc):
    """The conversation routes ask a channel about its conversations (AG-67 §8), and `web`
    never bootstraps: without this the seam would fail closed for every request."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.channels import registry

    registry.clear()
    (etc / "modules.json").write_text(json.dumps({"subagent": False}))
    load_module_routes(etc, _dirs(Path(CORE_MODULES_DIR)))

    assert registry.get_channel("conversation").name == "conversation"
    with pytest.raises(ValueError):
        registry.get_channel("subagent")


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
    # It reads cwd/app/etc (no /app/etc here): never the host checkout's modules.json.
    from agento.framework import event_manager

    _status(_app_etc(tmp_path), {"demo": True})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(event_manager, "_EVENT_MANAGER", None)
    seen: list = []
    monkeypatch.setattr(server, "connect", lambda: None, raising=False)
    monkeypatch.setattr(module_discovery, "module_dirs_by_name",
                        lambda c, u: seen.append((c, u)) or dirs)
    monkeypatch.setattr(api, "ROUTES", list(api.ROUTES))
    monkeypatch.setattr(acl, "_REGISTRY", None)
    server.compose_routes()
    assert seen, "compose_routes() derives its own roots instead of using module discovery"
    assert any(r.pattern.match("/api/demo/hello") for r in api.ROUTES)


OBSERVER = '''
SEEN = []


class Seen:
    def execute(self, event):
        SEEN.append(event)
'''


@pytest.mark.parametrize(("enabled", "count"), [(True, 1), (False, 0)])
def test_web_registers_the_observers_of_enabled_modules_only(etc, code, monkeypatch, enabled, count):
    """`web` never runs bootstrap(): an event a route dispatches (artifact_delete_after) must
    still reach the observers of the enabled modules."""
    from agento.framework import event_manager
    from agento.web.routes_registry import load_module_observers

    monkeypatch.setattr(event_manager, "_EVENT_MANAGER", None)
    mod = _module(code, "demo", [ROUTE])
    (mod / "src" / "observers.py").write_text(OBSERVER)
    (mod / "events.json").write_text(json.dumps({"demo_thing_after": [
        {"name": "demo_seen", "class": "src.observers.Seen"}]}))
    _status(etc, {"demo": enabled})

    load_module_observers(etc, _dirs(code))

    assert event_manager.get_event_manager().observer_count("demo_thing_after") == count


# --- the route acl (D-ADMIN-ACL-2) -------------------------------------------

def _leaves(*ids: str) -> dict:
    return {"screens": [{"title": "Demo", "leaves": [{"id": i, "title": i} for i in ids]}]}


@pytest.mark.parametrize(("value", "why"), [
    (None, "needs an 'acl'"),                        # no acl at all
    ("demo.nothing", "unknown leaf 'demo.nothing'"),  # an id nobody declares
    ("other.view", "unknown leaf 'other.view'"),      # another module's leaf, not in the sequence
    ("anonymous", "'anonymous'"),                     # only the sign-in route is anonymous
    ([], "non-empty list"),
    (["demo.view", 7], "non-empty list"),
])
def test_a_module_route_with_no_or_an_unknown_acl_is_refused(etc, code, value, why):
    _module(code, "other", [], acl=_leaves("other", "other.view"))
    decl = {k: v for k, v in ROUTE.items() if k != "acl"} | ({} if value is None else {"acl": value})
    _module(code, "demo", [decl], acl=_leaves("demo", "demo.view"))
    _status(etc, {})

    with pytest.raises(RouteDeclarationError) as exc:
        load_module_routes(etc, _dirs(code))
    assert why in str(exc.value)


@pytest.mark.parametrize(("value", "expected"), [
    ("self", "self"),
    ("demo.view", "demo.view"),                       # its own leaf
    ("admin.jobs", "admin.jobs"),                     # a core leaf
    ("other.view", "other.view"),                     # a leaf of a module in its sequence
    (["demo", "other"], ("demo", "other")),           # any one of them
])
def test_a_module_route_names_self_its_own_a_core_or_a_sequence_leaf(etc, code, value, expected):
    _module(code, "other", [], acl=_leaves("other", "other.view"))
    _module(code, "demo", [{**ROUTE, "acl": value}], sequence=["other"], acl=_leaves("demo", "demo.view"))
    _status(etc, {})

    assert [r.acl for r in load_module_routes(etc, _dirs(code))] == [expected]


def test_a_leaf_of_a_disabled_sequence_module_is_refused(etc, code):
    """The registry holds the enabled modules only, as bootstrap() refuses a disabled sequence."""
    _module(code, "other", [], acl=_leaves("other"))
    _module(code, "demo", [{**ROUTE, "acl": "other"}], sequence=["other"])
    _status(etc, {"other": False})

    with pytest.raises(RouteDeclarationError):
        load_module_routes(etc, _dirs(code))


def _app_etc(root: Path) -> Path:
    (root / "app" / "etc").mkdir(parents=True)
    return root / "app" / "etc"


def _compose(monkeypatch, tmp_path, dirs):
    from agento.framework import event_manager, module_discovery
    from agento.web import server

    monkeypatch.setattr(event_manager, "_EVENT_MANAGER", None)
    monkeypatch.setattr(module_discovery, "module_dirs_by_name", lambda c, u: dirs)
    monkeypatch.setattr(api, "ROUTES", list(api.ROUTES))
    monkeypatch.setattr(acl, "_REGISTRY", None)
    monkeypatch.chdir(tmp_path)
    server.compose_routes()


def test_every_composed_route_has_a_valid_acl(monkeypatch, tmp_path):
    """Class guard: the shipped table, built-ins and core modules, composes with an acl on each
    route, and the registry it installs is the one the routes were checked against."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.route_rules import acl_error

    _compose(monkeypatch, tmp_path, _dirs(Path(CORE_MODULES_DIR)))

    registry = acl.registry()
    assert "versioned_artifacts.delete" in registry.resources
    assert not {"miniapps", "miniapps.approve"} & registry.resources  # per-app rights (D-MINIAPP-3)
    assert [f"{r.method} {r.pattern.pattern}" for r in api.ROUTES
            if acl_error(r.acl, registry.resources, auth=r.auth)] == []
    assert any(r.pattern.pattern == "^/api/miniapps/previews$" for r in api.ROUTES)


def test_a_core_route_with_an_unknown_leaf_stops_the_start(monkeypatch, tmp_path):
    """A typo in a built-in acl fails at start, not at the first request (SEC-9)."""
    import re

    typo = api.Route("GET", re.compile("^/api/typo$"), lambda req: None, acl="admin.jbos")
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, typo])
    with pytest.raises(RouteDeclarationError) as exc:
        _compose(monkeypatch, tmp_path, [])
    assert "admin.jbos" in str(exc.value)


SNAPSHOT_FILES = ["modules.json", "module.json", "di.json", "events.json", "system.json", "data_patch.json",
                  "cron.json", "config.json", "acl.json", "i18n.json", "en.json", "pl.json", "pack.json"]


@pytest.mark.parametrize("piped", SNAPSHOT_FILES)
def test_the_catalog_snapshot_path_opens_no_file_that_is_not_a_regular_file(tmp_path, monkeypatch, piped):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "modules.json").write_text(json.dumps({"admin": True}))
    module_dir = write_module(
        tmp_path / "code", "admin", di_json={}, events_json={}, data_patch_json={}, cron_json={},
        system_json={"locale/language": {"type": "string", "label": "Language"}},
        config_json={"locale/language": "pl"}, acl_json={"screens": []},
        i18n_json={"version": 1, "catalog": {"en": "en.json", "pl": "pl.json"}, "packs": {"pl": "pack.json"}},
        **{"en.json": {"greeting": "Hello"}, "pl.json": {"greeting": "Cześć"},
           "pack.json": {"admin": {"greeting": "Hej"}}})
    path = etc / piped if piped == "modules.json" else module_dir / piped
    path.unlink()
    os.mkfifo(path)
    opened = []
    read_text = Path.read_text

    def never_a_pipe(self, *args, **kwargs):
        if stat.S_ISFIFO(self.stat().st_mode):
            opened.append(self.name)
            return "{}"
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", never_a_pipe)
    dirs = [("admin", module_dir)]

    catalogs = i18n.load(i18n.ordered_sources(i18n.enabled_manifests(dirs, {})))
    routes_registry._sequence(module_dir)

    assert opened == []
    assert catalogs.messages["pl"]["panel"]
    assert (catalogs.default_language == "pl") is (piped not in ("module.json", "config.json"))


MARKER = "marker-7f3a"
DEEP = "[" * 100_000
HEALTHY_DEFAULTS = {"locale/language": "pl", "locale/date_format": "iso", "locale/timezone": "browser"}
OUTSIDE_LINK = object()
BROKEN_SNAPSHOT_FILES = [
    pytest.param("config.json", OUTSIDE_LINK, "en", id="config-outside-link"),
    pytest.param("config.json", DEEP, "en", id="config-deep-nesting"),
    pytest.param("config.json", "{", "en", id="config-not-json"),
    pytest.param("config.json", ["pl"], "en", id="config-not-an-object"),
    pytest.param("config.json", {"locale/language": 5, "locale/date_format": ["iso"],
                                 "locale/timezone": {"zone": "browser"}}, "en", id="config-wrong-types"),
    pytest.param("config.json", {"locale/language": MARKER, "locale/date_format": MARKER,
                                 "locale/timezone": MARKER}, "en", id="config-defaults-that-are-no-option"),
    pytest.param("config.json", {"locale/language": "pl"}, "pl", id="config-lacks-the-display-fields"),
    pytest.param("system.json", OUTSIDE_LINK, "pl", id="system-outside-link"),
    pytest.param("system.json", {"locale/date_format": {"type": "select", "options": 5},
                                 "locale/timezone": 7}, "pl", id="system-wrong-types"),
    pytest.param("system.json", {"locale/date_format": {"type": "select", "options": [{"value": [MARKER]}]},
                                 "locale/timezone": {"type": "select", "options": [{"value": {"a": MARKER}}]}},
                 "pl", id="system-option-values-that-are-not-text"),
]


def _admin_module(tmp_path: Path) -> Path:
    from agento.framework.bootstrap import CORE_MODULES_DIR

    module_dir = Path(shutil.copytree(Path(CORE_MODULES_DIR) / "admin", tmp_path / "code" / "admin"))
    (module_dir / "config.json").write_text(json.dumps(HEALTHY_DEFAULTS))
    return module_dir


def _start(monkeypatch, tmp_path, module_dir: Path):
    monkeypatch.setattr(i18n, "_CATALOGS", None)
    monkeypatch.setattr(api, "_DISPLAY", None)
    _compose(monkeypatch, tmp_path, [("admin", module_dir)])
    return i18n.current().default_language, api._DISPLAY


def test_the_start_takes_the_file_defaults_of_a_whole_admin_module(monkeypatch, tmp_path):
    language, display = _start(monkeypatch, tmp_path, _admin_module(tmp_path))

    assert language == "pl"
    assert {key: setting.default for key, setting in display.items()} == {"date_format": "iso", "timezone": "browser"}


@pytest.mark.parametrize(("name", "content", "language"), BROKEN_SNAPSHOT_FILES)
def test_a_snapshot_file_that_is_not_sound_gives_no_default_and_does_not_stop_the_start(
        monkeypatch, tmp_path, caplog, name, content, language):
    module_dir = _admin_module(tmp_path)
    target = module_dir / name
    if content is OUTSIDE_LINK:
        outside = tmp_path / f"outside-{name}"
        outside.write_text(target.read_text())
        target.unlink()
        target.symlink_to(outside)
    else:
        target.write_text(content if isinstance(content, str) else json.dumps(content))

    with caplog.at_level(logging.WARNING):
        got = _start(monkeypatch, tmp_path, module_dir)

    dropped_language = name == "config.json" and isinstance(content, dict) and content.get("locale/language") != "pl"
    assert got == (language, None)
    assert "give no display settings" in caplog.text
    assert ("is not a registered language" in caplog.text) == dropped_language
    assert ("i18n skip: module=admin file=config.json reason=unusable file" in caplog.text) == (
        name == "config.json" and content in (OUTSIDE_LINK, DEEP, "{"))
    assert MARKER not in caplog.text


STOPS = [
    pytest.param("app/etc/modules.json", ["admin"], AttributeError, id="status-a-list"),
    pytest.param("app/etc/modules.json", 7, AttributeError, id="status-a-number"),
    pytest.param("app/etc/modules.json", None, AttributeError, id="status-null"),
]


@pytest.mark.parametrize(("relative", "content", "raised"), STOPS)
def test_a_present_file_of_a_base_reader_with_a_wrong_shape_stops_the_composition(
        monkeypatch, tmp_path, relative, content, raised):
    module_dir = _admin_module(tmp_path)
    _app_etc(tmp_path)
    (tmp_path / relative).write_text(content if isinstance(content, str) else json.dumps(content))

    with pytest.raises(raised):
        _start(monkeypatch, tmp_path, module_dir)


GERMAN = {"version": 1, "languages": [{"tag": "de", "name": "Deutsch"}]}


@pytest.mark.parametrize("linked", [False, True], ids=["own-manifest", "manifest-linked-from-outside"])
@pytest.mark.parametrize(("status", "languages"), [
    ({}, ["en", "pl", "de"]), ({"pack_dir": False}, ["en", "pl"]), ({"pack_de": False}, ["en", "pl"]),
], ids=["enabled", "disabled-by-directory-name", "disabled-by-manifest-name"])
def test_web_and_a_cli_process_take_the_languages_of_the_same_enabled_modules(monkeypatch, tmp_path, status,
                                                                              languages, linked):
    module_dir = write_module(tmp_path / "code", "pack_dir", module_json={"name": "pack_de"}, i18n_json=GERMAN)
    if linked:
        (module_dir / "module.json").rename(tmp_path / "outside.json")
        (module_dir / "module.json").symlink_to(tmp_path / "outside.json")
    (_app_etc(tmp_path) / "modules.json").write_text(json.dumps(status))
    monkeypatch.setattr(i18n, "_CATALOGS", None)
    monkeypatch.setattr(api, "_DISPLAY", None)

    _compose(monkeypatch, tmp_path, [("pack_dir", module_dir)])
    in_web = list(i18n.current().languages)
    monkeypatch.setattr(i18n, "_CATALOGS", None)
    in_cli = list(i18n.languages())

    assert (in_web, in_cli) == (languages, languages)
