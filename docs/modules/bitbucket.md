# Bitbucket Cloud PR-review Channel

Watches an agent's **open Bitbucket Cloud pull requests** in a configured repo allow-list and queues
PR-review work on two triggers. Modeled on the [Outlook channel](outlook.md): a Python publisher that
holds **no credential**, and a toolbox (Node) layer that is the **only** holder of the Bitbucket API
token. Ships **disabled and inert**; nothing happens until you enable it per agent_view.

## Architecture

```
cron ──► Python publisher (NO token) ──HTTP──► Toolbox (only token holder) ──HTTPS──► api.bitbucket.org/2.0
         bitbucket:publish-changes  (~1m)      POST /api/bitbucket/verify     (onboarding)
         bitbucket:publish-comments (~2h)      POST /api/bitbucket/open-prs   (publisher)
                                               MCP tools bitbucket_*           (agent, opt-in)
```

The publisher loops active agent_views, resolves each view's scoped config, and asks the toolbox for
that view's open PRs. The toolbox decrypts the token, enforces the allow-list, and computes the per-PR
records. The publisher decides (in pure functions) whether a PR has work and publishes one job per PR.

## Configuration

All fields resolve at **DEFAULT** *or* **AGENT_VIEW** scope (views are fully independent — different
workspaces/repos with different credentials).

| Path | Type | Purpose |
|---|---|---|
| `bitbucket/enabled` | boolean | Channel inert until `1` (default `0`) |
| `bitbucket/bitbucket_workspace` | string | Workspace slug |
| `bitbucket/bitbucket_email` | string | Agent Atlassian account email (Basic-auth username) |
| `bitbucket/bitbucket_api_token` | **obscure** | Atlassian API token — encrypted at rest, **toolbox-only** |
| `bitbucket/bitbucket_account_uuid` | string | Agent's Bitbucket account UUID (`author.uuid` match) |
| `bitbucket/repo_allowlist` | string | Comma-separated **bare** repo slugs (`api,web` — never `acme/api`; the workspace comes from `bitbucket_workspace`); **empty ⇒ view skipped** (no scan) |
| `bitbucket/poll_top` | integer | Max open PRs fetched per repo per poll (clamped 1..50, default 20) |

### Config is always agent_view-scoped (important)

Bitbucket config is **always per-agent_view** — there is no DEFAULT-scope Bitbucket setup, single-view or
not. Two reasons:

- **Security — the API token must never be at DEFAULT scope.** The framework's `bootstrap()` resolves
  DEFAULT-scope obscure config in the cron process and would **decrypt** a DEFAULT-scope token there.
  Keeping the token (`bitbucket_api_token`) agent_view-scoped means `bootstrap()` (DEFAULT-only) never
  sees it, and the publisher resolves only non-secret fields — so the token is **only ever decrypted
  inside the toolbox**.
- **Attribution — no fan-out.** Each view's `bitbucket_account_uuid` + `repo_allowlist` must be at **its
  own agent_view scope**, so a view never inherits another's account/repos and fan the same PR out. A
  view lacking its own `account_uuid`/`repo_allowlist` is **skipped** (logged, not errored).

`setup:upgrade` onboarding writes everything at the owning view's scope automatically (it auto-selects the
sole active view, or prompts when there are several, and refuses if there are none). Manual `config:set`
users must pass `--scope=agent_view --scope-id=<id>` for **every** Bitbucket key. Onboarding's "complete"
check requires `bitbucket_api_token` + `bitbucket_account_uuid` + `repo_allowlist` at a view's own
agent_view scope (workspace/email may inherit DEFAULT/ENV) — so "complete" always implies "secure and will
actually publish".

### API-token scopes (granular, no implication)

Atlassian API-token scopes do **not** implicitly grant one another (unlike OAuth), so each is required
explicitly:

- `read:user:bitbucket` — `GET /2.0/user` (verify + discover the account UUID)
- `read:repository:bitbucket` — PR list / diff / commits
- `read:pullrequest:bitbucket` — read PRs, comments, activity; collaborate (comment/approve)
- `write:pullrequest:bitbucket` — create PRs, request changes

`write:repository:bitbucket` is **not** requested — the channel never writes repo contents via the API;
pushing commits is the agent's own git identity (see *Security model* below). App-password / OAuth scope
names (`pullrequest`, `pullrequest:write`, `repository`) are deprecated and unused.

## Onboarding (verify before save)

```bash
agento setup:upgrade            # choose "bitbucket" — prompts for workspace, email, API token, repos
```

Onboarding **verifies the credential against `GET /2.0/user` (inside the toolbox) before saving
anything**: on failure it offers retry/abort and writes nothing; on success it captures the account UUID
and writes all fields in one transaction. A reachable `core/toolbox/url` is required (the token is only
ever used inside the toolbox).

