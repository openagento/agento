#!/bin/bash
set -euo pipefail

# test-cron.sh — Health check for cron container (consumer + Claude CLI)
# Usage: bin/test-cron.sh

CONTAINER="agento-cron"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

pass=0
fail=0

agent_exec() {
  docker exec "$CONTAINER" su - agent -c "$*" 2>/dev/null
}

root_exec() {
  docker exec "$CONTAINER" "$@" 2>/dev/null
}

echo -e "${CYAN}=== Cron Container Health Check ===${NC}"
echo ""

# Check container running
if ! docker inspect "$CONTAINER" --format='{{.State.Running}}' 2>/dev/null | grep -q true; then
  echo -e "${RED}✗${NC} Container $CONTAINER is not running"
  echo -e "  Start: ${DIM}cd docker && docker compose up -d cron${NC}"
  exit 1
fi

# ============================================================
echo -e "${CYAN}[1/4] Processes${NC}"
echo "========================================"

# Consumer process
if root_exec pgrep -f "consumer" > /dev/null; then
  echo -e "  ${GREEN}✓${NC} consumer process running"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} consumer process not running"
  ((fail++)) || true
fi

# Cron daemon
if root_exec pgrep cron > /dev/null; then
  echo -e "  ${GREEN}✓${NC} cron daemon running"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} cron daemon not running"
  ((fail++)) || true
fi

echo ""

# ============================================================
echo -e "${CYAN}[2/4] Config files${NC}"
echo "========================================"

# .claude.json not empty
if root_exec test -s /workspace/.claude.json; then
  echo -e "  ${GREEN}✓${NC} .claude.json exists and is not empty"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} .claude.json is missing or empty (0 bytes)"
  echo -e "      ${DIM}Fix: echo '{}' > workspace/.claude.json${NC}"
  ((fail++)) || true
fi

# .credentials.json exists
if root_exec test -f /workspace/.claude/.credentials.json; then
  echo -e "  ${GREEN}✓${NC} .credentials.json exists"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} .credentials.json missing — Claude CLI won't authenticate"
  echo -e "      ${DIM}See README.md Auth section for setup instructions${NC}"
  ((fail++)) || true
fi

echo ""

# ============================================================
echo -e "${CYAN}[3/4] Claude CLI (as agent user)${NC}"
echo "========================================"

# claude --version
VERSION=$(agent_exec "claude --version" 2>&1) || true
if echo "$VERSION" | grep -q "Claude Code"; then
  echo -e "  ${GREEN}✓${NC} claude --version: $VERSION"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} claude --version failed: $VERSION"
  ((fail++)) || true
fi

echo ""

# ============================================================
echo -e "${CYAN}[4/4] Claude CLI prompt (as agent user)${NC}"
echo "========================================"

RESPONSE=$(agent_exec "cd /workspace && claude -p 'Respond with exactly: OK' --output-format json --max-turns 1" 2>&1) || true
if echo "$RESPONSE" | grep -qE '"is_error"[[:space:]]*:[[:space:]]*true'; then
  if echo "$RESPONSE" | grep -qiE "authentication_error|OAuth token has expired|Not logged in"; then
    echo -e "  ${RED}✗${NC} claude prompt: OAuth token expired — register the credential again"
    echo -e "      ${DIM}Run: agento credential:register claude <label>${NC}"
  else
    echo -e "  ${RED}✗${NC} claude prompt: CLI error — ${RESPONSE:0:200}"
  fi
  ((fail++)) || true
elif echo "$RESPONSE" | grep -q '"result"'; then
  echo -e "  ${GREEN}✓${NC} claude prompt executed successfully"
  ((pass++)) || true
else
  if echo "$RESPONSE" | grep -q "Not logged in"; then
    echo -e "  ${RED}✗${NC} claude prompt: not authenticated"
    echo -e "      ${DIM}See README.md Auth section for setup instructions${NC}"
  elif echo "$RESPONSE" | grep -q "corrupted"; then
    echo -e "  ${RED}✗${NC} claude prompt: .claude.json corrupted"
    echo -e "      ${DIM}Fix: echo '{}' > workspace/.claude.json${NC}"
  else
    echo -e "  ${RED}✗${NC} claude prompt failed: ${RESPONSE:0:200}"
  fi
  ((fail++)) || true
fi

# Summary
echo ""
echo "========================================"
echo -e "Result: ${GREEN}${pass} OK${NC}, ${RED}${fail} errors${NC}"

[ "$fail" -eq 0 ]
