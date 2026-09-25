# Auth context v1

Every capability the toolbox accepts yields **one** structure, whatever the transport. The
toolbox reads the `toolbox_capability` row and derives the context from it. It never reads a
claim from the URL, the body or a query string.

- Node (verifies): `src/agento/toolbox/auth-context.js` → `deriveAuthContext()`
- Python (issues): `src/agento/framework/auth_context.py` → `derive_auth_context()`
- Shared fixture: `tests/fixtures/auth_context_v1.json`. Both languages run every case. When
  the two sides disagree, a test fails on the side that is wrong.

`issue_capability()` runs the same derivation on the row before it writes it, once for each
transport that row carries. Python therefore cannot mint a row that Node would reject.

## Shape

```
{ actor, subject_id, on_behalf_of, agent_view_id, workspace_id, job_id, execution_id,
  app: {artifact_code, version_id, launch_id} | null, tool_ceiling, allowed_transports,
  kind, expires_at, capability_id }
```

- `job_id` and `capability_id` are decimal strings. A BIGINT does not survive a JS number.
- `app.version_id` is a versioned-artifacts version id, a string (`v-20260925-120000-ab12`).
  The verifier accepts a non-empty string of at most 64 characters, the column width
  (`042`). The VA grammar is checked where the id is made, not here.
- `expires_at` is epoch seconds.
- `on_behalf_of` is always `null`. Nothing verifies delegation yet, so no profile can claim it.

The verifier returns `{context, single_use, permitted_tools}` or `null`. A rule that cannot be
derived is a rejection. It is never a permissive default.

## Profiles

| kind              | actor   | endpoints                                   | source | single use |
|-------------------|---------|---------------------------------------------|--------|------------|
| `mcp_job`         | agent   | sse, messages, mcp, invoke                  | —      | no         |
| `mcp_interactive` | agent   | sse, messages, mcp, invoke                  | —      | no         |
| `internal_rest`   | service | api, config_test, health (viewless: config_test only) | — | no  |
| `user_session`    | user    | invoke                                      | session | yes       |
| `miniapp`         | user    | invoke                                      | launch | yes        |

Each endpoint uses one transport: `sse` and `messages` use `sse`, and all other endpoints use
`http`. The row's `allowed_transports` must contain the transport of the endpoint. The column
has **no default**: a missing or empty value fails every kind at every endpoint. At issue time,
a set that contains `sse` is capped at the SSE TTL of 4 h, because a `?cap=` query string goes
into access logs. The invoke endpoint accepts the token **only** in the `Authorization` header.
A `?cap=` there gets a 401.

Migration `036_toolbox_capability_auth_context` backfills the rows that existed before E1:

- MCP kinds get `["sse","http"]`.
- `internal_rest` gets `["http"]` and subject `service:legacy-internal-rest`.
- Every legacy token's expiry is reduced to at most 4 h.

## TTL bounds

`core/auth/session_max_ttl` (default 43200, ceiling 86400), `core/auth/launch_max_ttl`
(3600 / 43200), `core/auth/capability_ttl` (30 / 300).

- Resolution order: ENV `CONFIG__CORE__AUTH__<KEY>` → workspace row → default row →
  `config.json` → code default.
- An `agent_view` row is never read.
- A value above its ceiling is clamped, and a WARN is logged once. Config can only make a bound
  smaller.
- A value that is not a positive integer, or a failed query, is an error (the verifier answers
  503). It never falls back to the default.

> Contract deviation: the PRD names these `auth/*`. A config path starts with its module, so the
> keys live in the framework's `core` namespace.

## For E2 / E6 (sessions and launches)

1. Mint through `issue_capability(conn, kind="user_session"|"miniapp", ...,
   allowed_transports=["http"], subject_id=<user id>, workspace_id=..., source_id=..., app=...,
   tool_ceiling=...)`. You own the session or launch, and you must verify it before you mint.
2. Export the source checker from your module's `toolbox/` code:
   `export const authSources = [["session", check]]`. At startup the toolbox collects every
   enabled module's `authSources`. A kind that two modules claim is dropped with an ERROR, so
   neither checker runs. `check(sourceId,
   {capability_kind})` returns the live source record `{kind, id, user_id, workspace_id,
   agent_view_id, permitted_tools, created_at, expires_at}` (a launch also has `launch_id`,
   `artifact_code`, `version_id`), or `null` when the source is revoked.
