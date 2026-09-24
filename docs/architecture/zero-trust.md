# Zero-Trust Credential Model

Toolbox is the **only** container with access to secrets. The AI agent holds no upstream service
credentials — the one credential it carries is its own run's capability token, which buys nothing but
the toolbox, scoped to one agent_view and expiring.

> **Enforced per field, still aspirational for the rest.** A field marked
> `"access": "toolbox_only"` in `system.json` is **never** resolved by Python: `resolve_field` returns
> `None`, `ScopedConfigService.get()` raises, and the bulk `resolve_all()` skips it. The Outlook Graph
> secret, certificate and certificate password are classified this way, so `bootstrap()` no longer
> decrypts them in the cron.
>
> Other `obscure` fields are still decrypted transiently by `bootstrap()` while resolving DEFAULT-scope
> module config, so the cron, the consumer (every hot-reload) and the CLI briefly hold them. One of them
> is used there on purpose: `app_monitor` consumes the obscure SMTP password **cron-side** to send breach
> alerts (`observers.py`), so that credential genuinely lives in the cron and still needs migration to a
> toolbox-owned transport. `secrets.env` is also mounted into the cron service today, and the `CONFIG__*`
> ENV path is plaintext regardless — which is why `allowEnv: false` refuses the ENV source for a field
> that must not travel that way. Remaining work: item 3 of the
> [toolbox-only secret boundary](../security/toolbox-only-secret-boundary.md) PRD.

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
            │ MCP over streamable HTTP (Claude + Codex) — capability only, no service creds
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

## Toolbox East-West Authentication

Nothing reaches the toolbox anonymously. Every MCP session and every `/api` route carries a
**capability token**, and the toolbox derives the request's scope from that token — never from what
the caller says about itself.

**The claims are server-side.** The token is a random opaque string; the `toolbox_capability` table
stores only its SHA-256 hash, together with `kind`, `agent_view_id`, an optional `job_id`,
`expires_at` and `revoked_at`. On each request the toolbox hashes the presented token, looks the row
up, and uses **that row's** `agent_view_id` / `job_id`. A query string or request body may still
carry an `agent_view_id`, but it is only compared with the capability's scope — a mismatch is refused
(400), never honoured. So with the capability issued to its own run, an agent cannot select another
view — a guarantee rather than a hope. What it does not cover is an agent that obtains a *different*
run's capability off the shared workspace; see the co-tenant limit below.

`POST /config-test` (a live credential probe, see [testers](../config/testers.md)) is guarded the
same way: an `internal_rest` capability, scope from the row. It is the one route that also accepts a
**viewless** `internal_rest` capability, which tests the default scope; every other guard refuses
one. `run_id` on an MCP URL names an interactive run's desk directory only — it grants no scope, and
a `job_id` on the URL may only agree with the capability's job.

**Five kinds, each with the smallest privilege that works:**

| Kind | Holder | Reaches | TTL |
|------|--------|---------|-----|
| `mcp_job` | a consumer-run job | `/mcp`, `/sse`, `/messages`, invoke | the job's lifetime |
| `mcp_interactive` | one interactive `agento run` | `/mcp`, `/sse`, `/messages`, invoke | 12 h |
| `internal_rest` | Python publishers, channels, onboarding | `/api/*`, scoped `/health`, `/config-test` | 120 s |
| `user_session` | the Web API, for a logged-in user | invoke only | `core/auth/capability_ttl`, single use |
| `miniapp` | the Web API, for a miniapp launch | invoke only | `core/auth/capability_ttl`, single use |

Invoke is `POST /internal/tools/{name}:invoke`. The two user kinds need a live source (session or
launch) on every call; E1 ships no source checker, so they are refused until E2/E6 add one. Every
tool call on every transport goes through one dispatcher that authorizes it per call and writes a
`tool_invocation` audit row — see [auth-context.md](auth-context.md).

`mcp_job` cannot be minted by hand ([`capability:mint`](../cli/capability.md) refuses it): its
lifetime is bound to the job's terminal transition, and a hand-minted one would outlive the code that
revokes it. The scoped `/health` diagnostic accepts `internal_rest` **only** — an MCP kind lives
inside the sandbox, and a diagnostic that reports backend reachability would be an infrastructure
oracle for the agent.

**Status codes:** `401` = no token. `403` = a token that is present but invalid, expired or revoked,
or a view the resolver cannot resolve. `503` = the scope resolver itself failed (a DB blip) — the
request is refused rather than widened to global scope.

**Revocation stops the next tool call on both transports.** Streamable HTTP re-verifies the
capability on every request. SSE is two halves: the `/sse` stream is verified once at connect (a
long-lived stream has no per-request hook), but `POST /messages` — the half that actually carries
the tool calls — is verified per request like `/mcp`. So a revoked token stops the next CALL either
way; what a revoke cannot do on SSE is tear down the open stream itself, which then delivers nothing
new. **Both halves are also bound to the session's own capability:** a session is owned by the
capability that opened it, and a different — even perfectly valid — capability driving it is `403`.
That matters because `/messages` addresses the session by a **query string** `sessionId`, a value
access logs keep; the id alone authorizes nothing.

