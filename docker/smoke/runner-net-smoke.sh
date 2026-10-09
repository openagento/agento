#!/bin/bash
set -uo pipefail

# runner-net-smoke.sh — checks every cell of the reachability table in
# docs/architecture/containers.md#networks. OPT-IN: needs the whole stack up, incl. runner-1.
#
#   bash docker/smoke/runner-net-smoke.sh [--project <compose project, default: from docker/.env>]
#
# "yes" = a TCP connect works (where nothing listens: the name resolves). "no" = the name does
# not resolve AND a connect to the target's IP fails. Only "no" cells are security claims.
# host.docker.internal is checked on Linux only (Docker Desktop forwards it to host loopback,
# where the guard is MySQL auth: the runner holds no DB user). Not covered: the runner
# supervision step of docs/architecture/runner.md.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_PROJECT="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$PROJECT_DIR/docker/.env" 2>/dev/null | tail -1)"
PROJECT="${COMPOSE_PROJECT_NAME:-${ENV_PROJECT:-agento}}"
[ "${1:-}" = --project ] && PROJECT="$2"

ctr() {  # the one running container of a service
  local ids
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1")
  [ "$(printf '%s\n' "$ids" | grep -c .)" = 1 ] || { echo "Need one running '$1' in '$PROJECT'." >&2; return 1; }
  echo "$ids"
}
for s in web cron runner-1 sandbox toolbox mysql proxy; do ctr "$s" >/dev/null || exit 1; done

pass=0; fail=0
ok()  { echo "  ok   $1"; pass=$((pass+1)); }
bad() { echo "  FAIL $1"; fail=$((fail+1)); }

# Host and port go in as ENV, never spliced into the probe source. toolbox has node, no bash.
tcp() {  # tcp <service> <host> <port>
  if [ "$1" = toolbox ]; then
    docker exec -e H="$2" -e P="$3" "$(ctr toolbox)" node -e '
      require("net").connect(+process.env.P, process.env.H, () => process.exit(0))
        .on("error", () => process.exit(1)); setTimeout(() => process.exit(1), 3000);' >/dev/null 2>&1
  else
    docker exec -e H="$2" -e P="$3" "$(ctr "$1")" timeout 3 bash -c '</dev/tcp/$H/$P' >/dev/null 2>&1
  fi
}
resolves() {  # resolves <service> <name>
  if [ "$1" = toolbox ]; then
    docker exec -e H="$2" "$(ctr toolbox)" node -e '
      require("dns").lookup(process.env.H, e => process.exit(e ? 1 : 0));' >/dev/null 2>&1
  else
    docker exec "$(ctr "$1")" getent hosts "$2" >/dev/null 2>&1
  fi
}
yes_tcp() { tcp "$1" "$2" "$3" && ok "$1 -> $2:$3 connects" || bad "$1 -> $2:$3 should connect"; }
yes_dns() { resolves "$1" "$2" && ok "$1 -> $2 resolves" || bad "$1 -> $2 should resolve"; }
no_tcp()  { tcp "$1" "$2" "$3" && bad "$1 -> $2:$3 connects" || ok "$1 -> $2:$3 no route"; }
no_route() {  # no_route <from> <to service> <port>
  if resolves "$1" "$2"; then bad "$1 -> $2 resolves (shared network)"; return; fi
  no_tcp "$1" "$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
    "$(ctr "$2")" | awk '{print $1}')" "$3"
}

MYSQL_HOST_PORT="$(docker port "$(ctr mysql)" 3306/tcp 2>/dev/null | head -1 | sed 's/.*://')"
MYSQL_HOST_PORT="${MYSQL_HOST_PORT:-3306}"
GATEWAY="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' "${PROJECT}_exec-net" 2>/dev/null)"
docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -q "Docker Desktop" && DESKTOP=1 || DESKTOP=0

echo "1. web"
yes_dns web cron
no_route web runner-1 22
yes_tcp web toolbox 3001
yes_tcp web mysql 3306
yes_tcp web proxy 443

echo "2. cron"
yes_tcp cron web 8000
no_route cron runner-1 22
docker exec "$(ctr cron)" test -S /run/agento-runner/runner-1/runner-1.sock \
  && ok "cron -> runner-1 by socket only" || bad "cron has no runner-1 socket"
yes_tcp cron toolbox 3001
yes_tcp cron mysql 3306
no_route cron proxy 443

for from in runner-1 sandbox; do
  echo "3. $from"
  no_route "$from" web 8000
  no_route "$from" cron 22
  yes_tcp "$from" toolbox 3001
  no_route "$from" mysql 3306
  yes_tcp "$from" proxy 443
  if [ -n "$GATEWAY" ]; then no_tcp "$from" "$GATEWAY" "$MYSQL_HOST_PORT"; else bad "no gateway for ${PROJECT}_exec-net"; fi
  [ "$DESKTOP" = 1 ] && echo "  skip $from -> host.docker.internal (Docker Desktop)" \
    || no_tcp "$from" host.docker.internal "$MYSQL_HOST_PORT"
done

echo "4. toolbox"
yes_tcp toolbox web 8000
STATUS="$(docker exec "$(ctr toolbox)" node -e '
  fetch("http://web:8000/api/admin/dashboard").then(r => console.log(r.status), () => console.log(0));' 2>/dev/null)"
[ "$STATUS" = 401 ] && ok "toolbox -> web, no session: 401" || bad "toolbox -> web, no session: got $STATUS, not 401"
yes_dns toolbox cron
yes_dns toolbox runner-1
yes_tcp toolbox mysql 3306
yes_tcp toolbox proxy 443

echo "passed: $pass, failed: $fail"
[ "$fail" = 0 ]
