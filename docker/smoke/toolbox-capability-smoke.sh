#!/bin/bash
set -uo pipefail

# toolbox-capability-smoke.sh — container smoke test for toolbox east-west authentication.
#
# OPT-IN. It is deliberately NOT wired into bin/test, which is unit-only and must stay
# Docker-free. It needs a RUNNING dev stack:
#
#   cd docker && docker compose -f docker-compose.dev.yml up -d
#   bash docker/smoke/toolbox-capability-smoke.sh --agent-view <code> [--other-agent-view <code>] \
#        [--project <compose project, default: agento>]
#
# It proves, against the real containers:
#   1. /mcp and /sse: no capability -> 401; a random bearer -> 403.
#   2. every /api/* route: no capability -> 401.
#   3. /health -> 200 unauthenticated; /health?test=true -> 401 unauthenticated.
#   4. an internal_rest token drives /api and the scoped diagnostic for ITS OWN view only.
#   5. a token minted for one view cannot select another view (body mismatch is refused).
#   6. the cron container holds no Outlook secret — checked by KEY, never by value.
#   7. two capabilities survive a toolbox restart, and while BOTH MCP sessions are open at the
#      same time each one resolves only its own view — and neither transport lets one session be
#      driven by another view's capability (/mcp and /messages both answer 403) or by none (401).
#   8. a BIGINT job_id survives the live mysql2 driver exactly (the unit suite can only assert
#      that the CAST is in the statement).
#   9. a revoked token stops the next /mcp request.
#
# What it does NOT prove: that one concurrent run cannot READ another run's live token off the
# shared workspace. Every agent process runs as the same `agent` account, so that is not a
# filesystem question — see the co-tenant paragraph in docs/architecture/zero-trust.md.
#
# Item 4's "read/reply/mark only the triggering message" for a real Outlook job needs live
# Graph credentials and a real job; it is asserted here at the capability layer (job scope comes
# from the row) and covered end-to-end by tests/integration/test_outlook_publish_pipeline.py.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
AGENTO="$PROJECT_DIR/bin/agento"

AGENT_VIEW=""
OTHER_VIEW=""
OTHER_ID=""
PROJECT="${COMPOSE_PROJECT_NAME:-agento}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-view) AGENT_VIEW="$2"; shift 2 ;;
    --other-agent-view) OTHER_VIEW="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "Usage: $0 --agent-view <code> [--other-agent-view <code>] [--project <name>]"; exit 1 ;;
  esac
done
[ -n "$AGENT_VIEW" ] || { echo "--agent-view is required"; exit 1; }

# Resolve the containers by their COMPOSE LABELS, never by a guessed name. Compose names a
# container "<project>-<service>-1", and one host commonly runs several deployments of this
# stack, so a fixed name either misses every container or — worse — hits the wrong deployment
# and reports its results as this one. Exactly one match is required.
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
TOOLBOX="$(resolve_container toolbox)"
CRON="$(resolve_container cron)"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass=0; fail=0

ok()   { echo -e "  ${GREEN}✓${NC} $1"; pass=$((pass+1)); }
bad()  { echo -e "  ${RED}✗${NC} $1"; fail=$((fail+1)); }
note() { echo -e "  ${YELLOW}⊘${NC} $1"; }

REST_TOKEN=""; REST_TOKEN_A=""; REST_TOKEN_B=""; MCP_TOKEN=""; MCP_TOKEN_B=""
# Step 7 writes a per-view marker row. Each view whose row THIS RUN created is recorded here, so
# an interrupt between the two writes restores exactly what was created and nothing else. A view
# that already had a row is never listed, because step 7 refuses to touch one.
MARKER_TOOL=schedule_followup
MARKER_MADE=""
cleanup() {
  for t in "$REST_TOKEN" "$REST_TOKEN_A" "$REST_TOKEN_B" "$MCP_TOKEN" "$MCP_TOKEN_B"; do
    [ -n "$t" ] && printf '%s' "$t" | "$AGENTO" capability:revoke >/dev/null 2>&1 || true
  done
  for v in $MARKER_MADE; do
    "$AGENTO" config:remove "tools/$MARKER_TOOL/is_enabled" --agent-view "$v" >/dev/null 2>&1 ||
      echo "  could not remove tools/$MARKER_TOOL/is_enabled for agent_view '$v' — remove it by hand" >&2
  done
}
trap cleanup EXIT

