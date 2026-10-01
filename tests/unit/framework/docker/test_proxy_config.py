"""E1.5 §2.4: the proxy config's security properties, read from the Caddyfile itself.

The live counterpart (a real Caddy, a captured log) is docker/smoke/proxy-smoke.sh.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import agento.framework.docker as docker_ctx

PROXY = Path(docker_ctx.__file__).parent / "proxy"
SHARE_HOSTS = json.loads((Path(__file__).parents[3] / "fixtures" / "share_host_v1.json").read_text())


def _lines() -> list[str]:
    # The running config is the Caddyfile plus share.caddy (entrypoint.sh appends it).
    text = (PROXY / "Caddyfile").read_text() + (PROXY / "share.caddy").read_text()
    return [ln.split("#", 1)[0].rstrip() for ln in text.splitlines()]


def _blocks() -> list[tuple[list[str], str]]:
    """Every directive line with the stack of block headers that encloses it."""
    stack: list[str] = []
    out: list[tuple[list[str], str]] = []
    for line in _lines():
        s = line.strip()
        if not s:
            continue
        if s == "}":
            stack.pop()
            continue
        out.append((list(stack), s))
        if s.endswith("{"):
            stack.append(s[:-1].strip())
    return out


def _sites() -> dict[str, list[str]]:
    sites: dict[str, list[str]] = {}
    for stack, s in _blocks():
        if stack and "$AGENTO_" in stack[0]:
            sites.setdefault(stack[0], []).append(s)
    return sites


def test_no_route_targets_the_toolbox():
    assert not any("toolbox" in ln for ln in _lines())


def test_proxy_secret_is_set_only_inside_forward_auth():
    uses = [(stack, s) for stack, s in _blocks() if "X-Agento-Proxy-Auth" in s]
    assert uses
    for stack, _ in uses:
        assert stack[-1].startswith("forward_auth "), stack


def test_every_site_strips_caller_identity_and_logs_through_the_filter():
    sites = _sites()
    assert len(sites) == 3
    for name, body in sites.items():
        assert "import strip_identity" in body, name
        assert "import access_log" in body, name
    strip = [s for stack, s in _blocks() if stack == ["(strip_identity)"]]
    assert "request_header -X-Agento-*" in strip


def test_every_forward_auth_runs_after_the_strip_and_before_any_rewrite():
    # Caddy sorts directives: at site level forward_auth runs BEFORE request_header, and in
    # a handle `uri` runs before forward_auth. Only a route keeps the written order.
    auths = [stack for stack, s in _blocks() if s.startswith("forward_auth ")]
    assert len(auths) == 1
    for stack in auths:
        assert stack[-1] == "route" and stack[-2].startswith("handle"), stack


def test_panel_hides_internal_paths_before_proxying_to_web():
    body = _sites()["{$AGENTO_PANEL_HOST}"]
    assert body.index("handle /internal/* {") < body.index("reverse_proxy web:8000")
    assert body[body.index("handle /internal/* {") + 1] == "respond 404"


def test_panel_sends_only_api_and_health_to_web_and_serves_the_rest_statically():
    blocks = _blocks()
    panel = "{$AGENTO_PANEL_HOST}"
    to_web = [stack[1] for stack, s in blocks if s == "reverse_proxy web:8000" and stack[0] == panel]
    assert to_web == ["handle /api/*", "handle /health"]
    shell = [s for stack, s in blocks if stack == [panel, "handle"]]
    assert shell == ["root * /srv/panel", 'header Cache-Control "no-store"', "try_files {path} /index.html", "file_server"]
    assert [s for stack, s in blocks if stack == [panel, "handle /assets/*", "route"]] == _IMMUTABLE("/srv/panel")


# A missing file answers 404 before the cache header, so no 404 or shell is cached forever.
def _IMMUTABLE(root: str) -> list[str]:
    return [f"root * {root}", "@missing not file", "respond @missing 404",
            'header Cache-Control "public, max-age=31536000, immutable"', "file_server"]


def test_apps_serve_the_kit_under_ui_without_auth_or_web():
    blocks = _blocks()
    apps = "{$AGENTO_APPS_HOST}"
    assert [s for stack, s in blocks if stack == [apps, "handle_path /_ui/*", "route"]] == _IMMUTABLE("/srv/miniapp-ui")
    site = _sites()[apps]
    assert site.index("handle_path /_ui/* {") < site.index("handle /a/* {")


@pytest.mark.parametrize("compose", [
    Path(__file__).parents[4] / "docker" / "docker-compose.dev.yml",
    Path(docker_ctx.__file__).parents[1] / "cli" / "templates" / "docker-compose.yml",
])
def test_proxy_mounts_the_built_frontend_read_only(compose):
    text = compose.read_text()
    proxy = text[text.index("\n  proxy:\n"):]
    proxy = proxy[: proxy.index("\n\n")]
    assert re.search(r"framework/web/panel:/srv/panel:ro$", proxy, re.M)
    assert re.search(r"framework/web/miniapp-ui:/srv/miniapp-ui:ro$", proxy, re.M)
    # The artifacts service never sees the kit: proxy serves it.
    assert "miniapp-ui" not in text.replace(proxy, "")


def test_apps_redeem_is_post_only_and_goes_to_web():
    blocks = _blocks()
    apps = "{$AGENTO_APPS_HOST}"
    matcher = [s for stack, s in blocks if stack == [apps, "@redeem"]]
    assert matcher == ["method POST", "path /launch"]
    handler = [s for stack, s in blocks if stack == [apps, "handle @redeem"]]
    assert handler == ["rewrite * /internal/launch/redeem", "reverse_proxy web:8000"]
    # Nothing else on apps reaches web except the forward_auth subrequest: GET /launch is 404.
    body = _sites()[apps]
    assert body[-2:] == ["handle {", "respond 404"]
    assert sum("reverse_proxy web:8000" in s for s in body) == 1


def test_cap_and_code_are_redacted_in_the_access_and_the_error_log():
    blocks = _blocks()
    for owner in ("(access_log)", "log redacted_errors"):
        filtered = [s for stack, s in blocks if owner in stack and "request>uri query" in stack]
        assert "replace cap REDACTED" in filtered, owner
        assert "replace code REDACTED" in filtered, owner
    errors = [s for stack, s in blocks if stack[-1:] == ["log redacted_errors"]]
    assert "include http.log.error" in errors
    default = [s for stack, s in blocks if stack[-1:] == ["log default"]]
    assert "exclude http.log.error" in default


def test_apps_fetches_only_the_path_web_returned():
    # PRD E6 §6.2: one parse. The proxy never derives the file from the request itself.
    apps = "{$AGENTO_APPS_HOST}"
    route = [s for stack, s in _blocks() if stack == [apps, "handle /a/*", "route"]]
    assert route[1:] == ["rewrite * {http.request.header.X-Agento-Upstream-Path}",
                         "request_header -X-Agento-Upstream-Path", "reverse_proxy artifacts:8080"]
    auth = [s for stack, s in _blocks() if stack[-1:] == ["forward_auth web:8000 {"[:-2]]]
    assert "copy_headers X-Agento-Upstream-Path" in auth
    assert not any("path_regexp" in s or "strip_prefix" in s for s in _lines())


def test_share_origin_never_reaches_web_and_sends_no_referrer():
    body = _sites()["*.{$AGENTO_SHARE_HOST}"]
    assert not any("web:8000" in s or "forward_auth" in s for s in body)
    assert "header Referrer-Policy no-referrer" in body
    handler = [s for stack, s in _blocks() if stack == ["*.{$AGENTO_SHARE_HOST}", "handle @share"]]
    assert handler == ["rewrite * /s/{re.share.1}{uri}", "reverse_proxy artifacts:8080"]


def _run_entrypoint(tmp_path: Path, share_host: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "caddy"
    stub.write_text('#!/bin/sh\necho "SECRET=$AGENTO_PROXY_SECRET"\necho "ARGS=$*"\n')
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AGENTO_PROXY_SECRET_FILE": str(tmp_path / "proxy-secret"),
        "AGENTO_PROXY_CADDYFILE": str(PROXY / "Caddyfile"),
        "AGENTO_PROXY_RUN_CONFIG": str(tmp_path / "run-Caddyfile"),
    }
    env.pop("AGENTO_SHARE_HOST", None)
    if share_host is not None:
        env["AGENTO_SHARE_HOST"] = share_host
    return subprocess.run(["sh", str(PROXY / "entrypoint.sh")], env=env,
                          capture_output=True, text=True, check=check)


def test_entrypoint_writes_the_secret_once_and_exports_it(tmp_path):
    first = _run_entrypoint(tmp_path)
    secret = (tmp_path / "proxy-secret").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert f"SECRET={secret}" in first.stdout
    assert f"run --config {tmp_path / 'run-Caddyfile'} --adapter caddyfile" in first.stdout

    second = _run_entrypoint(tmp_path)
    assert (tmp_path / "proxy-secret").read_text() == secret
    assert f"SECRET={secret}" in second.stdout


def test_empty_share_host_renders_no_share_site(tmp_path):
    _run_entrypoint(tmp_path, share_host="")
    assert "*.{$AGENTO_SHARE_HOST}" not in (tmp_path / "run-Caddyfile").read_text()


@pytest.mark.parametrize("host", SHARE_HOSTS["valid"])
def test_valid_share_host_appends_the_share_site(tmp_path, host):
    _run_entrypoint(tmp_path, share_host=host)
    assert (tmp_path / "run-Caddyfile").read_text().endswith((PROXY / "share.caddy").read_text())


@pytest.mark.parametrize("host", SHARE_HOSTS["invalid"])
def test_invalid_share_host_stops_the_proxy(tmp_path, host):
    r = _run_entrypoint(tmp_path, share_host=host, check=False)
    assert r.returncode != 0
    assert "ARGS=" not in r.stdout  # caddy never started
    assert "not a valid host name" in r.stderr