**Git commit identity is seeded automatically.** In the same transaction, onboarding also writes
`agent_view/identity/git_author_email` (the email you entered) and `agent_view/identity/git_author_name`
(the verified account's `username`) so the agent's PR commits are **authored by — and link to — this
Bitbucket account**. Bitbucket links a commit only when the author email matches a *verified* email on
the account, which is exactly the email used here. `workspace:build` materializes these into the
sandbox's `~/.gitconfig` (see [identity docs](../config/identity.md)); override anytime with
`config:set agent_view/identity/git_author_*`.

Offline alternative — set everything manually:

# All Bitbucket keys go at AGENT_VIEW scope (the token must never be at DEFAULT). Replace <id> with the
# owning agent_view's id.
```bash
agento config:set core/toolbox/url http://toolbox:3001
agento config:set bitbucket/bitbucket_workspace acme            --scope=agent_view --scope-id=<id>
agento config:set bitbucket/bitbucket_email agent@acme.com      --scope=agent_view --scope-id=<id>
# obscure ⇒ encrypted; read from stdin (never pass the token as a positional value):
printf '%s' "$BITBUCKET_API_TOKEN" | agento config:set bitbucket/bitbucket_api_token --scope=agent_view --scope-id=<id>
agento config:set bitbucket/bitbucket_account_uuid '{your-uuid}' --scope=agent_view --scope-id=<id>
agento config:set bitbucket/repo_allowlist 'api,web'            --scope=agent_view --scope-id=<id>
agento config:set bitbucket/enabled 1                           --scope=agent_view --scope-id=<id>
# Git commit identity so PR commits link to the account (onboarding sets these for you).
# The email MUST be a verified email on the Bitbucket account.
agento config:set agent_view/identity/git_author_email agent@example.com --scope=agent_view --scope-id=<id>
agento config:set agent_view/identity/git_author_name 'Agent Acme'    --scope=agent_view --scope-id=<id>
```

See [docs/cli/onboarding.md](../cli/onboarding.md) for the onboarding model.

## Triggers (two cron-driven publishers)

| Command | Cadence | What it flags | Priority |
|---|---|---|---|
| `bitbucket:publish-comments` | every 2h (`0 */2 * * *`) | OPEN PRs with **unanswered reviewer feedback** | base |
| `bitbucket:publish-changes` | every 1m (`* * * * *`) | reviewer set **"changes requested"** on the agent's PR | **fast lane** (base + 30, capped 100) |

Both are operator-runnable for debugging (like `outlook:publish`):

```bash
agento bitbucket:publish-comments --agent-view <code>   # --agent-view limits to one view
agento bitbucket:publish-changes  --top 5               # --top narrows poll_top for this run
```

- **"Unanswered"** = a non-deleted, **non-resolved** comment by someone other than the agent whose
  `created_on` is newer than **both** the agent's last comment **and** the PR's last commit (a timestamp
  watermark — survives force-push; a resolved thread counts as addressed). **One job per PR**, never one
  per comment; the requester is the author of the newest unanswered comment.
- **"Changes requested"** is read from the PR's **`/activity` event log** (the authoritative who/when),
  taking the newest non-agent `changes_request` event. The agent's own events are ignored.

### No duplicate / no repeated work

Each job uses `reference_id = "{workspace}/{repo}:{pr_id}"`, `skip_if_active=True`, and a **distinct
`source` per lane** (`bitbucket-comments` / `bitbucket-changes`). Distinct sources are what let the
urgent changes-requested job run even while a sweep job for the same PR is still active. The idempotency
key carries the newest feedback/changes timestamp, so a no-op rescan dedupes while genuinely new feedback
re-queues (≤1 outstanding job per PR per trigger).

## What the agent can do on a PR (each capability opt-in)

Every tool is **disabled by default**; enable per scope. The API token is never reachable by the agent.

| Tool | Capability | Token scope |
|---|---|---|
| `bitbucket_get_pr` | read PR + description | `read:repository` / `read:pullrequest` |
| `bitbucket_get_pr_diff` | read the diff | `read:repository` |
| `bitbucket_get_pr_comments` | read comments | `read:pullrequest` |
| `bitbucket_get_pr_activity` | read review history | `read:pullrequest` |
| `bitbucket_add_comment` | reply (incl. inline file:line) | `read:pullrequest` |
| `bitbucket_resolve_comment` | resolve a thread | `read:pullrequest` |
| `bitbucket_set_review` | approve / request changes / none | `read:pullrequest` (collaborate) |
| `bitbucket_create_pr` | open a new PR | `write:pullrequest` |

```bash
for t in bitbucket_get_pr bitbucket_get_pr_diff bitbucket_get_pr_comments bitbucket_add_comment; do
  agento tool:enable "$t" --agent-view <code>
done
```

**Checkout + push** is **not** a Bitbucket tool — it is the agent's own git identity (the existing
`workspace_build` SSH identity, config `agent_view/identity/ssh_private_key`). It is "opt-in" by virtue
of that identity being configured (no identity ⇒ no push). The Bitbucket API token (REST) and the SSH
key (git) are different credentials.

