# GitHub PR-review Channel

Watches an agent's **open GitHub pull requests** in a configured repo allow-list and queues PR-review
work on two triggers. A port of the [Bitbucket channel](bitbucket.md): a Python publisher that holds
**no credential**, and a toolbox (Node) layer that is the **only** holder of the stored GitHub token.
Ships **disabled and inert**; nothing happens until you enable it per agent_view.

## Architecture

```
cron ──► Python publisher (NO token) ──HTTP──► Toolbox (only token holder) ──HTTPS──► api.github.com
         github:publish-changes  (~1m)         POST /api/github/verify     (onboarding)
         github:publish-comments (~2h)         POST /api/github/open-prs   (publisher)
                                               MCP tools github_*           (agent, opt-in)
```

The publisher loops active agent_views, resolves each view's scoped config, and asks the toolbox for
that view's open PRs. The toolbox decrypts the token, enforces the allow-list, and computes the per-PR
records. The publisher decides (in pure functions) whether a PR has work and publishes one job per PR.

**GitHub Enterprise Server is not supported.** The API host is hardcoded (`https://api.github.com`,
GraphQL `https://api.github.com/graphql`) exactly as the Bitbucket module hardcodes `api.bitbucket.org`.
A caller-supplied API base would turn the toolbox — the one container holding secrets, reachable from
the agent's Docker network — into an SSRF probe against internal hosts. If GHES is ever wanted it must
arrive with an operator-configured origin allow-list, not a request-body field.

## Configuration

| Path | Type | Scope | Purpose |
|---|---|---|---|
| `github/enabled` | boolean | any | Channel inert until `1` (default `0`) |
| `github/github_owner` | string | any | Owner (user or organization) that owns the watched repos |
| `github/github_login` | string | **agent_view only** | The agent account's GitHub login (author/self match) |
| `github/github_token` | **obscure** | **agent_view only** | Personal access token — encrypted at rest, **toolbox-only** |
| `github/repo_allowlist` | string | **agent_view only** | Comma-separated repo names; **empty ⇒ view skipped** (no scan) |
| `github/poll_top` | integer | any | Max **agent-authored** open PRs per repo per poll (clamped 1..100, default 20) |

Onboarding additionally seeds `agent_view/identity/git_author_email` and
`agent_view/identity/git_author_name`. Those two paths belong to `agent_view`, not to this module — the
channel writes them once as a convenience and never reads them.

### Which config is agent_view-only

Three fields — `github_token`, `github_login`, `repo_allowlist` — are declared
`showInDefault: false` / `showInWorkspace: false` in `system.json`, so `config:set` **refuses** them at
DEFAULT and WORKSPACE scope. This is stronger than the Bitbucket module, which states the same rule in
prose only. Two reasons:

- **Security — the token must never be at DEFAULT scope.** The framework's `bootstrap()` resolves
  DEFAULT-scope obscure config in the cron process and would **decrypt** a DEFAULT-scope token there.
  `load_db_overrides` selects `scope = 'default' AND scope_id = 0` **only**, so an agent_view-scoped
  token row is invisible to it — and `showInDefault: false` is what keeps the row out of DEFAULT.
- **Attribution — no fan-out.** Each view's `github_login` + `repo_allowlist` must be at **its own**
  agent_view scope, so a view never inherits another's account/repos and fan the same PR out. In a
  multi-view deployment a view lacking its own pair is **skipped** (logged, not errored).

The other three paths are deliberately unrestricted: `github_owner` may sensibly be one value for the
whole deployment and inherit from DEFAULT, and `enabled` / `poll_top` are operational switches with no
per-view secrecy requirement.

### The limit of that enforcement (read this)

Scope enforcement covers the **DB** path only:

- a stored (DB) token is **never** decrypted in the cron process — see `load_db_overrides` above;
- an **ENV** var (`CONFIG__GITHUB__GITHUB_TOKEN`) still outranks DB config in `resolve_field`, and is
  resolved during `bootstrap()` *before any module code runs*, so an operator who exports one puts the
  plaintext in the cron process's reach for that boot;
- the module therefore **refuses to operate** in that state on all four surfaces — the publisher
  (`run_lane` → 0 jobs), the REST handlers (HTTP 503), MCP registration (zero tools registered), and
  onboarding (`is_complete()` reports incomplete and names the offending key). The same three keys are
  refused on both sides — Python (`src/env_guard.py`) and Node (`toolbox/env-guard.js`) — and an
  integration test asserts the two key lists cannot drift apart;
- the proper fix is a per-field `toolbox_only` exclusion in the framework resolver. **It does not exist
  today.** It is recorded as a framework follow-up (see [DECISIONS.md](../../DECISIONS.md) and
  [ROADMAP.md](../../ROADMAP.md)), not as something this module claims to have solved.

