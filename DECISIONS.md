# Decision Log

Architectural and technical decisions — *why*, not *what*. For implementation details see [docs/](docs/) and code.

---

## 2026-09-25 — One tracked RULES.md with permanent rule IDs

- **Problem: three roles read three rule sets.** The implementer read AGENTS.md, the reviewer read an
  untracked `agento-code-review/RULES.md` (different in each clone), and the triager read CLAUDE.md and
  docs. About half of review findings cited no rule, and 3.3% cited AGENTS.md. Loop rounds per task went
  from 5.25 (June) to 11.7 (August); findings that came back in a later round went from 31% to 49%.
- **Fix: one tracked `RULES.md` at the repo root.** Every role reads it. AGENTS.md keeps facts (how the
  system works) and points to it; the skills point to it and keep no copy. Each rule has a permanent ID
  and a tier (P0 security, P1 architecture/correctness, P2 hygiene). Findings, triage, and comments cite
  the ID, never a line number, so a citation survives an edit.
- **Two review principles close the churn loop.** "Judge the change, not the repo": an old violation
  is reported once as `DEBT` and does not block. "Decisions are binding": a dated entry in this file is
  approved design, so a finding against it needs new evidence.
- **Tiers do not gate the verdict.** A "P2 does not block" gate was replayed on 44 past rounds: it saved
  2, and one of those would have approved a round one step before the reviewer found a P0.
- **New plan rules:** `EVT-1` (a plan names the event for each state change, or says why none) and
  `PLC-1` (placement questions: framework, core module, or user module). `PLN-1` (each runtime fact in a
  plan has proof, or is an `ASSUMPTION` with a spike step) had the largest effect in the replay.
- **Owner approval:** Marcin Klauza, 2026-09-25 — approved applying the drafted rules and loop changes
  ("Apply all"), on main first, with `RULES.md` at the repo root.

## 2026-09-25 — E2 panel, sessions, launches and RBAC

Contract deviations (PRD E2 / E1.5 against the code, built as below):

- **The `session` checker receives the capability's scope.** A session is not scoped to a view, the
  capability row is. The verifier calls `check(sourceId, {capability_kind, workspace_id,
  agent_view_id, query})`, and the checker computes the role's permitted tools for that scope. It
  does not echo a stored list.
- **Visibility is per role, not per user.** With two roles the PRD's `role → grants` model gives
  per-role visibility. Per-user grants are a follow-up.
- **The exchange code is a POST form field, not the `code` query parameter.** A one-time code in a
  URL reaches history, a `Referer` and logs, and a prefetch or a scanner can consume it. The panel
  submits a form to `https://apps…/launch`; there is no GET route. The `code` log redaction stays.
- **No manifest seam in E2.** `launch.manifest_fingerprint` and `allowed_actions` are NOT NULL; E2
  writes `sha256("")` and `[]`. An event would need `bootstrap()` in `web`, which would resolve every
  module's config, and a veto observer that fails to load would fail open. E6 designs the seam.
- **`current` resolves through the toolbox.** `POST /api/launches` calls
  `versioned_artifact_get_current` with a `user_session` capability instead of mounting the store
  into `web`. So a launch needs that tool grant **and** the `artifact.launch` operation grant in the
  scope, and the tool must be enabled there.

Choices:

- **The redeem needs no proxy secret.** The exchange code (30 s, once, hashed) is the credential; the
  redeem also requires `Origin` = panel and a form body.
- **Panel cookie `SameSite=Strict`, launch cookie `SameSite=Lax`.** The launch cookie must survive the
  top-level navigation after the cross-origin form post.
- **The CSRF token is HMAC-SHA256(session token, `agento-csrf`).** Nothing to store, and a page that
  cannot read the HttpOnly cookie cannot compute it.
- **One atomic `UPDATE … JOIN user` redeems.** `rowcount == 1` wins; concurrent redeems give one token.
- **Deny responses clear dead launch cookies.** Caddy returns the `forward_auth` deny response,
  `Set-Cookie` included, to the client (measured in the Task 0 spike).
- **Launch eviction orders by `created_at`, which has 1 s precision.** Ties break by id. A sequence
  column is a follow-up if exact order ever matters.
- **Launch ids are 32 hex characters**, so each launch cookie name is fixed-length and a bad name is
  refused before any DB read.
- **Login throttle is in-process** (10 failures per username per 15 min). A DB-backed throttle when
  `web` runs more than one replica.
- **`config_write.write_config` vs `save_config`.** `write_config` is the shared commit-and-dispatch
  step (`config:set`, admin TUI); `save_config` adds validation and the not-a-secret proof that the
  panel needs, because `web` holds no encryption key. The admin TUI skips value validation because
  tool gate keys have no schema.
- **Every access write locks its `user` rows in one ordered `SELECT … FOR UPDATE`**, and
  `create_launch` locks the user row and re-checks the grant in the same transaction, so a launch
  racing a role or grant change is either refused or revoked with it.
- **Operator seeding for launches** (Task 0): `artifact:init --source` runs on the host, and the view
  needs `versioned_artifacts/allowed_artifacts` plus `tool:enable` for `versioned_artifact` and
  `versioned_artifact_get_current`.

---

## 2026-09-25 — E1.5 platform foundation: Caddy proxy, secret-authenticated subrequests, string version ids

- **Caddy, not nginx.** `forward_auth` is the `auth_request` subrequest, `tls internal` terminates
  TLS with no certificate step, and the log `filter` encoder redacts query values. nginx needs a
  `map` per redacted parameter and has no internal CA.
- **The subrequest carries a secret, not a network position.** `web` shares `agento-net` with
  `sandbox`, so being reachable proves nothing. The proxy's entrypoint writes a random secret into the
  `proxy-internal` volume, which only `proxy` and `web` mount; `web` reads it per request, so start
  order does not matter. The proxy sets it only inside `forward_auth`, never on a forwarded request.
- **The `X-Agento-*` header namespace is the proxy's.** The proxy strips every client header in it,
  so any header the proxy adds later is protected by the same one rule.
- **The error log is redacted too.** Caddy's `http.log.error` logger writes the raw URI when an
  upstream fails; a global logger with the same `cap`/`code` filter covers it. Measured, not assumed.
- **The artifacts host port is removed with nothing in its place.** Previews and Basic-auth shares
  go dark until E2/E6 implement the decisions behind `/internal/authz/{app,share}`. A second,
  unauthenticated path to the same files would make that authorization worthless.
- **`app_version_id` is `VARCHAR(64)`** (`042`). A VA version id is a string; `036`/`037` declared
  `BIGINT`. The verifier checks a bounded string, not the VA grammar — that belongs where the id is
  made. The shared fixture is held to the grammar by the Node suite.
- **`role_grant` has no exactly-one-scope CHECK.** MySQL 8.0 refuses a CHECK on a column with an FK
  referential action (ER 3823), and the cascade is what removes grants with their agent_view. Writers
  set exactly one scope; readers treat both or neither as no grant.
- **The fixture's table rows are data only.** Nothing maps a `user`/`launch` row into an auth source
  yet (that is E2), so no mapper ships; the integration test inserts the rows against the real schema.

---

## 2026-09-24 — E1 toolbox auth: one dispatcher, per-call checks, TTL config in `core/auth/*`

- **Every tool call goes through one `executeTool()`.** The MCP SDK validates arguments before a
  per-tool handler runs, so an invalid call would never reach our code and go unaudited. We replace
  the SDK's `tools/call` handler with the dispatcher; the SDK keeps `tools/list`.
- **The audit row is written first.** A failed insert answers `unavailable` and runs nothing. The row
  holds a SHA-256 of the arguments, never their values.
- **Single use is one `UPDATE … WHERE consumed_at IS NULL`.** The row count decides the winner; no
  lock and no second store of bearers.
- **Strict argument validation** (`z.object(...).strict()`): an unknown key is `invalid_arguments`.
  This is stricter than the SDK default, which drops unknown keys without an error.
