"""E1.5 §2.4: the proxy config's security properties, read from the Caddyfile itself.

The live counterpart (a real Caddy, a captured log) is docker/smoke/proxy-smoke.sh.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import agento.framework.docker as docker_ctx

PROXY = Path(docker_ctx.__file__).parent / "proxy"


def _lines() -> list[str]:
    return [ln.split("#", 1)[0].rstrip() for ln in (PROXY / "Caddyfile").read_text().splitlines()]


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
    assert len(auths) == 2
    for stack in auths:
        assert stack[-1] == "route" and stack[-2].startswith("handle"), stack


def test_panel_hides_internal_paths_before_proxying_to_web():
    body = _sites()["{$AGENTO_PANEL_HOST}"]
    assert body.index("handle /internal/* {") < body.index("reverse_proxy web:8000")
    assert body[body.index("handle /internal/* {") + 1] == "respond 404"


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


def test_apps_route_regexes_match_versioned_artifacts():
    js = (PROXY.parents[2] / "modules" / "versioned_artifacts" / "toolbox" / "paths.js").read_text()

    def body(name: str) -> str:
        return re.search(rf"{name} = /\^(.*)\$/;", js).group(1).replace(r"\d", "[0-9]")

    matcher = next(s for s in _lines() if "path_regexp version" in s)
    assert f"^/a/({body('ARTIFACT_CODE_RE')})/v/({body('VERSION_ID_RE')})/" in matcher


def _run_entrypoint(tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "caddy"
    stub.write_text('#!/bin/sh\necho "SECRET=$AGENTO_PROXY_SECRET"\necho "ARGS=$*"\n')
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AGENTO_PROXY_SECRET_FILE": str(tmp_path / "proxy-secret"),
    }
    return subprocess.run(["sh", str(PROXY / "entrypoint.sh")], env=env,
                          capture_output=True, text=True, check=True)


def test_entrypoint_writes_the_secret_once_and_exports_it(tmp_path):
    first = _run_entrypoint(tmp_path)
    secret = (tmp_path / "proxy-secret").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert f"SECRET={secret}" in first.stdout
    assert "run --config /etc/agento-proxy/Caddyfile --adapter caddyfile" in first.stdout

    second = _run_entrypoint(tmp_path)
    assert (tmp_path / "proxy-secret").read_text() == secret
    assert f"SECRET={secret}" in second.stdout