Nothing in this module, its docs or its onboarding asks anyone to set a `CONFIG__GITHUB__*` override
for a credential: credentials are stored encrypted in `core_config_data` at AGENT_VIEW scope, as
`bitbucket` and `outlook` do.

### Token permissions

Fine-grained PAT, scoped to the watched repositories (REST `2022-11-28`):

| Need | Permission |
|---|---|
| List/Get PRs, comments, reviews; post comments; submit reviews | **Pull requests: Read and write** |
| Any repository endpoint | **Metadata: Read-only** (mandatory for all) |
| Read the head commit (`GET /repos/{o}/{r}/commits/{sha}`, the comments-lane force-push watermark) | **Contents: Read-only** |

Classic PAT equivalent: `repo` (public repositories only: `public_repo`). `GET /user` returns the
authenticated user's public fields — including `login` and `id`, the only two this module reads — with
**no** scope, so `read:user` is not requested. **No** `workflow` and **no** write to Contents: pushing
commits is the agent's own git/SSH identity, exactly as in Bitbucket.

## Onboarding (verify before save)

```bash
agento setup:upgrade            # choose "github" — prompts for owner, PAT, repos
```

Onboarding **verifies the credential against `GET /user` (inside the toolbox) before saving anything**:
on failure it offers retry/abort and writes nothing; on success it captures the account's `login` and
`id` and writes all fields in one transaction at the owning view's agent_view scope. A reachable
`core/toolbox/url` is required (the token is only ever used inside the toolbox).

**Git commit identity is seeded automatically.** In the same transaction onboarding writes
`agent_view/identity/git_author_name` (the verified `login`) and `agent_view/identity/git_author_email`,
defaulting to `<id>+<login>@users.noreply.github.com`. GitHub links a commit to an account only when the
author email is a **verified** email on it — the `users.noreply` form always links and needs no address
the operator has to verify. `workspace:build` materializes these into the sandbox's `~/.gitconfig` (see
[identity docs](../config/identity.md)); override anytime with `config:set agent_view/identity/git_author_*`.

Offline alternative — set everything manually:

```bash
# The token, login and allow-list are REFUSED at DEFAULT/WORKSPACE scope. Replace <id> with the owning
# agent_view's id.
agento config:set core/toolbox/url http://toolbox:3001
agento config:set github/github_owner acme                              # may be DEFAULT-scoped
agento config:set github/github_login agent-bot   --scope=agent_view --scope-id=<id>
# obscure ⇒ encrypted; read from stdin (never pass the token as a positional value):
printf '%s' "$GITHUB_TOKEN" | agento config:set github/github_token --scope=agent_view --scope-id=<id>
agento config:set github/repo_allowlist 'api,web' --scope=agent_view --scope-id=<id>
agento config:set github/enabled 1                --scope=agent_view --scope-id=<id>
# Git commit identity so PR commits link to the account (onboarding sets these for you).
agento config:set agent_view/identity/git_author_email '42+agent-bot@users.noreply.github.com' \
  --scope=agent_view --scope-id=<id>
agento config:set agent_view/identity/git_author_name 'agent-bot' --scope=agent_view --scope-id=<id>
```

See [docs/cli/onboarding.md](../cli/onboarding.md) for the onboarding model.

## Triggers (two cron-driven publishers)

| Command | Cadence | What it flags | Priority |
|---|---|---|---|
| `github:publish-comments` | every 2h (`0 */2 * * *`) | OPEN PRs with **unanswered reviewer feedback** | base |
| `github:publish-changes` | every 1m (`* * * * *`) | reviewer currently at **"changes requested"** on the agent's PR | **fast lane** (base + 30, capped 100) |

Both are operator-runnable for debugging:

```bash
agento github:publish-comments --agent-view <code>   # --agent-view limits to one view
agento github:publish-changes  --top 5               # --top narrows poll_top for this run
```

- **"Unanswered"** = a **non-resolved** comment by someone other than the agent whose `created_at` is
  **at or after both** the agent's last comment **and** the PR's head commit (a timestamp watermark —
  survives force-push; a resolved thread counts as addressed). GitHub timestamps are second-precision
  and nothing orders a comment against a commit within one second, so **equal-second feedback is
  treated as actionable**. At worst that queues **one** unnecessary job — when the real order inside
  that second was "feedback, then answer" and nothing had been published for it yet; later scans dedupe
  on the unchanged idempotency key. The strict comparison would instead drop a real "answer, then
  follow-up" permanently, so the bounded false positive is the cheaper error. **One job per PR**, never
  one per comment; the requester is the author of the newest unanswered comment.