# status <method> <path> [bearer]  -> prints the HTTP status code, nothing else.
# The token goes in on STDIN, never in argv (ps / audit logs / set -x read argv).
status() {
  local method="$1" path="$2" tok="${3:-}"
  # The method and the path go in as ENV, never spliced into the JS source: a shell value
  # pasted into executable text is an injection, and it stays one even when today's callers
  # only pass literals.
  printf '%s' "$tok" | docker exec -i -e M="$method" -e P="$path" "$TOOLBOX" node -e '
    let tok = "";
    process.stdin.on("data", d => tok += d);
    process.stdin.on("end", async () => {
      const headers = { "content-type": "application/json" };
      if (tok.trim()) headers.authorization = "Bearer " + tok.trim();
      const init = { method: process.env.M, headers };
      if (process.env.M === "POST") init.body = "{}";
      try {
        const r = await fetch("http://localhost:3001" + process.env.P, init);
        console.log(r.status);
        // /sse is an endless stream: without cancelling the body this process never exits.
        if (r.body) { try { await r.body.cancel(); } catch (e) {} }
      } catch (e) { console.log("ERR"); }
    });
  ' 2>/dev/null
}

# scoped_view <bearer> -> prints the agent_view_id the toolbox RESOLVED for this token, or "".
# Two 200s prove both tokens authenticate; only the resolved id proves each is confined to its own
# view. The token goes in on stdin, and nothing is spliced into the JS source.
view_id() {  # view_id <agent_view code> -> the numeric id, or "" if it does not resolve
  # VIEW_CODE arrives as an environment value. Interpolating it into the Python source would
  # let `--other-agent-view "x'"'"'); import os; os.system(...)"` execute inside the cron
  # container — the one container that holds secrets.
  docker exec -e VIEW_CODE="$1" "$CRON" python -c '
import os
from agento.framework.cli.runtime import _load_framework_config
from agento.framework.db import get_connection_or_exit
from agento.framework.workspace import get_agent_view_by_code
db_config, _, _ = _load_framework_config()
conn = get_connection_or_exit(db_config)
av = get_agent_view_by_code(conn, os.environ["VIEW_CODE"])
print(av.id if av else "")
' 2>/dev/null | tr -d '[:space:]'
}

