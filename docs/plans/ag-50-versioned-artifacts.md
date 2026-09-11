# Plan: AG-50 — versioned_folders → serving, drafts on disk, operator CLI

**Branch:** `feature/ag-50-versioned-folders` · **Status:** planning · **Effort:** 16–18 dev-days, phases 0–6

> **Naming is decided:** `versioned_artifacts` (module) / `versioned_artifact` (switch) /
> `versioned_artifact_*` (tools) / `artifact:*` (CLI). See [The naming decision](#the-naming-decision)
> for what that costs the collaboration layer.

---

## Context

The module exists only as uncommitted working-tree state. `git ls-tree main` returns nothing for it and
released 0.16.0 shipped without it, so **every name, table, config key and store path is free to change
today and only today**. After the branch ships, a rename orphans every `tools/<name>/is_enabled` row in
`core_config_data` — and because enablement is keyed by tool name alone, an orphaned row fails *closed*:
the tool is silently disabled, with no error anywhere.

**What changes.** The agent stops editing files through MCP tools and starts editing a plain directory
with its native file tools. MCP handles lifecycle only. Saved versions become real directories under
`storage/`, served read-only over HTTP from their own container on `127.0.0.1`.

**What does not change.** The store stays toolbox-only. The publish compare-and-swap stays. Per-tool
`is_enabled` scoped to `agent_view` remains the only gate — no approval flag, no `is_served` column, no
ACL, no login panel.

---

## Does this collide with the agent-collaboration layer?

Two collisions survive scrutiny. Everything else is clear.

**1. The rename spends the word the destination needs.** The destination's own primitives are
`publish_artifact` / `list_artifacts` / `get_artifact`. `artifact` already names the per-job scratch
directory (`framework/artifacts_dir.py`) in agent-facing text the model reads at tool-selection time
(`core/module.json:14`, `jira/toolbox/jira.js:135`). `module_validator.py` compares exact strings
(`validate_tool_namespace:196-221`), so `versioned_artifact_list` and a future `list_artifacts` would
coexist with no error — the failure is a model or an operator picking the wrong one of two
near-identical tools, and nothing fails loudly. See [The naming fork](#the-naming-fork).

**2. Deleting every read path leaves a reviewer agent with no route to a version.** The settled plan
deleted `read_file` and `list_files` alongside `apply_changes`, and promised "create_draft returns a
filesystem path". But no mount exists that both the toolbox and the agent can reach: the store is bound
into the toolbox only (`templates/docker-compose.yml:59`, `docker-compose.dev.yml:61`), and the serving
container is `127.0.0.1`-only and off `agento-net`. A second agent in a second job could not read a
published version *at all* — not by path, not by tool, not by HTTP. Deleting the readers also kills
`resolveSelector` / `treeishFor` / `treeFiles`, after which `diff` is the only thing in the module that
can produce bytes, and it needs an open draft.

Both are fixed by one change, described below: a `materialize` tool.

**Not collisions** — do not churn these: the CAS publish, the minimal metadata table, the rejected
`is_served` gate, the decision to leave the other 62 tool names alone. `jobs.parent_id` and
`reference_id` need nothing from this plan.

---

## The naming decision

**Decided: `versioned_artifacts`.** The owner reserved the word for this module deliberately
("*artifacts zarezerwujmy dla tych właśnie artefaktów*"), and that stands.

A `versioned_site` alternative was considered and rejected. It would have freed the whole `artifact_*`
namespace for the collaboration layer's `publish_artifact` / `list_artifacts` / `get_artifact`, at the
same cost. It was rejected because "site" is wrong the moment this module holds something a browser does
not render.

**What this costs, recorded so nobody rediscovers it:** `module_validator.py` compares exact tool-name
strings (`validate_tool_namespace:196-221`), so a future `list_artifacts` would coexist with
`versioned_artifact_list` and raise nothing. The failure mode is a model or an operator picking the wrong
one of two near-identical tools, and it fails silently. The open follow-up is whether the collaboration
layer reuses this module rather than adding a second artifact namespace — see
[Reuse by the collaboration layer](#reuse-by-the-collaboration-layer).

---

## Reuse by the collaboration layer — decided

**The collaboration layer reuses this module by REFERENCE, not by row. There is no `type` column.**

A `type: http|handoff` discriminator was considered and rejected. The test applied: a discriminator
earns its keep when every branch on it is a *leaf* — a small local difference in *how* one operation
behaves, with the surrounding contract unchanged. It is a merge in disguise when branching on it changes
*which operations exist*, *who may call them*, or *what the core noun means*. Five of six branches are
not leaves:

| Dimension | Leaf? |
|---|---|
| retention default | leaf — and not even a divergence: phase 3 prunes only *materialized directories*, never history, so `keep_versions=0` is already right for both |
| creation authority | **no** — changes who may call. `init` is allow-list-exempt (`service.js:129-138`) precisely because an administrator runs it before an entry could exist |
| reachability | **no** — `allowed_artifacts` fails closed and blocks the *author*, not only the reader |
| serving | **no, and unreachable** — the check would have to run in a container with no DB, no `env_file`, off `agento-net`, all deliberate |
| `current` / `publish` CAS | **no** — under a handoff, `get_current` actively hides v1, and the CAS refuses the second writer with no merge path (merge/rebase/checkout are banned from the contract and a test asserts it) |

**The integration point is a string.** A collaboration record may carry `{artifact_code, version_id}`.
The *reading* agent calls `versioned_artifact_materialize` itself, gated by its own `is_enabled`. No
cross-module import (the repo has zero today), no `sequence` entry, both modules independently
disableable.

That also buys the thing the merge was reaching for: **the website case gets a review loop for free** —
agent A drafts a version, agent B reviews it, then publish, on the site artifact itself, with no handoff
artifact and no type field involved.

**The collaboration layer is a separate module with its own top-level store root, and its records are
write-once.** "Plan v2" is a new record whose parent is the first, not a new version of the same one.
Both A's plan and B's feedback must stay readable and citable forever; `current` exists to make one
version supersede all others.

**Asymmetry of being wrong:** if append-only proves too thin, ~150 lines are lost and the store can be
re-homed behind the same tool names. If the type column proves wrong, the creation path, the
authorization model and the HTTP exposure of two different things are merged inside one module whose
serving layer cannot tell them apart — discovered when a case record quoting customer data answers at
`http://127.0.0.1:8307/<case-code>/`.

**Deferred deliberately, do not design now:** the wildcard `allowed_artifacts`, an agent-callable
creation tool, a case-root authorization predicate, `job.parent_id`, case retention or deletion, a
version-to-version `diff`, promoting the engine into shared framework JS. Each is cheap when the
collaboration layer is actually built and a guess today. One thing to record so it is not rediscovered:
that layer must not ship bare `publish_artifact` / `list_artifacts` / `get_artifact`, because
`module_validator.py` compares exact strings and two near-identical names fail by silent wrong-selection.

---

## Design changes forced by the collision review

### The desk / store split

There is no mount that both the toolbox and the sandbox can reach for the store, and creating one is the
hardest item here to take back — it would falsify `CLAUDE.md:54-56` and
`docs/architecture/zero-trust.md:84-91`, and hand the agent a Git worktree including its `.git` file.

So: **the store stays authoritative and toolbox-only; the agent works on a disposable copy on its own
desk.** The desk is `/workspace/artifacts/<ws>/<av>/<job_id>/versioned-artifacts/<code>/<id>/` — the only
path both containers already mount read-write (`toolbox :56`, `cron :94`), and whose value
`src/agento/toolbox/server.js:44-53` already derives.

**No tool anywhere accepts a filesystem path as an argument.** The desk path is derived server-side from
the session context, which removes the containment question entirely rather than answering it.

### `materialize` replaces the deleted readers

`versioned_artifact_materialize(artifact_code, draft_id? | version_id?) → {path}` copies a draft or a version
onto the calling job's desk. `apply_changes` still dies (it is the writer the desk model supersedes);
the *readers* come back in a better shape — files the reading agent opens with its native tools, which
is also the natural shape of the destination's `get_artifact`.

It is not extra work: it is reused by phase 3 to build the published tree, and the selector helpers it
needs already exist.

### Drop the `user` parameter

`user: z.string().email().max(255)` (`versioned-folders.js:11-14`) is an unverified, self-declared
address, while the trustworthy identity — `jobId`, `agentViewId` — is already in `register(context)` and
already written to its own audit columns on the same row. An `agent_view` whose SOUL.md carries no email
cannot call any tool in the module: zod rejects before the handler runs.

Drop the field and the `service.forActor(args.user)` call. **Also delete `forActor` itself** — `cli.js`
uses the `actor` *constructor* option (`cli.js:21-22`), not `forActor`, so after this it has no
production caller. Two tests reference it (`service.test.js:138-142`, `tools.test.js:192`) and go with
it. **Leave the identical parameter alone** in `schedule.js`, `browser.js`, `jira.js` and `email.js` —
those interpolate the raw value into operator log lines, where `.email()` is real log-forging defence.

### Retention is opt-in

`serving/keep_versions` defaults to **0 = keep everything**. Pruning is right for a live site and is data
loss for a record. This revises the earlier "keep the last N" default; the "never prune the target of
`current`" rule stays and is enforced in code.

### `agent_view_id INT UNSIGNED`

`sql/001` declares `agent_view_id INT NULL`; `agent_view.id` is `INT UNSIGNED`
(`framework/sql/014_workspace_agent_view.sql`). One word, free now, an `ALTER MODIFY` later.

---

## Definition of Done

1. Ten tools: one switch + nine members, every `is_enabled` default `"0"`, every member `requires` the switch.
2. No tool schema contains a `user` key or any path-shaped parameter.
3. The agent can create a draft, edit the returned directory with plain shell commands, save a version,
   keep editing, and save again — two distinct versions from one draft.
4. Every saved version exists as a real directory under the published root; `current` is a **relative**
   symlink swapped with `fs.rename`.
5. `curl http://127.0.0.1:<port>/<code>/` serves the published version from the Mac; the same request
   from the LAN address is refused.
6. `agento artifact:list` and `agento artifact:publish` work from the host.
7. `AGENTO_E2E=1 bin/test` green, `module:validate` green.
8. No doc in the repo still claims a behaviour this work changed.

---

## Phase 0 — Close the Jira proxy credential hole · 0.5 d · fully independent

Ships on its own commit, before anything else. Touches no `versioned_*` file.

1. `jira/toolbox/jira-proxy.js:26` — delete the `req.body.jira_host ||` override so the line reads
   `const host = config.host;`. No in-repo caller passes it (`onboarding.py` writes `jira/jira_host` via
   `config_set`; `jira_periodic_tasks` passes only `admin_auth`). Leave the `auth_user`/`auth_token`
   overrides — bringing your own credential to the *configured* host is harmless.
2. **Scope addition, named as such:** removing `jira_host` alone does **not** close the
   credential-exfiltration class. `:52` does `fetch(host + path)`, so `path: '@evil.example/rest/api/3/myself'`
   yields a valid URL whose host is `evil.example`, and the `Authorization: Basic` header follows it.
   After the `ALLOWED_METHODS` check, refuse unless `typeof path === 'string' && path.startsWith('/') && !path.startsWith('//')`
   → 400 `{error: 'path must be a host-relative path'}`.
3. Log one WARN when `req.body.jira_host` is present, so an out-of-repo caller learns why it was ignored.
4. `jira/src/toolbox_client.py` — remove the `jira_host` keyword and its payload branch.

**Tests:** a request naming another host reaches only the configured host; `@evil.example/...` and
`//evil.example/x` are both 400; the normal path still works.
**Done when:** the proxy cannot be made to send the token anywhere but `config.host`.

---

## Phase 1 — Rename, schema fixes, storage directories · 1.5 d · depends on nothing

One mechanical sweep, done once while nothing has shipped. After it the module behaves exactly as before
under new names.

1. `git mv` the trees: `modules/versioned_folders` → `modules/versioned_artifacts`;
   `toolbox/tests/versioned-folders` → `versioned-artifacts`; `tests/unit/modules/versioned_folders` →
   `versioned_artifacts`; `toolbox/versioned-folders.js` → `versioned-artifacts.js`;
   `workspace/versioned-folders.md` → `versioned-artifacts.md`; `docs/modules/versioned-folders.md` →
   `versioned-artifacts.md`; `docs/cli/versioned-folder-init.md` → `docs/cli/artifact-init.md`.
2. Sweep identifiers across `src/ tests/ docs/ docker/`: module `versioned_folders` → `versioned_artifacts`
   (also the config-path prefix); tool prefix `versioned_folder_` → `versioned_artifact_`; master switch →
   **singular** `versioned_artifact`; table `versioned_folder_audit` → `versioned_artifact_audit`; audit OPS
   strings (`service.js:38-45`); `GIT_OPERATION_FAILED` → `STORAGE_OPERATION_FAILED` (`errors.js:9`,
   `service.js asVfError`, `cli.js` fallback, `git-backend.js` call sites).
   **Not a blind sed:** `module.json`'s `toolset` field currently holds the same literal as both the
   module name and the switch. Map it explicitly — `toolset: "versioned_artifacts"` (module name, per
   `module_validator.py:404-409` and `module_scaffold.py:78`), switch tool name `versioned_artifact`.
3. Rename the domain noun: `folder_code` → `artifact_code` everywhere; `FOLDER_CODE_RE` → `ARTIFACT_CODE_RE`,
   `validateFolderCode` → `validateArtifactCode`, `folderRoot` → `artifactRoot`; config `allowed_folders` →
   `allowed_artifacts`; `FOLDER_NOT_FOUND` / `FOLDER_ACCESS_DENIED` / `FOLDER_TOO_LARGE` → `ARTIFACT_*`.
4. CLI: `di.json` `versioned-folder:init` → `artifact:init`; `TOOLBOX_CLI` → `/app/modules/core/versioned_artifacts/toolbox/cli.js`.
   In `framework/cli/__init__.py:48-50` set `_LOCAL_MODULE_COMMANDS = {"artifact:init", "artifact:list", "artifact:publish"}`
   (the last two arrive in phase 4; between now and then `agento artifact:list` fails as an unknown command
   rather than being proxied — acceptable, but know it).
   **Also change `VersionedFolderInitCommand.shortcut`** (`src/commands/init.py:126`), which returns
   `"vf:init"`. If it survives the sweep, `agento vf:init` is no longer in the local set and
   `_should_proxy` (`framework/cli/__init__.py:68`) sends it into the cron container, which has neither
   the docker socket nor the compose file — it breaks with no readable error.
   `tests/unit/modules/.../test_init_command.py:45-46` asserts the membership being changed.
5. SQL: rename `sql/001_versioned_folder_audit.sql` → `001_versioned_artifact_audit.sql`, rename the table
   inside, change `agent_view_id INT NULL` → `INT UNSIGNED NULL`. Add `sql/002_versioned_artifact.sql`:
   `artifact_code VARCHAR(64) PRIMARY KEY, title VARCHAR(255) NULL, owner VARCHAR(255) NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`.
   No `current_version` column — the ref CAS is the only serialization point. No authorization
   columns. **No `type` column** — see [Reuse by the collaboration layer](#reuse-by-the-collaboration-layer-—-decided);
   should that decision ever reverse, `sql/003 ADD COLUMN type … DEFAULT 'http'` backfills every
   existing row correctly, which is what `schema_migration` is for.
6. Storage paths: `storage_root` → `/srv/versioned-artifacts/store`; change the toolbox bind in **both**
   `templates/docker-compose.yml:59` and `docker-compose.dev.yml:61` to
   `../storage/versioned-artifacts:/srv/versioned-artifacts`.
   **Move any existing store**, or every initialized folder is orphaned one level up:
   `mv storage/versioned-folders storage/versioned-artifacts/store`. Record it next to the schema recovery note.
   **`storage/versioned-artifacts/` is this module's store root alone.** A future collaboration
   store gets its own top-level directory, never a sibling inside this tree — that separation is
   what makes an `is_served` flag and a serve-time type check unnecessary, because unserved bytes
   are physically outside the served tree.
   (`published_root` and its validation both land in phase 3 — splitting a config key from the code that
   validates it means shipping a field `createService` ignores.)
7. Add `ensure_storage_dirs(project_dir)` to `framework/cli/_provisioning.py`: `mkdir(parents=True, exist_ok=True)`
   for `storage/versioned-artifacts/store` and `.../published`. Call it from `install.py _scaffold`
   (replacing the `storage/versioned-folders` literal at `:87`) **and** from `upgrade.py` immediately
   before `regenerate_compose(project_root)` at `:229`. This is the answer to the open item: creating
   the directories from the host CLI is what gets the ownership right on Linux — they inherit the
   invoking user, which is `HOST_UID`, which is `agent` inside the container.
8. `framework/docker/toolbox/entrypoint.sh` — replace the `/srv/versioned-folders` block with
   `mkdir -p /srv/versioned-artifacts/store /srv/versioned-artifacts/published` and a **non-recursive** chown of
   each to `agent`, still `|| true`. This is the braces for the dev-compose path where nobody ran the CLI.
9. Test scaffolding that names the module by path: `toolbox/tests/tool-declaration.test.js` (import,
   registry, file list, config fixture) and the `DIR` constant in the renamed `layering.test.js`.
10. Pass `--template=` to `git init --bare` (`git-backend.js:290`). Every artifact currently carries
    14 unused hook samples (~26 KiB, 14 inodes) while `HARDEN` already sets `core.hooksPath=/dev/null`
    (`git-backend.js:15`), so they can never run. One argument, one assertion that a fresh artifact
    has no `hooks/*.sample`. Right either way; awkward once a production store exists.
11. Prose: `CLAUDE.md`, `docs/architecture/zero-trust.md:78-96`, **`docs/architecture/containers.md`**,
    **`framework/docker/toolbox/Dockerfile:30-32`** (its comment names the store path),
    `docs/modules/README.md`, `docs/cli/README.md`, `docs/tools/README.md`, `ROADMAP.md`,
    **`AGENTS.md`** and **`README.md`**. `grep -rln 'versioned_folder\|versioned-folder\|VersionedFolder' .`
    is the authoritative list — 43 files.

**Tests:** `module:validate`; `tool-declaration.test.js`; the whole renamed vitest suite unchanged in
behaviour; `test_install.py` and `test_upgrade.py` each create both storage directories;
`test_provisioning.py` asserts the new toolbox bind.
**Done when:** `AGENTO_E2E=1 bin/test` green and `grep -rn 'versioned_folder\|versioned-folder' . --exclude-dir=.git`
returns nothing outside `DECISIONS.md`'s historical entries.
**Risk:** a missed string in a JSON manifest silently disables a tool — which is why `module:validate`
and `tool-declaration.test.js` both gate the phase.
**Dev DB recovery:** `DROP TABLE versioned_folder_audit; DELETE FROM schema_migration WHERE module='versioned_folders';`

---

## Phase 2 — Desk copies, `materialize`, `save_version` · 4.5 d · needs phase 1

The heaviest and most delicate phase. It rewrites the draft state machine, which the regression suite
covers most heavily. Budget honestly: this is not a 3-day phase.

1. Settle the surface in `module.json` / `config.json` — `versioned_artifact` (switch) plus
   `_list`, `_get_current`, `_list_versions`, `_create_draft`, `_materialize`, `_save_version`, `_diff`,
   `_publish`, `_discard_draft`.
2. Delete `apply_changes`, `list_files`, `read_file` from the manifests and `versioned-artifacts.js`. In
   `git-backend.js` delete `applyChanges` (`:525-634`) and `restoreWorktree` (`:516-523`).
   **Keep** `requireOneSource`, `treeishFor`, `resolveSelector`, `treeFiles` (`:212-250`) — `materialize`
   is their new caller.
3. Drop `user` from `folderArg`; call the service directly in the `run` helper; delete `forActor`
   (`service.js:257`) and its two tests.
4. Desk derivation in `versioned-artifacts.js`: `deskDir(artifactsDir, artifactCode, id)`, with `artifactsDir`
   destructured from the MCP session context. If it is missing or ends in `_fallback`, every
   desk-touching tool returns `WORKSPACE_UNAVAILABLE` rather than writing to a shared fallback directory.
5. Add `materialize(storageRoot, artifactCode, selector, destDir)` to `git-backend.js`. Two paths,
   deliberately not abstracted: a draft is `fs.cp(draftDir, destDir, {recursive:true, dereference:false})`
   filtering the top-level `.git`; a version is `git archive --format=tar --output=<tmp> <treeish>` then
   `tar -xf <tmp> -C <destDir>` — two sequential `execFile` calls through `git-exec.js`, no pipes, no new
   dependency. Verify `tar --version` exists in the toolbox image once. Route both through
   `resolveSelector` so an unknown version still yields `VERSION_NOT_FOUND`.
6. Wire `versioned_artifact_materialize` → `{path}`: rm -rf and recreate the derived desk dir, then call the
   backend.
7. `create_draft` creates the draft in the store as today, then materializes it, returning
   `{draft_id, base_version, path}`.
8. `finalize` → `save_version(artifact_code, draft_id, description)`. Split today's finalize (`:697-760`)
   into (a) mirror the desk into the draft worktree — delete worktree files absent from the desk, copy
   the rest, enforcing `max_files` / `max_file_size` / `max_total_size` and rejecting symlinks through
   the existing `safeResolve` walk; (b) `git add -A` + commit with the existing trailers; (c)
   `createVersionRef`. **Remove the teardown** — the draft stays open, so a second `save_version` yields
   a second version. `discard_draft` becomes the only way to close a draft.
9. **Replace the crash-window idempotency the `finalized` marker provided.** `finalizedRef`
   (`git-backend.js:34, :166`) is what makes a finalize interrupted between `git commit` and
   `createVersionRef` converge on retry, and `service.js:207-217` does exactly one automatic retry
   because of it. Removing the marker without a replacement means a process death in that window yields
   two commits and two versions for one piece of work. Decide the replacement before writing the code —
   the cheapest is to keep writing the marker but treat it as "a version was created from this tree",
   checked and cleared at the start of the next `save_version`.
10. Follow the lifecycle change through `draftState` / `requireOpenDraft` / `recoverDraft` /
    `reconcileDrafts` / `teardownDraft`, and `service.prepareDraft`'s `allowMarkers` (`:122-125`).
    **`missing` and `incomplete` stay** — `reconcileDrafts` (`:417-424`) tears down exactly the
    `incomplete` drafts, and that is the startup sweep the module advertises and that
    `regressions.test.js:259` covers. Only `finalized` changes meaning.
11. `versioned_artifact_list` returns the artifact codes this scope may use (`allowed_artifacts` ∩ `be.listFolders`),
    each with `current_version` **and its open drafts**. Without the drafts the recovery story does not
    close: a retried attempt starts with a wiped desk and a fresh agent that does not know the
    `draft_id`. Read `title`/`owner` from the `versioned_artifact` table, falling back to the store listing
    for artifacts with no row yet (phase 4 writes the rows).
12. Rewrite `workspace/versioned-artifacts.md` for the new flow: create_draft → edit the returned path with
    your normal file tools → save_version → publish → discard_draft; materialize to read a version or to
    recover after a retry.

**Tests:** the registered set is exactly ten names; no schema has a `user` key or a path-shaped key;
extend the git-leak guard to include `tag` **and** to scan tool *descriptions*, not just names and
parameter keys. New `materialize.test.js`: a draft materializes without `.git`; a version materializes
byte-identically; unknown version → `VERSION_NOT_FOUND`; both selectors named → refused; missing
`artifactsDir` → `WORKSPACE_UNAVAILABLE`; materialize over an existing desk replaces it.
`git-backend-drafts.test.js`: two `save_version`s on one draft yield two version ids and `diff` works
between them; a desk deletion propagates; a desk symlink is refused; each limit is enforced at save.
`service.test.js`: `allowed_artifacts` still denies before any store access and still audits the denial.
**Done when:** `npm test` green, and a manual toolbox session can create a draft, edit the directory
with plain shell commands, save twice and diff — with no tool call that names a path.
**Risk:** run `regressions.test.js` and the injected-git-failure suite first after every change.

---

## Phase 3 — Published tree, relative `current`, opt-in retention · 4 d · needs 1–2

1. Add `published_root` **and its validation** (reuse `asStorageRoot`, `service.js:30`), plus
   `serving/keep_versions` (int, default 0) and `serving/public_base_url` (default `http://localhost:8080`)
   to `config.json` and `system.json`.
2. Layout, chosen so the URL path maps 1:1: `<published_root>/<artifact_code>/v/<version_id>/…`, and
   `<published_root>/<artifact_code>/current` as a **relative** symlink to `v/<version_id>`.
3. In `save_version`, after the version ref exists, materialize into the published tree. A
   materialization failure **logs and returns `preview_path: null`** — it must never fail a save that
   already succeeded in the store.
4. In `publish`, after the CAS succeeds: re-materialize the target if retention pruned it, then
   `fs.symlink('v/<id>', current.tmp-<rand>)` + `fs.rename(tmp, current)`. **Never `mv`** — a direct test
   proved an `mv` swap leaves `current` on the old version and drops a stray `.tmpnew` link inside it.
5. `pruneVersions(publishedRoot, artifactCode, keep, currentTarget)`: with `keep > 0` delete materialized
   directories beyond the newest `keep`, **always excluding what `current` resolves to**. `keep === 0`
   prunes nothing. Called at the end of `save_version` and `publish`, inside the folder lock. The prune
   list is computed from the published tree only, never from a caller-supplied value.
6. `list_versions` returns `preview_path` — the relative `/<artifact_code>/v/<version_id>/` when the
   directory exists, `null` when pruned. A pruned version stays fully readable through `materialize`;
   only the browser preview is gone.
7. Build the absolute URL in exactly **one** helper, used by `publish` and `get_current` only.
   `list_versions` stays relative — a wrong absolute URL in a message to a human is worse than no URL.
   Never set `CONFIG__` for this in compose: ENV beats DB and would kill `config:set`.

**Tests** (new `published-tree.test.js`): save materializes identical bytes; `current` is relative and
its realpath stays inside the root; no `current.tmp-*` survives a publish and `current` resolves to the
new version (the direct regression for the `mv` bug); `keep_versions=0` prunes nothing; `keep_versions=2`
keeps the two newest **and** an older current target; a pruned version reports `preview_path: null` and
is still materializable; publishing a pruned version re-materializes first; a materialization failure
returns `preview_path: null` without throwing. `service.test.js`: the CAS is unchanged.
**Done when:** `ls -l storage/versioned-artifacts/published/<code>/` shows `current -> v/v-…` after a
publish, with the previous version still present under `keep_versions=0`.

---

## Phase 4 — `artifact:list`, `artifact:publish`, and the metadata row · 1.5 d · needs 1–3

1. Teach `toolbox/cli.js` an `--op` flag (`init | list | publish`), keeping the contract exactly: one
   JSON object on stdout, `{error_code, message}` on failure, never a stack trace, exit 1 on error.
2. `service.init` INSERTs the `versioned_artifact` row inside the same `audited()` call. A failed INSERT must
   not fail the init — log and continue, the way `recordAudit` degrades.
3. `--title` / `--owner` on `src/commands/init.py`, passed through the payload.
4. `src/commands/list.py` — artifact_code, title, owner, created_at, current version, preview URL, one row
   per artifact. Copy `_last_json_object` / `_sanitized` into `src/commands/_toolbox.py` rather than
   importing across command files.
5. `src/commands/publish.py` — `agento artifact:publish <artifact_code> <version_id> --expected <version_id>`.
   Same CAS, no new mechanism, **no approval flag**: publication is a human running this or explicitly
   asking the agent.
6. Register both in `di.json` `commands`. They are already in `_LOCAL_MODULE_COMMANDS` from phase 1, so
   they run on the host and exec into the toolbox rather than being proxied into cron (which sees
   neither the host source nor the storage volume).

**Tests:** `cli.test.js` — `--op list` returns rows; `--op publish` performs the CAS and returns
`CURRENT_VERSION_CHANGED` on a stale `--expected`; an unknown `--op` returns an error code, never a
throw. `test_list_command.py` — a valid-JSON non-object reply prints an error, not a traceback; a Node
stack on stderr is replaced by the fixed fallback line. `test_publish_command.py` — a stale `--expected`
exits non-zero with the code visible; a **missing** `--expected` is refused by argparse (never defaulted
— a default silently disables the concurrency guard).

---

## Phase 5 — The `artifacts` serving container · 1.5 d · needs phase 3

1. `src/agento/modules/versioned_artifacts/server/artifacts-server.js` — under `server/`, **not** `toolbox/`,
   because `src/agento/toolbox/config-loader.js:424-433` imports every `.js` in a module's `toolbox/`
   into the secrets container.
2. ~40 lines, zero new npm dependencies: an index at `/` listing the directories under the root; two
   ordered routes — `/:artifact_code/v/:version_id/*` with `version_id` matched against `VERSION_ID_RE`, and
   `/:artifact_code/*` rewritten onto `<artifact_code>/current/<rest>`; **our own realpath containment check**
   (proven necessary — `send` does zero `lstat`/`realpath` and served a file through a symlink pointing
   outside the root); then `express.static(root, {dotfiles: 'deny'})`. A pruned version falls through to
   the ordinary 404 — no branch. Root paths are read from env with defaults; note in a comment that
   since the service carries no `environment:` key, **the defaults are the only reachable values** and
   the env reads exist for the vitest harness.
3. Retry once on `EINVAL`/`ESTALE` before responding — the first request after a symlink swap returned
   500 through the macOS VirtioFS mount and the identical retry returned 200.
4. Add the `artifacts` service to **both** `templates/docker-compose.yml` and `docker-compose.dev.yml`, above
   the mysql block, using the existing image with **no `build:` block**. (The reason is not the drift
   guard — `test_provisioning.py:511-521` only compares the three known services and would ignore a new
   one. The real reason: `build_base_images` has a hardcoded three-entry `specs` list and would never
   build the tag.)
5. Keys, exactly: `command: ["node", "/app/modules/core/versioned_artifacts/server/artifacts-server.js"]` (it must
   sit under `/app` or Node cannot resolve express — proven: `ERR_MODULE_NOT_FOUND` at `/srv/app/`);
   two read-only volumes; `ports: - "127.0.0.1:${AGENTO_ARTIFACTS_PORT:-8080}:8080"`; an explicit healthcheck
   probing `127.0.0.1:8080/` (the baked `HEALTHCHECK` at `Dockerfile:73-74` probes 3001 and reports the
   container unhealthy while it serves fine — proven live); `restart: unless-stopped`;
   `security_opt: - no-new-privileges:true`.
   **The two files differ in the modules mount source** — the template uses
   `../.venv/lib/python{{ python_version }}/site-packages/agento/modules`, the dev file uses
   `../src/agento/modules` (`:57`). The provisioning test reads only the template, so the dev file needs
   a manual diff at review.
6. A comment block directly above it in both files: the absence of `networks:`, `env_file:` and
   `environment:` is **deliberate and must stay**. One `networks:` line added for consistency puts every
   artifact on `agento-net`, where every agent in every agent_view can read every artifact over plain HTTP with
   `allowed_artifacts` bypassed for reads — silently, with no audit row.
   Third sentence for the same block: the service mounts `storage/versioned-artifacts/published`
   **exactly**, never the store root, and no other module writes into that tree. `docker-compose.override.yml`
   is user-owned and Compose *appends* volumes, so this mount is the enforcement that replaces the
   rejected `is_served` gate. Assert in `test_provisioning.py` that the volume source ends in `/published`.
7. On this Mac set `AGENTO_ARTIFACTS_PORT=8307` in `docker/.env` next to `MYSQL_PORT=3307`: five agento
   stacks run here at once and a bare 8080 collides.

**Tests** (new `artifacts-server.test.js`): a symlink pointing at `/etc/passwd` is refused; a dotfile never
returns its content; `/` lists the codes; unknown code and pruned version both 404; `/<code>/` serves
`current` and `/<code>/v/<id>/` serves a non-current version; a `stat` that throws `EINVAL` once then
succeeds yields 200. `layering.test.js` — `server/artifacts-server.js` imports neither `service.js` nor
`git-backend.js` and spawns no process (needs a **second directory constant**: `layering.test.js:10`
pins `DIR` to `toolbox/` and readdirs only that). `test_provisioning.py` — the rendered `artifacts` service
declares no `networks:`, no `env_file:`, no `environment:`, binds only `127.0.0.1`, carries its own
healthcheck, and has no `build:` block.
**Done when:** `curl http://127.0.0.1:8307/<code>/` returns the published version, `curl http://<LAN>:8307/`
is refused, and `docker compose ps` shows the service healthy.

**Accepted deviation, to be recorded in DECISIONS.md:** the service is added unconditionally, because
`regenerate_compose` has no per-module service mechanism. A deployment with the module disabled still
runs a container bound to loopback serving an empty tree. This is in tension with "every module must be
safely disableable"; the precedent is the existing unconditional storage bind mount.

---

## Phase 6 — Docs, recorded gaps, end-to-end · 3 d · needs 1–5

Nothing depends on this, so it absorbs schedule pressure — **except the ROADMAP entries, which should
land even if the rest slips**, since they are the only record of three framework gaps.

1. Rewrite `docs/modules/versioned-artifacts.md`: the ten tools, the desk/store split, the published tree and
   the `current` symlink, retention defaulting to keep-everything, the serving container and its
   deliberate isolation, and a corrected "Out of scope" — **the current text at `:193-195` still says HTTP
   serving is out of scope.**
2. `docs/architecture/zero-trust.md:78-96` — "the store is mounted into the toolbox only" stays **true**
   and must be restated precisely, alongside the new fact that a materialized copy lives on the job's own
   desk under `workspace/artifacts`, which every agent run shares.
3. Update the `CLAUDE.md` bullet: new name, the desk-copy model, no path parameters, the serving
   container off `agento-net`.
4. `docs/cli/artifact-list.md`, `docs/cli/artifact-publish.md`, refresh `artifact-init.md`, index all three.
5. Record three gaps in `ROADMAP.md`, each as one sentence of consequence rather than a fix:
   - **(a)** append to the existing "Ungated Toolbox REST endpoints" entry that agent_view identity is
     self-asserted — `src/agento/toolbox/server.js:95` reads `agent_view_id` from a query parameter
     supplied by a config file living in the job's own writable artifacts directory — so it is the
     ceiling on every per-agent_view gate, `allowed_artifacts` included. The fix is session-bound identity in
     the framework, never a module-local caller check.
   - **(b)** there is no per-job isolation: `consumer.py:204-206` runs jobs as threads in one process,
     and `run_preparation.py:153-162` puts each run's credential in the directory that is also its HOME,
     so one job can read another job's desk and credential. Delegation makes concurrent jobs the normal
     case.
   - **(c)** `schedule_agent_job` needs a `job.type` widening plus an opened `AgentType`
     (`framework/job_models.py:17-21`, `bootstrap.py:255`) and `publisher.publish()` must learn `context`
     and `parent_id` (`publisher.py:84-93` writes neither), rather than a module re-implementing
     dedupe-then-insert. **`schedule_followup` must not be widened** — its idempotency key
     `followup:{source}:{reference_id}:{minute}` (`schedule.js:91`) collapses a two-agent fan-out in the
     same minute into one job and reports success.
6. Run `AGENTO_E2E=1 bin/test`, then a live end-to-end on the dev stack: `artifact:init` → enable the toolset
   for one agent_view → a real agent run that creates a draft, edits the returned directory, saves and
   publishes → `curl` the preview URL → `artifact:list` shows the new current.

**Done when:** `AGENTO_E2E=1 bin/test` green, the live e2e completes, and
`grep -rn 'apply_changes\|read_file\|finalize\|versioned_folder' docs/ CLAUDE.md` finds nothing that
still describes removed behaviour.

---

## Effort

| Phase | Days | Ships alone? |
|---|---|---|
| 0 — Jira proxy | 0.5 | yes, independent of everything |
| 1 — rename, schema, storage dirs | 1.5 | yes |
| 2 — desk copies, materialize, save_version | 4.5 | yes (no serving yet) |
| 3 — published tree, symlink, retention | 4 | yes (nothing serving it yet) |
| 4 — operator CLI | 1.5 | yes |
| 5 — serving container | 1.5 | yes |
| 6 — docs, ROADMAP, e2e | 3 | absorbs slip |
| **Total** | **16.5** | **envelope 16–18** |

Phase 2 was costed at 3 days and is honestly 4.5: it deletes `applyChanges` and `restoreWorktree`, adds
`materialize` with two backends, adds server-side desk derivation, splits `finalize` into mirror +
commit + ref with every limit and the symlink walk re-enforced on the mirror, and rewrites the draft
state machine and its consumers — against a suite whose heaviest file (`regressions.test.js`, 683 lines
across 17 describes) is largely about exactly that machinery.

---

## Still open

1. **Abandoned drafts.** `save_version` keeps the draft open, so nothing closes a draft except
   `discard_draft`. Should a draft untouched for N days be swept at toolbox start, or is a stuck draft an
   operator-visible condition cleared by hand, like today's abandoned locks? (The existing startup sweep
   reclaims *incomplete* drafts; this is about complete but abandoned ones.) This matters more than it
   looks: the boot sweep costs ~44 ms across 1000 artifacts when clean and ~7 s with 200 open drafts,
   because `draftState` spawns three or more git processes per draft in a serial loop — and `consumer.py`
   runs up to 10 job threads in one process, so abandoned drafts are normal residue, not an edge case.

2. **For the collaboration layer, not this ticket:** does the shared case area need a filesystem store at
   all, or is a DB row with a body plus the existing desk sufficient for the MVP? This decides whether
   that module is ~150 lines or ~400. Settle it when that layer is built, not now.

---

## Recorded gaps (not fixed in this release)

- **There is no deletion path anywhere in the module** — no tool, no CLI, no service method, no history
  prune. Removing an artifact means `rm -rf` inside the toolbox with no audit row.
- **`finalize` compares the draft only to its own base, never to `current`.** Harmless with one editor
  per artifact; with two it can mint an immutable version that silently reverts a peer. The CAS guards
  only the publish step, and only as well as the `expected_current_version` the caller chooses to pass.
- **`versioned_artifact_audit` is not a message log.** It has no body column and is explicitly exempted
  from the module's "the audit sinks never hold content" claim; `description` is agent-supplied, capped
  at 255 and mirrored verbatim into `audit-fallback.log`.
- **"Materialize" means two things in this plan** — the tool that copies onto the calling job's desk, and
  writing a version directory into the served tree. They are unrelated; the desk copy comes straight from
  the store. Keep them distinct in code and docs.