- **TTL bounds live at `core/auth/*`, not `auth/*`** (the PRD's path). A config path starts with its
  module, and the framework's module is `core`. Hard ceilings are in code; config can only narrow.
- **`/mcp` gets the token in a header; `/sse` keeps `?cap=`.** An SSE client sends no headers, so the
  query path stays for it, with a 4 h ceiling for any token that may travel on `sse`. New MCP tokens
  are `["http"]`; the migration backfill gives legacy rows both transports on the same 4 h clock.
- **Source checkers come from modules.** A module exports `authSources`; the toolbox collects them at
  startup and drops a kind that two modules claim. E1 ships none, so user kinds fail closed.
- **`on_behalf_of` is always null** until something verifies delegation; a non-null value is refused.
- **Invoke builds the tool registry per request.** It costs about 0.5 ms, so there is no cache.
- **Enabling a tool takes effect in the next MCP session**, not in the open one. Disabling takes
  effect on the next call. Widening an open session would run `register()` for tools the scope does
  not grant.
- **Thrown tool errors reach the caller as a fixed `tool failed`.** A thrown message can quote an
  upstream response body; the log gets only the error class. An `isError` result is the tool's own
  answer: MCP passes it to the agent as before E1 (the agent needs "issue not found" to recover),
  and invoke drops it, so a browser or miniapp caller gets only the error code.

## 2026-09-24 — E0 contracts for panel, toolbox and miniapps

E0 fixes the shared semantics so E1–E7 can start without re-deciding them. The detailed PRDs are
intentionally **not** in this repo (owner decision) — they live beside it, outside version control.
Each decision below stands on its own.

### Identity and capability

- **`kind` is kept and `actor` is added beside it, not in place of it.** Existing `toolbox_capability`
  rows migrate without a rewrite. `kind` describes a token's *purpose*; it never stands in for who the
  caller is.
- **Per-kind actor invariants, never a default.** A blanket `actor = agent` would label the platform's
  own viewless `internal_rest` config-test capability an agent. Each legacy kind gets one fail-closed
  row of required and must-be-null fields; a row whose claims cannot be derived fails verification. A
  permissive default on an identity field is exactly the failure the field was added to prevent.
- **Legacy `internal_rest` maps to one reserved subject constant** (`service:legacy-internal-rest`),
  not a per-row guess and not a rejection. `035_toolbox_capability.sql` stores no component identity,
  so the row cannot say which service issued it; rejecting breaks live callers and inventing a subject
  is fabrication. The constant is allowed a fixed endpoint set, no delegation and no app scope, and it
  retires by TTL expiry — no drain step.
- **The auth context is verified once and carried in one structure, not re-derived per transport.**
  MCP and HTTP must not drift on what a caller is allowed to be.

### Transport

- **`?cap=` stays as an MCP-client compatibility path while `Authorization` becomes the runtime path.**
  The SSE transport hands the client a bare URL which the client posts to verbatim with no headers, so
  a header-only `/messages` would 401 every legitimate SSE client. Removing the query path first breaks
  clients that cannot set headers.
- **Transport is a capability claim, not a property of the request.** A bearer moves freely between a
  header and a query string, so "query tokens get a shorter TTL" is unenforceable as a request
  property. Tokens carry an explicit `allowed_transports`, checked per endpoint, with no default —
  missing or empty fails verification once the migration backfill has run.

### Miniapps, artifacts and origins

- **Miniapps is a separate module over Versioned Artifacts, not a VA feature.** VA stays free of user
  and RBAC concepts: it owns files, drafts, versions and the `current` pointer; Miniapps owns the
  manifest, user access, launch and actions.
- **Miniapp files are authorized by a reverse-proxy subrequest to the Web API**, rather than by moving
  file serving into Python. That keeps the artifacts service credential-free and keeps a single
  authorization source. The apps origin serves **only** immutable `/v/<id>/` paths — `current` is resolved once, by
  authenticated launch creation on the panel origin, and pinned on the launch. Keeping a `current` route on the apps
  origin would break launch pinning and cannot be repaired by a proxy rewrite: concurrent launches each set their own
  `Path=/` cookie, so the credential is ambiguous, and it is opaque to the proxy anyway, so the pinned version is not
  knowable before the authorization subrequest that validates it.
- **The artifacts service loses its published host port when E6 activates.** An authorization layer
  with a second, unauthorized path to the same bytes authorizes nothing.
- **One apps origin shared by all miniapps, separate from the panel origin.** Owner decision, waiving
  PRD E0 §6 requirement 5 (app↔app isolation). The panel/apps split is what stops agent-generated code
  from **reading** panel data — responses, DOM, session cookie — and is not negotiable. It is not write
  protection on its own: sibling subdomains are same-site, so `SameSite` does not stop an apps page
  causing a credentialed panel request. Writes are blocked by separate CSRF controls, which are part
  of the same decision, not an optional extra. App↔app isolation is traded for one DNS name
  and one certificate, and is only valid while every artifact reachable from one session is one that
  user could open anyway. Upgrade path (per-artifact origins) is in ROADMAP.md.
- **Artifact codes are path segments, not host labels.** The shared apps origin therefore needs no
  DNS-safe identifier, no host→artifact mapping and no sanitising step. The VA grammar
  (`toolbox/paths.js`) allows 64 characters and a trailing hyphen, both illegal in a DNS label — which
  is also what blocks the per-artifact-origin upgrade.
- **The Basic-auth share gets its own origin, mandatory for E6.** Browsers attach cached Basic
  credentials automatically per origin, so sharing an origin with agent-generated scripts would put
  those credentials inside the miniapp trust domain. This is a credential boundary, and it is not
  covered by the two-origin decision above. The share origin grants no CORS permission to the panel or
  apps origins. Shipping without a separate share origin needs a new explicit owner waiver.
- **Shares need one origin *per share*, not one share origin.** Share content is agent-authored HTML
  and JavaScript served behind a single fixed Basic realm, and browsers replay cached Basic
  credentials per origin+realm — so one shared share-origin lets script in one share read another
  share's DOM and fetch its files with its credentials. The label is an opaque generated **share
  token**, not the artifact code, so unlike the apps-origin upgrade this is not blocked on the VA
  code grammar. The single-origin fallback is a separate, explicit waiver.

### Sequencing

- **The platform foundation is front-loaded into E1, as E1.5.** All Compose changes and the framework
  schema the contracts already pin to field level (`user`, `session`, `launch`, `role_grant`, the
  `toolbox_capability` columns) ship once, in one track, before E2/E3–E5/E6 fan out. The two things
  this removes were never real dependencies — a generated Compose file and a single global migration
  sequence are *collision hazards*, not dependencies, and serializing four tracks behind them costs more than doing the
  work once.
- **Front-loading stops at the edge of what is specified.** The conversation model and the miniapp
  manifest are deliberately undefined (PRD E2 §6 lists what its API "must not foreclose"), so E1.5 does
  not create their tables. Designing schema for an epic that does not exist buys a migration when the
  epic disagrees. Those tables live in their owning **module**, whose migrations are numbered per
  module — which is why two epics can both add `001` and never collide.
- **E7 stays last.** It needs E2's RBAC *enforcement*, not merely its tables, so no amount of
  front-loading parallelizes it.

### Documentation

- **Contracts describe the post-#42 system, with pending items marked.** E0 must unblock E1–E7, all of
  which land after PR #42.
- **Historical records are superseded in place, never rewritten.** A decision log that edits its own
  past stops being evidence. Corrections are appended with a `Superseded by` line.

---

## 2026-09-17 — `blocked` verdict category, halts to `FAILED` (AG-55)

- **Problem: a deterministic config fault was retried 3× and dead-lettered as an agent failure.** A
  domain observer vetoes a superficially-successful job with `Verdict(retryable=True)` when a business
  result is missing. When the *root cause* is infrastructure — a missing MCP tool credential — the world
  state does not change between attempts, so the full LLM run repeats identically three times (~0.49 USD
  and ~2.5 min of worker per job, guaranteed to fail) before dead-lettering with a message that blames
  the agent. The observer only sees "no business result", not *why*, so `retryable` cannot express this.
- **Fix: a distinct verdict category, not a fourth `retryable` meaning.** `Verdict` gains
  `blocked: bool = False`. It is orthogonal to `retryable` and **overrides** it: `retry_policy.evaluate`
  short-circuits a blocked verdict to `should_retry=False` on the first attempt regardless of
  `retryable` or the error class. Keeping `retryable` a pure "would another identical attempt help?"
  boolean and adding `blocked` for "the deployment is broken" avoids overloading one flag with two axes.
- **A blocked job goes to `FAILED`, not `DEAD` — reusing the dead enum value, not adding one.** `DEAD`
  means "the agent exhausted its retries"; reusing it would keep blaming the agent. The `job.status` enum
  already carries a `FAILED` member that the current consumer never writes (retries go to `TODO`, terminal
  agent failures go to `DEAD`), so rather than add a *new* `BLOCKED` status — and leave `FAILED` as dead
  code — the consumer repurposes `FAILED` as "halted by a blocked verdict" and dispatches
  `job_blocked_after` instead of `job_dead_after`. No migration and no schema change are needed; `DEAD`
  stays reserved for agent exhaustion, so `job:list --status FAILED` isolates config faults from it.
  `app_monitor` grows a `JobBlockedAlertObserver` that emails ops an alert framed as a
  configuration/infrastructure fault — the "alert instead of dead-letter" the report asked for. `FAILED`
  is terminal, so it is excluded from the active-job dedupe set and the dequeue query for free.
  (Reviewer AG-55: prefer reusing the unused `FAILED` value over introducing a parallel `BLOCKED` status.)
- **The framework defines the mechanism; the domain observer opts in.** The observer that knows the veto
  cause is a missing credential (it lives per-deployment in `app/code/`, out of this repo) sets
  `blocked=True`. The framework only honors the contract — no `if provider == …` coupling, no framework
  knowledge of any specific tool.

---

## 2026-09-12 — VersionedArtifacts: the `artifacts` container ships unconditionally, and a disabled module answers 503

- **Decision:** the serving container is written into every rendered `docker-compose.yml` with no
  per-module condition, and `app/etc/modules.json` is bind-mounted read-only so the server itself
  refuses to serve when `versioned_artifacts` is disabled.
- **Why unconditional:** `regenerate_compose` has no per-module service mechanism — `render_compose`
  (`framework/cli/_provisioning.py`) is placeholder substitution, and "modules declare compose services"
  is a framework feature this ticket is not buying. The precedent is the existing unconditional
  `storage/versioned-artifacts` bind mount on the toolbox.
- **Why the gate exists anyway:** without it, `mo:di versioned_artifacts` removes the tools and leaves
  every previously published version still answering on loopback, which breaks CLAUDE.md's "every module
  must be safely disableable". An earlier wording of this deviation claimed a disabled deployment serves
  "an empty tree" — that is only true before anything is published.
- **The gate mirrors module enablement, not the `is_enabled` tool gate.** Absent file, absent key or
  unparseable file all mean SERVE, because `app/etc/modules.json` lists only explicitly toggled modules,
  so absence is "enabled". The `is_enabled` gate is the one that fails closed; do not conflate them.
- **Severity, stated honestly:** the port is loopback-only, so the content was reachable only by someone
  who already has a shell on the host and could read the published tree directly. The gate buys correct
  disablement semantics and an operator expectation that holds, not a new privilege boundary.
- **No `networks:`, no `env_file:`, no `environment:` — deliberate, and a comment above the service says
  so in both compose files.** One `networks:` line added for consistency puts every artifact on
  `agento-net`, where every agent in every agent_view can read every artifact over plain HTTP with
  `allowed_artifacts` bypassed for reads, silently and with no audit row.

## 2026-09-12 — VersionedArtifacts: the serving container is `node:http`, not Express

- **Decision:** `server/artifacts-server.js` uses `node:http` plus a small extension→MIME map instead of
  `express.static`, although `express` is already a toolbox dependency.
- **Why:** `express` does not resolve under vitest — vite maps the bare specifier to a phantom path at
  the vitest root and the package's own `require('./lib/express')` then fails (`Cannot find module
  './lib/express'`). An Express version of this file could carry no test at all, and the plan's test
  list — symlink refused, dotfile refused, 503 gate, `EINVAL` retry — *is* the security story.
- **Alternative rejected — a test-only `resolve.alias` in a new `vitest.config.js`.** It works
  (measured), but it changes module resolution for the whole suite to paper over one quirk, and it would
  leave the tested path different from the production path.
- **What Express would have bought is small:** the containment check is ours either way — `send` does
  zero `lstat`/`realpath` and served a file through a symlink pointing outside the root. What is
  genuinely absent is `Range`, `ETag` and conditional requests; acceptable for an artifact preview, and
  recorded here rather than discovered later.
- **Consequence:** the server has no npm dependency at all, so `/app/modules/core` is a consistency
  choice, not a module-resolution requirement.

## 2026-09-06 — VersionedArtifacts: Git as the storage engine, invoked only from the toolbox

- **Decision:** back versioned artifacts with the real `git` binary, added to the toolbox image, over a
  dedicated `storage/versioned-artifacts` volume.
- **Alternative rejected — `isomorphic-git`.** It has no linked-worktree API, and a draft in PRD §6 *is*
  a linked worktree: several editable checkouts of one repository, isolated from each other. Emulating
  that with copies would lose the atomic ref operations the whole design rests on.
- **Why it is safe:** the agent never receives a generic `git(command)` for this store (PRD §32), and the
  store is not mounted in its container at all. Every invocation is built by the module with fixed
  `-c core.hooksPath=/dev/null -c core.symlinks=false` flags and a scrubbed environment (no `HOME`, no
  global/system config, no credential helper, `GIT_ALLOW_PROTOCOL=none`), so repository content can
  never execute and no remote can ever be reached.

## 2026-09-06 — VersionedArtifacts: the store is toolbox-only

- **Decision:** mount `storage/versioned-artifacts` into the toolbox and nowhere else — not cron, not the
  sandbox.
- **Why:** the toolbox is the only container with secrets and the only one that validates requests. A
  mount in the sandbox would let the agent read and write versioned content directly, bypassing the
  `is_enabled` gate, the per-`agent_view` artifact allowlist, the size limits, the locking and the audit
  trail in one step.

## 2026-09-13 — VersionedArtifacts: the agent owns the whole lifecycle, scoped by a derived namespace

- **Supersedes** the creation half of the 2026-09-06 entry below: creation is no longer withheld from
  the agent. Artifacts are a collaboration mechanism between agents, and a lifecycle that needs an
  operator to bootstrap or finalize it is not one — an agent must be able to init, iterate, hand the
  code to a sub-agent, and publish, unattended.
- **Decision:** `versioned_artifact_init` is a tool, gated on its own `is_enabled` key like every
  other. An agent may create `av<agent_view_id>-*` — DERIVED from the session, never configured and
  never stored — plus anything `allowed_artifacts` grants it, capped by `limits/max_agent_artifacts`.
- **Why an `owner` marker beside the artifact and not the `owner` column.** The column decorates the
  store (a failed INSERT keeps the artifact), so it cannot carry authorization. The marker is written
  inside `init`'s own compensating `try`, so a half-written one takes the artifact with it, and an
  artifact with no marker is reachable only through `allowed_artifacts` — which is what makes the
  change need no migration. This replaced an `av<id>-` code PREFIX: the prefix carried ownership in
  the NAME, so the agent had to know and type it and every artifact wore an operator concern in its
  URL.
- **Why the cap counts `filter(mayUse)` and not the store.** A store-wide count answers "how many
  artifacts does every other agent_view hold" in at most `cap` calls, and — with delete reachable only
  by an operator — lets one view lock creation out for all of them until a human intervenes. One word of filtering removes both.
- **Alternative rejected — a `publish/agent_owned` switch.** `save_version` already materializes every
  saved version into the served tree; `publish` only moves `current`. A switch there would guard an
  open wall, block the loop this entry exists to enable, and be a second allow-list beside
  `is_enabled`, which CLAUDE.md forbids. The operator's opt-in is the `is_enabled` pair.
- **Known limit, deliberate:** `agent_view_id` is asserted by the caller (`?agent_view_id=` on the SSE
  URL), so the namespace SCOPES cooperating views rather than authorizing them. (Closed by AG-16: the
  view now comes from the session's capability row.) The fix is
  session-bound identity in the framework, not a module-local check — see ROADMAP.md.
- **Cost, accepted:** `artifact:delete` is the operator's alone — destroying an immutable history is
  not something a self-asserted identity may do — so for an agent the cap is still a one-way ratchet
  and a human is what resets it.

## 2026-09-06 — VersionedArtifacts: artifact creation is a host command, not an HTTP route

- **Decision:** `artifact:init` runs on the host and pipes a payload into
  `docker compose exec -T toolbox node …/cli.js`. It stays the operator's equivalent of the tool —
  it is the only path that can import a host directory as version 1.
- **Alternative rejected — an Express route on the toolbox.** The toolbox authenticates no caller, so
  every route it serves is reachable by the agent. A route would have meant handing the toolbox an
  arbitrary host path to read — which is also why the TOOL takes no `--source` (see the entry above).
- **Consequence:** the command must not be proxied into cron (which sees neither the host source nor the
  storage volume), but it is still a *module* command, so it lives in `_LOCAL_MODULE_COMMANDS` — that
  stops the proxy while leaving the module bootstrap that registers it with argparse intact.

## 2026-09-06 — VersionedArtifacts: no runtime stale-lock breaking

- **Decision:** a held lock is simply held. Abandoned locks are cleared by a sweep at toolbox startup,
  never by a waiter.
- **Why:** `mkdir` gives atomic *acquire*, not atomic *break*. Breaking is a read-then-delete pair, and a
  third process can acquire in the window between them — so a waiter that deletes an aged lock can
  destroy a lock someone else is actively holding. An ownership token makes *release* safe; it cannot
  make breaking safe.
- **Cost, accepted:** an abandoned lock returns `DRAFT_LOCKED` until the next toolbox start. A stuck
  draft is recoverable by an administrator; two interleaved mutations on one worktree are silent
  corruption. If this bites in practice the fix is a kernel-backed lock, not a shorter stale timeout.

## 2026-09-06 — VersionedArtifacts: one toolbox instance per `storage_root`

- **Decision:** a given store may be used by exactly one toolbox instance. Recorded in `system.json`
  field help, the developer doc, and here.
- **Why:** acquire is safe across processes, but the startup sweep is a check-then-delete pair with no
  concurrent acquirer *by assumption*. A second toolbox on the same volume can take a lock inside that
  window and have it deleted underneath a live mutation.
- **Alternative rejected — a longer stale timeout.** It narrows the window and looks like a fix; it does
  not close it. Supporting multiple instances needs a real distributed lock, which PRD §28 explicitly
  scopes out ("one storage/VPS").

## 2026-09-06 — VersionedArtifacts: a version carries no description

- **Decision:** `versioned_artifact_list_versions` returns `{version_id, revision}` only. The label passed
  to `save_version` is persisted in `versioned_artifact_audit.description`.
- **Superseded 2026-09-12:** the tool now returns `{version_id, revision, preview_path}` — a third field
  that says where the version is served, not a label. The decision above still stands for the *label*:
  no version carries a description of its own.
- **Why:** a version is a ref pointing at a commit and cannot hold a message of its own. Returning the
  draft's last change message under the name `description` would hand the caller a field that looks like
  the label they supplied and is not — the worse of the two failure modes. Inventing a field the PRD
  does not define is the other.
- **Forward path:** an annotated tag object per version, whose subject is a real version label.

## 2026-08-23 — Toolbox east-west auth: a DB-backed capability, not a shared secret or a network boundary

Closes the caller-authentication half of the N5-2 internal-caller gap for all four channels at once
(the co-tenant half — concurrent runs sharing the `agent` UID — stays OPEN) (`jira`, `outlook`, `bitbucket`,
`github`). Reference: [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md),
[docs/cli/capability.md](docs/cli/capability.md).

- **The claims move server-side.** `/mcp`, `/sse` and every `/api` route now require a capability token.
  The toolbox looks the token's SHA-256 hash up in `toolbox_capability` and takes `agent_view_id` and
  `job_id` from **that row**. A query string or a request body may still carry an `agent_view_id`, but it
  is only ever *compared* with the capability's scope — a mismatch is refused, never honoured. A caller
  therefore cannot select a scope it was not issued.
- **Why a DB capability and not a shared HMAC secret.** A signing key placed in the cron container would
  sit next to a shell-capable agent, so it is exfiltratable; and one key mints *every* scope, so a single
  leak grants the whole deployment. A capability is a random opaque string that only the DB can
  interpret, is minted per run for one view (and one job), expires, and can be revoked at any moment.
  Nothing reusable ever reaches the agent-adjacent container — which is also why this needs **no new
  `AGENTO_*` cron env var** (see [docs/architecture/cron-env-contract.md](docs/architecture/cron-env-contract.md)).
- **Why network segmentation alone was rejected.** Marking `agento-net` internal and exposing only MCP to
  the sandbox stops an *outsider*, not the agent. The agent must legitimately reach the MCP port, and over
  that same port it could open a session naming another view. Segmentation cannot authorize; it can only
  reduce who may ask. It stays a useful defence in depth, not the fix.
- **Three kinds, least privilege each.** `mcp_job` (a consumer-run job — never mintable by hand, revoked
  on the job's terminal transition), `mcp_interactive` (one `agento run` session, 12 h), `internal_rest`
  (Python publishers and channels, 120 s). Scoped `/health` diagnostics accept `internal_rest` only: an
  MCP kind lives inside the sandbox, and a diagnostic that reports backend reachability would be an
  infrastructure oracle for the agent.
- **`/mcp` and `/sse` revoke differently, deliberately.** Streamable HTTP re-verifies on every request, so
  a revocation is immediate. SSE verifies at connect, so a revocation lands at the next connect. Both are
  documented rather than papered over; job capabilities are additionally bound to the job's lifetime.
- **`/config-test` (added in v0.17) joins the guarded set, with a viewless exception.** A config test at
  the default scope has no view to mint against, so `toolbox_capability.agent_view_id` is nullable for
  an `internal_rest` row without a job — and only `/config-test` opts in (`allowViewless`). Every other
  guard still refuses a viewless row, because the lenient loaders read global config for a missing
  view. `run_id` (the interactive-run desk) stays a query parameter: it names a directory, not a scope.
- **The token is a credential.** It goes to stdout once, travels onward only by stdin or a mode-0600
  file, is replaced with `cap=***` in any agent output the framework persists, and is never written to
  argv, a log, or shell history.

---

## 2026-08-18 — Job dedupe by SELECT-then-INSERT, not `INSERT IGNORE` (AG-22)

- **`INSERT IGNORE` burns an auto_increment id on every rejected duplicate.** MySQL/InnoDB allocates the
  next `job.id` *before* the unique-key check on `idempotency_key`, so a duplicate publish that inserts
  nothing still advances the counter. High-churn sources (an idempotency key that rotates on every remote
  poll) therefore drove `job.id` up unbounded even though no row was written. The dedupe worked; the id
  space leaked.
- **Fix: `SELECT id ... WHERE idempotency_key = ?` first, then a plain `INSERT` only when absent.** No
  probe row is inserted, so the counter stays flat for duplicates. Applied in both publish paths —
  `framework/publisher.py` (Python) and `modules/core/toolbox/schedule.js` (the `schedule_followup`
  tool). The `uq_jobs_idempotency` unique key is kept as the source of truth.
- **The unique key still guards the race.** SELECT-then-INSERT is not atomic: two publishers can both
  pass the SELECT. Both paths catch the resulting duplicate-key error and report a duplicate, never an
  error — the Python path on `pymysql.err.IntegrityError` (rolls back, returns `False`); the JS path on
  `ER_DUP_ENTRY` (errno 1062), returning the same "duplicate prevented" message as a SELECT hit.
  Correctness still rests on the unique constraint; the SELECT only spares the common case its wasted id.

---

## 2026-08-14 — GitHub PR-review channel (`src/agento/modules/github/`)

A port of the Bitbucket channel to GitHub: same two lanes, same zero-trust token boundary, no framework
changes, no schema migrations. Details: [docs/modules/github.md](docs/modules/github.md).

- **Standalone module, no shared "forge" abstraction with `bitbucket`.** The two providers' comment,
  review and resolution models diverge materially (three comment surfaces vs one; GraphQL-only thread
  resolution; an append-only review history instead of an event log; no server-side author filter), so a
  common base class would be an abstraction over the parts that differ. Per CLAUDE.md principle 1, three
  similar lines beat a premature abstraction. `bitbucket` is not touched.
- **Bearer auth, no email.** GitHub authenticates with `Authorization: Bearer <PAT>`; unlike Bitbucket's
  Basic `base64(email:token)` there is no account email in the auth path at all.
- **A failed call reports GitHub's `message`, not the bare status** (2026-08-15, reversing this module's
  original drain-and-discard). The first live run proved the cost of hiding it: `github_create_pr` was
  refused because the PAT could not write, the agent was told only `create failed: HTTP 404`, and it
  concluded — reasonably and wrongly — that the repo was off the allow-list, because "no such repo",
  "your token may read but not write" and "the org has not approved this token" are the SAME 404 with
  different messages. The boundary is unchanged, only narrowed to what it was protecting: `describeHttpError`
  reads `message` + up to three `errors[]` `resource/field/code` triples and NOTHING else (a non-JSON
  body is an error page — a rate-limit or proxy HTML — and is dropped), caps the text at 300 chars, and
  redacts the configured token from it first, so relaying the provider's words cannot re-open the
  credential boundary that drain-and-discard held structurally. Redaction ignores secrets under 8 chars:
  a one-character "token" is a substring of ordinary words, and rewriting them destroys the diagnostic.
  `bitbucket` keeps the old behaviour — the same change there would alter a shipped channel's error
  contract and belongs to its own change, not to this port.
- **The `repo` ARGUMENT is normalized; the `repo_allowlist` is not** (2026-08-15, reversing "both
  spellings are DIAGNOSED, never accepted" — GitHub *and* Bitbucket, on the same live-run evidence). The
  refusal was one half of the same incident: the agent wrote `repo: "openagento/agento"`, was told only
  that it was "not allowed", rewrote it, and read the resulting 404 as proof that the allow-list was
  wrong. Two changes, both on the tool side. (1) The format rule now lives in the **argument's own
  schema description** ("Bare repository name WITHOUT the owner prefix, e.g. `agento` not
  `openagento/agento`"), because `github_create_pr`'s prose already said "the owner is fixed by
  configuration" and that is a sentence about policy, not about format — a model that has used `gh`
  writes `owner/repo` regardless. (2) A prefix that **equals** the configured owner/workspace is
  stripped and the call proceeds; any other prefix is refused *by name* (`the owner is fixed to "acme"`).
  This widens nothing: the request URL is built from the configured owner either way, so normalizing the
  argument cannot redirect a call, and what remains after the strip must still be an **exact** allow-list
  entry — normalizing *config* entries is the widening that stays forbidden, and an owner-prefixed
  `repo_allowlist` is still only diagnosed in the error text. Bitbucket's `source_repository` is
  untouched: `workspace/repo` is its documented form, not a mistake to absorb.
- **Client-side author filter, `poll_top` applied after it.** `GET /repos/{o}/{r}/pulls` has no author
  filter, so agent-authored PRs are selected in the toolbox and the cap counts *matching* PRs. Capping
  before the filter would let a repo full of third-party PRs silently starve the agent's own.
- **Three comment surfaces merged into one ordering** (issue comments, inline review comments, review
  bodies). The watermark is therefore cross-surface: an agent reply on any surface silences the others.
- **The watermark comparison is `>=`, not `>`.** GitHub timestamps are second-precision and nothing
  orders a comment against a commit (or against a reply on another surface) inside one second, so a tie
  cannot be broken — only given a direction. `>=` costs at most one unnecessary job, when the real order
  was "feedback, then answer" and nothing had been published yet; later scans dedupe on the unchanged
  idempotency key. `>` instead loses a real "answer, then follow-up" permanently and silently. Bitbucket
  keeps `>` because its `created_on` carries microsecond precision, where the tie is not the ordinary
  case.
- **Thread resolution is GraphQL-only, and its failure means "unknown", not "unresolved".** REST exposes
  neither thread ids nor resolved state. If the GraphQL call fails or its 100-thread/100-comment bound is
  hit, that PR's comments lane is skipped for the poll. Degrading to "unresolved" would re-raise feedback
  the agent already resolved, every poll, forever.
- **`github_set_review` has no `none`.** GitHub cannot retract your own review; the API takes
  `APPROVE | REQUEST_CHANGES | COMMENT`. `comment` was added in place of the dropped `none`.
- **Rate limits: 403 *or* 429, and never retry earlier than instructed.** A 403 counts as a rate limit
  only when it carries `retry-after` or `x-ratelimit-remaining: 0`. If the instructed wait exceeds the
  15s cap the toolbox gives up the poll rather than shortening the wait — the cap exists because a hold
  longer than the publisher's 60s HTTP timeout buys nothing, and an integration test asserts the two
  numbers stay compatible across the language boundary.
- **The changes lane uses each reviewer's current position, not "any CHANGES_REQUESTED ever".**
  `GET /pulls/{n}/reviews` is a full history that keeps a superseded review forever, so the history is
  folded per reviewer (ignoring `COMMENTED`/`PENDING`) and only reviewers currently at
  `CHANGES_REQUESTED` count. Otherwise an approved PR would be re-queued indefinitely.
- **Entries without a login are dropped at the toolbox.** GitHub returns `user: null` for deleted/ghost
  accounts; such a comment must never become a `RequesterTrust.ACCOUNT` requester key.
- **Truncation blocks publishing, it does not just get logged.** Every bounded scan records itself on the
  PR record, and the publisher skips a lane whose decision depends on a truncated scan. The force-push
  watermark reads the PR's **head commit** directly instead of paging its commit list — one request that
  is always correct — and an absent/unfetchable/undated head commit is itself a truncation.
- **The API host is fixed (`api.github.com`); GitHub Enterprise Server is out of scope.** A
  caller-supplied API base on `/verify` would turn the toolbox — the only container holding secrets,
  reachable from the agent's Docker network — into an SSRF probe against internal hosts.
- **Scope enforcement is declarative, not prose.** `github_token`, `github_login` and `repo_allowlist`
  set `showInDefault: false` / `showInWorkspace: false`, so `config:set` refuses them outside agent_view
  scope (`framework/config_schema.py:is_scope_allowed`). The token therefore cannot reach DEFAULT, where
  `bootstrap()` would decrypt it in the cron process. The remaining three paths are deliberately
  unrestricted: `github_owner` may legitimately be deployment-wide, and `enabled`/`poll_top` are
  operational switches.
- **Two-sided ENV guard.** `showIn*` binds the DB write path only; `CONFIG__GITHUB__*` still outranks the
  DB in `resolve_field`. `src/env_guard.py` and `toolbox/env-guard.js` therefore refuse the same three
  keys on all four surfaces (publisher, REST handlers, MCP registration, onboarding completeness), and an
  integration test asserts the two key lists cannot drift apart. A one-sided guard would be worthless:
  the toolbox's own `config-loader.js` accepts the same override.

**Two framework follow-ups this port deliberately did not make** (actionable by someone else):

- **No per-field `toolbox_only` in the config resolver.** A field only the toolbox should ever resolve is
  still resolved by `bootstrap()` from ENV (`framework/config_resolver.py:209`) for every enabled module,
  before any module code — including this module's guard — can run; the only remedy available to a module
  is to refuse to operate. **Owner sign-off 2026-08-13:** this port ships with that residual accepted,
  because it is a misconfiguration path, not a deployment one — credentials are stored encrypted in
  `core_config_data` at AGENT_VIEW scope (as `bitbucket` and `outlook` do), `load_db_overrides` reads
  DEFAULT only, and nothing in this module asks for a `CONFIG__*` credential override. Setting one is the
  same act that would leak `jira`'s, `outlook`'s or `bitbucket`'s credential today with no guard at all;
  `github` is the only one of the four that detects it and refuses. See ROADMAP.
- **N5-2, internal-caller auth — CLOSED 2026-08-23.** This port shipped at parity with the residual
  accepted (owner sign-off 2026-08-13). The framework fix landed later and applies to all four modules
  at once: every MCP session and every `/api` route needs a capability token, and `agent_view_id` comes
  from the capability row rather than from a query string or a body. See the 2026-08-23 entry above.

---

## 2026-08-04 — Refresh lease + quarantine provenance: one job at a time may rotate a single-use credential

- **The bug.** On a 10-worker deployment (`AGENTO_CONSUMER_MAX_WORKERS=10`) the Claude OAuth
  row `id=1` was auto-quarantined with `401 Invalid authentication credentials` four times
  inside log retention, until both OAuth rows were `status='error'` and **all** production
  traffic silently ran on the paid `anthropic_api_key` — a spend-cap risk, not a cosmetic
  one. `select_credential` commits inside itself, so its `FOR UPDATE` row lock dies the moment
  selection returns: ten workers were handed the same row, each materialized the same
  single-use refresh token, and the first job to rotate it invalidated the copy the other
  nine were about to replay. Per-run HOME isolation (2026-05) isolates *files*; it never
  isolated the shared rotating *credential*.
- **The 401 moved from `AUTH_ERROR_PHRASES` to the existing `TransientAuthError` rule — a
  one-line deletion, not a new mechanism.** `_classify_error` is ordered: rule 1 (known
  permanent phrases) is checked before rule 3 (`_is_transient_credential_rejection` =
  credential word + `\b401\b`). Rule 3 already matched the incident's message; listing the
  phrase in rule 1 **shadowed** it and poisoned a healthy subscription instead. Removing the
  phrase routes it onto the throttle path that already existed — `throttled_until`,
  `status='ok'`, `retry_with_other_token`, `credential_auth_throttled_after` — whose docstring
  already named this cause ("usually a stale access-token copy from a concurrent refresh").
  A replayed single-use secret is transient by construction.
- **The credential-aware half of that decision lives in the consumer, because the parser cannot
  make it.** A parser sees only a message, so a genuinely dead **non-rotating** credential (a
  revoked API key) emitting the same phrase would now be throttled forever — trading one
  silent failure for another. `_handle_transient_auth` therefore branches on the
  framework-owned flat `refresh_token` field (never a provider-specific `type == 'oauth'`
  literal, principle #6): rotatable ⇒ throttle; nothing to rotate ⇒ delegate to
  `_handle_auth_failure` so it still reaches the visible fail-closed state. The three
  remaining phrases keep their one-strike poison semantics: a dead licence must surface.
- **A refresh lease makes a near-expiry rotating credential exclusive for the duration of one
  run.** `select_credential` excludes rows under a live lease for **every** caller, and takes
  the lease in the SAME statement that stamps `used_at` (so the returned `CredentialRecord`
  truthfully carries it, and the statement count stays 3). Only a credential judged
  *refresh-imminent* is made exclusive — a fresh credential is still shared by all ten workers,
  so the fix does not serialize the pool. Freshness comes from `credential_ttl_seconds(credential)`
  on the harness's `WorkspaceAdapter` (claude: `expiresAt` from the encrypted payload; codex:
  the `exp` claim of its own access-token JWT). It is **declared on the protocol**, not
  `getattr`-probed, for the same reason `capture_refreshed_credentials` was (D13 below): a
  silently-missing hook is a silently-skipped freshness check. That is safe because `bootstrap`
  isinstance-gates the `AgentHarnessAdapter` only, and `runtime_checkable` `isinstance` is a
  presence check over that class's own attributes — it never recurses into `workspace_adapter`,
  so adding a member here cannot unregister a harness.
- **`leased_until` is a renewed LIVENESS deadline, not a duration estimate.** Nothing bounds a
  job's wall clock — `job_timeout_seconds` bounds each CLI *subprocess*, and stale-job
  recovery skips any live pid (a real 27-minute run succeeded) — so a clock-derived TTL would
  break exactly the long runs it was meant to protect. Instead the consumer renews the leases
  it holds from its own main-loop tick **and** from an uncapped shutdown drain (today's
  `executor.shutdown(wait=True)` is itself uncapped; a cap would re-break the lease for the
  jobs that outlive it). A lease therefore lives exactly as long as this process has a worker
  for that job — covering the pre-pid setup window and the post-exit capture window — and a
  dead consumer's leases expire within one TTL with **no reaper**. Renewal is deliberately not
  driven from `_recover_stale_jobs`: that runs once at startup and is pid-based, and
  `os.kill(pid, 0)` is false between subprocess exit and credential capture, so a pid-driven
  reaper could free a lease mid-rotation.
- **The lease stops reselection, not credential writes — so `register_credential` refuses to
  write through a live one** (`CredentialLeasedError`, surfaced by `credential:register` /
  `credential:refresh` after a rollback). Otherwise an operator's fresh credential would be
  overwritten minutes later by the leased job's own capture, which rotates from the chain it
  materialized *before* the refresh. The expiry comparison is done in SQL
  (`leased_until > UTC_TIMESTAMP()`), like every other lease comparison, so a skewed host clock
  can neither refuse a dead lease nor write through a live one. No CAS on the credential write:
  the two colliding writers are excluded structurally instead, and a CAS miss would *discard*
  the only live refresh token.
- **`error_source ENUM('auto','operator')` lets the framework clear only its own
  quarantines.** The 2026-06-15 rule — a rotation must not resurrect an operator's decision —
  still holds; an *automatic* quarantine simply is not operator state. So a successful run
  (not a rotation: a run can rotate and still 401) clears `error_source='auto'` and nothing
  else. `mark_credential_error`'s default is `'operator'`, fail-closed, so a caller that forgets
  to say `source="auto"` gets the stickier state. NULL provenance — every row quarantined before
  migration `034`, including production `id=1` — is treated as operator/unknown and never
  auto-cleared: the migration resurrects nothing, at the cost of one explicit `credential:reset`.
- **No new env var.** The freshness horizon derives from `job_timeout_seconds` (+900s slack);
  the entrypoint whitelist only forwards `AGENTO_*` knobs and an AST test guards every
  `from_env()` literal against it, so a derivable knob is pure maintenance cost.
- **Honest scope.** Replay is not made *impossible*: the horizon is a heuristic, so a run
  handed a credential judged fresh can still cross expiry mid-run. That residual path is why
  the reclassification matters — the consequence is a self-expiring throttle plus fail-over
  rather than a quarantine — and it is measured by the `rotated WITHOUT holding a refresh
  lease` ERROR log, which must stay empty in production. Closing it outright needs mid-run
  capture, or withholding the refresh token from non-holders; both are follow-ups. A proactive
  warm-up refresh (which would make the exclusive window nearly invisible) is gated behind a
  live-CLI experiment and is not part of this change.

---

## 2026-08-04 — One closed `AgentProvider` enum split into harness / provider / model

The refactor that made the "framework is agent-agnostic" rule (AGENTS.md #6, now RULES.md PLC-2) actually
true. Full contract: [docs/architecture/harness-contract.md](docs/architecture/harness-contract.md).

- **D1 — Three axes, not one.** `AgentProvider(CLAUDE|CODEX)` keyed **five** registries
  (runners, config writers, CLI invokers, auth strategies, transcript readers), so one enum
  value had to mean three unrelated things: the driving *program*, the model *vendor*, and
  implicitly the credential pool. Adding a third agent meant editing the enum — i.e.
  editing the framework, which the rule forbids. Split into **harness** (program) /
  **provider** (model-API vendor) / **model**. The axes are genuinely independent: one
  harness can offer several providers, only some of which need a credential.
- **D2 — One open registry replaces five enum-keyed maps.** `register_harness(descriptor,
  adapter, module, runtime_config_fields, config_schema)` from a single `agent_harnesses`
  entry in the module's `di.json`; one loader (`bootstrap._load_agent_harnesses`) replaces
  five. The last three arguments are positional and required: a default would let a missed
  call site register a silently broken config channel (an empty namespace resolves nothing,
  an empty allow-list disables it) that bypasses declaration validation altogether.
- **D2b — A command is argv *plus* stdin.** `CommandBuilder.stdin_payload` was added
  because a CLI may accept its prompt only on stdin: Pi's `-p` refuses a value starting
  with `-` (`dist/cli/args.js:109-116`) and Agento prompts come from Jira titles and mail
  subjects. It belongs to the builder, not the runner, because there are **two** spawn
  paths (the consumer's `SubprocessRunner` and `agento run` via `prepare_run.py`) and one
  definition of the invocation. `input=None` does NOT mean `DEVNULL` — it inherits the
  caller's stdin — so the no-payload branch keeps `stdin=DEVNULL` explicitly, and
  `input=<str>` requires `text=True` or it raises before the agent is spawned.
- **D2g — Raw MCP JSON Schema is used as Pi's tool parameter schema, unconverted.**
  Spike S1 against the live API: a `tools/list` `inputSchema` containing a nested object, a
  string `enum` and an optional field works directly as `ToolDefinition.parameters`, and the
  model calls the tool with correctly typed arguments. So there is no `Type.Object`
  conversion layer and no `StringEnum` shim — the plan's contingency for those is dead and
  deliberately not implemented.
- **D2h — Only `registerTool()` may be called while a Pi extension loads.**
  `core/extensions/loader.js:135-155` installs throwing stubs for every *action* method
  (`getAllTools`, `appendEntry`, `sendMessage`, `setModel`, `getCommands`, …) until
  `Runner.bindCore()` runs; the source comments that `registerTool()` is the exception. The
  bridge therefore does its whole handshake and registration in the factory (where a throw
  is usefully fatal) but touches no other API until a handler fires. Test doubles for the
  extension API MUST throw on action methods during load — one that implemented
  `getAllTools()` as a working function hid a bridge that could not load at all.
- **D2c — The Pi bridge has zero runtime dependencies.** Pi ships no MCP client, so
  `modules/pi/bridge/agento-toolbox.js` is one. It cannot import `@modelcontextprotocol/sdk`
  or `zod`: Node resolves bare imports by walking up from the importing file, and the
  per-job build directory it is loaded from has no `node_modules` above it (the globally
  installed Pi's modules are not on that path). Validation is therefore hand-written and
  total. `RULES.md` gained a scoped rule for this artifact class (TBX-5) rather than the code
  taking a silent exception to the Zod requirement.
- **D2d — Pi's `cost_reporting` is `false` and its `num_turns` is not comparable.** Pi
  prices from its own model catalogue, and a generated `models.json` (Ollama) carries no
  rates, so a cost figure would be fiction there (same choice as codex). ⚠ Qualified by a
  live spike: `usage.cost.total` **is** populated for OpenRouter, so the "no rates"
  reasoning covers Ollama only. Capabilities are declared per **harness**, not per
  provider, so `false` stays as the conservative value — but reporting cost for
  credentialed providers is a defensible follow-up, not a closed question. `num_turns` counts Pi
  `turn_end` events, which do not mean what claude's or codex's turns mean — compare Pi
  runs only to Pi runs.
- **D2e — Model identity is verified positively, never by absence of a warning.** Pi
  resolves an unmatched model by silent substring matching on id *or* name and returns the
  highest-sorting alias, emitting nothing (`dist/core/model-resolver.js:104-127`); its
  "not found" warning fires only when nothing matched at all. So the runner compares the
  `provider`/`model` on each assistant message (`docs/session-format.md:85-86`) against
  the request and fails the job on a mismatch, and the bridge does the same in-process on
  every spawn path, setting `process.exitCode = 1` when headless. A mismatch is a plain
  run error, never a credential verdict.
- **D2f — Toolbox MCP sessions expire on an idle TTL.** They used to be dropped only via
  `transport.onclose`, but the consumer SIGKILLs a timed-out agent, so no `DELETE` is ever
  sent and the entry survived until the container restarted. This affected **every**
  harness (`/mcp` serves codex too) and cannot be fixed client-side, because SIGKILL leaves
  no chance to clean up. `toolbox/mcp-sessions.js` adds a refresh-on-request TTL and an
  unref'd sweeper.
- **D3 — Descriptors are pure data parsed off disk.** Three callers must enumerate
  harnesses before any Python import or DB access: `config:set` validating a `select`
  value, `enumerate_sandbox_packages` at install/upgrade/doctor time, and
  `module:validate` inside `setup:upgrade` (which must fail *before* the first schema
  change). So static metadata lives in `di.json`, never in the adapter class.
- **D4 — `credential_required` is the single source of truth, validated bidirectionally.**
  `true` requires a `credential_scope` **and** non-empty `registration_modes`; `false`
  must declare neither. A half-declared provider fails to load instead of failing later at
  `credential:register`.
- **D5 — One credential scope has exactly one owning harness.** `di.json` carries only a
  class path, so authenticator identity cannot be checked statically, and `module:validate`
  must work without importing Python. Sharing a pool between harnesses would need an
  explicit manifest field; until something needs it, a collision is a hard error at
  registration.
- **D6 — The caller claims the credential; the runner has no pool access.** Previously the
  runner could fall back to the pool itself, so the command it built and the process it
  spawned could end up on two different credentials. Now the credential travels in
  `HarnessRunContext`.
- **D7 — `usage_log.credential_id` is nullable.** A provider that needs no credential must
  still be observable; the row is attributed by `(harness, provider)`. The first draft had
  an early `return` in `_record_usage` that cancelled this benefit — removed, because it
  would have made exactly that class of run invisible.
- **D8 — Six files in `framework/harness/`, not twelve.** The draft split one protocol per
  file; grouping by cohesion (`descriptor`, `runtime`, `protocols`, `registry`, `manifest`,
  `subprocess_runner`) reads better and matches AGENTS.md #1 (now RULES.md CODE-3; "three similar lines >
  premature abstraction").
- **D9 — Sandbox package fields are schema-validated, NOT shell-quoted.** They are rendered
  into a Dockerfile, so a third-party `di.json` must not be able to inject shell — hence a
  regex per field plus an allow-list of managers. But `shlex.quote` was *wrong* here: the
  rendered line is `"@openai/codex@${CODEX_VERSION}"`, and quoting stops the `ARG` from
  expanding, breaking the very pin it was meant to protect. Safety comes from validation.
- **D10 — No `provider_options/base_url` field.** Speculative: nothing in tree needs a
  per-provider base URL yet, and an unused config path is a maintenance liability. Add it
  when a provider actually requires it.
- **D11 — `WorkspaceAdapter.serialize_toolbox_connection` is deliberately unconstrained.**
  Writing an `.mcp.json` is a *Claude* implementation detail. A harness that consumes the
  Toolbox as CLI flags, an extension, or env vars is equally valid, so the framework hands
  over a `ToolboxConnectionSpec` and lets the harness decide.
- **D12 — `CredentialAuthenticator` is a total protocol.** Both methods always exist so
  structural typing (`isinstance` on a `runtime_checkable` Protocol) actually passes; what
  limits the modes is the *declaration*, and an unsupported branch raises
  `UnsupportedRegistrationMode`. Replaces `hasattr` probing.
- **D13 — `capture_refreshed_credentials` joins the protocol.** The consumer used to probe
  for it with `getattr`, which silently skipped credential capture for any adapter that
  named the method differently.
- **D14 — Credential events dual-dispatch under both names.** Renaming five events would
  break third-party observers, so `dispatch_credential_event` emits the new
  `credential_*` name AND the deprecated `token_*` name, building **both** payloads from
  the same data so an observer on either name sees consistent values. Removal tracked in
  ROADMAP.md.
- **D15 — Legacy config resolution compares ORIGINS, not presence.** Pre-0.15,
  `agent_view/provider` held the harness id. Because the new `config.json` always ships a
  default harness, "harness unset" never happens — a presence test would give an operator
  carrying `CONFIG__AGENT_VIEW__PROVIDER=codex` the *default* harness plus a provider it
  does not offer. So the fallback ranks ENV > agent_view > workspace > default >
  `config.json`. A legacy value is recognised **structurally** (a provider naming a
  registered harness); two earlier attempts at this used a hardcoded `claude`/`codex` list,
  which reintroduced in the framework precisely the branch the refactor removes. A pair
  that is neither valid nor legacy now **raises** instead of silently falling back to
  Claude.
- **D16 — The data patch's harness→provider map is FROZEN in the patch.** A data patch is
  applied once and tracked permanently in `data_patch`, so it must not depend on which
  modules happen to be enabled at upgrade time: reading the harness registry would skip
  `provider=codex` rows on a deployment with the codex module disabled, and a later
  `module:enable codex` would never re-run the patch — leaving that config permanently
  unmigrated.
- **Two live bugs fixed on the way.** (a) Claude's flags lived in *two* places
  (`TokenClaudeRunner._build_command` and `ClaudeCliInvoker.headless_command`) and had
  already drifted: the invoker omitted `--mcp-config .mcp.json --strict-mcp-config`, so
  `agento run <view> "<prompt>"` started the agent with **no toolbox**. One
  `CommandBuilder` per harness makes that unrepresentable. (b) `materialize_agent_credentials`
  iterated *every* registered harness, handing a Claude view the Codex credential merely
  because the codex module was enabled — now exactly one scope is resolved (least
  privilege, AGENTS.md "Security").
- **`job.provider` added after all (reversal).** The first implementation deliberately
  skipped it — `job.agent_type` already stores the harness id, and the provider was recorded
  per run on `usage_log.provider`. Review round 2 supplied the failure that judgement had
  missed: `replay` had nothing to read, so it substituted the harness's `default_provider`
  and a run on a non-default provider replayed on the **wrong** one. Migration
  `031_job_provider.sql` adds the column (nullable — pre-migration rows genuinely do not
  know) and also widens `agent_type` to `VARCHAR(64)` to match the harness-id contract,
  which the pre-0.15 `VARCHAR(20)` would have truncated for a long third-party id.

---

## 2026-07-30 — Token-pool claim uses blocking `FOR UPDATE`, not `SKIP LOCKED`

- **The bug.** `select_token` claimed the LRU token with `SELECT id … LIMIT 1 FOR UPDATE SKIP LOCKED` followed by a *separate* `UPDATE … WHERE id = <that id>`. Under concurrency this handed the **same** token row to two workers: a tightly-synchronized 10-way burst over a 10-token pool produced only 9 distinct tokens ~⅓ of the time (MySQL 8.0.45, REPEATABLE READ). It defeated LRU fairness exactly under the concurrency the pool exists for — surfacing as an intermittent `test_concurrent_materialization` failure and reproduced deterministically in a barrier-synchronized harness.
- **Root cause.** `EXPLAIN` on the real query is `type=ALL … Using filesort` — the `(expires_at IS NULL OR expires_at > …)` / `(throttled_until IS NULL OR …)` OR-predicates (and the tiny table) defeat `idx_oauth_token_pool_select`, forcing a full-scan + filesort. `FOR UPDATE SKIP LOCKED` over a filesorted `LIMIT 1` does **not** keep the returned row exclusively locked through the follow-up `UPDATE`: concurrent claimants skip/re-read and pick the same low-`used_at` row. The docstring's claim that "`SKIP LOCKED` prevents two concurrent workers from picking the same row" was simply false for this query shape.
- **The fix.** Drop `SKIP LOCKED`; use a plain blocking `FOR UPDATE`. A concurrent claimant now *blocks* on the held row lock instead of skipping it; when the holder commits its `used_at` bump the waiter resumes with a fresh current-read and picks a *different* (now higher-`used_at`) row — serializing claims so two workers never receive the same token. Because `select_token` commits immediately, the block is only microseconds. Verified: 0 duplicates and 0 lock-wait/deadlock errors across 240+ synchronized bursts up to 6× oversubscription (5 tokens / 30 workers).
- **Alternatives considered.** (a) *Optimistic CAS loop* (`UPDATE … WHERE id=? AND used_at <=> <read value>`, iterate candidates): correct for pool ≥ workers but **deadlocks** at heavy oversubscription — it accumulates row locks across candidate `UPDATE`s within one transaction (40/40 deadlocks at 5 tokens / 30 workers). Rejected. (b) *Single atomic `UPDATE … ORDER BY … LIMIT 1` + unique claim-marker column, then select back*: correct but needs a schema migration to identify the claimed row. Rejected as non-surgical for a same-behavior fix. (c) *Index the pool to kill the filesort*: doesn't address the `SKIP LOCKED`-over-`LIMIT` release semantics and the OR-predicates still defeat the index. Rejected.
- **The `TokenResolver` retry loop stays** as a safety net. With blocking `FOR UPDATE`, `select_token` resolves contention internally and only returns `None` when no eligible row exists, so the "all locked by concurrent workers; retry shortly" branch is now rarely hit — but it remains correct defense-in-depth and its diagnostics are still accurate for the genuinely-exhausted-pool case.
- **The job-queue dequeue is unaffected and NOT changed.** `DEQUEUE_SQL` also uses `FOR UPDATE SKIP LOCKED`, but the job claim is a conditional CAS (`UPDATE job SET status='RUNNING' WHERE id=%s AND status='TODO'`) — a different, correct pattern where the status transition is the guard. (Separately noted: `_try_dequeue` does not check that `CLAIM_SQL`'s rowcount is 1 — worth a follow-up hardening pass, tracked outside this change.)
- **Regression test.** `tests/integration/test_token_pool_concurrency.py` runs 25 barrier-synchronized 10-way bursts over a fresh 10-token pool and asserts 10 distinct claims per burst (reliably red before, green after). `tests/unit/agent_manager/test_token_store.py` additionally pins that the claim SQL contains `FOR UPDATE` **without** `SKIP LOCKED`, so the race can't be reintroduced silently.

---

## 2026-07-26 — Per-view inbound `allowed_senders` for shared Outlook mailboxes (route-first authorization)

- **Only the allow-list moved per-view; DMARC and activation stay pre-route/mailbox-level.** Routing keys on the *sender*; activation keys on *addressing/summon_token* — orthogonal axes. Selecting a persona by sender and then asking "were you summoned?" would make `summon_token` dead, so activation still runs once inside `admit_mail` (pre-route) with the poll-owner's config. DMARC likewise stays global, unconditional, and pre-route; no view config may relax it. True per-persona summon (route-by-token) is deferred to a separate feature.
- **The divergence guard was NARROWED, not deleted.** `_shared_policy_divergence` still stalls a shared mailbox (`policy_divergence` `MailboxStalledEvent`) when members disagree on the five *activation* fields (`_MAILBOX_POLICY_FIELDS`) — because activation runs once with the owner's config, so a divergence would let the lowest-id view silently define activation for the whole mailbox. That guard is the mailbox's uniform activation floor (no weakest-activation-persona). Only `allowed_senders` is exempted from it; members may now legitimately differ on it.
- **The union pre-filter is auto-derived per poll, not hand-maintained.** In routed mode `admit_mail` is called with the in-memory **union** of the group's per-view `allowed_senders` (not the owner's list). This restores cheapest-first rejection *before* any routing DB/regex work (shared-fate DoS / alert-amplification mitigation), scopes the SECURITY_BREACH alert to union-trusted senders, and removes the old "hand-written union at owner scope" footgun without reintroducing manual upkeep. A post-route **per-view refinement** (`OutlookPublisher.sender_allowed`, same fail-closed matcher) then narrows to the routed-to view's own list; an unset per-view list inherits the default (agent_view→workspace→default) rather than black-holing.
- **Observability replaced the lost signal without adding an alert.** Removing the *allowed_senders*-divergence stall deleted a loud signal for a condition that is now desired, not a fault. Post-route drops (unroutable / ambiguous / per-view-allow-list) are surfaced once per poll via a **channel-generic** `InboundRouteDropEvent` (dispatched `inbound_route_drop_after`, mirroring `MailboxStalledEvent` — NOT an Outlook-named payload in the framework, per CLAUDE.md channel-agnosticism + the plain `{subject}_{verb}_{after}` core-event naming) plus a summary log; no alerting observer is wired (drops are normal traffic). A publisher-start "Effective outlook policy" log prints each view's allowed-senders **count** (never the raw patterns — PII/infra).
- **DoS caps: existing bounds suffice; no new per-poll message ceiling.** The union pre-filter (bounds router load to union-passing senders), per-sender routing memoization (one lookup per unique sender per poll), and the existing ReDoS budget on `match_ingress_identities` cover the shared-fate vector. A new hard per-poll ceiling was rejected — it would regress the bounded-load / anti-starvation behavior (`test_junk_starvation_gone_*`). Per-view cursors (a transient hold on one routed view still pins the single shared delta cursor) remain a pre-existing, orthogonal concern — deferred.

---

## 2026-07-24 — Regex + priority sender routing for shared Outlook mailboxes

- **Reuse ingress routing, no new router.** A shared mailbox UPN owned by ≥2 agent_views is polled once and each message is routed to a view by matching the normalized sender against `outlook_sender` ingress bindings (regex `fullmatch`, highest `priority` wins; a tie between different views is ambiguous → no job). Reuses `ingress_identity`, `ingress:*`, `IdentityRouter`, and the routing framework. A UPN owned by exactly one view stays byte-for-byte today's direct behavior.
- **Regex identity types are module-owned, not framework-hardcoded.** The framework holds an empty registry populated at bootstrap from each module's `di.json` `regex_identity_types`; `outlook` contributes `"outlook_sender"`. Disabling the module drops it. Both the runtime matcher and the `ingress:bind` CLI gate on one `is_regex_identity_type()` predicate — never a hardcoded string — so the framework stays channel-agnostic (CLAUDE.md #6, now RULES.md PLC-2, spirit + module-completeness).
- **`regex` dependency for a bounded ReDoS matcher — a deliberate minimal-deps exception.** Ingress patterns are admin-authored but matched against attacker-influenced senders, so catastrophic backtracking is a DoS risk. Stdlib `re` has no timeout, `signal.alarm` cannot interrupt a C-level match, and the GIL defeats thread timeouts; a per-command cron `timeout` would kill before cursor persistence (permanent mailbox pin) and miss non-cron callers. The pip **`regex`** module (self-contained C-extension, manylinux wheels — NOT google-re2/abseil) supports an operation-level `timeout=`, so the bound lives at the generic matcher `match_ingress_identities` and protects every caller. Justified like `cryptography` (a security dep). Pinned **`regex.VERSION0`** (re-compatible dialect) is used identically by the CLI validator and the runtime matcher so a pattern accepted at bind time behaves the same at match time. Dual wall-clock budget — per-pattern (~0.1s) + total-per-lookup (~0.5s) — so the whole lookup is bounded regardless of binding count; a timed-out/invalid binding is skipped (WARN by binding id via a bounded rate-limiter, never the raw pattern) and the poll advances (no pin). Process-isolation was rejected (fork storm on backlog polls).
- **PII discipline.** The router logs the sender (`identity_value`) domain-only + a short hash, and `RoutingCandidate.reason` carries `binding_ids`/`priority`/`agent_view_id` only — never the raw pattern (post-normalization it may be an email address).
- **Secret boundary (SEC-F1) is out of scope.** `bootstrap` transiently decrypts DEFAULT-scope `obscure` config in the cron/consumer/CLI — pre-existing, framework-wide, and unrelated to routing (this change adds only non-secret per-path reads). A blanket obscure-skip is both incomplete (consumer + ENV paths) and harmful (would break app_monitor's cron-side SMTP breach alert). A correct fix (a `toolbox_only` field class + all bootstrap callers + the ENV path + an app_monitor SMTP toolbox transport) is a separate security-hardening effort tracked at [docs/security/toolbox-only-secret-boundary.md](docs/security/toolbox-only-secret-boundary.md).

---

## 2026-07-18 — Process-owned SQL pool registry with tool isolation and a server budget

- **Why a process-owned registry.** MCP servers are created per session/agent_view, but the toolbox is one Node process. Library-global connections made the first MSSQL credentials win, while per-session pools would multiply connections without bound. The toolbox process now creates one `SqlPoolRegistry` and explicitly injects it through the session context into SQL adapters. Registry state is therefore shared only by deliberate dependency injection, not module-level mutable globals.
- **Pool identity and secret handling.** A pool is keyed by adapter, tool name, and the fully resolved connection config, using a process-random keyed HMAC. Different tools never share credentials; identical registrations of the same tool can reuse a pool across sessions. Raw credentials are not used as map keys or emitted in diagnostics.
- **Two independent limits.** `core/client_connection_pool_max_per_tool` (default 10, overridable per tool) caps physical client connections in one pool. `core/server_concurrency_budget` (default 10, default scope only) caps active operations across all pools for one adapter/host/port, so it does not multiply by tool or agent_view count. The process-wide wait queue is FIFO, bounded to 100 operations per endpoint, and cancellable by SQL deadline or `AbortSignal`.
- **Lifecycle and failure semantics.** A pool is closed after 30 seconds without active operations and all tracked pools are closed on `SIGTERM`. A failed close is logged without credentials and retried with bounded exponential backoff. At most one failed close is tracked per pool key and tracking stops after three failed attempts, preventing an unavailable driver from growing the registry without bound. MySQL keeps released connections reusable during the registry's idle window rather than sweeping every connection after one second.
- **Healthchecks cancel work, not just HTTP waiting.** The health runner aborts checks at its response deadline. MySQL healthchecks borrow one connection and destroy it on cancellation; MSSQL healthchecks call `Request.cancel()`. Both pass the same signal into the server-budget wait, so a timed-out health request releases or leaves the shared concurrency budget instead of starving normal tools.
- **Session-scoped execution policy.** SQL timeouts are converted and captured when each adapter is registered; no session can mutate another session's timeout. Both SQL adapters use the same conservative read-only scanner, which rejects batches and write/DDL/control keywords even behind a CTE. MSSQL remote-execution primitives (`OPENQUERY`, `OPENROWSET`, `OPENDATASOURCE`) are denied because their string arguments can mutate a linked server behind an outer `SELECT`. MySQL and PostgreSQL strings containing backslashes fail closed where SQL mode / `E'...'` semantics could make the lexer and server disagree. Database read-only users remain the second enforcement layer.
- **REST registration is a startup-only capability.** `registerModuleRestApis()` receives Express `app`; per-session and health registration through `registerTools()` explicitly strips it. REST-only modules also guard on startup-only context. This prevents MCP session creation and repeated `/health` calls from appending duplicate Express routes.

---

## 2026-07-04 — Outlook privacy, reply-all, and stateless loop safety

Hardens the Outlook channel against cross-user mail exposure in a shared mailbox and against bot-to-bot loops — with **no new DB tables and no persisted thread state**. See [docs/modules/outlook.md](docs/modules/outlook.md).

- **Privacy is by construction — remove enumeration + bind reads to the triggering message, not an ACL.** The leak vector was *enumeration*: `outlook_search_messages` / `outlook_get_new_messages` listed other people's mail (subjects, senders, ids) in a shared mailbox, and `outlook_get_message` would then read any harvested id. Both enumeration tools are removed, and `outlook_get_message`/`outlook_get_attachment` are **hard-bound to the current job's own triggering message** — resolved in the toolbox from `job.reference_id` via `jobId`, with a scope-checked lookup (`WHERE id=? AND agent_view_id=? AND source='outlook'`, fail-closed). A leaked opaque id cannot read another conversation. **Since 2026-08-23 `jobId` is not caller-supplied at all** — it comes from the session's capability row (see the toolbox east-west auth entry), so the agent cannot even name another job's id — within its own run's capability (a co-tenant run's token read off the shared workspace is the residual, see zero-trust.md). Chosen over a conversation-ACL gate table (no new state); email self-quoting carries prior thread context inline, so thread-walking is rarely needed. Interactive `agento run` (no `jobId`) is a deliberate, documented **operator escape hatch**, not part of the by-construction guarantee.
- **One reply verb — reply-to-all.** For a 1:1 mail reply-all == reply; for a group thread it keeps everyone in one thread (the actual goal). `outlook_reply` replies to `(Reply-To || From) ∪ To ∪ Cc` minus the agent's own mailbox; every recipient is gated against `core/email_whitelist`, and **only whitelisted addresses ever receive the reply**. A non-whitelisted recipient is handled per **`outlook/reply_policy`** (agent_view-scoped): `remove` (default) **drops** the blocked address and sends to the rest — so one bad address in a group thread never blocks the whole conversation — while `block` blocks the whole send (the original behavior). The whitelist invariant is identical either way (a blocked address never receives mail); `remove` was made the default because block-whole silently loses the entire reply on one stray Cc, whereas `remove` still reaches the humans on the thread and reports exactly who it dropped (so it is not silent to the agent). All-blocked under `remove` sends nothing and errors (cannot reply to nobody). Applies to `outlook_reply` only — `outlook_send_mail` still blocks the whole send (there the agent chose the addresses explicitly). Targeted 1:1 mail uses `outlook_send_mail`. The single-recipient reply behavior is dropped.
- **Activation is a pure function; loop-safety is stateless fleet-mailbox detection — no thread-state table, no per-message marker.** The publisher creates a job only when the mail is `direct` (the mailbox, or a `mailbox_aliases` entry, is the sole recipient across To+Cc — strictness via `direct_requires_sole_recipient`) or a `mention` (the `summon_token`, default `@agento`, appears in subject/body-preview); otherwise it stays silent and advances the cursor. Bot-to-bot loops are broken by treating an inbound message as agent-authored when its **DMARC-verified `From` is in the fleet mailbox set** — **auto-derived** by the toolbox delta handler from the active agent_views (the union of each outlook-enabled view's resolved `outlook/outlook_mailbox_user_id`, standard fallback), never a hand-maintained list; such mail is hard-suppressed unless `allow_bot_collaboration=true`. All computed from the current message — no hop counter, no persisted state.
- **Loop detection is address-based, NOT an outbound HMAC header — because Graph makes `internetMessageHeaders` read-only after create.** The original design stamped a signed `X-Agento` header on outbound mail. Impl review established (Microsoft Graph `message` resource docs: "add custom headers only when creating a message … after the message is sent you cannot modify the headers"; the property is Read-only) that headers can only be set at **create** time, and `createReplyAll`'s JSON `message` param documents only `comment`/`body` (headers only via the MIME path) — so a reliable signed marker on the main *reply* path would require manual MIME construction. Rather than carry that complexity (and an HMAC secret), loop detection keys on the sender address: the `From` is already DMARC-gated, so it can't be spoofed into a false positive, and a false positive only ever *suppresses* a reply (safe direction). This also removes the need for any loop-marker secret entirely (no `OUTLOOK_LOOP_MARKER_SECRET`, no `stamp_loop_marker`), so the SKILL.md §5a secret-decryption concern does not arise for loop-safety at all. Companion fix retained: the Outlook publisher reads its non-secret fields via per-path `.get()` (never `get_module("outlook")`) so it never resolves the Graph secret. Trade-off: the fleet is scoped to **this deployment's** agent_views — a cross-**deployment** peer's mailbox is not part of the auto-derived set, so intra-deployment loops (the primary risk) are covered with **zero config**, while cross-deployment ones fall back to the activation rule.

---

## 2026-06-19 — Bitbucket Cloud PR-review channel (`src/agento/modules/bitbucket/`)

A new core, disableable channel that watches an agent's open Bitbucket Cloud PRs and queues review work, modeled on the Outlook channel (Python publisher + toolbox token boundary) with **zero framework edits**. See [docs/modules/bitbucket.md](docs/modules/bitbucket.md).

- **D-1 routing by per-view config, not the router.** The agent_view's `bitbucket_account_uuid` + `repo_allowlist` *is* the binding (mirrors Outlook's mailbox). The ACC's optional explicit bind is met by the existing generic `ingress:bind bitbucket <account_uuid> <agent_view_code>` (zero new code); the publisher does not depend on it.
- **D-2 checkout+push uses the existing `workspace_build` SSH identity — a different credential from the API token.** The token (toolbox-only, never agent-reachable) drives all REST work; the SSH key is the agent's own push identity, "opt-in" by being configured. The module does **not** gate git push and the git layer is **not** module-allow-list-enforced (the API write surface is); this boundary is documented rather than overclaimed.
- **D-3 no schema migrations** — reuse `job` (incl. `requester_*`), `core_config_data`, `ingress_identity`.
- **D-4 onboarding requires a reachable toolbox to verify-before-save.** Keeps every Bitbucket API call inside the toolbox (the "Python must not hold the token" rule applies to onboarding too). If the toolbox is unreachable, onboarding verifies nothing and saves nothing; the offline path is manual `config:set`.
- **D-5 the toolbox is the authorization boundary, with NO framework/toolbox edit.** `enabled`, workspace, `account_uuid`, `repo_allowlist` are resolved from scoped config and enforced on every REST + MCP call; caller args/body may only narrow, never authorize. REST handlers call `loadScopedDbOverrides` themselves → have `agentViewMeta` → fail closed (404) on an unknown `agent_view_id`. MCP tools cannot see `agentViewMeta` and the agent (same Docker network as the toolbox) can in principle open its own session with a different/omitted `agent_view_id` — the **framework-wide N5-2 internal-caller-auth gap shared by Jira and Outlook**, explicitly out of scope for this port. **Closed framework-side on 2026-08-23** (see the entry above): an MCP session now needs a capability token and takes its `agent_view_id` from that row, so MCP *does* refuse a forged view claimed on a session's own capability. The module-level guarantee is unchanged: token toolbox-only; tools opt-in per scope; every read/write bounded to the resolved `repo_allowlist` (fail-closed by config-absence). **Token confinement (hardened in impl review round 2):** the API token is **only ever decrypted in the toolbox** — it is never stored at DEFAULT scope (so the framework's `bootstrap()`, which resolves DEFAULT-scope obscure config in the cron process, never decrypts it), and the publisher resolves only non-secret fields via per-path `.get()` (never `get_module()`, which would resolve the token field). Bitbucket config is therefore always agent_view-scoped (see D-11 update).
- **D-6 API-token scopes are the granular 2026 names, listed explicitly with no implication** (unlike OAuth, API-token scopes do not grant one another): `read:user:bitbucket`, `read:repository:bitbucket`, `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket` — NOT the deprecated `pullrequest`/`pullrequest:write`/`repository` names. `write:repository` is not requested (no API repo writes; push is SSH).
- **D-7 two registered channel instances, `.name` == published `job.source`.** The framework resolves a job's channel via `get_channel(job.source)` keyed on the instance `.name` (registry.py / consumer.py). Distinct sources are required for `skip_if_active` lane independence, so `di.json` registers `BitbucketCommentsChannel` (`bitbucket-comments`) and `BitbucketChangesChannel` (`bitbucket-changes`), both subclassing a shared `BitbucketPromptChannel`. A single `.name == "bitbucket"` channel would make every job fail at `get_channel`.
- **D-8 changes-requested detection via the `/activity` event log, not `participants[]`, order-independent.** `participants[].participated_on` is an approval/last-comment timestamp, not the changes-requested time, and cannot disambiguate multiple reviewers. The fast lane reads a bounded `/activity` window, keeps all non-agent `changes_request` events, and takes `max(date)` client-side (does not assume API sort order). `participants[]` is a cheap pre-filter only. Symmetrically, `last_commit_on = max(commit.date)` over a bounded commits window (not `values[0]`; empty list ⇒ null).
- **D-9 toolbox write-tool hardening (module-only, exceeds the Jira/Outlook bar).** Write tools re-fetch the PR and reject `state != OPEN` (also makes "closes mid-work" a clean no-op); `create_pr` validates BOTH `source.repository` (forks/cross-repo — the API accepts it) and the destination repo against the allow-list.
- **D-10 method-aware `bbFetch` retry.** Retry on 429 for any method (request not processed); retry on 5xx for idempotent GET only — mutating POSTs do not auto-retry on 5xx, to avoid duplicate comments/PRs. `Retry-After` honored; capped exponential backoff.
- **D-11 multi-view DEFAULT-scope fan-out guard.** `run_lane` reads each view's *effective* (fallback) config, so a DEFAULT-scope Bitbucket config in a >1-view deployment would make every view resolve the same account/repos and publish the same PR per view. Guard: with >1 active view, a view is processed only if `account_uuid` AND `repo_allowlist` are set at its OWN agent_view scope (direct `core_config_data` check); DEFAULT-only views are skipped (logged, not errored). `is_complete()` mirrors this so onboarding never reports "complete" for a setup `run_lane` would skip — "complete" always implies "will actually publish". Defense-in-depth: the global idempotency-key uniqueness (`INSERT IGNORE`) still prevents literal duplicate jobs even if misconfigured. **(Update — impl review round 2:)** the original "single-view deployments may use DEFAULT" convenience is **removed on security grounds** (see D-5): Bitbucket config is **always agent_view-scoped**, because a DEFAULT-scope token would be decrypted by the framework's `bootstrap()` in the cron process. Onboarding always writes at the owning view's scope (auto-selects the sole active view, prompts when several, refuses when none); `is_complete()` requires the token + `account_uuid` + `repo_allowlist` at a view's own agent_view scope (workspace/email may inherit DEFAULT/ENV).

---

## 2026-06-19 — Outlook poll progress tracked by a Graph delta cursor, not `isRead`

- **The problem.** The publisher polled each mailbox `isRead eq false` with a fixed `$top` window, oldest-first. Rejected mail (not allow-listed / DMARC-not-pass) was left unread forever, and published-but-unfinished mail also stayed unread, so the front of the window clogged and valid mail behind it was permanently starved. The root coupling: the poll tracked *what was read*, not *what it had already evaluated*.
- **The change.** A durable per-mailbox **delta cursor** (`outlook_poll_cursor`, the module's first `sql/` migration, keyed by the normalized mailbox UPN — the same key as the `seen_mailboxes` dedupe). The publisher loads all cursors, the toolbox resumes `mailFolders/Inbox/messages/delta` from the stored cursor (paging `@odata.nextLink` to the end — no fixed-window truncation) and returns new/changed mail + the next `@odata.deltaLink`; the publisher gates+publishes, then writes the cursor back **only after** publishing. `isRead` reverts to meaning only "the agent finished"; the publisher never mutates the mailbox.
- **Persist-then-advance, with a transient hold.** The cursor advances past terminal outcomes (published; spoof-rejected) and past every non-pass DMARC class — `none`/`bestguesspass`/`temperror`/not-allow-listed mail (so junk can't re-clog and Graph load stays bounded). It is **held** (not advanced) only on genuinely transient conditions — a publish exception or a toolbox/Graph error — so those batches are re-evaluated next poll. Nothing is ever marked read or recorded as permanently skipped, so a cursor resync re-evaluates everything against the current allow-list/DMARC with `idempotency_key` preventing duplicate jobs.
- **`temperror` advances, it does not hold (corrected in review).** A DMARC verdict — including `temperror` — is read from the **immutable receipt-time `Authentication-Results` header**; it is never re-evaluated on re-fetch. Treating `temperror` as "transient → hold" therefore pins the cursor *forever* (the same message re-surfaces every poll, the held delta grows without bound, eventually `502`-stalls the mailbox) — the exact bug this change fixes, reintroduced. So a frozen non-pass verdict is a *persistently non-terminal class*: it advances unpublished (re-evaluable only via a deliberate resync), not held.
- **Bounded-load vs re-evaluate-forever tension (resolved).** A single deltaLink advances past *everything* it returned; holding it to re-surface one message re-fetches "all changes since that link", which grows without bound if a never-resolving message pins it. Bounded load is the primary bug being fixed, so it wins: persistently non-terminal classes (incl. any frozen non-pass DMARC verdict) advance (re-evaluable only via resync), and only genuinely transient signals (a publish exception, a toolbox/Graph error) hold.
- **Store the full `@odata.deltaLink`, replayed as-is.** Microsoft's contract is explicit — the state token is *opaque*; "save and apply the **entire** `@odata.deltaLink` URL". An earlier opaque-`$deltatoken`-reconstruction design was rejected for relying on undocumented behaviour.
- **The replayed cursor is validated to the resolved mailbox before any token-bearing fetch.** The `cursors` map arrives in the REST body (a route reachable by the zero-trust agent) and is fetched with the Graph **app** token, which can read any mailbox. So the toolbox proves (with a *total*, never-throwing validator) the deltaLink is an `https://graph.microsoft.com` `…/users/{resolvedMailbox}/mailFolders/{folder}/messages/delta` URL with no embedded credentials **and a `$deltatoken`**; a foreign/malformed/token-less cursor is discarded → full base enumeration. This closes SSRF (token exfil to another host) and cross-mailbox reads (replaying another mailbox's deltaLink) while staying contract-compliant. The folder segment is left unconstrained (Graph resolves `Inbox` to an opaque folder id); the open risk is a tenant echoing the user object-id instead of the UPN (degrades to full enumeration each poll — bounded-load only — with a documented mitigation).
- **Resync is fail-closed on every stale-cursor signal.** A `410` *or* a `40x` carrying `error.code` `syncStateNotFound`/`resyncRequired` (parsed server-side, never surfaced) triggers a full re-enumeration. DMARC headers, if absent from a delta item, are hydrated via a per-message GET; an unverifiable verdict returns `502` so the cursor is held — DMARC can never be silently skipped.
- **The agent read tools enforce the same gate as the publisher (added in review).** `outlook_get_message`/`outlook_search_messages`/`outlook_get_new_messages` previously filtered only by `allowed_senders` — but the `From` header is forgeable, so a spoofed allow-listed sender on a **DMARC-failing** email (which the publisher correctly refuses to turn into a job) was still **readable** by the agent: a prompt-injection surface. The read tools now require an allow-list match **and** a DMARC `pass` (verdict from the immutable `internetMessageHeaders`, parsed with the shared `parseDmarcVerdict`), fail-closed — exactly mirroring the publisher gate. A Graph message **collection** does not reliably return `internetMessageHeaders` (unlike a single-message GET), so `search`/`get_new` **hydrate** the verdict via a per-message GET when the listing omits it — bounded to allow-listed senders (junk is never hydrated), mirroring the delta handler's hydration. `restrict_read_to_allowed_senders=false` still bypasses both checks (documented opt-out). `outlook_reply`/`outlook_send_mail` are unchanged (outbound, recipient-whitelisted); `outlook_mark_processed` is unchanged. **(Superseded 2026-07-04: `outlook_search_messages`/`outlook_get_new_messages` were removed, `outlook_reply` is now reply-all, and `outlook_get_message`/`outlook_get_attachment`/`outlook_reply`/`outlook_mark_processed` are all bound to the triggering job's message — see the 2026-07-04 entry above.)**
- **`parseDmarcVerdict` reads a token-anchored `dmarc=` (hardened in review).** The verdict regex is anchored to a real `Authentication-Results` token boundary (`(?:^|[\s;])dmarc=`) so a literal `dmarc=pass` substring inside an attacker-influenced field (a quoted local-part, `smtp.helo`, an `x-dmarc=` key) cannot forge a pass.
- **Contained.** Outlook module + toolbox only; no framework branching. The publisher path is GET-only (`list_delta`), so zero mailbox mutation is structural.

