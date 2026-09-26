#!/bin/bash
set -uo pipefail

# proxy-smoke.sh — container smoke test for the proxy, web, and the E2 panel + launch flow.
#
# OPT-IN, like toolbox-capability-smoke.sh. It needs a RUNNING dev stack with web and proxy:
#
#   cd docker && docker compose -f docker-compose.dev.yml up -d web proxy
#   bash docker/smoke/proxy-smoke.sh [--project <compose project, default: from docker/.env>]
#
# It proves, against the real containers:
#   1. the Caddyfile the proxy runs validates.
#   2. from sandbox, web's authorization endpoints answer 401 — with no header, with a
#      forged proxy secret plus identity headers, and with forged artifact headers and a
#      made-up launch cookie; the launch redeem answers 403 to a made-up code.
#   3. /internal/* through the panel origin is 404.
#   4. the apps and share origins reach web's authorization endpoint (403 with no launch
#      cookie; share is E6's and denies every request).
#   5. neither a `cap` value nor a launch exchange `code` reaches the proxy or web logs — with
#      web up, and with web stopped (the error-log path).
#   6. `artifacts` publishes no host port and does not resolve from sandbox.
#   7. the panel + launch flow (docker/smoke/panel-launch-smoke.py): log in, launch, redeem by
#      POST through the apps origin, file with / without the cookie, replay, a role change
#      ending the session and the launch, and no credential in the proxy or web logs.
#
# It stops and restarts `web` once (step 5). Step 7 seeds, idempotently, at the agent_view
# SMOKE_AGENT_VIEW (default: dev_01): the user `e2-smoke-user` (a fresh random password
# each run, via stdin), two role grants, two tool gates, the artifact `e2-smoke`, and
# `e2-smoke` appended to that view's versioned_artifacts/allowed_artifacts.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
# Default to the project docker/.env names — the one `docker compose` in docker/ uses.
ENV_PROJECT="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$PROJECT_DIR/docker/.env" 2>/dev/null | tail -1)"
PROJECT="${COMPOSE_PROJECT_NAME:-${ENV_PROJECT:-agento}}"
PORT="${AGENTO_PROXY_PORT:-8443}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "Usage: $0 [--project <name>] [--port <proxy host port>]"; exit 1 ;;
  esac
done

