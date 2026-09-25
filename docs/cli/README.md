# CLI Reference

`agento` is the main CLI (like Magento's `bin/magento`). Install via `uv tool install agento-core`.

## Command Reference

| Command | Description |
|---------|-------------|
| **Project Lifecycle** | |
| `doctor` | Check system prerequisites ([details](doctor.md)) |
| `install` | Install a new project — interactive wizard ([details](install.md)) |
| `upgrade [--version X.Y.Z]` | Upgrade Docker images to match CLI version ([details](upgrade.md)) |
| `up` | Start Docker Compose runtime |
| `down` | Stop Docker Compose runtime |
| `logs [service]` | Show container logs |
| `run <agent_view_code> [prompt]` | Run the configured agent CLI — interactive without a prompt, headless with one ([details](run.md)) |
| **Setup** | |
| `setup:upgrade [--dry-run] [--skip-onboarding]` | Apply migrations, data patches, install crontab, run onboarding ([onboarding details](onboarding.md)) |
| **Modules** | |
| `module:add <name>` | Add a module ([details](modules.md)) |
| `module:list` | List installed modules |
| `module:enable <name>` | Enable a module |
| `module:disable <name>` | Disable a module |
| `module:validate [name]` | Validate module structure |
| `module:remove <name>` | Remove a module |
| **Config** | |
| `config:set <path> <value> [--scope=S] [--scope-id=N]` | Set config override in DB ([details](config.md)) |
| `config:get <path\|module>` | Get config value (exact path or module tree view) |
| `config:list [prefix]` | List config values (all scopes) |
| `config:remove <path> [--scope=S] [--scope-id=N]` | Remove config override from DB |
| `config:test <path> \| --all [--agent-view CODE]` | Test a field's live credential or connection — four states, exit 1 on failure ([details](config.md#configtest)) |
| **Credentials** (LRU pool per scope — no sticky primary; `token:*` are deprecated aliases) | |
| `credential:register <scope> <label>` | Register OAuth credential interactively ([details](credentials.md)) |
| `credential:register <scope> <label> --with-api-key` | Register API-key credential; secret read from stdin/getpass ([details](credentials.md)) |
| `credential:register <scope> <label> --with-access-token` | Register access-token; JWT read from stdin/getpass ([details](credentials.md)) |
| `credential:set-priority <id> <priority>` | Set pool selection priority (lower wins) |
| `credential:list [--all]` | List credentials with type, priority, status (+ `auto`/`operator` provenance), last_used, expires_at, refresh-lease holder |
| `credential:refresh <id>` | Re-authenticate credential (clears status=error) |
| `credential:mark-error <id> "<msg>"` | Quarantine a credential (status=error) |
| `credential:reset <id>` | Clear error status without re-auth |
| `credential:deregister <id>` | Disable credential |
| `credential:usage` | Show credential usage (incl. credential-less runs by `(harness, provider)`) |
| **Ingress** | |
| `ingress:bind <type> <value> <agent_view> [--priority N]` | Bind inbound identity to agent_view. For regex identity types (e.g. `outlook_sender`), `<value>` is a case-insensitive `fullmatch` regex and `--priority` selects the winner (higher wins; ties between different views are ambiguous). |
| `ingress:list [--type <type>] [--json]` | List all identity bindings |
| `ingress:unbind <type> <value>` | Remove identity binding |
| **Agent view identity** | |
| `agent_view:identity:show <code>` | Show the stored SSH identity (public key + fingerprint; never the private key) |
| `agent_view:identity:check <code>` | Verify the stored SSH private key parses and pairs with the stored public key (never prints the key) |
| **Tools** | |
| `tool:list [--agent-view <code>]` | List registered tools with enabled/disabled status ([details](tools.md)) |
| `tool:enable <name> [--agent-view <code>]` | Enable a tool at given scope ([details](tools.md)) |
| `tool:disable <name> [--agent-view <code>]` | Disable a tool at given scope ([details](tools.md)) |
| **Skills** | |
| `skill:sync` | Scan skills from disk and sync to registry ([details](skills.md)) |
| `skill:list [--agent-view <code>]` | List registered skills with status ([details](skills.md)) |
| `skill:enable <name> [--agent-view <code>]` | Enable a skill at given scope ([details](skills.md)) |
| `skill:disable <name> [--agent-view <code>]` | Disable a skill at given scope ([details](skills.md)) |
| **Workspace** | |
| `workspace:build --agent-view <code> \| --all` | Build materialized workspace ([details](workspace-build.md)) |
| `workspace:build-status [--agent-view <code>]` | Show workspace build history ([details](workspace-build.md)) |
| **Versioned artifacts** | |
| `artifact:init <artifact_code> [--source <dir>]` | Create a versioned artifact from a host directory; operator equivalent of `versioned_artifact_init` ([details](artifact-init.md)) |
| `artifact:list` | List the artifacts the store holds ([details](artifact-list.md)) |
| `artifact:delete <artifact_code>` | Remove an artifact, its versions and its served pages; asks first, and has no tool equivalent ([details](artifact-delete.md)) |
| `artifact:publish <artifact_code> <version_id> --expected <version_id>` | Point an artifact's current at a version ([details](artifact-publish.md)) |
| `artifact:auth <artifact_code> [--user <u>] [--pass <p>] [--disable] [--show]` | Set, rotate, show or disable an artifact's HTTP Basic auth; has no tool equivalent ([details](artifact-auth.md)) |
| **Admin** | |
| `admin` | Launch interactive TUI dashboard ([details](admin.md)) |
| `config:schema [module] [--json]` | Show config field definitions from system.json |
| `config:resolve <module> [--scope=S] [--scope-id=N] [--json]` | Resolve effective config values with source info |
| **Panel users and grants** | |
| `user:create <username> [--role R]` | Create a panel user; password from a prompt or stdin, never argv ([details](user.md)) |
| `user:list` / `user:set-role <u> <role>` / `user:activate <u>` / `user:deactivate <u>` / `user:password <u>` | Manage panel users; a role change or deactivation ends their sessions and launches ([details](user.md)) |
| `grant:add --role R (--tool T \| --operation O) (--workspace C \| --agent-view C)` | Grant a role a tool or `artifact.launch` in one scope ([details](grant.md)) |
| `grant:list [--role R]` / `grant:remove <id>` | List or remove grants; removal ends that role's launches in the scope ([details](grant.md)) |
| **Capabilities** | |
| `capability:mint --kind K --agent-view C [--transport T] [--ttl N]` | Mint a toolbox capability token; prints it on stdout ([details](capability.md)) |
| `capability:revoke` | Revoke a capability — reads the raw token from **stdin** ([details](capability.md)) |
| **Jobs** | |
| `job:list [--status S] [--source SRC] [--agent-view C] [--limit N]` | List recent jobs; surfaces failed/dead jobs with their error ([details](job-pause-resume.md)) |
| `job:pause <job_id>` | Pause a running job (SIGTERM, keep session) ([details](job-pause-resume.md)) |
| `job:resume <job_id>` | Resume a paused job (re-queue for consumer) ([details](job-pause-resume.md)) |
| **Operations** | |
| `consumer` | Start job consumer loop |
| `jira:periodic:sync` | Sync Jira recurring tasks to crontab |
| `jira:periodic:configure [--check] [--project K]... [K ...]` | Create/verify the periodic status + Frequency field and sync its options from `frequency_map` across projects (setup command; uses the Jira admin token — `jira/jira_admin_token` paired with `jira/jira_admin_user`, falling back to `jira/jira_user`). Project keys may be given positionally or via `--project`. `--check` = read-only report, exit 1 on any inconsistency or if it could not be verified |
| `publish <kind> [--agent-view C]` | Publish a job (jira-cron, jira-todo, jira-mention). Publishing is **per-view**: a deployment with no active agent_view fails with an actionable message instead of publishing on global config |
| `bitbucket:publish-comments [--agent-view C] [--top N]` | Sweep open PRs for unanswered reviewer feedback ([details](../modules/bitbucket.md)) |
| `bitbucket:publish-changes [--agent-view C] [--top N]` | Detect reviewer "changes requested" on open PRs (fast lane) ([details](../modules/bitbucket.md)) |
| `github:publish-comments [--agent-view C] [--top N]` | Sweep open PRs for unanswered reviewer feedback ([details](../modules/github.md)) |
| `github:publish-changes [--agent-view C] [--top N]` | Detect an outstanding reviewer "changes requested" on open PRs (fast lane) ([details](../modules/github.md)) |
| `exec:todo [key] [--agent-view C]` | Execute next TODO task. `--agent-view` names the acting view; it is **required** when more than one active view exists, and there is no global fallback ([details](../architecture/zero-trust.md)) |
| `replay <job_id>` | Replay a completed job |
| `e2e` | Run end-to-end tests |

## How It Works

`agento` is a Python console_script installed via `uv tool install agento-core`. Standalone commands (doctor, install, upgrade, up/down/logs) work without a database. Runtime commands (consumer, config, token) require MySQL.

For development convenience, `bin/agento` delegates to `uv run agento`.

Source: [src/agento/framework/cli/](../../src/agento/framework/cli/)
