# Tool Commands

List, enable, and disable MCP tools per agent_view using scoped config. Tools are registered via module manifests (`module.json`).

## `tool:list`

### Usage

```bash
# List all tools (global status)
agento tool:list

# List tools with status scoped to an agent_view
agento tool:list --agent-view developer
```

Shortcut: `tl:li`

### Options

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--agent-view <code>` | No | — | Show enabled/disabled status for this agent_view |

### Output

```
  jira_search                    jira                 enabled
  jira_create                    jira                 disabled
  jira_get_attachment            jira                 disabled
  slack_post                     slack                enabled
  browser                        core                 enabled
```

Columns: tool name, module name, status. Tools are **disabled by default (opt-in)** — a tool is `enabled` only when `tools/{name}/is_enabled` resolves to `1` for the scope. Adding a module grants no access until you explicitly enable its tools.

## `tool:enable`

### Usage

```bash
# Enable at default scope
agento tool:enable mysql_reporting

# Enable for a specific agent_view
agento tool:enable mysql_reporting --agent-view developer

# Enable at explicit scope
agento tool:enable mysql_reporting --scope workspace --scope-id 1
```

Shortcut: `tl:en`

> **Gate key vs. tool name.** For adapter tools (mysql/mssql/opensearch) the gate key *is* the tool name. Some JS-implemented modules gate their tools under one module key — e.g. most tools in the `jira` module are gated by `tools/jira/is_enabled`, so you enable that group with `agento tool:enable jira`. Exception: `jira_get_attachment` is individually opt-in gated as `tools/jira_get_attachment/is_enabled` (enable with `agento tool:enable jira_get_attachment`).

### Options

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `tool_name` | Yes | — | Name of the tool (must be snake_case) |
| `--agent-view <code>` | No | — | Shortcut for `--scope agent_view` (resolves code → ID) |
| `--scope` | No | `default` | Config scope: `default`, `workspace`, `agent_view` |
| `--scope-id` | No | `0` | Scope ID |

### What It Does

Sets `tools/{name}/is_enabled = 1` in scoped config (`core_config_data` table).

**Validation**: `tool_name` must be snake_case — lowercase letters, digits, and underscores only, starting with a letter (`^[a-z][a-z0-9_]*$`). Names with spaces, uppercase, or special characters are rejected.

## `tool:disable`

### Usage

```bash
agento tool:disable jira_search
agento tool:disable jira_search --agent-view developer
```

Shortcut: `tl:di`

### Options

Same as `tool:enable`.

### What It Does

Sets `tools/{name}/is_enabled = 0` in scoped config.

## How Tool Config Works

### Tool Registration

Tools are declared in each module's `module.json` under the `tools` array. At bootstrap, the framework loads all module manifests and registers their tools. `tool:list` reads from these manifests.

### Scoped Config

Enable/disable uses the standard 3-level scoped config system:

- Config path: `tools/{name}/is_enabled`
- Value: `1` (enabled) or `0` (disabled)
- Scopes: `default` → `workspace` → `agent_view` (most specific wins)

Tools are **opt-in**: the resolved value must be `1` for a tool to be available. The value is resolved by the single config service (toolbox `config-loader.js` / Python `ScopedConfigService`) with the standard fallback **ENV → DB → `config.json`**, then three-state semantics apply:

- missing (no ENV/DB/`config.json` value) → **disabled**
- `1` → enabled
- `0` → disabled (explicit; an `agent_view`/`workspace` `0` overrides an inherited `1`)

This least-privilege default means a newly added module's tools — including DB tools that carry credentials — are unavailable until an operator enables them. Enable broadly at `default`, then narrow per `workspace`/`agent_view`, or enable only where needed. The admin TUI **Tools** screen (`agento admin`) offers a checkbox view of all tools grouped into sections by toolset (each with a "toggle all") for a chosen scope. A tool's toolset is its required `toolset` field in `module.json` (checked by `agento module:validate`; the screen falls back to the module name only if a value is absent).

**First-class (built-in) tools default-on.** Because the gate consults `config.json`, a module may ship a tool enabled by default. The framework's built-in tools do this — `core/config.json` sets `tools/email_send/is_enabled`, `tools/browser/is_enabled`, `tools/schedule_followup/is_enabled` to `1`, and `jira/config.json` sets `tools/jira/is_enabled` to `1` (covering most jira tools) — so the agent's baseline toolkit works out of the box. Exception: `jira_get_attachment` is individually opt-in and defaults to disabled. Credentialed/customer adapter tools ship no such default and stay opt-in. A DB `0` at any scope still disables a built-in (e.g. to lock `browser` out of a restricted agent_view).

### The `--agent-view` Shortcut

The `--agent-view <code>` flag resolves the agent_view code to `scope=agent_view, scope_id=<agent_view.id>`. It's equivalent to `--scope agent_view --scope-id <id>` but more convenient since you don't need to look up the numeric ID.

### Toolbox Integration

The toolbox uses the same `isToolEnabled` mechanism to filter which MCP tools are exposed to the agent CLI at runtime. Disabling a tool removes it from the agent's available tool set for that agent_view.

Source: `src/agento/modules/agent_view/src/commands/tool_list.py`, `tool_enable.py`, `tool_disable.py`
