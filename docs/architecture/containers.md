# Docker Containers

Seven containers. Six share the `agento-net` bridge network; `artifacts` declares no
`networks:` key and deliberately joins none of it. `proxy` is the only service on both
`agento-net` and the project `default` network that `artifacts` sits on.

## Services

| Service | Image | Role | Language |
|---------|-------|------|----------|
| **cron** | agento-cron | Job consumer + cron scheduler + Python CLI | Python |
| **toolbox** | agento-toolbox | MCP server — credential broker, tool execution | Node.js |
| **mysql** | mysql:8.0 | Job queue DB (`cron_agent`) | — |
| **sandbox** | agento-sandbox | Interactive agent execution (ad-hoc) | Python |
| **artifacts** | agento-toolbox | Static HTTP for the `versioned_artifacts` published tree | Node.js |
| **web** | agento-cron | Panel API (sign-in, roles and grants, admin, launches), the launch redeem, and `/internal/authz/app` for the proxy; holds no secret ([panel.md](panel.md)) | Python |
| **proxy** | caddy:2.11 | TLS, the panel / apps / share origins, `forward_auth` to `web`; the only route to artifact files | — |

## Volume Mounts

### Shared

| Mount | Containers | Access | Purpose |
|-------|-----------|--------|---------|
| `modules/` | cron, toolbox, sandbox | read-only | Module manifests + config.json |
| `logs/` | cron, toolbox | read-write | Structured JSON logs |

### Toolbox-Only

| Mount | Access | Purpose |
|-------|--------|---------|
| `storage/versioned-artifacts/` → `/srv/versioned-artifacts` | read-write | `versioned_artifacts` store (bare repos + draft checkouts) and its published tree (materialized versions plus the `current` symlink). Mounted into **toolbox only** — the sandbox and cron never see it, so the agent reaches versioned content exclusively through gated MCP tools. |

### Artifacts-Only

| Mount | Access | Purpose |
|-------|--------|---------|
| `storage/versioned-artifacts/published` → `/srv/published` | read-only | The published tree, and nothing else — never the store root. |
| `app/etc` → `/app/etc` | read-only | `modules.json` only, so `mo:di versioned_artifacts` makes every route answer 503 without a restart. |

The `artifacts` service declares **no `networks:` key** and carries no `env_file:` or
`environment:`. Compose therefore leaves it on the project's `default` network while every
other service names `agento-net` — measured: the sandbox cannot resolve the name
`artifacts`. It publishes **no host port**: the only other container on `default` is `proxy`,
which is the only route to these files, and it serves only `/a/<code>/v/<id>/` paths after
`web` authorizes them against a live launch ([panel.md](panel.md)). Shares are E6's and every
share request is denied until then, so VA `preview_url` links and Basic-auth shares are not
reachable from the host. Putting it on `agento-net` would let every agent in every
agent_view read every artifact over plain HTTP, bypassing `allowed_artifacts` with no audit row.

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

### Web
- `MYSQL_*` — MySQL connection (no `env_file`, no `AGENTO_ENCRYPTION_KEY`)
- `AGENTO_PANEL_HOST`, `AGENTO_APPS_HOST`, `AGENTO_PROXY_PORT` — the browser-facing origins, same defaults as `proxy`

### Cron
- `MYSQL_*` — MySQL connection
- `DISABLE_LLM` — Skip LLM calls (testing)
- `AGENTO_ENCRYPTION_KEY` — Encrypt config:set values

## Network

All containers **except `artifacts`** communicate on `agento-net` (bridge). DNS names match service names: `toolbox`, `mysql`, `web`. `artifacts` declares no `networks:` key, so Compose leaves it on the project `default` network, which it shares with `proxy` alone; no other container can even resolve its name.

### Proxy and web

`proxy` (Caddy, config in `src/agento/framework/docker/proxy/`) terminates TLS (`tls internal`
by default) on `127.0.0.1:${AGENTO_PROXY_PORT:-8443}` and serves three origins, named by
`AGENTO_PANEL_HOST`, `AGENTO_APPS_HOST` and `AGENTO_SHARE_HOST` (default `panel.localhost`,
`apps.localhost`, `*.share.localhost`):

