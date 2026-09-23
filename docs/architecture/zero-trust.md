# Zero-Trust Credential Model

Toolbox is the **only** container holding the credential store — API keys, tokens, DB
credentials. The AI agent has none of them, with **one documented exception**: the SSH private key used for git, delivered per run into a private
`ssh-agent` and never written to disk. That exception is a dated, scoped waiver of
`RULES.md:112` — see [DECISIONS.md](../../DECISIONS.md) (D-SSH-1).

**Closed in this release:** the credential store no longer reaches uid `agent` at all. The cron container's store file is `root:root 0600` and is read by a root-owned program that drops privilege in-process before loading it, so it crosses no `execve`; the managed crontab is rendered by root from inputs the agent cannot write. [D-SSH-1](../../DECISIONS.md) residual channel (6), **closed 2026-09-23** — see [cron-privileges.md](cron-privileges.md).

> **Known limitation (aspirational, not yet fully enforced on the Python side).** `bootstrap()`
> transiently decrypts **all** DEFAULT-scope `obscure` config while resolving module config, so the
> cron, the consumer (every hot-reload), and the CLI briefly hold decrypted secrets. For
> *toolbox-only* creds (e.g. the Outlook Graph secret) this decryption is unnecessary and the value
> is discarded unused. But it is **not** universally unused: `app_monitor` intentionally consumes
> the obscure SMTP password **cron-side** to send breach alerts (`observers.py`), so that credential
> genuinely lives in the cron today and needs migration to a toolbox-owned transport. `secrets.env`
> is also mounted into the cron service today, and the `CONFIG__*` ENV path is plaintext regardless.
> A per-field `toolbox_only` classification + the app_monitor SMTP transport migration are the
> tracked fix — see [toolbox-only secret boundary](../security/toolbox-only-secret-boundary.md).

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
secrets.env (host filesystem)
    │
    └──► toolbox container (env_file in docker-compose)
              │
              ├── JIRA_HOST, JIRA_USER, JIRA_TOKEN
              ├── SMTP_HOST, SMTP_USER, SMTP_PASS
              ├── AGENTO_ENCRYPTION_KEY
              └── CONFIG__* overrides

              + core_config_data (MySQL) for per-tool credentials
```

## What the Agent CAN Access

- Its own OAuth tokens (Claude/Codex) — stored in `tokens/`, mounted to `/etc/tokens`
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