resolve_container() {
  local service="$1" ids
  ids=$(docker ps -q \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$service" 2>/dev/null)
  if [ "$(printf '%s\n' "$ids" | grep -c .)" != 1 ]; then
    echo "Expected exactly one running '$service' container in compose project '$PROJECT'." >&2
    echo "Start the stack, or pass --project <name>." >&2
    exit 1
  fi
  docker inspect --format '{{.Name}}' "$ids" | sed 's|^/||'
}
PROXY="$(resolve_container proxy)" || exit 1
WEB="$(resolve_container web)" || exit 1
SANDBOX="$(resolve_container sandbox)" || exit 1
ARTIFACTS="$(resolve_container artifacts)" || exit 1
CRON="$(resolve_container cron)" || exit 1
MYSQL="$(resolve_container mysql)" || exit 1

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
pass=0; fail=0
ok()  { echo -e "  ${GREEN}✓${NC} $1"; pass=$((pass+1)); }
bad() { echo -e "  ${RED}✗${NC} $1"; fail=$((fail+1)); }
expect() { [ "$2" = "$3" ] && ok "$1 ($3)" || bad "$1 (expected $2, got $3)"; }

from_sandbox() { docker exec "$SANDBOX" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@"; }
via_proxy() {
  local host="$1"; shift
  curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
    --resolve "$host:$PORT:127.0.0.1" "https://$host:$PORT$1"
}

echo "1. Caddyfile"
if docker exec "$PROXY" sh -c 'AGENTO_PROXY_SECRET="$(cat /run/agento/proxy-secret)" caddy validate --config /etc/agento-proxy/Caddyfile --adapter caddyfile' >/dev/null 2>&1; then
  ok "caddy validate"
else
  bad "caddy validate"
fi

# The Caddyfile's order is not the run order: Caddy sorts directives. Check the adapted
# config: in every site the identity strip runs before the authz subrequest, and no
# rewrite runs before it.
if docker exec "$PROXY" sh -c 'AGENTO_PROXY_SECRET=x caddy adapt --config /etc/agento-proxy/Caddyfile --adapter caddyfile 2>/dev/null' | python3 -c '
import json, sys
# Only one route of a `handle` group runs, so a sibling in the same group never precedes.
def kind(h):
    k = h.get("handler")
    if k == "headers" and "X-Agento-*" in h.get("request", {}).get("delete", []):
        return "strip"
    if k == "reverse_proxy" and h.get("rewrite", {}).get("uri", "").startswith("/internal/authz/"):
        return "auth"
    return "rewrite" if k == "rewrite" else None
def flat(handlers):
    out = []
    for h in handlers:
        out += [kind(h)] if kind(h) else []
        for r in h.get("routes", []):
            out += flat(r.get("handle", []))
    return out
def before_auth(handlers, prefix):
    order = list(prefix)
    for h in handlers:
        if kind(h) == "auth":
            yield order
        elif kind(h):
            order.append(kind(h))
        routes = h.get("routes", [])
        for i, r in enumerate(routes):
            earlier = [x for e in routes[:i] if not (r.get("group") and e.get("group") == r.get("group"))
                       for x in flat(e.get("handle", []))]
            yield from before_auth(r.get("handle", []), order + earlier)
bad = 0
auths = 0
for srv in json.load(sys.stdin)["apps"]["http"]["servers"].values():
    for route in srv["routes"]:
        for before in before_auth(route["handle"], []):
            auths += 1
            bad += "strip" not in before or "rewrite" in before
sys.exit(1 if bad or auths != 2 else 0)
'; then
  ok "strip runs before each authz subrequest, no rewrite before it"
else
  bad "adapted config runs the authz subrequest out of order"
fi

echo "2. web from sandbox"
for path in /internal/authz/app /internal/authz/share; do
  expect "$path, no header" 401 "$(from_sandbox "http://web:8000$path")"
  expect "$path, forged secret + identity headers" 401 "$(from_sandbox \
    -H 'X-Agento-Proxy-Auth: forged' -H 'X-Agento-User: admin' -H 'X-Forwarded-User: admin' \
    "http://web:8000$path")"
done
LAUNCH_ID="cccccccccccccccccccccccccccccccc"
expect "/internal/authz/app, forged secret + artifact headers + made-up launch cookie" 401 "$(from_sandbox \
  -H 'X-Agento-Proxy-Auth: forged' -H 'X-Agento-Artifact-Code: demo' \
  -H 'X-Agento-Version-Id: v-20260925-120000-ab12' -H "Cookie: __Host-agento-launch-$LAUNCH_ID=made-up" \
  http://web:8000/internal/authz/app)"
expect "/internal/launch/redeem, made-up code" 403 "$(from_sandbox -X POST \
  -H "Origin: https://panel.localhost:$PORT" -H 'Content-Type: application/x-www-form-urlencoded' \
  --data "launch_id=$LAUNCH_ID&code=made-up" http://web:8000/internal/launch/redeem)"
expect "/health" 200 "$(from_sandbox http://web:8000/health)"

echo "3. panel origin"
expect "/internal/authz/app through panel" 404 "$(via_proxy panel.localhost /internal/authz/app)"

echo "4. apps and share origins"
VERSION_PATH="/a/demo/v/v-20260925-120000-ab12/index.html"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
expect "apps version path without a launch cookie, denied by web" 403 "$(via_proxy apps.localhost "$VERSION_PATH?cap=SECRETCAP&code=SECRETCODE")"
expect "apps non-version path" 404 "$(via_proxy apps.localhost /a/demo/)"
expect "share origin, denied by web" 403 "$(via_proxy abc.share.localhost "/?cap=SECRETCAP&code=SECRETCODE")"

echo "5. log redaction"
docker stop "$WEB" >/dev/null
expect "apps with web stopped" 502 "$(via_proxy apps.localhost "$VERSION_PATH?cap=SECRETCAP&code=SECRETCODE")"
docker start "$WEB" >/dev/null
logs="$(docker logs --since "$since" "$PROXY" 2>&1; docker logs --since "$since" "$WEB" 2>&1)"
if printf '%s' "$logs" | grep -q 'SECRETCAP\|SECRETCODE'; then
  bad "a secret value reached a log"
else
  ok "no cap or code value in the proxy or web logs"
fi
# The absence above is only evidence if the requests were logged at all.
printf '%s' "$logs" | grep -q '"logger":"http.log.access' && ok "access log recorded the requests" || bad "no access log line"
printf '%s' "$logs" | grep -q '"logger":"http.log.error' && ok "error log recorded the 502" || bad "no error log line"
printf '%s' "$logs" | grep -q 'REDACTED' && ok "query values logged as REDACTED" || bad "no REDACTED marker"

echo "6. artifacts"
[ -z "$(docker port "$ARTIFACTS")" ] && ok "no published host port" || bad "artifacts publishes a host port"
docker exec "$SANDBOX" getent hosts artifacts >/dev/null 2>&1 && bad "artifacts resolves from sandbox" || ok "artifacts does not resolve from sandbox"

echo "7. panel and launch flow"
VIEW="${SMOKE_AGENT_VIEW:-dev_01}"
RUN=/opt/cron-agent/run.sh
sql() { docker exec -i "$MYSQL" sh -c 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" cron_agent' <<<"$1" 2>/dev/null; }
VIEW_ID="$(sql "SELECT id FROM agent_view WHERE code = '$VIEW'")"
SEED="$(umask 077; mktemp -d)"
trap 'rm -rf "$SEED"' EXIT
python3 -c 'import secrets; print(secrets.token_urlsafe(18))' > "$SEED/pw"
if [ -z "$VIEW_ID" ]; then
  bad "agent_view '$VIEW' not found (set SMOKE_AGENT_VIEW)"
else
  docker exec -i "$CRON" $RUN user:create e2-smoke-user --role user < "$SEED/pw" >/dev/null 2>&1 \
    || docker exec -i "$CRON" $RUN user:password e2-smoke-user < "$SEED/pw" >/dev/null
  docker exec "$CRON" $RUN user:set-role e2-smoke-user user >/dev/null
  docker exec "$CRON" $RUN grant:add --role user --tool versioned_artifact_get_current --agent-view "$VIEW" >/dev/null
  docker exec "$CRON" $RUN grant:add --role user --operation artifact.launch --agent-view "$VIEW" >/dev/null
  for tool in versioned_artifact versioned_artifact_get_current; do
    docker exec "$CRON" $RUN tool:enable "$tool" --agent-view "$VIEW" >/dev/null
  done
  mkdir "$SEED/src" && echo '<h1>e2 smoke</h1>' > "$SEED/src/index.html"
  (cd "$PROJECT_DIR" && uv run bin/agento artifact:init e2-smoke --source "$SEED/src" --actor proxy-smoke) >/dev/null 2>&1 || true
  allowed="$(sql "SELECT value FROM core_config_data WHERE scope = 'agent_view' AND scope_id = $VIEW_ID AND path = 'versioned_artifacts/allowed_artifacts'")"
  case ",${allowed// /}," in
    *,e2-smoke,*) ;;
    *) docker exec "$CRON" $RUN config:set versioned_artifacts/allowed_artifacts "${allowed:+$allowed,}e2-smoke" --agent-view "$VIEW" >/dev/null ;;
  esac
  if python3 "$SCRIPT_DIR/panel-launch-smoke.py" "$PORT" "$SEED/pw" "$VIEW_ID" e2-smoke "$CRON" "$PROXY" "$WEB"; then
    ok "panel and launch flow"
  else
    bad "panel and launch flow"
  fi
fi

echo
echo "passed: $pass, failed: $fail"
[ "$fail" = 0 ]
