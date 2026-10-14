#!/bin/bash
set -euo pipefail

# test-sandbox.sh — Health check for the sandbox image (Claude Code), in runner-1
# Usage: bin/test-sandbox.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_DIR="$PROJECT_DIR/docker"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

pass=0
fail=0

# The sandbox image has no service: runner-1 runs the cron image, which is FROM it.
run_in_sandbox() {
  docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T -u agent runner-1 "$@" 2>/dev/null
}

echo -e "${CYAN}=== Sandbox Health Check ===${NC}"
echo ""

# Check runner-1 is running
if [ -z "$(docker compose -f "$COMPOSE_DIR/docker-compose.yml" ps -q runner-1 2>/dev/null)" ]; then
  echo -e "${RED}✗${NC} runner-1 is not running"
  echo -e "  Start: ${DIM}cd docker && docker compose up -d${NC}"
  exit 1
fi

# --- claude --version ---
echo -e "${CYAN}[1/2] Claude CLI binary${NC}"
echo "========================================"
VERSION=$(run_in_sandbox claude --version 2>&1) || true
if echo "$VERSION" | grep -q "Claude Code"; then
  echo -e "  ${GREEN}✓${NC} claude --version: $VERSION"
  ((pass++)) || true
else
  echo -e "  ${RED}✗${NC} claude --version failed: $VERSION"
  ((fail++)) || true
fi

echo ""

# --- claude prompt ---
echo -e "${CYAN}[2/2] Claude CLI prompt (auth + API)${NC}"
echo "========================================"
RESPONSE=$(run_in_sandbox claude -p "Respond with exactly: OK" --output-format json --max-turns 1 2>&1) || true
if echo "$RESPONSE" | grep -q '"result"'; then
  echo -e "  ${GREEN}✓${NC} claude prompt executed successfully"
  ((pass++)) || true
else
  # Check for common failures
  if echo "$RESPONSE" | grep -q "Not logged in"; then
    echo -e "  ${RED}✗${NC} claude prompt: not authenticated"
    echo -e "      ${DIM}Run: cp ~/.claude/.credentials.json workspace/.claude/${NC}"
  elif echo "$RESPONSE" | grep -q "corrupted"; then
    echo -e "  ${RED}✗${NC} claude prompt: .claude.json corrupted"
    echo -e "      ${DIM}Run: echo '{}' > workspace/.claude.json${NC}"
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
