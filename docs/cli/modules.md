# Module Commands

## module:add

Create a new module with tool definitions.

```bash
bin/agento module:add my-ecommerce \
  --description="My e-commerce platform" \
  --repo=git@github.com:org/my-ecommerce.git \
  --tool mysql:mysql_ecom_prod:"Production MySQL (read-only)" \
  --tool mysql:mysql_ecom_staging:"Staging MySQL" \
  --tool opensearch:opensearch_ecom:"Product search index"
```

### Tool Format

`--tool TYPE:NAME:DESCRIPTION`

Types: `mysql`, `mysql_root`, `mssql`, `opensearch`

The command auto-generates field schemas based on tool type:
- **mysql/mysql_root/mssql**: host, port, user, pass (obscure), database, client_connection_pool_max_per_tool
- **opensearch**: host, port, user, pass (obscure), index

Each scaffolded tool also gets `toolset` set to the module name — it is required by `module:validate` and groups the tool in the admin TUI. Edit it if you want a different grouping.

> ⚠️ **`mysql_root` grants FULL read/write** (INSERT, UPDATE, DELETE, TRUNCATE, DDL) on its database — there is no app-layer SQL guard, unlike `mysql`, which is read-only. The declared type IS the capability, so it is granted at review time and cannot be changed at runtime. `_root` is a reserved suffix: a `mysql_root` tool's name must end in it (`--tool mysql_root:mysql_sandbox_root:"…"`) and no other type may use it. `module:add` rejects either violation, so promoting a tool means renaming it — and a new name has no `is_enabled` grant to inherit. Back it with a least-privilege DB login scoped to that one database, and enable it only on agent_views that do not ingest untrusted content. See [Built-in Adapters → MySQL root](../tools/built-in-adapters.md#mysql-root-full-readwrite).

### What It Creates

```
modules/my-ecommerce/
  module.json       # Manifest with tool definitions and field schemas
  config.json       # Empty defaults (edit to add non-secret defaults)
  knowledge/
    README.md       # Placeholder
  prompts/
  skills/
```

After creating, run `workspace:build --all` to update workspace builds.

## module:list

```bash
bin/agento module:list
```

Lists all modules (core + user) in dependency order with their enabled/disabled status.

Output:
```
  ✔ core                 enabled    1.0.0    Framework core services
  ✔ crypt                enabled    1.0.0    Encryption backend
  ✔ jira                 enabled    1.0.0    Jira Cloud integration (requires: core)
  ✘ codex                disabled   1.0.0    OpenAI Codex runtime
```

## module:enable

```bash
bin/agento module:enable <name>
```

Enable a module. Stores state in `app/etc/modules.json`. After enabling, the module's CLI commands, cron jobs, config, routes, and observers are loaded on next bootstrap.

## module:disable

```bash
bin/agento module:disable <name>
```

Disable a module. When disabled, the module is not loaded — its CLI commands, cron jobs, config, routes, and observers are skipped. If another enabled module depends on the disabled one (via `sequence`), `setup:upgrade` and bootstrap will raise an error.

Modules not listed in `app/etc/modules.json` default to **enabled** (backward compatible).

## module:validate

```bash
bin/agento module:validate [name]
```

Validate module structure and manifests. Checks:
- Required fields in `module.json`
- Class paths in `di.json` and `events.json` resolve to `.py` files
- `sequence` entries reference modules that exist on disk
- Field types in `system.json` are valid
- Literal `server.tool('x', …)` calls in `toolbox/*.js` are declared in `tools[]` — a **best-effort**
  textual scan that covers the common case, including in your own `app/code` modules. It is not
  exact: a bare `/` cannot be classified as division or a regex without a real JS parser, so
  unusual code can slip past. The exact checks are
  `src/agento/toolbox/tests/tool-declaration.test.js` (it executes `register()`, so it also sees
  names computed at runtime) and the runtime `drift WARN`
- `requires` names a tool declared in the same module, is not self-referential, and forms no cycle
- Tool names are unique — within the manifest, and across modules
- A `config.json` `tools/<name>/is_enabled` default belongs to a tool this module declares

### `setup:upgrade` aborts on these too

`setup:upgrade` validates every **enabled** manifest before any DB change, so a module that
breaks one of the tool rules above blocks the upgrade (safely — nothing is migrated). Remediation
is mechanical: each error message states exactly what to add. This most often bites `app/code/`
modules that register tools without declaring them — on a deployment carrying `jira_servicedesk`,
`mobile_qa` or `qa_automation`, each needs its `tools[]` entries added before the next upgrade. So
pre-flight before upgrading:

```bash
agento module:validate          # no argument — only this form checks CROSS-module collisions
```

`agento module:validate <name>` inspects a single manifest and therefore cannot see a tool-name
collision with a *different* module (a duplicate inside the same manifest it does catch).

## module:remove

```bash
bin/agento module:remove my-ecommerce
```

Deletes `modules/my-ecommerce/`. Run `workspace:build --all` to update workspace builds.

## Workspace Integration

Module content (`knowledge/`, `prompts/`, `skills/`, `workspace/`) is compiled into per-agent_view workspace builds by `workspace:build`. After adding, removing, or modifying modules, run:

```bash
agento workspace:build --all
```

See [workspace:build](workspace-build.md) for details.
