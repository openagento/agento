# Credential Management

> **RENAMED in v0.15:** every `token:*` command is now `credential:*`, and the
> `oauth_token` table is now `credential`, keyed by **credential scope** rather than by
> agent type. The old `token:*` names remain as hidden aliases for one release cycle so
> existing runbooks keep working; removal is tracked in [ROADMAP.md](../../ROADMAP.md).
> A "scope" is the credential pool a `(harness, provider)` pair draws from — see
> [harness-contract.md](../architecture/harness-contract.md).

Agento maintains an LRU pool of credentials per **scope**. **Credential contents are stored encrypted in the database** (`credential.credentials`, AES-256-CBC via the framework `Encryptor`). They are only read once at registration time — the server never needs filesystem access at runtime.

A provider that requires no credential at all (`credential_required: false` in its `agent_harnesses` declaration — e.g. a locally-hosted model) has no scope and no pool; its runs are still recorded in `usage_log` with `credential_id = NULL`, attributed by `(harness, provider)`.

> **BREAKING CHANGE (v0.10+):** The positional `credentials_path` argument (`agento credential:register <scope> <label> creds.json`) has been removed. Operators who relied on file-based registration must migrate:
> - If the file held a `refresh_token` (OAuth flow) → re-register interactively: `agento credential:register <scope> <label>`.
> - If the file held an API key → use `--with-api-key` (read from stdin or interactive prompt; see [Reading secrets](#reading-secrets)).
>
> **BREAKING CHANGE (v0.11+):** `--with-api-key` / `--with-access-token` no longer accept an **inline value** (e.g. `--with-api-key sk-...`). Inline secrets leak through shell history, `ps`, and CI logs. The flags are now boolean switches; the secret is read from stdin (piped or via interactive `getpass` prompt). See [Reading secrets](#reading-secrets) below.

## Credential Types

| Type                | Description                                      | Registration flag         |
|---------------------|--------------------------------------------------|---------------------------|
| `oauth`             | Claude Code / Codex OAuth session (refresh token) | interactive (no flags)    |
| `openai_api_key`    | OpenAI API key for Codex                         | `--with-api-key`          |
| `anthropic_api_key` | Anthropic API key for Claude                     | `--with-api-key`          |
| `codex_access_token`| OpenAI short-lived access token (JWT)            | `--with-access-token`     |

## Credential Pool Model

All credentials registered under a given scope form a **pool**. When the consumer needs one for a job it calls `select_credential(scope)`, which picks the row with the **lowest priority** (ties broken by LRU: least-recently-used healthy token, `status='ok'`, not expired) atomically and stamps its `used_at`. Multiple credentials in the same scope therefore share traffic without any "primary" flag.

The **caller** claims the credential exactly once per run and passes it into the runner's context; the runner has no pool access of its own, so the built command and the spawned process can never end up on two different credentials.

Health state lives on each row:

| Column       | Meaning                                                                 |
|--------------|-------------------------------------------------------------------------|
| `status`     | `ok` or `error`. Flipped to `error` only on a **known-permanent** auth failure (invalid credentials, expired OAuth, not logged in). A revoked/stale access token — or any further wording the **harness module** classifies as a transient credential rejection — is treated as **transient** instead: a short `throttled_until` cooldown, `status` stays `ok`. (Claude classifies any `401` credential-rejection wording this way, including `401 Invalid authentication credentials`; Codex covers revoked/stale-token wording only.) The consumer then has the final say from the credential itself: one with **nothing to rotate** (an API key) rejected this way really is dead, so it is poisoned rather than throttled forever. |
| `error_msg`  | Operator-visible reason for the latest failure.                         |
| `error_source` | Who quarantined the row: `auto` (the framework) or `operator` (an explicit human decision). `auto` is lifted automatically by the next **successful** run; `operator` never is. `NULL` means unknown provenance — every row quarantined before this column existed — and is treated as `operator`. |
| `expires_at` | Credential expiry (from the stored payload). A row is skipped once `expires_at` is in the **past** (a *future* value means still-valid). **Claude OAuth leaves this NULL on purpose** — see the note below. |
| `throttled_until` | Temporary **cooldown**. Set either to a usage/session limit's reset time (default 1h when unparseable), or to `now + 15 min` on a **transient auth failure** (revoked/stale access token — usually a concurrent-refresh race, not a dead credential). The pool skips the token while `throttled_until` is in the **future** and auto-includes it once it passes. Distinct from `expires_at` (credential expiry) and from `status='error'` (poison): `status` stays `'ok'` and the token self-recovers. |
| `lease_owner` / `leased_until` | **Refresh lease.** While `leased_until` is in the future, exactly one run (`job-<id>-attempt-<n>`) may use this row, because its credential is close enough to expiry that the run will probably rotate it — and a rotating refresh token is single-use, so a second worker replaying it would 401. `leased_until` is a *liveness* deadline the owning consumer keeps pushing forward for as long as that job runs, not a guess at the job's duration; if the consumer dies, the lease expires on its own (no reaper). Only near-expiry rotating credentials are leased — a fresh one is still shared by every worker. |
| `used_at`    | Last time a worker claimed the row — drives LRU ordering within a priority tier. |
| `priority`   | Pool selection weight. Lower value wins; 0 = default.                   |

**Four ways a credential leaves the pool (in increasing permanence):**
- **Throttled** (`throttled_until` in the future, `status='ok'`): hit a session/usage/rate limit. Temporary — the token auto-recovers at the reset time and the job **fails over** to another healthy token meanwhile. No operator action needed.
- **Transient-auth throttled** (`throttled_until` in the future, `status='ok'`): the CLI rejected the stored credential with a revoked/stale-token 401. Temporary — the same token label is often still serving other jobs, so it is **not** poisoned; the job fails over to another healthy token and this one returns to the pool after 15 minutes. No operator action needed.
- **Expired** (`expires_at` in the past): credential lapsed. Cleared by `credential:refresh`.
- **Leased** (`leased_until` in the future, `status='ok'`): a live run holds it exclusively while
  rotating a near-expiry credential. Temporary and automatic — other jobs use another credential (or
  requeue) and it returns to the pool seconds after that run ends. **Do not intervene**: see the
  warning below.
- **Errored** (`status='error'`): auth failure poisoned it. An `error_source='auto'` quarantine
  clears itself on the next successful run; an `operator` one (and any pre-existing quarantine,
  whose provenance is unknown) needs `credential:reset` or `credential:refresh`.

`credential:reset` clears **both** `status='error'` and any `throttled_until` cooldown. It
deliberately does **not** free a refresh lease.

> **Never `credential:refresh` a leased row** (`credential:list` marks it 🔒). Freeing the
> lease hands the credential to a second worker mid-refresh, which is exactly the race the lease
> exists to prevent; and `credential:refresh` would be undone minutes later when the leased job's own
> post-run capture rotates from the credential chain it materialized *before* your refresh.
> `credential:refresh` therefore **refuses** while the lease is live (it goes through the same upsert
> as `credential:register`), telling you the holder and when it expires — wait for it, or stop the
> job holding it. `credential:reset` is *not* refused: it only lifts `status`/`throttled_until` and
> deliberately leaves the lease alone, so it cannot free the credential mid-refresh. Resetting a
> leased row is still pointless — nothing about the row's health is what the lease is about.

> **Claude OAuth tokens** intentionally leave the row `expires_at` **NULL**.
> Claude's `expiresAt` is the short-lived (~8h) *access*-token expiry in epoch
> milliseconds; the long-lived refresh token is rotated by the CLI *during* a
> job run and written back to `credentials` afterwards
> (`ClaudeWorkspaceAdapter.capture_refreshed_credentials`). Storing the access-token
> expiry as the row `expires_at` would make `select_credential` skip the row after
> an idle gap even though it can still self-heal — so it is deliberately not set.
> A Claude credential showing `expires_at = NULL` is correct, not a bug.

## Credential Lifecycle

```
register → [use via LRU+priority] → (near-expiry rotating credential → exclusive refresh lease
                                     for one run, released at run end)
                                  → (transient 401 → 15-min throttle, self-recovers)
                                  → (permanent auth failure → auto-flagged status='error',
                                     error_source='auto' → cleared by the next successful run)
                                  → refresh | reset → deregister
```

## Register a Credential

### Interactive OAuth

Requires a TTY — opens a browser for the OAuth flow.

```bash
# Claude (OAuth)
agento credential:register claude my-token

# Codex (OAuth)
agento credential:register codex  my-token
```

### With an API key

The secret is never on the command line. Three input modes are supported (see [Reading secrets](#reading-secrets)):

```bash
# 1) Interactive prompt (TTY, input hidden via getpass):
agento credential:register claude my-token --with-api-key

# 2) Pipe:
echo "$ANTHROPIC_API_KEY" | agento credential:register claude my-token --with-api-key

# 3) File redirect:
agento credential:register codex my-token --with-api-key < /path/to/openai-key.txt
```

`--with-api-key` maps to `anthropic_api_key` for scope `claude` and `openai_api_key` for scope `codex`. Which modes a scope accepts is declared, not guessed: `registration_modes` in the provider's `agent_harnesses` entry. Passing a flag a scope does not declare fails with the supported list instead of a confusing downstream error.

### With an access token (JWT)

Same three input modes — JWT is read from stdin:

```bash
# Interactive prompt:
agento credential:register codex my-token --with-access-token

# Pipe:
echo "$CODEX_ACCESS_TOKEN" | agento credential:register codex my-token --with-access-token
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

If the account you are registering (its OAuth e-mail) already backs another enabled label in the same scope, `register` prints a warning and still proceeds. Two credentials on one account share a single-use refresh token and race on rotation, which can invalidate the chain — `credential:deregister` one unless the duplicate is intentional.

## List Credentials

```bash
agento credential:list
agento credential:list --scope claude
agento credential:list --json
agento credential:list --all    # include disabled tokens
```

Each row shows `account`, `type`, `priority`, `status`, `last_used`, and `expires`. `account` is the **real authenticated identity** behind the label — the OAuth e-mail Claude records in `oauthAccount.emailAddress`, or the one in a Codex `id_token` — extracted from the stored credential, not from the free-text label. It shows `account=?` when the credential carries none (API keys) or it cannot be read. Because the label is free text, this is the surface for spotting a label that does not match its account, or two labels pointing at one account. A token that is temporarily rate/usage-limited shows `status=ok (throttled)` plus a `⏳ throttled until <time>` line; an errored token shows its truncated `error_msg` and its provenance — `status=error (auto)` (self-clears on the next successful run), `status=error (operator)` (never does), or `status=error (operator?)` for a quarantine predating the `error_source` column. A row under a live refresh lease adds a `🔒 refresh lease held by job-<id>-attempt-<n> until <time>` line — leave it alone. `--json` includes `account`, `error_source`, `lease_owner` and `leased_until` alongside `throttled_until` and `expires_at`. The `credentials` blob is never surfaced.

## Set Pool Priority

Lower priority wins. Tokens with the same priority are ranked by LRU.

```bash
agento credential:set-priority <credential_id> <priority>
```

Example — pin token 3 as preferred (priority 0) and demote token 5 (priority 10):

```bash
agento credential:set-priority 3 0
agento credential:set-priority 5 10
```

## Refresh an Expired OAuth Credential

```bash
agento credential:refresh 1    # Re-authenticate token ID 1 (interactive OAuth)
```

Refresh overwrites the stored `credentials`, re-parses `expires_at` from the new payload, and resets `status='ok'` / `error_msg=NULL` / `error_source=NULL`. It **refuses** (leaving everything untouched) while a refresh lease on that label is still live. The `id`, `label`, and `type` are preserved so downstream references stay valid.

Note: `credential:refresh` only supports the interactive OAuth flow. To update an API key or access token, re-register with the appropriate flag (same label will upsert the existing row).

Distinct from interactive refresh, **automatic post-run capture** (the agent CLI rotating its own OAuth token during a job, written back afterwards) updates the stored `credentials` (and refreshes `expires_at` from the payload) but does **not** touch operator/health state — it does **not** re-enable a disabled token or clear an `error` status. An operator who disables or quarantines a token while a job is running keeps that decision; the rotated credentials are still saved so the token is healthy if it is later re-enabled.

## Manual Error Control

Useful when you know a license has been revoked or want to take one offline for a bit:

```bash
agento credential:mark-error 1 "Revoked by admin 2026-04-23"
agento credential:reset 1    # clear status=error AND any throttle, status back to 'ok'
```

`mark-error` records `error_source='operator'`, so the framework's own self-heal will never lift it — an operator's decision outlives any number of successful runs. `reset` puts it back in rotation without a full re-auth round-trip (it also lifts a usage-limit `throttled_until` cooldown, should you want to force a throttled token back early).

## Usage Stats

```bash
agento credential:usage             # Show usage stats across all scopes
agento credential:usage --scope claude --window 72
```

## Deregister

```bash
agento credential:deregister 1   # soft-disable (enabled=FALSE); data retained
```

## Binding a harness + provider to an `agent_view`

The consumer requires `agent_view/harness` to be set — there is no sticky-primary
fallback. The provider defaults to the harness's own `default_provider`, so a
single-provider harness needs only the one setting:

```bash
agento config:set agent_view/harness  claude    --scope=agent_view --scope-id=1
agento config:set agent_view/provider anthropic --scope=agent_view --scope-id=1
```

Without a harness, jobs for that agent_view fail fast with
`No agent_view/harness configured`.

**Pre-0.15 configs keep working.** Before the split, `agent_view/provider` held what is
now the *harness* id. A value that names a registered harness is recognised as such
(structurally — there is no hardcoded `claude`/`codex` list) and mapped to that harness
plus its default provider, with a warning telling you how to migrate. Because
`config.json` now always ships a default harness, the fallback compares the two values'
**origins** (ENV > agent_view > workspace > default > `config.json`): an operator still
carrying `CONFIG__AGENT_VIEW__PROVIDER=codex` in ENV gets `codex`, not the default
harness plus a provider it does not offer. `setup:upgrade` also migrates stored rows
once via the `SplitProviderIntoHarness` data patch.

## Requirements

- `AGENTO_ENCRYPTION_KEY` must be set (same key used for `core_config_data` obscure fields). See [encryption.md](../config/encryption.md).
- The `credential` schema is maintained by framework migrations beginning with `019_oauth_token_inline_credentials.sql`; the rename plus the `scope` column land in `030_credential_scope_and_rename.sql`, and `error_source` / `lease_owner` / `leased_until` in `034_credential_error_source_and_refresh_lease.sql`. `agento setup:upgrade` applies pending migrations.

Source: [src/agento/framework/cli/credential.py](../../src/agento/framework/cli/credential.py) (deprecated aliases: [credential_aliases.py](../../src/agento/framework/cli/credential_aliases.py))
