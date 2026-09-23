# Workspace Build Commands

Materialized workspace builds — pre-built config directories per agent_view. The consumer copies from build dirs into per-job run dirs at execution time, eliminating per-job config generation.

## `workspace:build`

### Usage

```bash
# Build for a specific agent_view
agento workspace:build --agent-view developer

# Build for all active agent_views
agento workspace:build --all

# Force rebuild even if the existing build's checksum matches
agento workspace:build --all --force
```

Shortcut: `wo:bu`

### Options

| Flag | Required | Description |
|------|----------|-------------|
| `--agent-view <code>` | One of these | Agent view code to build for |
| `--all` | is required | Build for all active agent_views |
| `--force` | No | Rebuild even if a matching build already exists. Retires the prior same-checksum build (deletes its on-disk directory, marks the DB row `failed`) and produces a fresh `build_id`. Use when something outside the checksum inputs has changed (manual theme edits, external template updates, a file accidentally removed from disk). |

The `--agent-view` and `--all` flags are mutually exclusive; `--force` can be combined with either.

### What It Does

1. Resolves the agent_view and its scoped config overrides (agent_view → workspace → global fallback)
2. Fetches enabled skills for this agent_view (soft dependency on `skill` module)
3. Computes a SHA-256 checksum over sorted config values + skill checksums + per-source build strategies
4. **Legacy key pruning** — deletes any private key an older version left in this view's build generations. It runs **before** the skip check, so an unchanged build still gets cleaned. For a repository-wide sweep see [`workspace:ssh-purge`](#workspacessh-purge)
5. **Skip check** — if a `ready` build with the same agent_view + checksum exists **and its `build_dir` is intact on disk**, skips the rebuild and updates the `current` symlink if needed. `--force` bypasses this check; a missing on-disk directory also forces a rebuild (the stale DB row is retired first).
6. Creates a build directory and materializes each source using its configured strategy (copy or symlink):
   - **Theme** — merges `workspace/theme/`, `workspace/theme/_{ws_code}/`, `workspace/theme/_{ws_code}/_{av_code}/` via the manifest algorithm
   - **Agent CLI configs** — `.claude.json`, `.mcp.json`, `.codex/config.toml`, `.pi/` (via the harness's `WorkspaceAdapter`)
   - **Instruction files** — `AGENTS.md`, `SOUL.md` from DB if set (otherwise keeps theme files), `CLAUDE.md` always written
   - **Module workspaces** — each enabled module's `workspace/` with the same `_` prefix scoping convention
   - **Skills** — `.claude/skills/<name>/` directories (SKILL.md + companion files like `references/`, `scripts/`); a `.agents/skills` symlink pointing to `.claude/skills` is also created for Codex compatibility
   - **No SSH files at all** — the build carries **no** `.ssh/` directory. The non-secret files (`id_rsa.pub`, `config`, `known_hosts`) are written **per run** into the run HOME, and the **private key is never written anywhere on disk**; it is resolved per run and loaded into a per-run `ssh-agent` (see [identity docs](../config/identity.md))
   - **Git commit author identity** — when `agent_view/identity/git_author_name` / `git_author_email` are set, writes `.gitconfig` `[user]` (git-quoted, injection-safe) so the agent's commits are authored correctly; the email must be a verified email on the target Bitbucket/Git account for commits to link (see [identity docs](../config/identity.md))
   - **Persistent-state symlinks** — each registered agent module declares relative-to-HOME paths that must survive rebuilds (e.g. `.claude/projects` for session history). Framework symlinks each to a per-agent_view `state/` directory outside the build dir.
7. Marks the build as `ready` in the `workspace_build` table
8. Updates the `current` symlink to point to the new build
9. **Retention GC** — prunes oldest `builds/N/` directories beyond `workspace_build/retention/max_builds` (default 10). The current build is always kept. See [Retention](#retention).

Theme and module workspace directories use the **`_` prefix convention**: directories starting with `_` are scope boundaries (never copied as content), while all other files and directories are content. See [workspace architecture](../architecture/workspace.md) for full details and examples.

### Build Strategy

Three config keys control how file sources are materialized — globally (not per agent_view):

| Config path | Values | Default | Applies to |
|---|---|---|---|
| `workspace_build/strategy/theme` | `copy` \| `symlink` | `copy` | `workspace/theme/` layers |
| `workspace_build/strategy/modules` | `copy` \| `symlink` | `copy` | Each module's `workspace/` |
| `workspace_build/strategy/skills` | `copy` \| `symlink` | `copy` | Skill directories |

```bash
agento config:set workspace_build/strategy/theme symlink    # symlink theme (saves disk for large repos)
agento config:set workspace_build/strategy/modules symlink
agento config:set workspace_build/strategy/skills symlink
```

`symlink` creates one symlink per resolved file/directory entry. `copy` produces fully independent real files. Both strategies produce identical logical trees — only on-disk representation differs. Changing any strategy key changes the checksum, triggering a new build.

**Migration note:** The former single key `workspace_build/building_strategy` is automatically migrated to `workspace_build/strategy/modules` by `agento setup:upgrade`. No manual action required.

Config files are only generated when the corresponding `agent_view/*` config paths exist in scoped overrides. If no `agent_view/*` paths are set for an agent_view, those files are skipped.

### Build Directory Layout

```
/workspace/build/{workspace_code}/{agent_view_code}/
├── state/                  # PERSISTENT per agent_view — never wipe'd on rebuild
│   ├── .claude/
│   │   ├── projects/       # Claude Code session history (.jsonl)
│   │   └── todos/
│   └── .codex/
│       ├── sessions/
│       └── history.jsonl
├── builds/
│   ├── 1/                  # IMMUTABLE build template copied into per-run HOME
│   │   │                                   # NO .ssh/ here at all — the non-secret SSH
│   │   │                                   # files are written per run into the run HOME
│   │   ├── .gitconfig                      # optional — [user] name/email (copied per-run, not symlinked)
│   │   ├── .claude.json
│   │   ├── .mcp.json
│   │   ├── .claude/
│   │   │   ├── settings.json
│   │   │   ├── projects -> ../../../state/.claude/projects     # SYMLINK
│   │   │   └── todos    -> ../../../state/.claude/todos        # SYMLINK
│   │   ├── .codex/config.toml
│   │   ├── .codex/sessions -> ../../../state/.codex/sessions   # SYMLINK
│   │   ├── CLAUDE.md
│   │   ├── AGENTS.md
│   │   ├── SOUL.md
│   │   ├── .claude/skills/
│   │   │   └── my_skill/
│   │   └── .agents/
│   │       └── skills -> ../.claude/skills  # symlink (Codex compatibility)
│   └── 2/                  # Build ID 2 (newer)
└── current -> builds/2     # Symlink to latest ready build
```

**Key design**: `builds/<id>/` is ephemeral (rebuilt on every `workspace:build`), `state/` is persistent (accumulates sessions, caches). Symlinks from the build dir into `state/` bridge them so that session history survives rebuilds while the current config always reflects the latest build.

### Retention

Old build directories are garbage-collected after each build. Controlled by a single global config path:

| Config path | Type | Default | Notes |
|---|---|---|---|
| `workspace_build/retention/max_builds` | integer | 10 | Number of newest builds to keep per agent_view. The current build is always kept. |

```bash
agento config:set workspace_build/retention/max_builds 20
```

This setting is global only (`showInDefault=true`, `showInWorkspace=false`, `showInAgentView=false`).

### Events

| Event | Dispatched when |
|-------|-----------------|
| `WorkspaceBuildStartedEvent` | Build begins (status → building) |
| `WorkspaceBuildCompletedEvent` | Build completes or is skipped (status → ready) |
| `WorkspaceBuildFailedEvent` | Build fails (status → failed) |

## `workspace:build-status`

### Usage

```bash
# Show recent builds for all agent_views
agento workspace:build-status

# Filter by agent_view
agento workspace:build-status --agent-view developer
```

Shortcut: `wo:bs`

### Options

| Flag | Required | Description |
|------|----------|-------------|
| `--agent-view <code>` | No | Filter by agent view code |

### Output

Shows the 20 most recent builds:

```
   ID  Agent View            Checksum        Status      Current  Created At
------------------------------------------------------------------------------------------
    2  dev_01             a6ce0904e451  ready              *  2026-04-08 12:55:47
    1  agent01               cb5933ea9df5  ready              *  2026-04-08 12:55:47
```

The `*` in the Current column indicates the build that the `current` symlink points to.

## `workspace:ssh-purge`

> **Deprecated — scheduled for removal in v0.17 or later.** This command exists only for
> deployments upgrading ACROSS the release that stopped writing the key to disk. On this release no
> run writes a key file, so once a deployment has upgraded and swept once, the command has nothing
> left to find. Tracked in [ROADMAP.md](../../ROADMAP.md) under "Deprecation removals due in v0.17
> or later".

One-time admin cleanup. Earlier versions wrote `agent_view/identity/ssh_private_key` into
`<build_dir>/.ssh/id_rsa`; build retention kept every generation and artifact retention kept every
copy a run made. Those files stay readable until something deletes them, and a build only cleans its
own view's tree — so this command sweeps the whole `workspace/` mount.

### Usage

```bash
# List what would be deleted, delete nothing
agento workspace:ssh-purge --dry-run

# List, then ask for confirmation before deleting
agento workspace:ssh-purge
```

Shortcut: `wo:sp`.

### Options

| Flag | Description |
|---|---|
| `--dry-run` | Print the list and exit without deleting anything |

There is no `--yes`: the scan reaches `workspace/artifacts/`, where agents clone repositories, so a
human always sees the full list before anything is unlinked.

### Detection scope — stated, not implied

A file is reported when it starts with a complete PEM private-key envelope
(`-----BEGIN … PRIVATE KEY-----`), or when it is named `id_rsa`. A key that is embedded mid-file,
re-encoded, compressed or split is **not** detected. This clears what this system itself wrote; it is
**not an exfiltration detector**. A checked-out source file that merely quotes a PEM header is not
offered for deletion, and symlinks are never followed.

### Exit status — it fails closed

The command exits **non-zero** whenever a path could not be scanned or a listed key could not be
deleted. The two are reported differently: paths it could not read are listed under
`Could NOT scan (a key may still be present):` before the hit list, while a key it found but could
not unlink is reported inline as `Could not delete <path>: <error>` as it goes. Both end in the same
non-zero exit naming the number of unresolved paths. An incomplete scan never reports
`No private keys found`; `--dry-run` and a cancelled confirmation also exit non-zero when the
scan was incomplete — a scan that could not read everything is not entitled to claim absence.

A **zero** exit means the scan was complete and every deletion it was asked to make succeeded.
It does **not** by itself assert a clean tree: `--dry-run` deletes nothing by design, and a
cancelled confirmation leaves the listed keys in place — both exit zero after a complete scan,
because neither was asked to delete anything. Only a completed, confirmed run exiting zero means
no key this command can see is left.

### Where it belongs in an upgrade — the order matters

**Deploy first, then purge.** Purging before the deploy is undone by the next build: the old code
re-materialized `agent_view/identity/ssh_private_key` into `<build_dir>/.ssh/id_rsa`. On this release
no run writes a key file, so the consumer may keep running while you purge — which matters, because
the host CLI reaches this command by exec'ing into the **running** cron container, so a stopped cron
means a stopped purge.

```bash
agento upgrade                      # new code + images, containers back up (migrations included)
bin/agento workspace:ssh-purge      # proxied into the running cron container; safe to re-run
```

Then **rotate** every affected `agent_view/identity/ssh_private_key` — deleting a copy cannot un-leak
a key that was readable by every agent_view while it sat on the shared mount.

**Install the new public key on the remote FIRST.** That, not any local file, is what authenticates:
the run forces `-o IdentitiesOnly=no` in `GIT_SSH_COMMAND` (`framework/ssh_identity.py:350`), so git
offers the agent-loaded identity regardless of what `id_rsa.pub` says. Then set both config values —
the public half is a separate value the run writes (with `config` and `known_hosts`) into the run HOME,
so leaving it stale means the files describing the identity no longer match the identity in use:

```bash
cat new_id_rsa | bin/agento config:set agent_view/identity/ssh_private_key \
  --scope agent_view --scope-id <id>
cat new_id_rsa.pub | bin/agento config:set agent_view/identity/ssh_public_key \
  --scope agent_view --scope-id <id>
```

No rebuild is needed for the identity itself: the build writes no SSH files, and the run resolves both
halves from config every time. A `workspace:build` is only worth running here to prune legacy keys from
that view's older build generations.

**Purging a deployment still on the old code** (you want the keys gone before you can deploy): the
consumer must be stopped, and then the proxy has no container to exec into — so build the **new** image
first and run the sweep in a one-off container from it, on the same mounts:

```bash
agento upgrade --no-restart         # builds the new images; starts nothing
cd docker
docker compose stop cron           # the old consumer must not be running
docker compose run --rm --no-deps --entrypoint /opt/cron-agent/run.sh cron workspace:ssh-purge
docker compose up -d               # now the release goes live (setup:upgrade runs on start)
```

`--no-restart` is what makes this work: the command only exists in **this** release, so the sweep has to
run from the new image, and nothing may start the old consumer first. `--entrypoint` **replaces** the
service entrypoint — so the consumer is never started, and none of the entrypoint's own checks run
either; the container is just the CLI (`/opt/cron-agent/run.sh` is the wrapper the proxy execs) on the
same `/workspace` mount. It needs no `--local`: the in-container CLI never proxies. `--no-deps` is
enough — the CLI bootstraps without a reachable database and the purge only touches the filesystem
(verified: the command runs and scans with `MYSQL_PORT` pointed at nothing). `docker compose run` gives
it a TTY, so the confirmation prompt works. Re-run the purge after the containers come up anyway.

`workspace:build` also prunes legacy private keys from its own view's build generations — including
when the build is skipped as unchanged — so a rebuild of every view is the narrower alternative.

## How It Integrates

At job execution time, the consumer:

1. Calls `get_current_build_dir()` to find the `current` symlink target for this agent_view
2. Copies / symlinks build artifacts into the per-job artifacts directory (`workspace/artifacts/<ws>/<av>/<job_id>/`)
3. Recreates provider-declared persistent-state symlinks and materializes the selected token's credentials into that artifacts directory
4. Makes `<artifacts_dir>/.ssh/` a real 0700 directory and deletes any `id_rsa` found there — **always**, whether or not this run gets an identity — then materializes the **non-secret** SSH files (`id_rsa.pub`, `config`, `known_hosts`) into it; the private key is loaded into a per-run `ssh-agent` instead (see [identity docs](../config/identity.md))
5. Sets `HOME=<artifacts_dir>` and `cwd=<artifacts_dir>` on the agent subprocess
6. The agent CLI executes
7. The artifacts directory is **retained** after job completion (it holds the run's artifacts for review) and is wiped only when the same run dir is reused; `state/` is never touched

Run `workspace:build` after changing agent_view config, skills, or instruction files to ensure the next job picks up the changes. **Not for the SSH identity** — the build writes no SSH files and the run resolves the identity from config on every job, so a rotation takes effect without a rebuild (a rebuild only prunes legacy keys from that view's older generations).

## Database

The `workspace_build` table tracks build history:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INT | Auto-increment primary key |
| `agent_view_id` | INT | Foreign key to `agent_view` |
| `build_dir` | VARCHAR(500) | Full filesystem path to build directory |
| `checksum` | VARCHAR(64) | SHA-256 of config + skill checksums |
| `status` | ENUM | `building`, `ready`, `failed` |
| `created_at` | TIMESTAMP | Build creation time |

Source: `src/agento/modules/workspace_build/src/builder.py`, `src/agento/modules/workspace_build/src/commands/`
