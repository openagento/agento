# Zero-Trust Credential Model

**Target model:** the toolbox is the only container that holds **tool** credentials (Jira/GitHub
tokens, the tool DB user, SMTP). The AI agent holds no tool credential, only its own harness
credential (OAuth). **Today the code does not meet this model:** the next section lists every known
gap, including a headless agent that inherits the DB password and the encryption key. Rules:
`RULES.md` SEC-1, SEC-7, SEC-9.

## Known exceptions and debt

This is the one register of security exceptions and gaps. An **accepted** row links a dated
DECISIONS.md entry with the owner's approval. A `Part of the model` row restates what the model
allows. A listed item is not a new review finding, unless a change makes it worse or depends on it. A
gap already in the repo that is not listed is reported as `DEBT`, and the owner adds its row. A gap a
change adds is a finding (`RULES.md` SEC).

| Item | Where | Status |
|---|---|---|
| **A headless agent runs in the cron container, and its CLI subprocess inherits the consumer env: `AGENTO_ENCRYPTION_KEY` (from `secrets.env`) and `MYSQL_USER`/`MYSQL_PASSWORD` (the same `cron_agent` login the toolbox uses). With both, the agent can read and decrypt every stored credential.** Only interactive `agento run` uses the `sandbox` container. | `framework/harness/subprocess_runner.py` (`env = {**os.environ, …}`) | Debt — P0. Fix: pass an allow-listed env to the agent subprocess |
| `bootstrap()` decrypts all DEFAULT-scope `obscure` config while it resolves module config, so cron, the consumer (each hot-reload), and the CLI hold decrypted secrets for a short time. For toolbox-only secrets (the Outlook Graph secret) the value is not used. | `framework/bootstrap.py` | Debt — fix tracked in [toolbox-only secret boundary](../security/toolbox-only-secret-boundary.md) |
| The `jira` observer decrypts an agent_view's `jira/jira_token` cron-side on each bootstrap until that view's account id is resolved, only to check that it is set. | `modules/jira/src/observers.py` (`module_ready_after`) | Debt |
| `app_monitor` uses the `obscure` SMTP password **cron-side** to send breach alerts. | `modules/app_monitor/src/observers.py` | Debt — same fix as `bootstrap()` (move to a toolbox transport) |
| `secrets.env` is mounted into the cron service (`env_file`). | `framework/cli/templates/docker-compose.yml` | Debt |
| `CONFIG__*` ENV values are plaintext in every container that has them. | ENV level of the config fallback | Part of the model (CFG-1) |
| `/opt/cron-agent/env` holds `MYSQL_*`, `CONFIG__*`, and `AGENTO_*` (including `AGENTO_ENCRYPTION_KEY`) and is mode `0644`, so every uid in the cron container can read it. | `framework/docker/cron/entrypoint.sh` | Debt |
| The toolbox takes `agent_view_id` from the caller (the MCP query string and the REST request body) and `job_id` from the MCP query string. An absent id means DEFAULT scope ([DECISIONS.md](../../DECISIONS.md) 2026-06-18). On the MCP path and in the Jira REST handlers, an unknown or unparseable id falls back to global config (`config-loader.js`); the Outlook and Bitbucket REST handlers return 404. | `src/agento/toolbox/server.js`, `config-loader.js`, module `toolbox/` handlers | Absent id = DEFAULT scope: accepted (DECISIONS.md 2026-06-18). Caller-supplied id: debt, the framework-wide internal-caller-auth gap N5-2 ([DECISIONS.md](../../DECISIONS.md) 2026-06-19 D-5) |
| An MCP session without `job_id` gets Outlook reads and actions that are not bound to a trigger. The toolbox cannot tell interactive `agento run` (the intended user) from any other caller that leaves out `job_id`. | `modules/outlook/toolbox/outlook.js` | Accepted for interactive `agento run` — [DECISIONS.md](../../DECISIONS.md) 2026-07-04; other callers are debt |
| The agent holds its own harness OAuth credential. | per-run HOME (for example `.claude/.credentials.json`), written from the encrypted `credential` row | Part of the model (SEC-1) |
| The agent holds an SSH key for git. | per-run HOME `.ssh/id_rsa`, written by `workspace_build` from the encrypted `agent_view/identity/ssh_private_key` | Accepted — the git push identity, [DECISIONS.md](../../DECISIONS.md) 2026-06-19 D-2 |