- **"Changes requested"** is read from `GET /pulls/{n}/reviews`, folded to each reviewer's **current**
  position: the latest *deciding* review per reviewer (`COMMENTED`/`PENDING` do not change a position),
  counted only if that position is `CHANGES_REQUESTED`. The agent's own reviews are ignored.

### No duplicate / no repeated work

Each job uses `reference_id = "{owner}/{repo}:{pr_number}"`, `skip_if_active=True`, and a **distinct
`source` per lane** (`github-comments` / `github-changes`). Distinct sources are what let the urgent
changes-requested job run even while a sweep job for the same PR is still active. GitHub timestamps are
second-precision, so a key built from the timestamp alone would drop a second comment posted in the same
second — and the three comment surfaces have independent id namespaces, so "timestamp + the newest
comment's id" would drop one too. The comments key therefore carries the newest timestamp **plus a
digest of every unanswered comment at that second** (`surface:id`, sorted); the changes key carries the
review's submission time plus its id. A no-op rescan dedupes; genuinely new feedback re-queues (≤1
outstanding job per PR per trigger).

## What the agent can do on a PR (each capability opt-in)

Every tool is **disabled by default**; enable per scope. The token is never reachable by the agent.

| Tool | Capability |
|---|---|
| `github_get_pr` | read PR + description |
| `github_get_pr_diff` | read the diff |
| `github_get_pr_comments` | read comments — conversation + inline review comments (review **bodies** come from `github_get_pr_reviews`) |
| `github_get_pr_reviews` | read review history |
| `github_add_comment` | reply (top-level, threaded reply, or inline file:line) |
| `github_resolve_thread` | resolve a review thread (GraphQL) |
| `github_set_review` | approve / request changes / comment |
| `github_create_pr` | open a new PR |

```bash
for t in github_get_pr github_get_pr_diff github_get_pr_comments github_add_comment; do
  agento tool:enable "$t" --agent-view <code>
done
```

**Checkout + push** is **not** a GitHub tool — it is the agent's own git identity (the existing
`workspace_build` SSH identity, config `agent_view/identity/ssh_private_key`). It is "opt-in" by virtue
of that identity being configured (no identity ⇒ no push). The GitHub PAT (REST/GraphQL) and the SSH
key (git) are different credentials.

## Differences from the Bitbucket channel

- **Comments span three GitHub surfaces** — conversation (issue) comments, inline review comments, and
  review bodies. The **publisher's** watermark merges all three into one ordering, so an agent reply on
  any surface moves the watermark and silences the others. (The agent's *read* tools keep them apart:
  `github_get_pr_comments` returns the two comment surfaces, `github_get_pr_reviews` the review bodies.)
- **Thread resolution is GraphQL-only** (REST exposes neither thread ids nor resolved state). If GraphQL
  is unavailable, or its 100-thread / 100-comment-per-thread bound is hit, the resolution state is
  **unknown**, so that PR's comments lane is skipped for the poll and retried on the next one. It is
  never treated as "unresolved and therefore actionable".
- **`github_set_review` has no `none`** — GitHub cannot retract your own review — and adds `comment`.
  GitHub also refuses `approve`/`request_changes` on your **own** PR (HTTP 422, its generic validation
  status, which also covers a stale commit or an already-pending review), so on the agent's own PRs only
  `comment` is usable.
- **`poll_top` caps agent-authored PRs per repo** (max 100), applied *after* a client-side author
  filter, because GitHub's list-pulls endpoint has no server-side author filter. A repo full of
  third-party PRs therefore cannot starve the agent's own.
- **The changes lane tracks each reviewer's current position**, so a later approval retires an earlier
  change-request (GitHub keeps the superseded review in the list forever).
- **GitHub Enterprise Server is out of scope** (see *Architecture*).
- **Git identity defaults to `<id>+<login>@users.noreply.github.com`**, which always links, instead of
  Bitbucket's "must be a verified account email".
- **Comments and reviews from deleted/ghost accounts are ignored** (GitHub returns `user: null`), so no
  job is ever attributed to an unidentifiable author.
- **An incomplete scan suppresses that lane's publish for the affected PR** — a page cap hit,
  unavailable GraphQL resolution state, or an unfetchable/undated head commit is reported in the
  publisher log and in `errors[]`, and the PR is skipped, never silently treated as complete. The read
  tools (`github_get_pr_comments`, `github_get_pr_reviews`) surface the same bound to the agent as a
  `truncated` marker.

### Verified API facts

Confirmed against GitHub, not from memory (these are the strings the code compares exactly):

