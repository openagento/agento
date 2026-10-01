"""What a `di.json` route declaration must look like (PRD E3-E5 §11).

One rule, two callers: `module:validate` refuses a bad declaration before it is installed,
and `web`'s route registry refuses it again at startup. A second copy of the grammar is a
second place for the two to disagree, and the one that matters is whichever runs last.
"""
from __future__ import annotations

import re

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")

# A plain segment, or a named capture the route table compiles as-is.
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# A capture is a CLOSED shape, not "anything that looks like `(?P<...>)`": one named
# group whose body is a single character class with a bounded repeat. A declaration is
# written by an operator and matched against a caller-supplied path, so an expression the
# grammar does not bound is an expression a request can make backtrack - `(?P<id>(a+)+)`
# passes any prefix/suffix check and hangs the listener on one URL (SEC-5). This shape has
# no nesting, no alternation and no unbounded repeat, so it matches in linear time and
# needs no `regex` timeout. Widen it only with a shape that keeps that property.
_CAPTURE = re.compile(r"^\(\?P<[a-z][a-z0-9_]*>\[[A-Za-z0-9_-]+\]\{[0-9]{1,3}(,[0-9]{1,3})?\}\)$")


class RouteDeclarationError(ValueError):
    """A refused route declaration. Never a warning: a route that is wrong in a manifest
    is a route serving something nobody reviewed."""


def route_prefix(module_name: str) -> str:
    """A module owns `/api/<its own name>/` and nothing else.

    The URL space is least privilege too. It is also what keeps a module away from every
    built-in path: none of them lives under another module's name.
    """
    return f"/api/{module_name}/"


def _is_capture(segment: str) -> bool:
    return bool(_CAPTURE.match(segment))


def declaration_error(module_name: str, decl: object) -> str | None:
    """The reason this declaration is refused, or None."""
    if not isinstance(decl, dict):
        return f"a route must be an object, got {decl!r}"
    method, path, handler = decl.get("method"), decl.get("path"), decl.get("handler")
    prefix = route_prefix(module_name)

    if method not in METHODS:
        return f"route method must be one of {METHODS}, got {method!r}"
    if not isinstance(path, str) or not path.startswith(prefix):
        return f"route path must start with {prefix!r}, got {path!r}"
    if not all(_SEGMENT.match(s) or _is_capture(s) for s in path[len(prefix):].split("/")):
        return (f"route path segment is not a plain segment or a bounded named capture "
                f"such as (?P<id>[0-9]{{1,19}}): {path!r}")
    # The grammar bounds the shape; compiling is what proves the shape is a pattern. A
    # character class can still be an invalid range (`[z-a]`), a repeat can still be
    # reversed (`{19,1}`), and two segments can still claim one group name - all of which
    # the grammar admits and `re` refuses. Compiling HERE, on the anchored path the
    # registry compiles, is what stops `module:validate` accepting a declaration that then
    # raises a raw `re.error` out of web's startup.
    try:
        re.compile(compiled_pattern(path))
    except re.error as exc:
        return f"route path is not a valid pattern ({exc}): {path!r}"
    if not isinstance(handler, str):
        return f"route handler must be a dotted path, got {handler!r}"
    # `auth: "login"` is the sign-in route's own exemption, not a manifest switch: a
    # module that could set it could publish an unauthenticated endpoint.
    if decl.get("auth", "session") != "session":
        return "a module route is always auth='session'"
    return None


def compiled_pattern(path: str) -> str:
    """The anchored expression the route table matches. One definition, so the validator
    and the registry cannot compile two different things."""
    return f"^{path}$"


def route_key(decl: dict) -> str:
    return f"{decl['method']} {decl['path']}"
