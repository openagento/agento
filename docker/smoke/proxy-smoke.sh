#!/bin/bash
set -uo pipefail

# proxy-smoke.sh — container smoke test for the E1.5 proxy and web scaffold.
#
# OPT-IN, like toolbox-capability-smoke.sh. It needs a RUNNING dev stack with web and proxy:
#
#   cd docker && docker compose -f docker-compose.dev.yml up -d web proxy
#   bash docker/smoke/proxy-smoke.sh [--project <compose project, default: agento>]
#
# It proves, against the real containers:
#   1. the Caddyfile the proxy runs validates.
#   2. from sandbox, web's authorization endpoints answer 401 — with no header and with a
#      forged proxy secret plus identity headers.
#   3. /internal/* through the panel origin is 404.
#   4. the apps and share origins reach web's authorization endpoint (403 until E2/E6).
#   5. neither a `cap` value nor a launch exchange `code` reaches the proxy or web logs — with
#      web up, and with web stopped (the error-log path).
#   6. `artifacts` publishes no host port and does not resolve from sandbox.
#
# It stops and restarts `web` once (step 5).

PROJECT="${COMPOSE_PROJECT_NAME:-agento}"
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
PROXY="$(resolve_container proxy)"
WEB="$(resolve_container web)"
SANDBOX="$(resolve_container sandbox)"
ARTIFACTS="$(resolve_container artifacts)"

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

echo "2. web from sandbox"
for path in /internal/authz/app /internal/authz/share; do
  expect "$path, no header" 401 "$(from_sandbox "http://web:8000$path")"
  expect "$path, forged secret + identity headers" 401 "$(from_sandbox \
    -H 'X-Agento-Proxy-Auth: forged' -H 'X-Agento-User: admin' -H 'X-Forwarded-User: admin' \
    "http://web:8000$path")"
done
expect "/health" 200 "$(from_sandbox http://web:8000/health)"

echo "3. panel origin"
expect "/internal/authz/app through panel" 404 "$(via_proxy panel.localhost /internal/authz/app)"

echo "4. apps and share origins"
VERSION_PATH="/a/demo/v/v-20260925-120000-ab12/index.html"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
expect "apps version path, denied by web" 403 "$(via_proxy apps.localhost "$VERSION_PATH?cap=SECRETCAP&code=SECRETCODE")"
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

echo
echo "passed: $pass, failed: $fail"
[ "$fail" = 0 ]
