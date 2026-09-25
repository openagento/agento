"""Origins, cookies and the write guard of the panel (PRD E2 §4.1, §4.2, §4.4).

The origin split stops cross-origin *reads*; it is not a CSRF boundary — panel and apps are
same-site. Every state-changing panel request therefore needs the exact panel Origin, a
same-origin fetch, and the CSRF token (checked in server.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from itertools import islice

from agento.framework.access.launches import MAX_CONCURRENT_CEILING

SESSION_COOKIE = "__Host-agento-session"
LAUNCH_COOKIE_PREFIX = "__Host-agento-launch-"
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_LAUNCH_COOKIES = MAX_CONCURRENT_CEILING  # one user holds at most this many live launches


@dataclass(frozen=True)
class Origins:
    panel: str
    apps: str

    @classmethod
    def from_env(cls) -> Origins:
        port = os.environ.get("AGENTO_PROXY_PORT", "8443")
        suffix = "" if port == "443" else f":{port}"
        panel = os.environ.get("AGENTO_PANEL_HOST", "panel.localhost")
        apps = os.environ.get("AGENTO_APPS_HOST", "apps.localhost")
        return cls(panel=f"https://{panel}{suffix}", apps=f"https://{apps}{suffix}")


# `__Host-` enforces Secure, no Domain and Path=/ only; HttpOnly and SameSite are explicit.
def session_cookie(token: str, max_age: int) -> str:
    return f"{SESSION_COOKIE}={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"


def launch_cookie_name(launch_id: str) -> str:
    if not _LAUNCH_ID.fullmatch(launch_id):
        raise ValueError("launch id must be 32 lower-case hex characters")
    return LAUNCH_COOKIE_PREFIX + launch_id


def launch_cookie(launch_id: str, token: str, max_age: int) -> str:
    return f"{launch_cookie_name(launch_id)}={token}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"


def clear_cookie(name: str) -> str:
    return f"{name}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"


def parse_cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def launch_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """``{launch_id: token}`` for the first ``MAX_LAUNCH_COOKIES`` well-formed launch cookies.

    Callers authorize, check liveness and clear over this one subset; a cookie past it is
    neither read nor cleared.
    """
    found = (
        (name[len(LAUNCH_COOKIE_PREFIX):], value)
        for name, value in cookies.items()
        if name.startswith(LAUNCH_COOKIE_PREFIX) and _LAUNCH_ID.fullmatch(name[len(LAUNCH_COOKIE_PREFIX):])
    )
    return dict(islice(found, MAX_LAUNCH_COOKIES))


def write_allowed(headers, origins: Origins) -> bool:
    """A state-changing panel request: exact panel Origin, a same-origin fetch, never a navigation."""
    if headers.get("Origin") != origins.panel:
        return False
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site != "same-origin":
        return False
    mode = headers.get("Sec-Fetch-Mode")
    return mode is None or mode in ("cors", "same-origin")
