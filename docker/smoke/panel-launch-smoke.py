"""E2 panel + launch flow through the real proxy (step 7 of proxy-smoke.sh). Stdlib only.

usage: panel-launch-smoke.py <port> <password file> <agent_view id> <artifact code> <cron container> <proxy> <web>
                              <miniapp code> <mysql container> <project dir>

Signs in as the seeded `e2-smoke-user`, launches the seeded artifact, redeems through the apps
origin, and checks the file gate, a spoofed X-Forwarded-Uri, the replay, a miniapp action and
its audit row (E6), a role change, and that no credential reached a log.
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

port, pw_file, view_id, code, cron, proxy, web, app_code, mysql, project_dir = sys.argv[1:11]
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
other = f"{APPS}/a/{code}/v/v-20000101-000000-aaaa/index.html"
check("another version with the cookie", call("GET", other, headers={"Cookie": f"{name}={token}"})[0], 403)
# The proxy sets X-Forwarded-Uri from the real request: a caller's value never decides.
check("another version with the cookie and a spoofed X-Forwarded-Uri naming the pinned one",
      call("GET", other, headers={"Cookie": f"{name}={token}",
                                  "X-Forwarded-Uri": f"/a/{code}/v/{version}/index.html"})[0], 403)
check("redeem replay", call("POST", launch["redeem"]["url"], body=fields, headers=navigate, form=True)[0], 403)
check("GET /launch on apps", call("GET", f"{APPS}/launch")[0], 404)



def sql(query):
    return subprocess.run(["docker", "exec", "-i", mysql, "sh", "-c", 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" cron_agent'],
                          input=query, capture_output=True, text=True).stdout.strip()


def launch_app():
    status, _h, body = call("POST", f"{PANEL}/api/launches",
                            body={"agent_view_id": int(view_id), "artifact_code": app_code}, headers=panel)
    return status, (json.loads(body) if status == 201 else None)


# E6: a launch before activation is files-only; after `miniapp:activate` it pins the manifest.
status, first = launch_app()
check("launch the miniapp artifact before activation", status, 201)
if first:
    app_version = first["version_id"]
    subprocess.run(["uv", "run", "bin/agento", "miniapp:activate", app_code, app_version, "--actor", "proxy-smoke"],
                   cwd=project_dir, capture_output=True, check=True)
    seen.append(first["redeem"]["fields"]["code"])
    status, h, _ = call("POST", first["redeem"]["url"], body=first["redeem"]["fields"], headers=navigate, form=True)
    seen.append(cookie(h, f"__Host-agento-launch-{first['launch_id']}")[0] or "")
    action = f"{PANEL}/api/launches/{first['launch_id']}/actions/versioned_artifact_get_current"
    check("action on a redeemed files-only launch", call("POST", action, body={"arguments": {}}, headers=panel)[0], 403)
    status, app = launch_app()
    check("launch the activated miniapp", status, 201)
    if app:
        seen.append(app["redeem"]["fields"]["code"])
        status, h, _ = call("POST", app["redeem"]["url"], body=app["redeem"]["fields"], headers=navigate, form=True)
        check("redeem the miniapp launch", status, 303)
        seen.append(cookie(h, f"__Host-agento-launch-{app['launch_id']}")[0] or "")
        action = f"{PANEL}/api/launches/{app['launch_id']}/actions"
        status, _h, body = call("POST", f"{action}/versioned_artifact_get_current",
                                body={"arguments": {"artifact_code": app_code}}, headers=panel)
        check("an allowed action through the panel", status, 200)
        check("an action the manifest does not allow",
              call("POST", f"{action}/versioned_artifact_list", body={"arguments": {}}, headers=panel)[0], 403)
        row = sql("SELECT CONCAT_WS(' ', app_artifact_code, app_version_id, app_launch_id) FROM tool_invocation"
                  f" WHERE app_launch_id = '{app['launch_id']}' ORDER BY id DESC LIMIT 1")
        check("the action's audit row carries the app triple", row, f"{app_code} {app_version} {app['launch_id']}")

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
