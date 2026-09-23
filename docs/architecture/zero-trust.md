# Zero-Trust Credential Model

**Target model:** the toolbox is the only container that holds **tool** credentials (Jira/GitHub
tokens, the tool DB user, SMTP). The AI agent holds no tool credential, only its own harness
credential (OAuth), with **one documented exception**: the SSH private key used for git, delivered per
run into a private `ssh-agent` and never written to disk — a dated, scoped waiver of `RULES.md` SEC-1
(see [DECISIONS.md](../../DECISIONS.md) D-SSH-1). **Today the code does not meet this model:** the next
section lists every known gap. Rules: `RULES.md` SEC-1, SEC-7, SEC-9.

**Closed in this release:** the credential store no longer reaches uid `agent` at all. The cron container's store file is `root:root 0600` and is read by a root-owned program that drops privilege in-process before loading it, so it crosses no `execve`; the managed crontab is rendered by root from inputs the agent cannot write. [D-SSH-1](../../DECISIONS.md) residual channel (6), **closed 2026-09-23** — see [cron-privileges.md](cron-privileges.md).

## Known exceptions and debt

This is the one register of security exceptions and gaps. An **accepted** row links a dated
DECISIONS.md entry with the owner's approval. A `Part of the model` row restates what the model
allows. A listed item is not a new review finding, unless a change makes it worse or depends on it. A
gap already in the repo that is not listed is reported as `DEBT`, and the owner adds its row. A gap a
change adds is a finding (`RULES.md` SEC).

| Item | Where | Status |
|---|---|---|
| `bootstrap()` decrypts all DEFAULT-scope `obscure` config while it resolves module config, so cron, the consumer (each hot-reload), and the CLI hold decrypted secrets for a short time. For toolbox-only secrets (the Outlook Graph secret) the value is not used. | `framework/bootstrap.py` | Debt — fix tracked in [toolbox-only secret boundary](../security/toolbox-only-secret-boundary.md) |
| The `jira` observer decrypts an agent_view's `jira/jira_token` cron-side on each bootstrap until that view's account id is resolved, only to check that it is set. | `modules/jira/src/observers.py` (`module_ready_after`) | Debt |
| `app_monitor` uses the `obscure` SMTP password **cron-side** to send breach alerts. | `modules/app_monitor/src/observers.py` | Debt — same fix as `bootstrap()` (move to a toolbox transport) |
| `secrets.env` is mounted into the cron service (`env_file`). | `framework/cli/templates/docker-compose.yml` | Debt |
| `CONFIG__*` ENV values are plaintext in every container that has them. | ENV level of the config fallback | Part of the model (CFG-1) |
| The toolbox takes `agent_view_id` from the caller (the MCP query string and the REST request body) and `job_id` from the MCP query string. An absent id means DEFAULT scope ([DECISIONS.md](../../DECISIONS.md) 2026-06-18). On the MCP path and in the Jira REST handlers, an unknown or unparseable id falls back to global config (`config-loader.js`); the Outlook and Bitbucket REST handlers return 404. | `src/agento/toolbox/server.js`, `config-loader.js`, module `toolbox/` handlers | Absent id = DEFAULT scope: accepted (DECISIONS.md 2026-06-18). Caller-supplied id: debt, the framework-wide internal-caller-auth gap N5-2 ([DECISIONS.md](../../DECISIONS.md) 2026-06-19 D-5) |
| An MCP session without `job_id` gets Outlook reads and actions that are not bound to a trigger. The toolbox cannot tell interactive `agento run` (the intended user) from any other caller that leaves out `job_id`. | `modules/outlook/toolbox/outlook.js` | Accepted for interactive `agento run` — [DECISIONS.md](../../DECISIONS.md) 2026-07-04; other callers are debt |
| The agent holds its own harness OAuth credential. | per-run HOME (for example `.claude/.credentials.json`), written from the encrypted `credential` row | Part of the model (SEC-1) |
| The agent can sign with the git SSH key through `SSH_AUTH_SOCK`; the key is never written to disk. | a per-run `ssh-agent`, loaded from the encrypted `agent_view/identity/ssh_private_key` | Accepted — [DECISIONS.md](../../DECISIONS.md) D-SSH-1 (Option A waiver, every residual channel listed there) |

The sections below show the **target model**. Where the code differs today, the table above says so.

## Security Boundary

```
┌────────────────────────────────────┐
│  Agent (cron/sandbox)              │
│                                    │
│  Has: workspace, tokens (OAuth),   │
│       an SSH signing socket (not   │
│       the key), modules (read-only)│
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

- **Python (cron):** Runs the LLM, executes the configured harness's CLI, manages the job queue. Holds the **harness/provider** credentials (the `credential` pool, one scope per credential-requiring provider) and, because it is the process that reads and decrypts them, the database credentials plus `AGENTO_ENCRYPTION_KEY`. What it does not hold is the **tool** credentials the toolbox brokers (Jira, GitHub, the read-only MySQL tool adapters). An agent process spawned by the consumer no longer inherits the database/encryption environment (`framework/credential_store_env.py`), but one uid still owns the store — see DECISIONS.md D-SSH-1 residual channel (6). It also holds the decrypted **SSH private key** in its own heap for the consumer process's lifetime (CPython cannot zeroize a `str`), as it already did for every provider credential — but since 2026-08-25 a same-uid peer cannot read it: framework processes are non-dumpable (`framework/process_hardening.py`), so `/proc/<pid>/mem` and `/proc/<pid>/environ` are denied.
- **Node.js (toolbox):** Holds the **tool** credential store (API tokens, the tool database user), executes database queries, manages the Jira API. Never runs LLM code, and no harness/provider credential passes through it.

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
- An `SSH_AUTH_SOCK` pointing at a per-run `ssh-agent` that dies with the run (measured 2 ms after it
  when probed, ≤10 s worst case) —
  a signing capability for git, **not** the key itself
- MCP tools — through toolbox, which validates and executes requests
- Filesystem — workspace/, modules/ (read-only)

## The SSH Key Exception

Git-over-SSH is the one place where the agent side handles a credential. How it is confined:

- The key is **never a filesystem object** — not on the shared `/workspace` mount, not in `/tmp`. It
  travels config → the cron process's memory → the wrapper's environment → an inherited file
  descriptor → `ssh-add`.
- The wrapper starts a **per-run `ssh-agent`**, scrubs the key from the environment with `env -u`, and
  `exec`s the agent command with `SSH_AUTH_SOCK` as its only secret-bearing capability — the
  non-secret `GIT_SSH_COMMAND`, `AGENTO_SSH_TTL` and `SSH_AGENT_PID` stay set. The agent can sign;
  it cannot read the key.
- If the key cannot be delivered safely the wrapper **drops** it — it never falls back to a file.

What this does **not** give you: all agent_views run as the same uid in one container, so a same-uid
peer is not barred from the wrapper's pre-`exec` environ, the inherited descriptor or the live agent
socket. (The consumer's heap **is** barred since 2026-08-25 — see `framework/process_hardening.py`.)
This is a large reduction in exposure, **not** an
authorization boundary between agent_views. The complete channel list with lifetimes, the waiver, and
the tracked follow-up (per-view OS uids) are in [DECISIONS.md](../../DECISIONS.md) (D-SSH-1).
