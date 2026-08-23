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
4. **Skip check** — if a `ready` build with the same agent_view + checksum exists **and its `build_dir` is intact on disk**, skips the rebuild and updates the `current` symlink if needed. `--force` bypasses this check; a missing on-disk directory also forces a rebuild (the stale DB row is retired first).
5. Creates a build directory and materializes each source using its configured strategy (copy or symlink):
   - **Theme** — merges `workspace/theme/`, `workspace/theme/_{ws_code}/`, `workspace/theme/_{ws_code}/_{av_code}/` via the manifest algorithm
   - **Agent CLI configs** — `.claude.json`, `.mcp.json`, `.codex/config.toml`, `.pi/` (via the harness's `WorkspaceAdapter`)
   - **Instruction files** — `AGENTS.md`, `SOUL.md` from DB if set (otherwise keeps theme files), `CLAUDE.md` always written
   - **Module workspaces** — each enabled module's `workspace/` with the same `_` prefix scoping convention
   - **Skills** — `.claude/skills/<name>/` directories (SKILL.md + companion files like `references/`, `scripts/`); a `.agents/skills` symlink pointing to `.claude/skills` is also created for Codex compatibility
   - **SSH identity** — decrypts `agent_view/identity/ssh_private_key`, writes it to `.ssh/id_rsa` with mode 600; also materializes `ssh_public_key`, `ssh_config`, `ssh_known_hosts` when present (see [identity docs](../config/identity.md))
   - **Git commit author identity** — when `agent_view/identity/git_author_name` / `git_author_email` are set, writes `.gitconfig` `[user]` (git-quoted, injection-safe) so the agent's commits are authored correctly; the email must be a verified email on the target Bitbucket/Git account for commits to link (see [identity docs](../config/identity.md))
   - **Persistent-state symlinks** — each registered agent module declares relative-to-HOME paths that must survive rebuilds (e.g. `.claude/projects` for session history). Framework symlinks each to a per-agent_view `state/` directory outside the build dir.
6. Marks the build as `ready` in the `workspace_build` table
7. Updates the `current` symlink to point to the new build
8. **Retention GC** — prunes oldest `builds/N/` directories beyond `workspace_build/retention/max_builds` (default 10). The current build is always kept. See [Retention](#retention).

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
│   │   ├── .ssh/id_rsa                     # 0600, decrypted from DB
│   │   ├── .ssh/id_rsa.pub
│   │   ├── .ssh/config                     # optional
│   │   ├── .ssh/known_hosts                # optional
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

## How It Integrates

At job execution time, the consumer:

1. Calls `get_current_build_dir()` to find the `current` symlink target for this agent_view
2. Copies / symlinks build artifacts into the per-job artifacts directory (`workspace/artifacts/<ws>/<av>/<job_id>/`)
3. Recreates provider-declared persistent-state symlinks and materializes the selected token's credentials into that artifacts directory
4. Sets `HOME=<artifacts_dir>` and `cwd=<artifacts_dir>` on the agent subprocess
5. The agent CLI executes
6. The artifacts directory is cleaned up after job completion; `state/` is never touched

Run `workspace:build` after changing agent_view config, skills, identity keys, or instruction files to ensure the next job picks up the changes.

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