- **Review states** (`PullRequestReviewState`): `PENDING`, `COMMENTED`, `APPROVED`,
  `CHANGES_REQUESTED`, `DISMISSED`. The submit-review `event` values are `APPROVE`, `REQUEST_CHANGES`,
  `COMMENT` (plus `DISMISS` for dismissing someone else's review, which this module does not do).
- **Review threads** (GraphQL): `PullRequestReviewThread` has `id: ID!`, `isResolved: Boolean!` and
  `comments: PullRequestReviewCommentConnection!`; the mutation is
  `resolveReviewThread(input: {threadId: ID!}) { thread { id isResolved } }`.
- **Comment ids:** `PullRequestReviewComment.databaseId` is **deprecated** in favour of
  `fullDatabaseId` (BigInt, delivered as a string), and the queries select `fullDatabaseId` **only** —
  naming a removed field fails the whole GraphQL query at validation, so selecting both would be an
  outage, not a fallback. `github_resolve_thread` compares it as a string and is exact at any size; the
  publisher's resolution scan converts it to a number to match the REST comment id, which JSON already
  delivered as a double, so that match is exact up to 2^53 (GitHub's comment ids are ~10 digits) — a
  limit of the REST representation, not of this code.

## Security model

- **Token boundary — decrypted only in the toolbox:** `github_token` is encrypted at rest, omitted from
  the Python `GitHubConfig`, and never echoed in logs/errors. Two things keep the cron/publisher from
  decrypting it: (1) the token is refused at DEFAULT scope, so `bootstrap()` never sees it; (2) the
  publisher resolves only the non-secret fields via per-path reads, never `get_module("github")`. The
  toolbox is the only place that resolves and decrypts it. The **agent** never receives it. The one
  deliberate transmitting path is onboarding's `verify(token)` hop, which posts the operator-typed token
  once, before anything is saved, and never persists it Python-side. The residual ENV window is
  described under *The limit of that enforcement* above.
- **Authorization boundary = the toolbox.** Owner, `github_login` and `repo_allowlist` are resolved
  from **scoped config** and enforced on **every** REST + MCP call; `github/enabled` gates the
  publishers and the REST poll (an inert view is never scanned), while the agent's MCP surface is gated
  per tool by `is_enabled` — turning the channel off does not retract a tool the operator enabled, so
  disable the tools (or the module) to remove the capability. Caller/body args may only
  *narrow* a request, never authorize it. The **owner is never a tool argument** — every MCP tool targets
  the configured `github_owner`, so a caller cannot redirect a call to another owner. Every read/write is
  bounded to the resolved `repo_allowlist` (empty allow-list rejects all — fail-closed by config
  absence). `github_create_pr` additionally requires any `owner:branch` head to name the configured
  owner. Write tools re-fetch the PR and reject anything but an **open** one.
- **Honest boundary (N5-2):** the MCP layer cannot see `agentViewMeta`, and the agent — a shell-capable
  process on the same Docker network as the toolbox — could in principle open its own MCP session with a
  different or omitted `agent_view_id` (`/sse` and `/mcp` take it from the query string; the module REST
  handlers take it from the body). That is the **framework-wide internal-caller-auth gap shared by the
  Jira, Outlook and Bitbucket channels** — `bitbucket` already exposes the same eight capabilities behind
  the same door. This module ships at parity: it neither worsens nor fixes the gap, and compensates the
  same way (token toolbox-only, opt-in tools, allow-list-bounded, fail-closed). The fix belongs in
  `src/agento/toolbox/server.js`, applied once for all four modules; it is recorded as a framework
  follow-up in [DECISIONS.md](../../DECISIONS.md) and [ROADMAP.md](../../ROADMAP.md).
- **Rate limits / outages:** GitHub signals a rate limit with **403 or 429**; a 403 counts as one only
  when it carries `retry-after` or `x-ratelimit-remaining: 0`. The toolbox never retries earlier than
  instructed, and if the instructed wait exceeds its 15s cap it **gives up this poll** — the cap is
  bounded by the publisher's 60s HTTP timeout, not by politeness (an integration test asserts the two
  numbers stay compatible). 5xx is retried for **idempotent GETs only**; mutating POSTs are never
  auto-retried, to avoid duplicate writes.
- **Failure isolation:** a failing repo is reported in `errors[]` and the run continues; a per-PR or
  per-view error is logged and skipped. Only open PRs are discovered; a PR that closes mid-work is a
  clean no-op (the agent stops, and the write tools reject it).

## Enable / disable

```bash
agento module:enable github              # then config + tool:enable + github/enabled 1
agento config:set github/enabled 1 --scope=agent_view --scope-id=<id>
agento module:disable github             # fully inert; the rest of the system is unaffected
```

The channel introduces **no framework changes** and **no schema migrations** — it reuses the existing
`job`, `core_config_data` and `ingress_identity` tables, and declares `sequence: []` so disabling it
leaves the system fully operational.