# two_live_sessions <mcpA> <mcpB> <restA> <restB> <markerTool>
# Prints, space separated:
#   <sseA> <sseB> <viewA> <viewB> <mcpCross> <msgCross> <msgNoAuth> <msgOwn> <markerA> <markerB>
two_live_sessions() {
  # Both /sse streams are held OPEN at the same time, and everything below is probed WHILE they
  # are. Two sequential probes cannot see the failure this exists for: a scope parked in a
  # process global, or a shared overrides object, only crosses when two sessions overlap in time.
  # All four tokens arrive on stdin — never argv, which `ps` reads for the life of the call.
  printf '%s\n%s\n%s\n%s\n%s\n' "$1" "$2" "$3" "$4" "$5" | docker exec -i "$TOOLBOX" node -e '
    let buf = "";
    process.stdin.on("data", d => buf += d);
    process.stdin.on("end", async () => {
      const [mcpA, mcpB, restA, restB, marker] = buf.split("\n").map(t => t.trim());
      const base = "http://localhost:3001";
      const bearer = (tok) => ({ authorization: "Bearer " + tok });

      // Open an SSE stream and read its first `endpoint` event, which carries the sessionId
      // the /messages half is addressed by. The stream stays open until close() is called.
      const openSse = async (tok) => {
        const c = new AbortController();
        const r = await fetch(base + "/sse", { headers: bearer(tok), signal: c.signal });
        let sessionId = "";
        if (r.ok && r.body) {
          const reader = r.body.getReader();
          const dec = new TextDecoder();
          let text = "";
          const deadline = Date.now() + 5000;
          while (!/sessionId=[0-9a-f-]+/.test(text) && Date.now() < deadline) {
            const { value, done } = await reader.read();
            if (done) break;
            text += dec.decode(value, { stream: true });
          }
          const m = text.match(/sessionId=([0-9a-f-]+)/);
          if (m) sessionId = m[1];
        }
        return { status: r.status, sessionId, close: () => c.abort() };
      };

      const view = async (tok) => {
        const r = await fetch(base + "/health?test=true", { headers: bearer(tok) });
        const b = await r.json().catch(() => ({}));
        return b.agent_view_id === undefined ? "" : String(b.agent_view_id);
      };

      const post = async (path, tok, body) => {
        const headers = { "content-type": "application/json", accept: "application/json, text/event-stream" };
        if (tok) Object.assign(headers, bearer(tok));
        const r = await fetch(base + path, { method: "POST", headers, body: JSON.stringify(body) });
        return r;
      };

      const INIT = {
        jsonrpc: "2.0", id: 1, method: "initialize",
        params: {
          protocolVersion: "2024-11-05", capabilities: {},
          clientInfo: { name: "capability-smoke", version: "0" },
        },
      };

      let a, b;
      try {
        [a, b] = await Promise.all([openSse(mcpA), openSse(mcpB)]);
        const [va, vb] = await Promise.all([view(restA), view(restB)]);

        // A real Streamable-HTTP session for EACH capability, driven CONCURRENTLY. Both are
        // initialized, both run tools/list, and the two lists are compared against a marker
        // tool enabled for the view of A only. Authenticating proves nothing about scope: a
        // toolbox that built the MCP server of B from the resolved configuration of A would
        // authenticate B perfectly and still hand it the tools of A.
        const openMcp = async (tok) => {
          const r = await post("/mcp", tok, INIT);
          const sid = r.headers.get("mcp-session-id") || "";
          if (sid) {
            // The SDK server answers tools/list only after the initialized notification.
            await fetch(base + "/mcp", {
              method: "POST",
              headers: {
                "content-type": "application/json",
                accept: "application/json, text/event-stream",
                "mcp-session-id": sid,
                ...bearer(tok),
              },
              body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
            });
          }
          return sid;
        };

        // "no-session" and "no" are DIFFERENT outcomes: a probe that could not list the tools
        // must never read as the marker being correctly absent.
        const hasMarker = async (tok, sid) => {
          if (!sid) return "no-session";
          const r = await fetch(base + "/mcp", {
            method: "POST",
            headers: {
              "content-type": "application/json",
              accept: "application/json, text/event-stream",
              "mcp-session-id": sid,
              ...bearer(tok),
            },
            body: JSON.stringify({ jsonrpc: "2.0", id: 3, method: "tools/list", params: {} }),
          });
          if (!r.ok) return "list-" + r.status;
          const text = await r.text();
          if (!/"tools"\s*:/.test(text)) return "no-list";
          // Whitespace-stripped so the check does not depend on how the SDK serializes.
          const flat = text.replace(/\s+/g, "");
          return flat.includes("\"name\":\"" + marker + "\"") ? "yes" : "no";
        };

        const [sidA, sidB] = await Promise.all([openMcp(mcpA), openMcp(mcpB)]);
        const [markerA, markerB] = await Promise.all([
          hasMarker(mcpA, sidA),
          hasMarker(mcpB, sidB),
        ]);

        // B tries to drive the live session of A. A session is owned by the capability that opened
        // it, so a different — still VALID — capability must be refused rather than inheriting
        // the scope of A.
        let mcpCross = "no-session";
        if (sidA) {
          const r = await fetch(base + "/mcp", {
            method: "POST",
            headers: {
              "content-type": "application/json",
              accept: "application/json, text/event-stream",
              "mcp-session-id": sidA,
              ...bearer(mcpB),
            },
            body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }),
          });
          mcpCross = String(r.status);
        }

        // The SSE half: /messages carries the tool CALLS, and its sessionId travels in a query
        // string — a value access logs keep. The id alone must authorize nothing.
        let msgCross = "no-session", msgNoAuth = "no-session", msgOwn = "no-session";
        if (a.sessionId) {
          const path = "/messages?sessionId=" + a.sessionId;
          msgCross = String((await post(path, mcpB, INIT)).status);
          msgNoAuth = String((await post(path, null, INIT)).status);
          msgOwn = String((await post(path, mcpA, INIT)).status);
        }
        console.log([a.status, b.status, va, vb, mcpCross, msgCross, msgNoAuth, msgOwn,
                     markerA, markerB].join(" "));
      } catch (e) {
        console.log("ERR ERR   ERR ERR ERR ERR ERR ERR");
      } finally {
        if (a) a.close();
        if (b) b.close();
      }
    });
  ' 2>/dev/null
}