3. The verifier checks the capability **and** its source on every call. E1 ships no checker,
   so every `user_session` and `miniapp` row is refused until one is installed.
4. Call a tool with `POST /internal/tools/{name}:invoke`, header `Authorization: Bearer <token>`,
   body = the tool arguments as JSON. Mint a new capability for each call: the first call
   consumes a single-use token, and a second call with it gets 403.

## Tool dispatch

`src/agento/toolbox/dispatcher.js` → `executeTool()` answers every tool call: MCP `tools/call` on
`/mcp` and `/messages`, and the invoke endpoint. For each call it:

1. writes a `tool_invocation` row with outcome `pending` (arguments only as a SHA-256 digest);
2. consumes a single-use capability (`consumed_at`, one atomic `UPDATE`);
3. verifies the capability and its source again, by capability id;
4. finds the tool, then reads its `is_enabled` chain again for the caller's scope;
5. checks the launch `tool_ceiling` and the user's `permitted_tools`;
6. validates the arguments strictly (an unknown key is an error);
7. runs the handler, then writes the final outcome to the audit row.

| outcome             | invoke status |
|---------------------|---------------|
| `ok`                | 200           |
| `invalid_arguments` | 400           |
| `unauthorized`      | 403           |
| `not_found`         | 404 (also a disabled tool) |
| `tool_error`        | 422           |
| `unavailable`       | 503 (also a failed audit insert: the tool does not run) |

A body that is not JSON gets 400 and no audit row, because no execution started. On MCP the
same outcomes come back as a `CallToolResult` with `isError: true`.

## Harness placement

`agento.framework.harness.run_scope.toolbox_auth(url, token)` puts the run's token on the
toolbox MCP entry: an `Authorization: Bearer` header on `/mcp` (URL unchanged), and `?cap=` on
`/sse`, because an SSE client sends no headers. Issuers mint `["http"]` tokens for `/mcp`; an
operator who pins `/sse` mints with `capability:mint --transport sse`.

## Transport matrix

| Endpoint | Token in | Rule |
|---|---|---|
| `POST /internal/tools/{name}:invoke` | `Authorization: Bearer` only | A `?cap=` gets 401, even beside a valid header |
| `/mcp`, `/api/*`, `/config-test`, `/health` | `Authorization: Bearer` | The row must allow `http`. The toolbox still reads `?cap=` here until the retirement rule below applies |
| `/sse` + `/messages` | `?cap=` permitted | The row must allow `sse`. Only for clients that cannot send headers |

**Revocation on SSE.** Every `/messages` POST is verified again, so a revoked token cannot send
another tool call. Only the open `/sse` GET stream survives: a running call completes, and
notifications on that stream continue until the client reconnects. `/mcp` verifies every request.

**The query path is not leak-free.** The toolbox redacts `cap` in its own log lines (a test covers
every guard error path). A reverse-proxy access log also records a query string; that proxy and its
redaction come with E2. Until E2 ships them, do not describe `?cap=` as safe from logs.

**Retirement rule.** Remove the query path when no supported client needs it. Clients tested in E1:

- claude, codex and pi: their `/mcp` config gets the header (unit tests of each `WorkspaceAdapter`
  and of the pi bridge). They were not run live against a toolbox in E1.
- SSE: the MCP SDK `SSEClientTransport` (`src/agento/toolbox/tests/sse-transport-auth.test.js`), which
  cannot send headers on `/messages`. It is the reason the query path stays.

## Enablement and registration

Every call reads the tool's `is_enabled` chain again, so a disable takes effect on the next call. A tool
that is **enabled** after an MCP session opened is not in that session's registration: it becomes
callable in the next session. At invoke the registry is built per request, so it is callable on the
next call. Building it (`register()` of every module, measured in-process with stub clients) costs
about 0.5 ms per call, so there is no cache. The two config queries per call come in addition.

## Requirements for E2 (no code in E1)

- The proxy strips every caller-supplied identity header before it forwards a request.
- The authorization endpoint authenticates the proxy (a shared internal credential, or a listener
  only the proxy can reach). A test shows that a direct call from the `sandbox` container is denied.
- The proxy redacts `cap` in its access log, with an acceptance test.

## Deployment restriction

Agents share a UID and the workspace mount, so an agent can read another run's directory, including
its capability. Until a runtime-isolation task closes that, expose the panel only where every user is
trusted with every agent_view it can reach. RBAC controls the API surface, not what a process can
read from a shared mount.