## Security model

- **Token boundary — decrypted only in the toolbox:** `bitbucket_api_token` is encrypted at rest, omitted
  from the Python `BitbucketConfig`, and never echoed in logs/errors. Two things guarantee the cron/
  publisher never decrypts it: (1) the token is **always agent_view-scoped, never at DEFAULT** — so the
  framework's `bootstrap()`, which resolves DEFAULT-scope obscure config, never sees it; (2) the publisher
  resolves only the non-secret fields via per-path reads, never the token path. The toolbox is the only
  place that resolves+decrypts it (and onboarding verifies it with body creds, transiently). The **agent**
  never receives it in any case.
- **Authorization boundary = the toolbox.** Workspace, `account_uuid` and `repo_allowlist` are resolved
  from **scoped config** and enforced on **every** REST + MCP call; `bitbucket/enabled` gates the
  publishers and the REST poll, while the agent's MCP surface is gated per tool by `is_enabled` —
  turning the channel off does not retract a tool the operator enabled. Caller/body args may
  only *narrow* a request, never authorize it. The **workspace is never a tool argument** — every MCP
  tool targets the configured `bitbucket_workspace`, so a caller cannot redirect a call to another
  workspace (an injected `workspace` arg is ignored). Every read/write is bounded to the resolved
  `repo_allowlist` (empty allow-list rejects all — fail-closed by config absence). `bitbucket_create_pr`
  validates **both** the destination repo and any `source.repository` (forks/cross-repo, whose workspace
  half must equal the configured workspace) against the allow-list. Write tools re-fetch the PR and
  reject anything but an OPEN PR.
- **`repo` is a bare slug — the argument is normalized, the allow-list is not.** Every tool's `repo`
  parameter carries the rule in its own schema description ("Bare repository slug WITHOUT the workspace
  prefix"). If the caller writes `workspace/repo` anyway and the prefix **equals**
  `bitbucket_workspace`, the prefix is stripped and the call proceeds — the request URL is built from the
  configured workspace either way, so this neither redirects the call nor widens anything. A prefix
  naming any **other** workspace is refused by name. The **allow-list itself is never normalized**: it
  stays an exact match on bare slugs, and workspace-prefixed *config* entries are only diagnosed. This
  does not touch `source_repository`, which legitimately takes the `workspace/repo` form.
- **Honest boundary (N5-2):** the MCP layer cannot see `agentViewMeta`, and the agent — a shell-capable
  process on the same Docker network as the toolbox — could in principle open its own MCP session with a
  different/omitted `agent_view_id`. That is the **framework-wide internal-caller-auth gap shared by the
  Jira and Outlook channels**; fixing it needs a framework change and is out of scope here. The Bitbucket
  module does not worsen it and compensates (token toolbox-only, opt-in tools, allow-list-bounded,
  fail-closed). Bitbucket config is **always agent_view-scoped** (never DEFAULT), which also minimizes
  cross-view exposure under this gap.
- **Rate limits / outages:** the toolbox retries on 429 for any method (the request was rejected, not
  processed) and on 5xx for **idempotent GETs only** — mutating POSTs (comment/resolve/review/create) are
  **not** auto-retried on 5xx, to avoid duplicate writes. `Retry-After` is honored; backoff is capped.
- **Failure isolation:** a failing repo is reported in `errors[]` and the run continues; a per-PR or
  per-view error is logged and skipped. Only OPEN PRs are discovered; a PR that closes mid-work is a
  clean no-op (the agent stops, and the write tools reject it).

## Enable / disable

```bash
agento module:enable bitbucket          # then config + tool:enable + bitbucket/enabled 1
agento config:set bitbucket/enabled 1 --scope=agent_view --scope-id=<id>   # config is agent_view-scoped
agento module:disable bitbucket         # fully inert; the rest of the system is unaffected
```

The channel introduces **no framework changes** and **no schema migrations** — it reuses the existing
`job`, `core_config_data`, and `ingress_identity` tables. (The ACC's optional explicit bind is met by the
generic `agento ingress:bind bitbucket <account_uuid> <agent_view_code>`; the publisher does not depend
on it — routing is by per-view config, like Outlook's mailbox.)