marker_row_exists() {  # marker_row_exists <agent_view id> -> "yes" | "no" | "" if the probe failed
  # The EXACT (scope, scope_id, path) row, not the resolved value: only a row this run created
  # may be deleted afterwards, and a resolved value cannot tell the two apart.
  docker exec -e VIEW_ID="$1" -e MPATH="tools/$MARKER_TOOL/is_enabled" "$CRON" python -c '
import os
from agento.framework.cli.runtime import _load_framework_config
from agento.framework.db import get_connection_or_exit
db_config, _, _ = _load_framework_config()
conn = get_connection_or_exit(db_config)
with conn.cursor() as cur:
    cur.execute(
        "SELECT 1 FROM core_config_data WHERE path=%s AND scope=%s AND scope_id=%s",
        (os.environ["MPATH"], "agent_view", int(os.environ["VIEW_ID"])),
    )
    print("yes" if cur.fetchone() else "no")
' 2>/dev/null | tr -d '[:space:]'
}

scoped_view() {
  printf '%s' "$1" | docker exec -i "$TOOLBOX" node -e '
    let tok = "";
    process.stdin.on("data", d => tok += d);
    process.stdin.on("end", async () => {
      try {
        const r = await fetch("http://localhost:3001/health?test=true", {
          headers: { authorization: "Bearer " + tok.trim() },
        });
        const body = await r.json();
        console.log(body.agent_view_id === undefined ? "" : String(body.agent_view_id));
      } catch (e) { console.log(""); }
    });
  ' 2>/dev/null | tr -d "[:space:]"
}

expect() {  # expect <label> <expected> <actual>
  if [ "$3" = "$2" ]; then ok "$1 -> $3"; else bad "$1 -> got $3, want $2"; fi
}

echo -e "${CYAN}=== Toolbox capability smoke test ===${NC}"
for c in "$TOOLBOX" "$CRON"; do
  docker inspect "$c" --format='{{.State.Running}}' 2>/dev/null | grep -q true || {
    echo -e "${RED}✗${NC} container $c is not running"; exit 1; }
done

# ---------------------------------------------------------------- 1. MCP routes
echo -e "\n${CYAN}[1] MCP routes reject an unauthenticated or bogus caller${NC}"
expect "POST /mcp with no capability"  401 "$(status POST /mcp)"
expect "GET  /sse with no capability"  401 "$(status GET  /sse)"
expect "POST /mcp with a random bearer" 403 "$(status POST /mcp 'not-a-real-capability')"
expect "GET  /sse with a random bearer" 403 "$(status GET  /sse 'not-a-real-capability')"

# ---------------------------------------------------------------- 2. every /api route
echo -e "\n${CYAN}[2] Every module REST route rejects an unauthenticated caller${NC}"
for route in /api/outlook/delta /api/github/verify /api/github/open-prs \
             /api/bitbucket/verify /api/bitbucket/open-prs \
             /api/jira/search /api/jira/issue/comments /api/jira/request; do
  expect "POST $route with no capability" 401 "$(status POST "$route")"
done
# Any route under /api inherits the guard, including one that does not exist.
expect "POST /api/does-not-exist with no capability" 401 "$(status POST /api/does-not-exist)"

# ---------------------------------------------------------------- 3. health split
echo -e "\n${CYAN}[3] Liveness is open, the scoped diagnostic is not${NC}"
expect "GET /health (liveness)"                200 "$(status GET /health)"
expect "GET /health?test=true unauthenticated" 401 "$(status GET '/health?test=true')"
expect "GET /health?agent_view_id=1 unauthenticated" 401 "$(status GET '/health?agent_view_id=1')"