The sections below show the **target model**. Where the code differs today, the table above says so.

## Security Boundary

```
┌────────────────────────────────────┐
│  Agent (cron/sandbox)              │
│                                    │
│  Has: workspace, tokens (OAuth),   │
│       SSH key, modules (read-only) │
│                                    │
│  Does NOT have: secrets.env,       │
│  database passwords, API tokens    │
│  (except its own OAuth)            │
└───────────┬────────────────────────┘
            │ MCP over streamable HTTP (Claude + Codex) — no credentials in request
            ▼
┌────────────────────────────────────┐
│  Toolbox                           │
│                                    │
│  Has: secrets.env, JIRA_TOKEN,     │
│       all DB passwords, SMTP,      │
│       AGENTO_ENCRYPTION_KEY        │
│                                    │
│  Validates: read-only queries,     │
│  email whitelist, domain whitelist  │
└────────────────────────────────────┘
```

## How It Works

1. Agent calls MCP tool: `mysql_myapp_prod` with query `SELECT * FROM users LIMIT 5`
2. Toolbox receives the request (no credentials in the request — just tool name + query)
3. Toolbox resolves connection config from modules + core_config_data + ENV
4. Toolbox validates the query is read-only (`SELECT` only)
5. Toolbox executes the query using its own credentials
6. Toolbox returns results to the agent

## Why Two Languages

The Python/Node.js split is **intentional** — the language boundary IS the security boundary:

- **Python (cron):** Runs the LLM, executes the configured harness's CLI, manages the job queue. Holds agent credentials (the `credential` pool, one scope per credential-requiring provider). In the target model it holds no tool credentials; see the table above for what it holds today.
- **Node.js (toolbox):** Holds all tool credentials, executes database queries, manages Jira API. Never runs LLM code.

You cannot accidentally `import secrets` in agent code because it's a different language, different container, different filesystem.

## Credential Flow

```
secrets.env (host filesystem) — holds only AGENTO_ENCRYPTION_KEY
    │
    ├──► cron container (env_file in docker-compose) — debt, see the table above
    └──► toolbox container (env_file in docker-compose)
              │
              └── decrypts tool credentials (Jira, SMTP, DB logins, API tokens)
                  stored encrypted in core_config_data (MySQL)

CONFIG__* ENV overrides take precedence over core_config_data (plaintext)
```

## What the Agent CAN Access

- Its own OAuth credential (Claude/Codex/Pi) — written into its per-run HOME from the encrypted `credential` row
- SSH key — written into its per-run HOME, for cloning git repositories
- MCP tools — through toolbox, which validates and executes requests
- Filesystem — workspace/, modules/ (read-only)

## What the Agent CANNOT Access, by Mount

The `versioned_artifacts` store (`storage/versioned-artifacts/` → `/srv/versioned-artifacts`) is
mounted into the **toolbox only**, so the agent container cannot reach it at all. The agent does
have `git` — it commits its own workspace with it, see
[identity.md](../config/identity.md) — and that is beside the point: the store is not
there to operate on, and no generic `git` operation on it is ever exposed to the agent
(PRD §32). Versioned file trees are therefore reachable only through the opt-in
`versioned_artifact_*` tools, each additionally bounded by a per-`agent_view` artifact
allowlist. Artifact *creation* is not a tool at all — it is a host CLI command over
`docker compose exec`, because the toolbox authenticates no caller and any HTTP route on
that listener would be agent-callable.

The published half of that volume (`storage/versioned-artifacts/published`) is mounted
read-only into one more container, `artifacts`, which serves it over HTTP. That container
declares no `networks:` key, so Compose leaves it alone on the project `default` network
while every other service names `agento-net` — the agent cannot resolve its name, let
alone read an artifact it was never granted. It is published on `127.0.0.1` only. Putting
it on `agento-net` "for consistency" would make every artifact readable by every agent in
every agent_view over plain HTTP, with the `allowed_artifacts` allowlist bypassed and no
audit row written. It also holds no `env_file:` and no `environment:`, so the second
container touching artifact content still holds no secret.

The agent still edits with ordinary file tools, on a **copy**: `create_draft` and
`materialize` write the tree onto the agent's own workspace (its *desk*, under
`/workspace/artifacts/…`) and `save_version` copies it back. So the trust boundary is the
copy, not a shared mount — the store keeps the limits, the allowlist, the locking and the
audit trail, and the desk holds nothing the agent could not already read.
