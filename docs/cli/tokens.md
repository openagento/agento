# Token Management

Agento maintains an LRU pool of tokens per provider. **Token contents are stored encrypted in the database** (`oauth_token.credentials`, AES-256-CBC via the framework `Encryptor`). Credentials are only read once at registration time — the server never needs filesystem access at runtime.

> **BREAKING CHANGE (v0.10+):** The positional `credentials_path` argument (`agento token:register <agent> <label> creds.json`) has been removed. Operators who relied on file-based registration must migrate:
> - If the file held a `refresh_token` (OAuth flow) → re-register interactively: `agento token:register <agent> <label>`.
> - If the file held an API key → use `--with-api-key` (read from stdin or interactive prompt; see [Reading secrets](#reading-secrets)).
>
> **BREAKING CHANGE (v0.11+):** `--with-api-key` / `--with-access-token` no longer accept an **inline value** (e.g. `--with-api-key sk-...`). Inline secrets leak through shell history, `ps`, and CI logs. The flags are now boolean switches; the secret is read from stdin (piped or via interactive `getpass` prompt). See [Reading secrets](#reading-secrets) below.

## Token Types

| Type                | Description                                      | Registration flag         |
|---------------------|--------------------------------------------------|---------------------------|
| `oauth`             | Claude Code / Codex OAuth session (refresh token) | interactive (no flags)    |
| `openai_api_key`    | OpenAI API key for Codex                         | `--with-api-key`          |
| `anthropic_api_key` | Anthropic API key for Claude                     | `--with-api-key`          |
| `codex_access_token`| OpenAI short-lived access token (JWT)            | `--with-access-token`     |

## Token Pool Model

All tokens registered for a given provider form a **pool**. When the consumer needs a token for a job it calls `select_token(provider)` which picks the token with the **lowest priority** (ties broken by LRU: least-recently-used healthy token, `status='ok'`, not expired) atomically and stamps its `used_at`. Multiple tokens for the same provider therefore share traffic without any "primary" flag.

Health state lives on each row:

| Column       | Meaning                                                                 |
|--------------|-------------------------------------------------------------------------|
| `status`     | `ok` or `error`. Flipped to `error` only on a **known-permanent** auth failure (invalid credentials, expired OAuth, not logged in). A revoked/stale access token — or any further wording the **provider module** classifies as a transient credential rejection — is treated as **transient** instead: a short `throttled_until` cooldown, `status` stays `ok`. (Claude additionally classifies unrecognised `401` credential-rejection wording this way; Codex covers revoked/stale-token wording only.) |
| `error_msg`  | Operator-visible reason for the latest failure.                         |
| `expires_at` | Credential expiry (from the stored payload). A row is skipped once `expires_at` is in the **past** (a *future* value means still-valid). **Claude OAuth leaves this NULL on purpose** — see the note below. |
| `throttled_until` | Temporary **cooldown**. Set either to a usage/session limit's reset time (default 1h when unparseable), or to `now + 15 min` on a **transient auth failure** (revoked/stale access token — usually a concurrent-refresh race, not a dead credential). The pool skips the token while `throttled_until` is in the **future** and auto-includes it once it passes. Distinct from `expires_at` (credential expiry) and from `status='error'` (poison): `status` stays `'ok'` and the token self-recovers. |
| `used_at`    | Last time a worker claimed the row — drives LRU ordering within a priority tier. |
| `priority`   | Pool selection weight. Lower value wins; 0 = default.                   |

**Four ways a token leaves the pool (in increasing permanence):**
- **Throttled** (`throttled_until` in the future, `status='ok'`): hit a session/usage/rate limit. Temporary — the token auto-recovers at the reset time and the job **fails over** to another healthy token meanwhile. No operator action needed.
- **Transient-auth throttled** (`throttled_until` in the future, `status='ok'`): the CLI rejected the stored credential with a revoked/stale-token 401. Temporary — the same token label is often still serving other jobs, so it is **not** poisoned; the job fails over to another healthy token and this one returns to the pool after 15 minutes. No operator action needed.
- **Expired** (`expires_at` in the past): credential lapsed. Cleared by `token:refresh`.
- **Errored** (`status='error'`): auth failure poisoned it. Cleared by `token:reset` or `token:refresh`.

`token:reset` clears **both** `status='error'` and any `throttled_until` cooldown.

> **Claude OAuth tokens** intentionally leave the row `expires_at` **NULL**.
> Claude's `expiresAt` is the short-lived (~8h) *access*-token expiry in epoch
> milliseconds; the long-lived refresh token is rotated by the CLI *during* a
> job run and written back to `credentials` afterwards
> (`ClaudeConfigWriter.capture_refreshed_credentials`). Storing the access-token
> expiry as the row `expires_at` would make `select_token` skip the token after
> an idle gap even though it can still self-heal — so it is deliberately not set.
> A Claude token showing `expires_at = NULL` is correct, not a bug.

## Token Lifecycle

```
register → [use via LRU+priority] → (transient 401 → 15-min throttle, self-recovers)
                                  → (permanent auth failure → auto-flagged status='error')
                                  → refresh | reset → deregister
```

## Register a Token

### Interactive OAuth

Requires a TTY — opens a browser for the OAuth flow.

```bash
# Claude (OAuth)
agento token:register claude my-token

# Codex (OAuth)
agento token:register codex  my-token
```

### With an API key

The secret is never on the command line. Three input modes are supported (see [Reading secrets](#reading-secrets)):

```bash
# 1) Interactive prompt (TTY, input hidden via getpass):
agento token:register claude my-token --with-api-key

# 2) Pipe:
echo "$ANTHROPIC_API_KEY" | agento token:register claude my-token --with-api-key

# 3) File redirect:
agento token:register codex my-token --with-api-key < /path/to/openai-key.txt
```

`--with-api-key` maps to `anthropic_api_key` for `claude` and `openai_api_key` for `codex`.

### With an access token (JWT)

Same three input modes — JWT is read from stdin:

```bash
# Interactive prompt:
agento token:register codex my-token --with-access-token

# Pipe:
echo "$CODEX_ACCESS_TOKEN" | agento token:register codex my-token --with-access-token
```

### Reading secrets

`--with-api-key` and `--with-access-token` are **boolean switches** — they take no inline value. The secret is read from stdin:

- **Host stdin is a TTY** → interactive prompt via `getpass.getpass()` (input hidden, no echo).
- **Host stdin is not a TTY** (pipe / `<` redirect) → one line from stdin.

After reading, the CLI prints a masked confirmation to **stderr** so you can verify the right secret was read without leaking the full value:

```
Read api_key from stdin: sk-p************MPLE
```

(Format: first 4 + last 4 characters; everything else `*`. Secrets shorter than 8 characters are fully masked.)

If you pass an inline value (`--with-api-key sk-XXX`), argparse rejects it with a usage error — that path is intentionally closed to prevent leakage through shell history, `ps aux`, and CI logs.

### Common options

- `--token-limit N` — usage limit (0 = unlimited)

`register` also resets `status='ok'` and clears any prior `error_msg`, so re-running it on an existing label is a valid recovery path.

## List Tokens

```bash
agento token:list
agento token:list --agent-type claude
agento token:list --json
agento token:list --all    # include disabled tokens
```

Each row shows `type`, `priority`, `status`, `last_used`, and `expires`. A token that is temporarily rate/usage-limited shows `status=ok (throttled)` plus a `⏳ throttled until <time>` line; an errored token shows its truncated `error_msg`. `--json` includes a `throttled_until` field (ISO-8601, or `null`) alongside `expires_at`. The `credentials` blob is never surfaced.

## Set Pool Priority

Lower priority wins. Tokens with the same priority are ranked by LRU.

```bash
agento token:set-priority <token_id> <priority>
```

Example — pin token 3 as preferred (priority 0) and demote token 5 (priority 10):

```bash
agento token:set-priority 3 0
agento token:set-priority 5 10
```

## Refresh an Expired OAuth Token

```bash
agento token:refresh 1    # Re-authenticate token ID 1 (interactive OAuth)
```

Refresh overwrites the stored `credentials`, re-parses `expires_at` from the new payload, and resets `status='ok'` / `error_msg=NULL`. The `id`, `label`, and `type` are preserved so downstream references stay valid.

Note: `token:refresh` only supports the interactive OAuth flow. To update an API key or access token, re-register with the appropriate flag (same label will upsert the existing row).

Distinct from interactive refresh, **automatic post-run capture** (the agent CLI rotating its own OAuth token during a job, written back afterwards) updates the stored `credentials` (and refreshes `expires_at` from the payload) but does **not** touch operator/health state — it does **not** re-enable a disabled token or clear an `error` status. An operator who disables or quarantines a token while a job is running keeps that decision; the rotated credentials are still saved so the token is healthy if it is later re-enabled.

## Manual Error Control

Useful when you know a license has been revoked or want to take one offline for a bit:

```bash
agento token:mark-error 1 "Revoked by admin 2026-04-23"
agento token:reset 1    # clear status=error AND any throttle, status back to 'ok'
```

`mark-error` stops the pool from handing out that token; `reset` puts it back in rotation without a full re-auth round-trip (it also lifts a usage-limit `throttled_until` cooldown, should you want to force a throttled token back early).

## Usage Stats

```bash
agento token:usage                  # Show usage stats across all providers
agento token:usage --agent-type claude --window 72
```

## Deregister

```bash
agento token:deregister 1   # soft-disable (enabled=FALSE); data retained
```

## Binding a Provider to an `agent_view`

The consumer requires `agent_view/provider` to be set — there is no sticky-primary fallback.

```bash
agento config:set agent_view/provider claude --scope=agent_view --scope-id=1
```

Without this, jobs for that agent_view fail fast with `No agent_view/provider configured`.

## Requirements

- `AGENTO_ENCRYPTION_KEY` must be set (same key used for `core_config_data` obscure fields). See [encryption.md](../config/encryption.md).
- The `oauth_token` schema is maintained by framework migrations beginning with `019_oauth_token_inline_credentials.sql`; `agento setup:upgrade` applies pending migrations.

Source: [src/agento/framework/cli/token.py](../../src/agento/framework/cli/token.py)