# ---------------------------------------------------------------- 4. a real capability works
echo -e "\n${CYAN}[4] A real internal_rest capability is accepted for its own view${NC}"
REST_TOKEN=$("$AGENTO" capability:mint --kind internal_rest --agent-view "$AGENT_VIEW") || {
  bad "capability:mint --kind internal_rest failed"; REST_TOKEN=""; }
if [ -n "$REST_TOKEN" ]; then
  expect "GET /health?test=true with internal_rest" 200 "$(status GET '/health?test=true' "$REST_TOKEN")"
  # An MCP kind must NOT reach the scoped diagnostic, and internal_rest must not reach /mcp.
  MCP_TOKEN=$("$AGENTO" capability:mint --kind mcp_interactive --agent-view "$AGENT_VIEW") || MCP_TOKEN=""
  [ -n "$MCP_TOKEN" ] && expect "GET /health?test=true with an MCP kind" 403 "$(status GET '/health?test=true' "$MCP_TOKEN")"
  expect "POST /mcp with internal_rest" 403 "$(status POST /mcp "$REST_TOKEN")"
fi

# ---------------------------------------------------------------- 5. cross-view escalation
echo -e "\n${CYAN}[5] A capability cannot select another view${NC}"
if [ -z "$OTHER_VIEW" ]; then
  note "skipped — pass --other-agent-view <code> to exercise cross-view escalation"
else
  # VIEW_CODE arrives as an environment value. Interpolating "$OTHER_VIEW" into the Python
  # source would let `--other-agent-view "x'); import os; os.system(...)"` execute inside the
  # cron container — the one container that holds secrets.
  OTHER_ID=$(view_id "$OTHER_VIEW")
  if [ -z "$OTHER_ID" ]; then
    note "could not resolve the id of agent_view '$OTHER_VIEW' — skipping"
  else
    CODE=$(printf '%s' "$REST_TOKEN" | docker exec -i -e OTHER_ID="$OTHER_ID" "$TOOLBOX" node -e '
      let tok=""; process.stdin.on("data",d=>tok+=d); process.stdin.on("end", async () => {
        const r = await fetch("http://localhost:3001/api/jira/search", {
          method:"POST",
          headers:{"content-type":"application/json", authorization:"Bearer "+tok.trim()},
          body: JSON.stringify({ agent_view_id: Number(process.env.OTHER_ID), jql: "order by created" }),
        });
        console.log(r.status);
      });' 2>/dev/null)
    # A body agent_view_id may only MATCH the capability's scope; naming another view is refused.
    if [ "$CODE" = "400" ] || [ "$CODE" = "403" ]; then
      ok "body agent_view_id=$OTHER_ID with a token for '$AGENT_VIEW' -> $CODE"
    else
      bad "cross-view body claim -> got $CODE, want 400/403"
    fi
  fi
fi

# ---------------------------------------------------------------- 6. no secret in the cron env
echo -e "\n${CYAN}[6] The cron container holds no Outlook secret${NC}"
# KEYS ONLY. `env | grep` would print part of a VALUE when the assertion fails, which is the
# leak this check exists to disprove.
# The probe prints a sentinel FIRST, so "no keys" and "the probe never ran" are different
# outputs. Without it a container that refuses the exec produces empty output — which reads
# as the clean result this check exists to prove.
KEYS=$(docker exec "$CRON" python -c \
  'import os; print("PROBE_OK"); print("\n".join(sorted(k for k in os.environ if "outlook" in k.lower())))' 2>/dev/null)
case "$KEYS" in
  PROBE_OK) ok "no outlook-named env key in the cron container" ;;
  PROBE_OK*) bad "cron env keys:${KEYS#PROBE_OK}" ;;
  *) bad "the env-key probe could not run in the cron container" ;;
esac