An MCP client is configured with a bare URL and sends no headers of its own, so the `/messages`
endpoint the server advertises in its `endpoint` event carries `?cap=<capability>` — the same
credential the client used to open `/sse`, on the same channel, to the same client. The client posts
to that string verbatim. This is why the guard did not break the transport, and why `cap` must never
be echoed into a log line. Job capabilities are additionally revoked at every terminal transition (`SUCCESS`, retry
to `TODO`, `DEAD`, `pause_job`, and stale-job recovery), inside the same transaction that writes the
status — a failing revoke rolls the status back, leaving the job `RUNNING` for the recovery path.

**Where tokens come from.** The consumer mints one per job; `agent_view:prepare-run` mints one per
interactive run; both are injected into the run's own MCP config entry only, matched by origin **and**
path (`/mcp` or `/sse`), never by substring — an operator's third-party MCP server never receives it.
On `/mcp` the token goes in an `Authorization: Bearer` header and the URL stays unchanged; only an
`/sse` entry gets `?cap=`. Every token carries `allowed_transports` (`http` or `sse`), checked per
endpoint with no default, so an `["http"]` token is refused on `/sse` and `/messages`. For now
the toolbox still reads `?cap=` on `/mcp`, `/api`, `/config-test` and `/health` as well (until the
retirement rule applies); the invoke endpoint refuses it. See
[auth-context.md](auth-context.md).
Operators mint the other two kinds with [`capability:mint`](../cli/capability.md). A minted token is a
credential: stdout once, onward only through stdin or a mode-0600 file, never argv, a log, or shell
history. Any capability the framework persists from agent output is replaced with `cap=***` first.

**What a capability does NOT protect against: a co-tenant run on the same host.** A run's token is
written into that run's own MCP config, inside its own artifacts directory. Concurrent runs are
isolated by *directory*, not by *identity*: every agent process the consumer spawns runs as the same
`agent` account, and the cron container mounts the whole workspace, so file permissions cannot keep
one run out of another run's config file. A shell-capable agent that reads a co-tenant's live token
authenticates as that co-tenant's view. Mode `0600` does not change this — same UID, same access.

That is a residual limitation, not a regression: before capabilities the toolbox served **any**
caller on the network with **any** requested scope, so the same agent needed no theft at all. What
capabilities add is a bound on the window — a job token is revoked at the job's terminal transition,
so a stolen token dies with the run it came from — and a complete audit trail of which row a request
resolved through. Closing it fully needs a per-run **identity** boundary (a distinct UID per run, or
a container per run), which is the sandbox-segmentation half of the same work item and is tracked in
[ROADMAP.md](../../ROADMAP.md). Until then, treat concurrent runs in one deployment as mutually
trusting, and do not rely on view separation as a boundary between mutually hostile tenants.

**Why not a shared secret, and why not segmentation alone** — see the 2026-08-23 entry in
[DECISIONS.md](../../DECISIONS.md).

## How It Works

1. Agent calls MCP tool: `mysql_myapp_prod` with query `SELECT * FROM users LIMIT 5`
2. Toolbox receives the request (no service credentials in it — a capability token, the tool name and the query)
3. Toolbox resolves connection config from modules + core_config_data + ENV
4. Toolbox validates the query is read-only (`SELECT` only)
5. Toolbox executes the query using its own credentials
6. Toolbox returns results to the agent

## Why Two Languages

The Python/Node.js split is **intentional** — the language boundary IS the security boundary:

- **Python (cron):** Runs the LLM, executes the configured harness's CLI, manages the job queue. Holds agent credentials (the `credential` pool, one scope per credential-requiring provider) but no database/API credentials.
- **Node.js (toolbox):** Holds all credentials, executes database queries, manages Jira API. Never runs LLM code.

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
- SSH key — for cloning git repositories
- MCP tools — through toolbox, which validates and executes requests
- Filesystem — workspace/, modules/ (read-only)

## What the Agent CANNOT Access, by Mount

The `versioned_artifacts` store (`storage/versioned-artifacts/` → `/srv/versioned-artifacts`) is
mounted into the **toolbox only**, so the agent container cannot reach it at all. The agent does
have `git` — it commits its own workspace with it, see
[identity.md](../config/identity.md) — and that is beside the point: the store is not
there to operate on, and no generic `git` operation on it is ever exposed to the agent
(PRD §32). Versioned file trees are therefore reachable only through the opt-in
`versioned_artifact_*` tools, each additionally bounded to the artifacts that scope may use —
the artifacts it created itself plus its `allowed_artifacts` grant. Creation is one of
those tools: the agent owns the whole lifecycle, and what is withheld from it is the host
*path*, not the operation. `versioned_artifact_init` names a code and nothing else, while the
`artifact:init` CLI keeps `--source` — a host directory read is not something the toolbox may
be asked for over a listener that authenticates no caller. Note that the `agent_view_id` those
per-scope gates read is asserted by the caller, so they scope cooperating views rather than
authorize them; see ROADMAP.md.

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
