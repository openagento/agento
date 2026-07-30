# Docker Containers

Four containers on the `agento-net` bridge network.

## Services

| Service | Image | Role | Language |
|---------|-------|------|----------|
| **cron** | agento-cron | Job consumer + cron scheduler + Python CLI | Python |
| **toolbox** | agento-toolbox | MCP server — credential broker, tool execution | Node.js |
| **mysql** | mysql:8.0 | Job queue DB (`cron_agent`) | — |
| **sandbox** | agento-sandbox | Interactive agent execution (ad-hoc) | Python |

## Volume Mounts

### Shared

| Mount | Containers | Access | Purpose |
|-------|-----------|--------|---------|
| `modules/` | cron, toolbox, sandbox | read-only | Module manifests + config.json |
| `logs/` | cron, toolbox | read-write | Structured JSON logs |

### Agent-Only (cron + sandbox)

| Mount | Purpose |
|-------|---------|
| `workspace/` | Agent workspace — AGENTS.md, SOUL.md, systems/, app/, tmp/ |
| `tokens/` | OAuth credentials (Claude, Codex) |
| `id_rsa` | SSH key for git operations |

### Toolbox-Only

| Mount | Purpose |
|-------|---------|
| `modules/core/` | Core module toolbox JS (`src/agento/modules/`) |
| `modules/user/` | User module toolbox JS (`app/code/`) |
| `workspace/artifacts/` | Per-job writable directory (agent scratch, screenshots, videos, attachments) |

## Key Environment Variables

### Toolbox
- `CRONDB_*` — MySQL connection (job queue DB)
- `JIRA_HOST`, `JIRA_USER`, `JIRA_TOKEN` — Jira API (from secrets.env)
- `SMTP_*` — Email sending
- `AGENTO_ENCRYPTION_KEY` — Decrypt core_config_data secrets
- `CONFIG__*` — Config overrides (highest priority)

### Cron
- `MYSQL_*` — MySQL connection
- `DISABLE_LLM` — Skip LLM calls (testing)
- `AGENTO_ENCRYPTION_KEY` — Encrypt config:set values

## Network

All containers communicate on `agento-net` (bridge). DNS names match service names: `toolbox`, `mysql`.

Agent connects to Toolbox via MCP. Both agents use streamable HTTP at `http://toolbox:3001/mcp` — Claude via `.mcp.json` (`{"type": "http", "url": …}`; the `type` discriminator is mandatory, a typeless entry is dropped at validation), Codex via `.codex/config.toml`. Claude's toolbox entry also carries `"alwaysLoad": true`: Claude connects MCP servers non-blocking by default, so without it `system/init` always reports the toolbox as `pending` (and `job.toolbox_mcp_connected` can never be `TRUE`). `alwaysLoad` makes the CLI await that one handshake before emitting init, bounded by `MCP_CONNECT_TIMEOUT_MS` (default 5 s), after which it proceeds with the connect continuing in the background. Agento injects `alwaysLoad` **only** into the auto-injected toolbox entry: operator servers from `agent_view/mcp/servers` are preserved wholesale, so they stay non-blocking unless the operator explicitly opts in by setting `"alwaysLoad": true` themselves — and an operator entry that shadows `toolbox` replaces ours, opting out of the injected one. Each agent's `ConfigWriter` constructs the URL from the shared `core/toolbox/url` base value. The deprecated SSE transport at `/sse` is still served for operator-pinned `type: sse` entries.

Source: [docker/docker-compose.yml](../../docker/docker-compose.yml)