RESOLVED=$(docker exec "$CRON" python -c '
from agento.framework.config_resolver import resolve_field
rv = resolve_field("outlook", "outlook_client_secret",
                   {"type": "obscure", "access": "toolbox_only"}, {}, {})
print("None" if rv.value is None else "LEAKED")
' 2>/dev/null)
if [ "$RESOLVED" = "None" ]; then
  ok "cron resolves outlook_client_secret as None (toolbox_only)"
else
  bad "cron resolved outlook_client_secret: $RESOLVED"
fi

# ---------------------------------------------------------------- 7. isolation across a restart
# Both MCP streams are held open at the SAME time, and every claim below is checked while they
# are. Two sequential probes prove only that a restart invalidates neither and merges neither; a
# scope kept in a process global crosses solely when two sessions overlap in time.
echo -e "\n${CYAN}[7] Two live sessions stay valid, isolated and unshareable across a restart${NC}"
OWN_ID=$(view_id "$AGENT_VIEW")
if [ -z "$OTHER_VIEW" ] || [ -z "$OTHER_ID" ] || [ -z "$OWN_ID" ]; then
  note "skipped — needs --other-agent-view <code>, and both view ids must resolve"
else
  MCP_TOKEN_B=$("$AGENTO" capability:mint --kind mcp_interactive --agent-view "$OTHER_VIEW") || MCP_TOKEN_B=""
  # A per-view MARKER so the two concurrent MCP sessions return a DETERMINISTIC, view-specific
  # result instead of only authenticating. `schedule_followup` gates on nothing but its own
  # is_enabled key, and agent_view is the most specific scope, so 1 for one view and 0 for the
  # other decides the tool list in BOTH directions regardless of the workspace/global default.
  MARKER_SET=0
  # `tool:enable`/`tool:disable` are UPSERTS, so writing over a row this deployment already has
  # would destroy a real setting — and deleting it afterwards would silently fall back to the
  # shipped default, which for an is_enabled key can WIDEN tool access. The probe therefore
  # refuses to touch a view that already carries the row, and records only what it creates.
  HAD_A=$(marker_row_exists "$OWN_ID"); HAD_B=$(marker_row_exists "$OTHER_ID")
  if [ -z "$HAD_A" ] || [ -z "$HAD_B" ]; then
    # A probe that could not RUN is not a benign skip. Empty means the cron exec failed, and
    # without it nothing knows whether writing the marker would destroy a real setting.
    bad "the marker-row existence probe could not run — concurrent MCP scope isolation was NOT proven"
  elif [ "$HAD_A" != "no" ] || [ "$HAD_B" != "no" ]; then
    note "agent_view scope already sets tools/$MARKER_TOOL/is_enabled (A=$HAD_A B=$HAD_B) — the MCP scope comparison is skipped rather than overwrite it"
  elif "$AGENTO" tool:enable "$MARKER_TOOL" --agent-view "$AGENT_VIEW" >/dev/null 2>&1; then
    MARKER_MADE="$AGENT_VIEW"
    if "$AGENTO" tool:disable "$MARKER_TOOL" --agent-view "$OTHER_VIEW" >/dev/null 2>&1; then
      MARKER_MADE="$AGENT_VIEW $OTHER_VIEW"
      MARKER_SET=1
    else
      note "could not set the per-view marker for '$OTHER_VIEW' — the MCP scope comparison will be reported as unrun"
    fi
  else
    note "could not set the per-view marker for '$AGENT_VIEW' — the MCP scope comparison will be reported as unrun"
  fi
  # Restart the container this run RESOLVED, not "whatever compose picks up in ./docker": a
  # `docker compose restart` there obeys its own default project name, so with --project foo it
  # would restart the `agento` toolbox while this run probes foo's — the health check would then
  # pass without a restart ever touching the container under test, and a live deployment nobody
  # asked about would be disturbed. A restart that did not happen is a FAILED probe, not a skip,
  # so the failure is not swallowed and StartedAt must actually move.
  STARTED_BEFORE=$(docker inspect --format '{{.State.StartedAt}}' "$TOOLBOX" 2>/dev/null || true)
  if ! docker restart "$TOOLBOX" >/dev/null 2>&1; then
    bad "could not restart '$TOOLBOX' — capability survival across a restart was NOT proven"
  fi
  for _ in $(seq 1 30); do [ "$(status GET /health)" = "200" ] && break; sleep 1; done
  STARTED_AFTER=$(docker inspect --format '{{.State.StartedAt}}' "$TOOLBOX" 2>/dev/null || true)
  if [ -z "$STARTED_BEFORE" ] || [ -z "$STARTED_AFTER" ] || [ "$STARTED_BEFORE" = "$STARTED_AFTER" ]; then
    bad "'$TOOLBOX' did not restart (StartedAt unchanged) — capability survival across a restart was NOT proven"
  fi
  # internal_rest lives 120 s. The restart plus the wait above can spend most of that, so the
  # REST tokens for this step are minted HERE — reusing step 4's would make the probe fail on
  # its own clock and read as a scope defect.
  REST_TOKEN_A=$("$AGENTO" capability:mint --kind internal_rest --agent-view "$AGENT_VIEW") || REST_TOKEN_A=""
  REST_TOKEN_B=$("$AGENTO" capability:mint --kind internal_rest --agent-view "$OTHER_VIEW") || REST_TOKEN_B=""
  if [ -z "$MCP_TOKEN" ] || [ -z "$MCP_TOKEN_B" ] || [ -z "$REST_TOKEN_A" ] || [ -z "$REST_TOKEN_B" ]; then
    bad "could not mint the capabilities this probe needs — the isolation probe cannot run"
  else
    read -r A B VIEW_A VIEW_B MCP_CROSS MSG_CROSS MSG_NOAUTH MSG_OWN MARKER_A MARKER_B \
      <<<"$(two_live_sessions "$MCP_TOKEN" "$MCP_TOKEN_B" "$REST_TOKEN_A" "$REST_TOKEN_B" "$MARKER_TOOL")"
    # Capabilities live in the DB, so a restart does not invalidate them and does not merge them.
    # An explicit 200 for each. "anything but 401/403" also passes on ERR, 400, 404 and 500 —
    # every way the probe itself can fail reads as a success.
    if [ "$A" = "200" ] && [ "$B" = "200" ]; then
      ok "both sessions authenticate and stay open together after a restart (A=$A B=$B)"
    else
      bad "concurrent post-restart session states A=$A B=$B"
    fi
    # Authenticating is not isolation. With both sessions still open, compare the view the
    # toolbox RESOLVED for each internal_rest token against the EXACT id each was minted for.
    # "A != B" alone passes when both are wrong; the ids say which view each token really got.
    if [ "$VIEW_A" = "$OWN_ID" ] && [ "$VIEW_B" = "$OTHER_ID" ]; then
      ok "each token resolves to its OWN view while both sessions are live (A=$VIEW_A B=$VIEW_B)"
    else
      bad "scope isolation: A resolved to '$VIEW_A' (want $OWN_ID), B to '$VIEW_B' (want $OTHER_ID)"
    fi
    # A live session belongs to the capability that opened it. B's token is VALID — it just is
    # not this session's — so a 403 here is the difference between per-view scoping and a
    # session id that anyone who learns it can drive.
    expect "POST /mcp on A's session with B's capability" 403 "$MCP_CROSS"
    # The SSE half carries the tool calls, and its sessionId travels in a query string that
    # access logs keep. Unauthenticated is 401, another view's capability is 403, and the
    # session's own capability still works — otherwise the guard broke the transport.
    expect "POST /messages on A's session with B's capability" 403 "$MSG_CROSS"
    expect "POST /messages on A's session with no capability"   401 "$MSG_NOAUTH"
    if [ "$MSG_OWN" = "202" ] || [ "$MSG_OWN" = "200" ]; then
      ok "POST /messages on A's session with A's own capability -> $MSG_OWN"
    else
      bad "POST /messages with the session's OWN capability -> $MSG_OWN (want 200/202)"
    fi
    # Both MCP sessions were initialized and listed their tools WHILE both were open. The marker
    # is enabled for one view and disabled for the other, so the two lists must differ in exactly
    # that tool. This is the claim `/health` cannot make: it is the MCP server itself, per
    # session, resolving its own scope.
    if [ "$MARKER_SET" != 1 ] && [ "$HAD_A" = "yes" -o "$HAD_B" = "yes" ]; then
      note "concurrent MCP scope isolation not proven this run — the marker rows are pre-existing"
    elif [ "$MARKER_SET" != 1 ]; then
      # Covers both "could not write" and "could not read the prior state". Neither proves scope.
      bad "the per-view marker could not be set — concurrent MCP scope isolation was NOT proven"
    elif [ "$MARKER_A" = "yes" ] && [ "$MARKER_B" = "no" ]; then
      ok "each live MCP session lists the tools of its OWN view ($MARKER_TOOL: A=yes B=no)"
    else
      bad "concurrent MCP scope: $MARKER_TOOL seen A=$MARKER_A B=$MARKER_B (want A=yes B=no)"
    fi
    # And the query string cannot re-point a token at the other view: it is compared, never used.
    CODE=$(status GET "/health?test=true&agent_view_id=$OTHER_ID" "$REST_TOKEN_A")
    expect "GET /health?agent_view_id=<other view> with A's token" 400 "$CODE"
  fi
fi

# ---------------------------------------------------------------- 8. BIGINT job_id, live driver
echo -e "\n${CYAN}[8] A job_id above 2^53 survives the real mysql2 driver exactly${NC}"
# The unit suite can only assert the CAST is IN the statement — a fake query returning a string
# proves the fake. This is the live-driver half: a real row, the real pool, the real verifier.
BIG_JOB_ID=9007199254740993
PROBE=$(docker exec -e BIG_JOB_ID="$BIG_JOB_ID" -e VIEW_CODE="$AGENT_VIEW" "$CRON" python -c '
import os, secrets, hashlib
from agento.framework.cli.runtime import _load_framework_config
from agento.framework.db import get_connection_or_exit
from agento.framework.workspace import get_agent_view_by_code
db_config, _, _ = _load_framework_config()
conn = get_connection_or_exit(db_config)
av = get_agent_view_by_code(conn, os.environ["VIEW_CODE"])
token = secrets.token_urlsafe(32)
with conn.cursor() as cur:
    cur.execute(
        "INSERT INTO toolbox_capability (token_hash, kind, agent_view_id, job_id, expires_at) "
        "VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 5 MINUTE))",
        (hashlib.sha256(token.encode()).hexdigest(), "mcp_job", av.id, int(os.environ["BIG_JOB_ID"])),
    )
conn.commit()
print(token)
' 2>/dev/null | tr -d "[:space:]")
if [ -z "$PROBE" ]; then
  # Not a skip: the unit suite can only prove the CAST is in the statement, so if this probe
  # cannot run, nothing anywhere proves the driver does not round the job_id.
  bad "could not insert the BIGINT probe row — the live-driver check did not run"
else
  SEEN=$(printf '%s' "$PROBE" | docker exec -i "$TOOLBOX" node -e '
    let tok = "";
    process.stdin.on("data", d => tok += d);
    process.stdin.on("end", async () => {
      const { verifyCapability } = await import("/opt/agento-toolbox-src/capability.js");
      const claims = await verifyCapability(tok.trim(), { kinds: ["mcp_job"] });
      console.log(claims ? claims.jobId : "");
      process.exit(0);
    });
  ' 2>/dev/null | tr -d "[:space:]")
  if [ "$SEEN" = "$BIG_JOB_ID" ]; then
    ok "mysql2 returned job_id $SEEN exactly (not rounded)"
  else
    bad "job_id came back as '"'"'$SEEN'"'"', want $BIG_JOB_ID"
  fi
  printf '%s' "$PROBE" | "$AGENTO" capability:revoke >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- 9. revocation
echo -e "\n${CYAN}[9] A revoked token stops the next request${NC}"
if [ -n "$MCP_TOKEN" ]; then
  printf '%s' "$MCP_TOKEN" | "$AGENTO" capability:revoke >/dev/null 2>&1 || true
  # 403, not 401: the token is present, it is simply no longer valid.
  expect "POST /mcp with a revoked capability" 403 "$(status POST /mcp "$MCP_TOKEN")"
  MCP_TOKEN=""
else
  note "skipped — no mcp_interactive token was minted"
fi

echo ""
echo "========================================"
echo -e "Result: ${GREEN}${pass} OK${NC}, ${RED}${fail} errors${NC}"
[ "$fail" -eq 0 ]
