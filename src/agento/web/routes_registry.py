"""Module-declared panel routes, read from manifests only (PRD E3-E5 §11).

`web` composes its route table once at startup: the built-ins in `api.ROUTES` plus what
this returns. There is one dispatch path, so a module route gets the same auth, CSRF and
rate-limit treatment as a built-in - a route cannot opt out of any of them by being
declared here instead of there.

Two things this deliberately does NOT do:

* **It does not call `bootstrap()` and resolves no configuration.** A `di.json` and
  `modules.json` are already mounted read-only; reading them needs no registry, no DB and
  no decryptor. `web` shares a network with `sandbox`, so holding a module's secrets to
  serve a URL would be blast radius bought for nothing.
* **It does not clear anything on reload.** The two registries `bootstrap()` owns are
  cleared per poll tick because the consumer re-bootstraps in-process. This one is composed
  once, and `web` never re-bootstraps: enabling or disabling a module's routes takes effect
  when `web` restarts, which `module:enable` already does.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from agento.framework.job_types import register_job_type
from agento.framework.module_loader import import_class
from agento.framework.module_status import is_enabled, read_module_status
from agento.framework.route_rules import (
    RouteDeclarationError,
    compiled_pattern,
    declaration_error,
    route_key,
)

from .api import ROUTES, Route


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RouteDeclarationError(message)


def _route(module_name: str, module_dir: Path, decl: object, taken: dict[str, str]) -> Route:
    problem = declaration_error(module_name, decl)
    _check(problem is None, f"module {module_name}: {problem}")
    assert isinstance(decl, dict)
    handler = decl["handler"]

    key = route_key(decl)
    _check(key not in taken, f"module {module_name}: route {key} is already declared by {taken.get(key)}")
    taken[key] = module_name

    try:
        function = import_class(module_dir, handler)
    except (ValueError, FileNotFoundError, ImportError, AttributeError) as exc:
        raise RouteDeclarationError(f"module {module_name}: cannot load handler {handler!r}: {exc}") from None
    _check(callable(function), f"module {module_name}: handler {handler!r} is not callable")

    return Route(decl["method"], re.compile(compiled_pattern(decl["path"])), function,
                 json_body=bool(decl.get("json_body", False)))


def load_module_routes(etc_dir: Path, module_dirs: Sequence[tuple[str, Path]]) -> list[Route]:
    """Every enabled module's declared routes, in the order discovery returns them.

    `etc_dir` holds `modules.json`; `module_dirs` is `[(name, dir)]` from
    `module_discovery.module_dirs_by_name` - the ONE discovery path, so `web` sees the same
    module set as `bootstrap`, `setup:upgrade` and `module:validate`, with the same shadowing.
    Listing roots here instead is how an installed PyPI extension could be enabled, loaded,
    and still have every route it declares answer 404.

    It also registers each enabled module's `job_types`: a route that publishes a job
    resolves its type in this process, and `web` never runs `bootstrap()`, which is the
    only other place they are registered.
    """
    status_path = Path(etc_dir) / "modules.json"
    status = read_module_status(status_path) if status_path.is_file() else {}
    # A built-in path is taken before any module is read: a module cannot shadow sign-in.
    taken = {f"{r.method} {r.pattern.pattern.strip('^$')}": "web" for r in ROUTES}

    routes: list[Route] = []
    for name, module_dir in module_dirs:
        di = module_dir / "di.json"
        if not di.is_file() or not is_enabled(name, status):
            continue
        try:
            manifest = json.loads(di.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RouteDeclarationError(f"module {name}: unreadable di.json: {exc}") from None
        for value in manifest.get("job_types", []):
            register_job_type(value, module=name)
        routes += [_route(name, module_dir, d, taken) for d in manifest.get("routes", [])]
    return routes