---

## 2026-06-18 — Outlook routing: the mailbox identifies the agent_view (per-agent_view mailboxes)

- **The change.** The Outlook channel moves from one global mailbox fanned out by sender (`resolve_agent_view` + `ingress:bind email`) to N mailboxes, one per agent_view, mirroring the Jira per-agent_view toolbox contract. The `outlook:publish` cron loops `get_active_agent_views` in id order; per view it resolves the scoped `OutlookConfig`, polls the toolbox passing only `agent_view_id` (the toolbox resolves the mailbox + Graph secrets via `loadScopedDbOverrides`), and publishes that mailbox's authorized + DMARC-passing messages directly to that view. Python holds zero Graph secrets (zero-trust boundary unchanged).
- **Why.** Routing by sender forced an `ingress:bind email <sender> <view>` rule per correspondent and could not express "this mailbox belongs to this agent_view". Binding the mailbox to the view is the natural identity for email and matches how Jira already scopes per-agent_view polling. `allowed_senders` + DMARC remain purely the inbound **security** gate, not routing.
- **Toolbox `agent_view_id` is fail-closed.** `/api/outlook/unread` accepts an absent/null `agent_view_id` as the global scope (single-view / onboarding path), but a *supplied* id must be a positive integer that resolves to an existing agent_view — otherwise it returns `400`/`404` and does NOT fall back to the global mailbox (else a bad id could expose a default mailbox or enable cross-view probing, since the response now also returns the resolved mailbox UPN for dedupe). A strict superset of `jira/toolbox/api.js`, justified because Outlook's response carries a mailbox.
- **Shared mailbox ⇒ lowest agent_view id wins.** Iterating in id order plus a `seen_mailboxes` set (keyed by the resolved, lower-cased, non-secret UPN the toolbox returns) means two views pointing at the same inbox produce one set of jobs under the lowest-id view; the idempotency key (`outlook:mail:{message_id}`) is the backstop. The redundant Graph fetch for the second view still happens (a view's mailbox is only known from its own response) but creates no duplicate job — cheap and rare, accepted.
- **DMARC non-pass is split into spoof vs recordless.** An explicit failure (`fail`/`quarantine`/`reject`) on a domain that publishes a DMARC policy is a probable spoof → `SECURITY_BREACH` log + a framework `security_breach_after` event (app_monitor's `SecurityBreachAlertObserver` emails ops when configured). Any other non-pass (`none`/`bestguesspass`/`temperror`/missing) is just a recordless domain → info log, no breach, no alert. Both still leave the message unread (fail-closed); only a confirmed `pass` publishes. This avoids flooding the breach marker with EOP's `bestguesspass` on every poll.
- **`ingress:bind` / `resolve_agent_view` stay in the framework** for other channels (Teams, API); they are simply inert for Outlook now. Existing sender-based email bindings are harmless and removable with `ingress:unbind`. No data migration: a pre-existing global mailbox at `default` keeps working for a single-view deployment and acts as a deduped shared fallback in multi-view ones.

---

## 2026-06-17 — Outlook certificate auth: PEM stored encrypted in the DB, not a mounted file

- **The problem.** The original cert option (2026-06-16 entry below) built `ClientCertificateCredential` from `outlook_cert_path` — a filesystem path that had to be a PEM **mounted into the toolbox container**. But nothing in the managed compose mounts it: the operator had to hand-edit `docker-compose.override.yml` to add a read-only volume, then enter a container-side path. The onboarding prompt ("Path to certificate PEM (mounted into the toolbox)") implied a mount that did not exist — an undocumented, error-prone step, and inconsistent with how every other secret (client secret, SSH key) is handled.
- **The cert is now an `obscure` config value holding the PEM contents.** `outlook_cert_pem` (and optional `outlook_cert_password`) are `obscure` fields — AES-encrypted at rest in `core_config_data`, decrypted by the toolbox config-loader exactly like `outlook_client_secret` and the agent_view SSH key. `graph-auth.js` passes the decrypted contents straight to `@azure/identity` as `new ClientCertificateCredential(t, c, { certificate: pem, certificatePassword })` — the library accepts PEM **contents**, not just a path — so there is no file and no volume mount. Onboarding reads the pasted PEM (multi-line, terminated by an `END` line), validates it contains **both** a certificate block and a private-key block (Azure requires both; a cert-only paste otherwise fails opaquely at token acquisition), and stores it encrypted.
- **Switching auth methods clears the other branch's credentials, atomically.** Because the certificate takes precedence over the client secret when both are present, a re-onboarding that switches methods must delete the credentials it is not using — otherwise stale material silently wins. Onboarding issues those `config_delete` calls in the **same transaction** as the chosen-credential writes (before the single `conn.commit()`), so the immediately-following Graph verification — which reads via the toolbox's own DB connection and sees committed rows only — observes the cleaned state. Operators who configure manually (bypassing onboarding) must `config:remove` the unused keys themselves; the docs say so.
- **This supersedes the cert-path mechanism described in the 2026-06-16 entry.** `outlook_cert_path` is removed, not migrated (a path cannot be auto-converted to PEM contents); deployments that used path-based cert auth must re-run onboarding or set `outlook/outlook_cert_pem`. Cert config is at `scope='default'`; no data patch.

---

## 2026-06-16 — Outlook email channel: DMARC-gated, allow-listed inbound authorization

- **The problem.** Porting the Outlook/Microsoft-Graph channel from the sibling `k3-agent` project gave us a working mailbox poller, but k3 turned *every* unread email into a job — no sender authentication, no allow-list. For an external, unauthenticated channel that can auto-reply, that is a spoofing and abuse vector. The requirement: an inbound email creates a job **only** when its `From` is on an explicit allow-list **and** it passes DMARC.
- **Graph auth supports BOTH certificate and client secret, selected by config (via `@azure/identity`).** k3 used a mounted PEM (`ClientCertificateCredential`); the original plan defaulted to a client secret stored as an `obscure` config value. We support both: `graph-auth.js` builds a `ClientCertificateCredential` when `outlook_cert_path` is set, else a `ClientSecretCredential` from `outlook_client_secret` (cert wins when both present). This adds `@azure/identity` as a direct toolbox dependency but lets a deployment use whichever credential its Azure app registration permits. Token-acquisition errors are sanitized (code only — never the raw provider/cert detail).
- **Whitelist is a dedicated `outlook/allowed_senders` config, separate from routing.** Rather than overloading ingress binding as the implicit gate, the allow-list is an explicit, Outlook-owned security control (comma-separated, core `matchesWhitelist` glob semantics, **empty ⇒ block all / fail-closed**). Routing to an `agent_view` is still the framework's job (`resolve_agent_view` via `ingress:bind email …`). The redundancy is deliberate defense-in-depth for an external channel: the allow-list answers *who is authorized*, ingress answers *which agent_view handles them*.
- **DMARC is read from the FIRST `Authentication-Results` header (anti-spoof).** The toolbox unread endpoint adds `internetMessageHeaders` to the Graph `$select` and parses the **first** `Authentication-Results` header's `dmarc=` verdict. Exchange Online Protection prepends its own authoritative header at inbound ingestion, so the topmost is trusted; any lower `Authentication-Results` may be attacker-supplied and is ignored — trusting it would let a spoofer forge `dmarc=pass`.
- **The publisher gate (strict order): normalize → allow-list → DMARC → route → publish.** A non-allow-listed sender is ordinary non-routing (skip, info log of domain only, no breach). An **allow-listed** sender that fails (or lacks) DMARC is a probable spoof → a greppable `SECURITY_BREACH` error log (structured `event=security_breach reason=dmarc_not_pass`), **no publish**. A passing, allow-listed, routed sender publishes one job with the `From` stored in `job.requester_email` and `requester_trust = domain` (DMARC pass cryptographically aligns the From domain — stronger than the `claimed` default). DMARC enforcement is **unconditional and not configurable** — there is no `require_dmarc` opt-out. An earlier iteration exposed `require_dmarc` as a config flag (a local-testing escape hatch); it was removed because a bypassable spoofing gate on an external, auto-replying channel is not worth the risk, and a misconfigured `require_dmarc=0` in production would silently disable the anti-spoof protection.
- **The secret never enters the Python registry.** `OutlookConfig` deliberately omits the tenant/client/secret/cert/mailbox fields (exactly as `JiraConfig` omits `jira_token`): all Graph credentials and HTTP live in the toolbox (the zero-trust boundary). The Python side only carries `enabled`, `poll_top`, `allowed_senders`.
- **Generic workflows promoted to the framework.** `TodoWorkflow`/`FollowupWorkflow` were owned by `jira` but are channel-agnostic; they now register as framework defaults (before the module loop, so a module can still override) so the Outlook channel reuses them without an `outlook → jira` dependency or a workflow-registry collision.
- **Deferred.** Internal-caller auth for toolbox REST endpoints (`/api/outlook/unread`, like `/api/jira/*`) is a framework-wide concern tracked separately; the residual exposure is bounded (only `agento-net` containers, route removed on module-disable, errors sanitized).

---

## 2026-06-15 — Claude OAuth credential write-back + non-clobbering capture persistence

- **The bug.** Claude OAuth subscription tokens silently rotted. The CLI rotates `.claude/.credentials.json` in place during a run, but each job uses an ephemeral HOME re-seeded from the DB; without a write-back the rotated (single-use) refresh token was discarded and the next run replayed an invalidated token → daily `401 Invalid authentication credentials`. `ClaudeConfigWriter` had no `capture_refreshed_credentials`, so the consumer's provider-agnostic post-run hook resolved to `None` for Claude.
- **Clarification (2026-08-04).** The phrase-tightening described below was correct — the
  match really was narrowed from a bare `401` to the specific string. The defect this entry
  did not catch is that the specific string was left in the **poison** list, where rule 1
  shadowed the transient rule that already classified it; see the 2026-08-04 entry.
- **Parity fix.** Added `ClaudeConfigWriter.capture_refreshed_credentials` (mirrors the Codex writer): on a real `refreshToken` rotation it persists the refreshed credentials. The writer is registered via `di.json`, so no framework wiring was needed to dispatch it.
- **The hook never committed.** `get_connection` sets `autocommit=False`, and the shared capture hook closed its connection without committing — so the write-back (Claude's *and* Codex's, which was thus a silent no-op too) was rolled back. Fixed with one agent-agnostic `_conn.commit()` after a successful capture, matching the in-file `_handle_auth_failure` convention.
- **Capture must not resurrect operator state.** `register_token`'s upsert forces `enabled=TRUE`, `status='ok'`, `error_msg=NULL` — right for interactive (re)registration, wrong for *automatic* capture: once the hook commits, it would silently re-enable a token an operator disabled/quarantined mid-run. Added `update_refreshed_credentials(conn, token_id, credentials)` — a targeted `UPDATE` of only `credentials`/`expires_at` by id that preserves operator/health columns. **Both** the Claude and Codex writers use it (making the hook durable turned Codex's pre-existing `register_token` call into an *active* clobber, so Codex switched in the same change).
- **Why `expires_at` is left NULL for Claude.** `claudeAiOauth.expiresAt` is the ~8h *access*-token expiry in epoch **ms**. `select_token` treats `expires_at` as a hard "unusable after" filter and nothing proactively refreshes outside a job run, so populating it would retire a still-refreshable token after an idle gap — re-breaking the self-heal. Capture **explicitly** sets `expires_at = None` rather than relying on `_coerce_expires_at` dropping the ms value (that accident wouldn't protect a legacy/manual seconds- or ISO-valued `expires_at`). Proactive refresh is a separate daemon, out of scope.
- **Auth-error phrase tightened.** `AUTH_ERROR_PHRASES` now matches the specific `401 Invalid authentication credentials` rather than a bare `401`, so transient non-credential 401s no longer poison the token (a bare `401` flipped `status='error'` and quarantined an otherwise-healthy token). *(Superseded 2026-08-04: the specific phrase was removed from `AUTH_ERROR_PHRASES` entirely — it belongs on the transient path.)*

---

## 2026-08-04 — Every tool is declared, visible and individually switchable

- **The defects.** The admin Tools screen and `tool:list` enumerate `module.json` `tools[]` only, but
  most toolbox tools are registered imperatively with `server.tool(...)` and were never declared —
  **37 live tools were invisible** (D1). Worse, `jira` and `core/browser` gated on a single
  module-level `isToolEnabled('<module>')` early-return, so their per-tool `tools/<name>/is_enabled`
  keys were **dead** (D2). The trigger: a Service Desk agent posted a public customer-facing comment
  because `jira_internal_comment` was invisible-and-off while `jira_add_comment` could not be
  disabled without also killing `jira_get_issue`.
- **Declaration is now total.** Every tool a module can register is named in its `tools[]` — including
  all **26** `browser_*` tools — the set `@playwright/mcp` actually exposes under the flags
  `playwright-client.js` starts it with (`--caps devtools`), most of them proxied under a computed
  name. That set is NOT the package README's: scraping the README declared 12 tools production
  never exposes and missed 2 it does, leaving those 2 invisible and gate-denied.
  `jira` declares its master plus 9 tools. `core` declares 29 entries (2 core tools, the `browser` master, and 26 browser tools).
- **`tools/jira/is_enabled` was KEPT, not split.** `resolveConfigValue` checks ENV first, so an
  operator's `CONFIG__TOOLS__JIRA__IS_ENABLED=0` cannot be migrated by any data patch — retiring the
  key would have silently *granted* eight tools. Faithful DB migration was also not expressible:
  `default master=0` + `agent_view master=1` means enabled today, and under `master=1` a same-scope
  per-tool `0` was dead, so neither dropping nor copying rows preserves behaviour;
  `jira_get_attachment`'s effective value was `master AND own_key`, which no single-row copy
  reproduces. Accepted delta: a previously-dead per-tool jira key now takes effect.
- **The master became declarative (`requires`), not hand-written.** A `tools[]` entry may declare
  `requires: "<tool>"`. `registerTools`' gate walks the chain and fails closed on a cycle; Python's
  new `framework/tool_enablement.py` resolves the same declaration so the Tools screen annotates a
  blocked child `(blocked by jira)` and `tool:list` prints the **effective** status. One declaration,
  two languages, so the display cannot contradict the runtime. `jira.js` and `browser.js` both lost
  their module-level early-return — the rule now has **zero** exceptions.
- **`core/playwright_tool_whitelist` was retired.** A comma-separated Config string was a second
  gating mechanism the Tools screen could not render, which is precisely what left its browser tools
  invisible and un-togglable. It is deleted; a data patch converts DB values into per-tool `is_enabled`
  rows. Safe because the whitelist shipped empty ("deny all"), so the new keys default off and nothing
  is granted. A **DB**-set whitelist is translated per scope with an explicit on/off for every name —
  the whitelist was one scope-resolved value, so writing only the enabled ones would let a narrower
  child list inherit the parent's extras. Stale `tools/browser_*/is_enabled` rows (which `tool:enable`
  could always create, dead under the old gate) are cleared at every scope first. An **ENV**-set
  whitelist is deliberately not migrated: `setup:upgrade` runs in the cron container and cannot read a
  toolbox-container variable reliably, and converting it to permanent DB grants would widen durable
  state — so it fails toward *disabled* and the operator re-enables explicitly.
- **Alternative rejected: a `dynamic_prefix` pattern.** Declaring one `browser` switch plus a
  `browser_` prefix marker would have kept the whitelist, left those tools off the screen, and made a
  *pattern* a runtime authorization rule — which authorized any module for `browser_*` and needed a
  per-module gate factory to contain. Enumerating the names deleted more code than it added.
- **Anti-drift is two-sided.** `module:validate` (so `bin/test` and `setup:upgrade`, which aborts
  before any DB change) errors on a literal `server.tool('x')` missing from `tools[]`, a `requires`
  cycle or dangling reference, a duplicate tool name (same-module or cross-module), and a `config.json`
  `tools/<name>/is_enabled` default the module does not own. The cross-module check lives in a shared
  `validate_tool_namespace` helper because `setup._validate_manifests` validates module-by-module and
  would otherwise never run it. At runtime `registerTools` WARNs per-module on a registered-but-
  undeclared name — the backstop for a computed name, e.g. one a future Playwright version adds.
- **Two checks, because each catches what the other cannot.**
  - **`toolbox/tests/tool-declaration.test.js` is the exact one.** It *executes* every shipped
    module's `register()` with a recording server and asserts nothing registers that the manifest
    does not declare, feeding the real `@playwright/mcp` tool list through the passthrough loop so a
    version bump that adds a tool fails there. Its coverage guard is exhaustive and keyed by FULL
    path — every `toolbox/*.js` must be a listed registrar, a listed route-only file (*proven* to
    make no `server.tool` call and no `export *`), or a listed support file (*proven* to export no
    `register`, over-matching `export *` and destructured exports since production invokes any
    function-valued `register`). Path keying matters: a bare `api.js` would auto-exempt every
    future module's, including one that re-exports a registrar.
  - **`module:validate` (so `bin/test` and `setup:upgrade`) is a BEST-EFFORT pre-flight that can
    only MISS.** It scans toolbox JS textually — skipping comments and string/template literals,
    requiring an identifier boundary before `server`, accepting the quoted argument only when `,`
    or `)` follows — and catches the common case, including in a deployment's own `app/code`
    modules, which is why it is worth running before any DB change. On any other `/` it abandons
    the line. That is the whole design: deciding whether a bare `/` is division or a regex is a
    full lexical-goal problem (ASI, brace grammar, postfix operators and the enclosing construct
    all feed it), three successive token heuristics were each defeated by valid JavaScript, and
    since this raises a FATAL error the only acceptable failure direction is a miss — a false
    positive would abort a customer's upgrade over correct code. Making it exact would mean adding
    a standards-compliant JS parser to the Python dependencies; deliberately not done (simplicity
    over completeness), with exactness provided by the executing test above.
- **Why the admin list is not derived from the running toolbox.** Python cannot import the toolbox JS
  or enumerate an upstream MCP server's tools without starting it, so manifests stay the source of
  truth and drift is prevented on both sides instead.

---

## 2026-06-04 — Tools and skills are opt-in (disabled by default), resolved through the one config service

- **The problem.** Registered tools and synced skills were available to every agent_view by default: the gate (`isToolEnabled`, `get_enabled_skills`) treated "no `is_enabled` row" as enabled. Enabling was therefore opt-out — you could only ever *remove* access. For a fleet whose tools carry credentials (BI warehouse, Magento prod, NAV ERP, WMS), dropping in a module silently granted the whole fleet access.
- **Inverted the default to opt-in.** A tool/skill is available only when its resolved `is_enabled` value is `1`. Missing → disabled; `1` → enabled; `0` → disabled (explicit; an agent_view/workspace `0` overrides an inherited `1`, since the scope chain is merged into one value before the check).
- **Resolution goes through the single config service in each language — no bespoke gate logic.** `isToolEnabled` (JS) previously hand-indexed the merged DB map, ignoring ENV and `config.json`. It now resolves `tools/<name>/is_enabled` through `config-loader.js`'s standard **ENV → DB → config.json** fallback (`resolveConfigValue`), the mirror of Python's `ScopedConfigService`. Python gating likewise routes through `ScopedConfigService` (the source of truth for the merged scope chain): tool reads use `.get()` (snake_case names → dash-safe, full fallback); skill reads use the service's merged `.overrides` because skill names may contain dashes (e.g. `git-workflow`) and `.get()` path-normalizes dashes.
- **Two-tier defaults via `config.json`.** Because the gate now consults `config.json`, a module can ship a first-class tool **enabled by default**: `core/config.json` enables `email_send`, `browser`, `schedule_followup`; `jira/config.json` enables the `jira` group. Credentialed/customer adapter tools (BI/Magento/NAV/WMS) ship no default → stay **opt-in** (off until an explicit DB `1`). A DB `0` at any scope still disables a first-class tool. This keeps least privilege exactly where the issue wanted it (credentialed datastores) while the agent's built-in toolkit works out of the box. Skills carry no per-skill `config.json` default, so they remain fully opt-in.
- **~~Known asymmetry~~ — FIXED (see the 2026-08-04 entry).** The JS runtime gate merges every module's `config.json` by literal path, so it honors `tools/<name>/is_enabled` defaults; Python's `ScopedConfigService` resolved `config.json` by parsing the path's module, so it did **not** resolve these module-agnostic defaults. The original note called this invisible in practice *because* the first-class tools were not declared in any `module.json` — a premise that stopped holding the moment they were declared, at which point Python would have rendered every live tool as **disabled**. `ScopedConfigService._resolve_config_json` now falls back to `_resolve_literal_config_json`, mirroring `loadConfigDefaults()` including its last-module-wins order; global tool-name uniqueness plus owned `config.json` defaults (both `module:validate`-enforced) make that order unobservable.
- **No DB migration, no backfill.** Stored `0`/`1` values keep their meaning — only the interpretation of "missing" flips. A backfill would also be awkward: skills aren't in `skill_registry` until `skill:sync`, which runs *after* data patches in `setup:upgrade`.
- **Enablement UX.** `tool:enable`/`skill:enable` already write `1`/`0` at a scope (unchanged). Added two admin TUI screens (Textual `SelectionList`): a **Skills** screen (single alphabetical checkbox list) and a **Tools** screen (manifest-declared tools grouped into sections per **toolset**, each with a "toggle all"). A tool's toolset is a required `toolset` field on its `module.json` declaration — enforced by `agento module:validate`, `bin/test`, and `setup:upgrade` (which validates enabled manifests up front and aborts before any DB change); the Tools screen falls back to the module name only as a defensive runtime default. It's purely a grouping label with no resolution effect — `module_loader` carries it through as a raw key. Checkboxes reflect the resolved value at the selected scope; inherited enables are annotated. Convention-registered JS tools (email/browser/jira/schedule) are now declared and listed too — see the 2026-08-04 entry.

---

## 2026-06-02 — Per-run HOME credential materialization and microsecond token LRU

- **The bug.** A shared build HOME could carry provider credential files while token selection happened per run. With mixed same-priority tokens, one run could select an API-key token but inherit an OAuth file copied from the build, or parallel manual runs could collide on the same artifacts path.
- **Per-run HOME is authoritative.** The consumer and `agento run` now use the same run-preparation path: select one token, copy the current build into a unique artifacts directory, recreate provider-declared persistent-state symlinks, write only the selected token's credentials into that artifacts HOME, and spawn the agent with `HOME=<artifacts_dir>`.
- **Framework stays agent-agnostic.** The framework asks each provider's `ConfigWriter` for persistent paths, credentials, and runtime env. Claude-specific OAuth cleanup and Codex-specific auth file formats remain in their modules.
- **LRU fairness under concurrency.** `oauth_token.used_at` is `DATETIME(6)` and selection stamps with `UTC_TIMESTAMP(6)`. The existing order (`priority`, never-used first, oldest `used_at`, `id`) remains, but microsecond precision plus retry-on-contention avoids ten rapid `FOR UPDATE SKIP LOCKED` claims collapsing onto one low-id token.
- **Build credentials kept only for compatibility.** Runtime correctness no longer depends on credential files in build dirs; each run re-materializes the selected token into its own HOME.

---

## 2026-05-14 — `AGENTO_*` prefix for cron container env vars

- **The bug.** Cron entrypoint persists docker env to `/opt/cron-agent/env` through a prefix whitelist (`MYSQL_|TZ=|DISABLE_LLM=|PROVIDER=|CONFIG__|AGENTO_|PYTHONPATH=`) because the consumer is launched via `su - agent`, which wipes the parent environment. `CONSUMER_MAX_WORKERS`, `CONSUMER_POLL_INTERVAL`, and `JOB_TIMEOUT_SECONDS` weren't in the list, so values set in `docker-compose.override.yml` silently fell back to hardcoded defaults — the consumer always ran with `max_workers=1, poll_interval=5.0s, job_timeout=1200s`.
- **The convention.** Any env var the cron/consumer needs from `docker-compose` must use the `AGENTO_*` prefix. Five offending vars were renamed: `AGENTO_CONSUMER_MAX_WORKERS`, `AGENTO_CONSUMER_POLL_INTERVAL`, `AGENTO_JOB_TIMEOUT_SECONDS`, plus `AGENTO_AGENT_USAGE_WINDOW_HOURS` and `AGENTO_AGENT_ROTATION_INTERVAL_HOURS` (the latter two were surfaced by the regression guard — `AGENT_` doesn't match `AGENTO_`). Externally-conventional vars stay as-is (`MYSQL_*` for the driver, `TZ` for libc, `CONFIG__*` for the public config-fallback contract, `PYTHONPATH`, `PROVIDER`, `DISABLE_LLM`).
- **No backwards-compat aliases.** The old names never reached the consumer in production (that *is* the bug), so nobody is relying on them working — there's no behavior to preserve. Operators upgrading past this release who had the old names set in their compose override should rename them.
- **Regression test pins the contract.** `tests/unit/framework/test_entrypoint_env_whitelist.py` reads the regex out of `entrypoint.sh` and walks every `from_env()` classmethod under `src/agento/framework/` (via AST), collecting each literal var name passed to `os.environ.get(...)`; mismatches fail CI with a clear message. Stops the next framework knob from quietly drifting away from the whitelist — and surfaced the latent `AGENT_*` bug in `agent_manager/config.py` the moment the guard was broadened past `consumer_config.py`.
- **Alternatives considered.** (a) Extend the regex with `CONSUMER_|JOB_TIMEOUT_SECONDS=`: rejected — every new knob would need an entrypoint edit, and the bug class returns. (b) Drop the whitelist and pass everything: rejected — `source $ENV_FILE` breaks on values with quotes/newlines (some `CONFIG__*` values do) and would clobber `PATH`/`HOME`/`SHELL`. (c) Rename `MYSQL_*`/`CONFIG__*`/`TZ` to `AGENTO_*` too: rejected — those are externally-mandated conventions; renaming would break drivers, libc, and the public config contract.
- **Doc.** [docs/architecture/cron-env-contract.md](docs/architecture/cron-env-contract.md) explains the contract end-to-end and lists the migration for operators.

---

## 2026-04-23 — Token pool per provider, LRU selection

- **Dropped `oauth_token.is_primary`.** The sticky "global primary per provider" flag made a single token carry all traffic until manually rotated; a second license sat idle. Multi-license accounts (which are the common case for paid subscriptions) only benefited after the operator ran `token:set`, and even then the sticky winner kept being picked. The flag conflated two concepts — "preferred" and "active" — and couldn't express per-agent_view preferences either.
- **Added `status`, `error_msg`, `expires_at`, `used_at` to `oauth_token`.** `status` (enum `ok|error`) is flipped to `error` automatically when the runner reports an auth failure (Claude's `AuthenticationError` phrases; new Codex stderr patterns). `error_msg` stores the reason for the operator. `expires_at` is pulled from the credentials payload on `token:register` / `token:refresh` so expiry filtering happens in SQL without decrypting every row. `used_at` is the bump timestamp for LRU selection.
- **`select_token(provider)` replaces the rotator.** `SELECT ... ORDER BY used_at IS NULL DESC, used_at ASC LIMIT 1 FOR UPDATE` claims the least-recently-used healthy token, then stamps `used_at = UTC_TIMESTAMP()`; the in-line commit makes the bump visible before the runner executes. Why delete `rotator.py` entirely: selection *is* rotation now — the pool fans out naturally without a separate nightly job. **(Correction — see 2026-07-30 entry above:** this originally used `FOR UPDATE SKIP LOCKED` and claimed it prevented two concurrent workers from picking the same row; `SKIP LOCKED` over the filesorted pool scan actually double-claimed, so the claim now uses a blocking `FOR UPDATE`.)
- **Required `agent_view/provider`; no more primary-token fallback.** Consumer raises with an actionable message when `agent_view/provider` is unset. The fallback ("infer provider from whichever token happens to be primary") hid misconfigurations and made agent_view bindings non-authoritative. Operators must now explicitly bind provider per agent_view / workspace / global — which matches every other scoped config in the framework.
- **Auto-detect auth failures → `status='error'` + `TokenAuthFailedEvent`.** When the runner raises `AuthenticationError`, the consumer marks the offending token and dispatches an event for observers. The job still flows through the existing retry pipeline; the next attempt picks a different healthy token via LRU. Dead-letter only when no healthy token remains or retries are exhausted.
- **New CLI: `token:mark-error <id> "<msg>"`, `token:reset <id>`.** Manual levers for operators who want to quarantine or recover a token without re-authenticating. Removed `token:set` (and `rotate`) — they have no meaning under LRU.
- **Alternatives considered.** (a) Keep `is_primary` as a "must-use-first" hint and add LRU as tiebreaker: rejected — still leaves the sticky-preference muddle, just reordered. (b) Per-agent_view token binding table: deferred — buys fine-grained control but costs a new table, a UI surface, and a migration for something no one asked for yet. The per-provider pool handles the real use case (multiple licenses on one account).

---

## 2026-03-31 — UTC-everywhere for timestamps, scoped timezone config

- **All Python datetime calls use `datetime.now(timezone.utc)`, never naive `datetime.now()`.** Bug: container TZ (`Europe/Warsaw`, UTC+2) caused `scheduled_after` on the retry path to be stored 2h ahead of MySQL `NOW()` (UTC). Retries fired late by the timezone offset. Root cause: Python `datetime.now()` returns container-local time, but MySQL TIMESTAMP columns and `NOW()` operate in the server's session timezone (UTC by default).
- **MySQL session pinned to UTC via `init_command="SET time_zone = '+00:00'"`.** Every PyMySQL connection now explicitly sets the session timezone. This makes `NOW()`, `CURRENT_TIMESTAMP`, and parameter interpretation all UTC — regardless of MySQL server config or container TZ. Belt-and-suspenders with the Python fix: even if someone adds a naive datetime, MySQL interprets it as UTC.
- **`core/timezone` scoped config** (IANA timezone string, default `"UTC"`). Lives in the `core` module (`system.json` + `config.json`). Supports per-agent_view / per-workspace / global override via the standard 3-level fallback. Purpose: when code needs to reason in local time (idempotency key bucketing, future display/reporting), it reads the configured timezone rather than relying on container TZ. The `get_timezone()` helper in `config_resolver.py` resolves the value and returns a `ZoneInfo` instance.
- **Why not a `general` module (Magento convention)?** Magento groups locale under `general/locale/timezone`. We chose `core/timezone` because the `core` module already exists and owns framework-level settings. Adding a `general` module for a single field is premature. Can be moved later if `general` gains more fields.
- **Docker TZ env var unchanged.** After the fix, `TZ=Europe/Warsaw` in docker-compose only affects container-level concerns (cron daemon scheduling, log timestamps). All DB-facing code is timezone-independent.
- **Idempotency key bucketing stays UTC for now.** `build_idempotency_key()` uses `datetime.now(timezone.utc)` which is correct and consistent. Timezone-aware bucketing (so "9am Warsaw" cron keys align with configured timezone) deferred until the channel has access to scoped config at publish time.

---

## 2026-03-30 — Unified CLI and two installation paths for beta

- **Python CLI replaces bash wrapper.** The `bin/agento` bash script (821 lines) delegated host-side commands and proxied to Docker for runtime commands. This made `uv tool install agento` useless. Now all commands live in the Python package (`src/agento/framework/cli/` subpackage). `bin/agento` is a thin `exec uv run agento "$@"` wrapper, kept for backward compat.
- **CLI subpackage architecture.** `cli.py` (920 lines) split into `cli/__init__.py` (dispatch), `cli/runtime.py`, `cli/token.py`, `cli/config.py`, `cli/module.py` plus new standalone commands: `cli/doctor.py`, `cli/init.py`, `cli/compose.py`. Two-tier design: standalone commands (doctor, init, up/down) skip `bootstrap()` and heavy imports; runtime commands (consumer, config, token) require DB.
- **Single installation path:** Docker Compose (`agento init` → `agento up`). Includes MySQL in Compose. Zero external deps beyond Docker.
- **Deferred: GHCR images, PyPI publishing.** Beta is not the right time to add release infrastructure. Contracts still stabilizing (Phase 9.5 runtime, upcoming API/admin/broker). Pre-built images add tagging, compatibility, rollback, and pipeline maintenance overhead with little beta-stage payoff.
- **Golden path:** `uv tool install agento → agento init → agento up → agento setup:upgrade`.

---

## 2026-03-25 — Crypt module: adapter pattern for encryption backends

- **Encryptor protocol + get/set_encryptor accessor** in `framework/encryptor.py`. Callers use `get_encryptor().encrypt(value)` instead of importing `crypto.encrypt` directly. Why: the flat utility has no extension point — swapping to vault/KMS later would mean rewriting all callers.
- **AesCbcBackend as default** — wraps existing `crypto.py` (AES-256-CBC, `AGENTO_ENCRYPTION_KEY` env var). Zero behavioral change; purely structural refactor.
- **Fallback to crypto.py** when no backend is registered (tests, scripts that don't boot the module system). Backward compatible.
- **Node.js `crypto.js` unchanged** — toolbox only decrypts. Backend selection happens on the Python side (config:set writes encrypted values). Vault backend for JS is out of scope until needed.
- **Deferred: key rotation, vault adapters, config-driven backend selection.** The adapter pattern makes these additive changes.

---

## 2026-03-24 — Concurrent worker pool with per-run isolation (Phase 9.5)

- **ThreadPoolExecutor, not subprocess pool.** Threads are lightweight coordinators; the actual work runs in CLI subprocesses (Claude Code / Codex). Isolation comes from per-run directories, not process-level separation. Simpler shutdown semantics than subprocess supervision.
- **Per-run directory** `{AGENTO_WORKSPACE_DIR}/{workspace}/{agent_view}/runs/{job_id}/`: each job gets freshly generated `.claude.json`, `.mcp.json`, `.codex/config.toml`, `AGENTS.md`, `SOUL.md`. Eliminates the shared `.claude.json` corruption that forced `concurrency=1`. Directory is cleaned up after job completion.
- **`job.priority`** 0-100 (default 50), stamped at publish time from scoped config path `agent_view/scheduling/priority`. Dequeue uses `ORDER BY priority DESC, created_at ASC`. Changing config does not retroactively affect queued jobs — consistent with Jira's approach to sprint priorities.
- **`AGENTO_CONSUMER_MAX_WORKERS`** env var (default 10, per-run isolation makes it safe). Originally `CONSUMER_MAX_WORKERS`; renamed in 2026-05 (see [Cron container env prefix convention](#2026-05-14--agento_-prefix-for-cron-container-env-vars) below) because the entrypoint's whitelist dropped non-`AGENTO_*` framework knobs.
- **`agent_view_worker.py` deprecated** — the subprocess-per-agent_view model from Phase 9 is replaced by generic worker slots in the consumer's thread pool.

---

## 2026-03-24 — Deterministic ingress routing (Phase 10)

- **Router protocol + registry** — same extensibility pattern as channels, workflows, and runners. Modules declare routers in `di.json` with an `order` field.
- **All routers run, first match wins** — not short-circuit. Running all detects ambiguity (multiple routers claim the same identity). Ambiguity is logged + evented but the first match (by order) still wins. Why: debugging routing issues requires seeing what all routers think, not just the winner.
- **IdentityRouter as default** (`ingress_identity` table): maps `(identity_type, identity_value)` → `agent_view_id`. Simple, explicit, managed via `ingress:bind` CLI. No ML/semantic routing in MVP.
- **Routing at publish time** — `agent_view_id` is stamped on the job when published, not re-evaluated per execution attempt. Why: routing rules may change between attempts, and a job should stick to its resolved profile for consistency. Also avoids DB calls during the hot execution path.

---

## 2026-03-24 — Per-agent_view instruction files via observer (agent_view module)

- **Observer on `agento_agent_view_run_started`** (now `agent_view_run_start_before`) writes `AGENTS.md`, `SOUL.md`, and `CLAUDE.md` into the run directory. Why observer, not inline in consumer: Magento spirit — modules extend framework behavior via events. Keeps the consumer lean.
- **Content from `core_config_data`** with scoped fallback: `agent_view/instructions/agents_md` and `agent_view/instructions/soul_md`. Follows the same `agent_view/*` config path convention as `agent_view/model`, `agent_view/mcp/servers`, etc.
- **Fallback to workspace file on disk** if no DB value exists. This preserves backward compatibility — existing deployments with `workspace/AGENTS.md` keep working without DB config.

---

## 2026-03-23 — Config fallback simplified to 3 levels

- **Removed field schema defaults** (the 4th fallback level from `system.json` / `module.json` `"default"` keys).
- **3-level fallback**: ENV → DB → `config.json`. Default values live in `config.json` only.
- **Why**: Two places for defaults (schema + config.json) caused confusion and duplication. Every schema default was already mirrored in config.json. Single source of truth is simpler.
- **system.json retains field type/label**: still used for type coercion, encryption detection, and UI. Just no `"default"` key.
- **Migration**: Any `"default"` in schema that wasn't in config.json must be moved there first.

---

## 2026-03-20 — Toolbox JS into modules

- **Toolbox framework at `src/agento/toolbox/`**: peer to `src/agento/framework/` and `src/agento/modules/`. Contains server, config-loader, shared libs, and adapter registry.
- **Module-specific JS in `<module>/toolbox/`**: mirrors `<module>/src/` for Python. Jira MCP tools and REST routes live in `src/agento/modules/jira/toolbox/`. One module = complete package (Python + JS).
- **Core module for generic tools**: `src/agento/modules/core/` provides email, schedule, browser — framework services shipped as a module, like Magento's `Magento_Core`. Keeps toolbox framework lean (only server + adapters).
- **Convention-based discovery** over `di.json` declaration: any `.js` file in `toolbox/` is auto-discovered and must export `register(server, context)`. Simpler for module authors. Explicit is better, but the directory convention is explicit enough — you opt-in by creating the file.
- **Context injection** (`{ log, db, playwright, app }`): module JS tools receive framework utilities via function parameter, not global imports. Makes tools testable in isolation, decouples from file paths.
- **Single `package.json`** in `src/agento/toolbox/`: all JS shares one dependency set. Module JS files use framework-provided libraries. Splitting per module would be premature complexity.
- **`/workspace/tmp` mounted as single volume**: modules create subdirs at runtime (`jira/`, `screenshots/`). Not hardcoded per-module mounts.
- **Adapter registry** (`adapters/index.js`): extracted from old `tools/index.js`. Config-driven tools (mysql, mssql, opensearch) handled separately from module JS tools.

---

## 2026-03-18 — Module system (Phase 0)

- **Magento model**: one module = complete package (channel + workflows + tools + config). Not split by type.
- **Core vs user modules**: core in `src/agento/modules/` (git-tracked), user in `app/code/` (gitignored). User can override core.
- **`module.json` as single manifest**: declares everything a module provides. No multiple XML files like Magento.
- **`importlib` + `sys.path` for loading**: dotted paths in `module.json` relative to module `src/`. No `__init__.py` required in module root.
- **`entry_points["agento.modules"]`** for pip-installable third-party modules (same mechanism as pytest plugins).
- **`BlankWorkflow` in framework, not a module**: utility/testing workflow not tied to any integration.
- **Lazy fallback in registries**: old hardcoded behavior if bootstrap hasn't run (test isolation). Remove once all consumers use bootstrap.
- **Standard PyPA `src/` layout** at repo root (not nested under `docker/`) to enable `pip install agento`.

---

## 2026-03-05 — Agent runner (agent_manager)

- **Runner ABC shared by Claude and Codex**: common interface, replay support, e2e tests. Not two separate unrelated runners.
- **20-minute subprocess timeout**: agents have variable duration, but unbounded is too risky. 20 min covers longest observed tasks.
- **No timeout on ClaudeRunner for job execution**: agent tasks can legitimately run 30s–10min. Premature kill leaves Jira in inconsistent state. Timeout enforcement deferred.
