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
2. Supply the source checker once, at toolbox startup:
   `installAuthSources(createSourceLookup([["session", check]]))`. `check(sourceId,
   {capability_kind})` returns the live source record `{kind, id, user_id, workspace_id,
   agent_view_id, permitted_tools, created_at, expires_at}` (a launch also has `launch_id`,
   `artifact_code`, `version_id`), or `null` when the source is revoked.
3. The verifier checks the capability **and** its source on every call. E1 ships no checker,
   so every `user_session` and `miniapp` row is refused until one is installed.
