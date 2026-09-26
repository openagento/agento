"""E2 panel + launch flow through the real proxy (step 7 of proxy-smoke.sh). Stdlib only.

usage: panel-launch-smoke.py <port> <password file> <agent_view id> <artifact code> <cron container> <proxy> <web>

Signs in as the seeded `e2-smoke-user`, launches the seeded artifact, redeems through the apps
origin, and checks the file gate, the replay, a role change, and that no credential reached a log.
Prints one line per check and only status codes: no token is ever printed.
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

_resolve = socket.getaddrinfo
socket.getaddrinfo = lambda h, *a, **k: _resolve("127.0.0.1" if h.endswith(".localhost") else h, *a, **k)

port, pw_file, view_id, code, cron, proxy, web = sys.argv[1:8]
PANEL, APPS = f"https://panel.localhost:{port}", f"https://apps.localhost:{port}"
CTX = ssl._create_unverified_context()  # tls internal: the dev CA is not trusted on the host
failed = 0
seen: list[str] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX), _NoRedirect)


def call(method, url, *, body=None, headers=None, form=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def check(label, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"  {'✓' if ok else '✗'} {label} ({got}{'' if ok else f', expected {want}'})")


def cookie(headers, name):
    for line in headers.get_all("Set-Cookie") or []:
        if line.startswith(name + "="):
            return line.split(";", 1)[0].split("=", 1)[1], line
    return None, ""


with open(pw_file) as f:
    password = f.read().strip()
xhr = {"Origin": PANEL, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
status, h, body = call("POST", f"{PANEL}/api/session", body={"username": "e2-smoke-user", "password": password},
                       headers=xhr)
check("login through the panel origin", status, 200)
if status != 200:
    sys.exit(1)
session, _ = cookie(h, "__Host-agento-session")
seen.append(session)
panel = {**xhr, "Cookie": f"__Host-agento-session={session}", "X-CSRF-Token": json.loads(body)["csrf_token"]}

status, _h, body = call("POST", f"{PANEL}/api/launches", body={"agent_view_id": int(view_id), "artifact_code": code},
                        headers=panel)
check("create a launch", status, 201)
if status != 201:
    sys.exit(1)
launch = json.loads(body)
fields, version = launch["redeem"]["fields"], launch["version_id"]
seen.append(fields["code"])
check("redeem target is the apps origin, no code in the URL",
      launch["redeem"]["url"] == f"{APPS}/launch" and fields["code"] not in launch["redeem"]["url"], True)

navigate = {"Origin": PANEL, "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"}
status, h, _ = call("POST", launch["redeem"]["url"], body=fields, headers=navigate, form=True)
check("redeem by POST", status, 303)
name = f"__Host-agento-launch-{fields['launch_id']}"
token, line = cookie(h, name)
seen.append(token or "")
check("launch cookie attributes", all(f in line for f in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/")), True)
check("redirect to the pinned version", h.get("Location"), f"/a/{code}/v/{version}/")

file_url = f"{APPS}/a/{code}/v/{version}/index.html"
check("pinned file with the cookie", call("GET", file_url, headers={"Cookie": f"{name}={token}"})[0], 200)
check("pinned file without the cookie", call("GET", file_url)[0], 403)
check("another version with the cookie",
      call("GET", f"{APPS}/a/{code}/v/v-20000101-000000-aaaa/index.html", headers={"Cookie": f"{name}={token}"})[0], 403)
check("redeem replay", call("POST", launch["redeem"]["url"], body=fields, headers=navigate, form=True)[0], 403)
check("GET /launch on apps", call("GET", f"{APPS}/launch")[0], 404)

status, h, _ = call("OPTIONS", f"{PANEL}/api/session", headers={"Origin": APPS, "Access-Control-Request-Method": "POST"})
check("no CORS answer to the apps origin", any(k.lower().startswith("access-control-allow") for k in h), False)

subprocess.run(["docker", "exec", cron, "/opt/cron-agent/run.sh", "user:set-role", "e2-smoke-user", "admin"],
               capture_output=True, check=True)
try:
    tool = f"{PANEL}/api/tools/versioned_artifact_get_current:invoke"
    check("old session after user:set-role",
          call("POST", tool, body={"agent_view_id": int(view_id), "arguments": {"artifact_code": code}},
               headers=panel)[0], 401)
    check("launch cookie after user:set-role", call("GET", file_url, headers={"Cookie": f"{name}={token}"})[0], 403)
finally:
    subprocess.run(["docker", "exec", cron, "/opt/cron-agent/run.sh", "user:set-role", "e2-smoke-user", "user"],
                   capture_output=True)

logs = subprocess.run(f"docker logs --since 10m {proxy} 2>&1; docker logs --since 10m {web} 2>&1",
                      shell=True, capture_output=True, text=True).stdout
check("no exchange code, launch token or session token in the proxy and web logs",
      any(s and s in logs for s in seen), False)
sys.exit(1 if failed else 0)