- **panel** → `web:8000`, except `/internal/*`, which answers `404`;
- **apps** → only `/a/<code>/v/<id>/…`, authorized by `forward_auth` to `web`'s
  `/internal/authz/app`, then served by `artifacts`, and `POST /launch`, rewritten to `web`'s
  `/internal/launch/redeem` (the launch exchange); anything else is `404`;
- **share** (one origin per share) → `forward_auth` to `/internal/authz/share`.

Hardening: every caller-supplied `X-Agento-*`, `X-Forwarded-User`, `X-Remote-User` and
`Remote-User` header is dropped before any upstream or subrequest sees it; the proxy sets
`X-Agento-Proxy-Auth` only inside `forward_auth`. The `cap` and `code` (launch exchange code)
query values are replaced with `REDACTED` in both the access log and Caddy's error log, and no
route targets the toolbox.

`web` shares `agento-net` with `sandbox`, so **reachability is not trust**. The shared secret
`X-Agento-Proxy-Auth` lives in the `proxy-internal` volume, which only `proxy` (writes it once
at start) and `web` (reads it per request) mount. A request without it — a direct call from
`sandbox` — gets `401` on `/internal/authz/*`. Nothing on `web` may trust an identity or
forwarding header without that check.

Being on `agento-net` grants **reachability, not authorization**. The toolbox authenticates east-west
traffic itself: `/mcp`, `/sse`, `/config-test` and every `/api/*` route require a capability token and take their scope
from the `toolbox_capability` row, so a process on the network cannot pick a scope by asking for it. Bare
`/health` is unauthenticated **liveness only** (`200`, tool names and Playwright state). It runs no
adapter healthcheck and contacts no upstream service, but it is not free of I/O: deriving the tool
list reads the toolbox's configuration, which queries the configuration DB. The scoped
diagnostic `/health?test=true` needs an `internal_rest` capability (whose row supplies the view — a
`?agent_view_id=` that disagrees is a `400`) and answers `401`
without one. See [zero-trust.md](zero-trust.md) and [docs/cli/capability.md](../cli/capability.md).

Agent connects to Toolbox via MCP. Claude and Codex use streamable HTTP at `http://toolbox:3001/mcp` — Claude via `.mcp.json` (`{"type": "http", "url": …}`; the `type` discriminator is mandatory, a typeless entry is dropped at validation), Codex via `.codex/config.toml`. Claude's toolbox entry also carries `"alwaysLoad": true`: Claude connects MCP servers non-blocking by default, so without it `system/init` always reports the toolbox as `pending` (and `job.toolbox_mcp_connected` can never be `TRUE`). `alwaysLoad` makes the CLI await that one handshake before emitting init, bounded by `MCP_CONNECT_TIMEOUT_MS` (default 5 s), after which it proceeds with the connect continuing in the background. Agento injects `alwaysLoad` **only** into the auto-injected toolbox entry: operator servers from `agent_view/mcp/servers` are preserved wholesale, so they stay non-blocking unless the operator explicitly opts in by setting `"alwaysLoad": true` themselves — and an operator entry that shadows `toolbox` replaces ours, opting out of the injected one. **Shadowing also opts out of the injected capability.** The framework adds the token only to an entry whose URL matches the toolbox's own origin *and* path — an `Authorization: Bearer` header on `/mcp`, `?cap=<token>` on `/sse`; a shadowing entry that points anywhere else gets no token, and our toolbox refuses that session with `401`. Shadowing stays legal — but an operator who shadows `toolbox` must supply their own reachable MCP server, because ours will no longer serve that session. Each harness's `WorkspaceAdapter` constructs the URL from the shared `core/toolbox/url` base value (via `serialize_toolbox_connection`, which is deliberately unconstrained — a harness that consumes the Toolbox as CLI flags or env vars rather than MCP JSON is equally valid). The deprecated SSE transport at `/sse` is still served for operator-pinned `type: sse` entries.

Source: [docker/docker-compose.yml](../../docker/docker-compose.yml)
