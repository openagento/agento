# Configuration System

Magento-inspired 3-level config resolution with encrypted secrets.

## Fallback Hierarchy

```
┌─────────────────────────────────────────┐
│  1. ENV var (highest priority)          │  CONFIG__MODULE__TOOLS__TOOL__FIELD
├─────────────────────────────────────────┤
│  2. DB: core_config_data (scoped)      │  bin/agento config:set path value [--scope=S --scope-id=N]
│     agent_view → workspace → default   │
├─────────────────────────────────────────┤
│  3. config.json (lowest priority)      │  modules/{name}/config.json
└─────────────────────────────────────────┘
```

Most specific wins. Each level overrides the ones below it.

DB values support Magento-style scoping: `--scope=agent_view --scope-id=1` overrides `--scope=workspace --scope-id=2` overrides `default/0`.

## When to Use Each Level

| Level | Use Case | Example |
|-------|----------|---------|
| **ENV** | Docker/K8s deployments, CI overrides | `CONFIG__JIRA__HOST=https://staging.atlassian.net` |
| **DB** | Secrets, per-installation overrides | `bin/agento config:set jira/token` (omit value → paste prompt; see [cli/config.md](../cli/config.md#secrets--never-pass-on-the-command-line)) |
| **config.json** | Shared defaults across deployments | `{"tools": {"mysql_prod": {"port": 3306}}}` |

## How It Works at Runtime

There is **one resolver per language**, both implementing the same ENV → DB → config.json fallback:

- **Python framework + modules:** `ScopedConfigService` in [config_resolver.py](../../src/agento/framework/config_resolver.py). Every fallback read goes through it — `svc.get(path)` (raw string), `svc.get_module(name)` (typed config), `svc.resolve_field_with_source(...)` (admin/CLI display). Built once per `(scope, scope_id)` over pre-merged scoped overrides.
- **Toolbox (Node):** [config-loader.js](../../src/agento/toolbox/config-loader.js) — a deliberately separate mirror (the toolbox is the only container with secrets). Kept behaviorally in sync; not merged with the Python service.

Toolbox reads config at each MCP session:

1. Scans `/modules/*/module.json` for tool definitions
2. Loads all `core_config_data` rows from DB (one query, cached per session)
3. For each tool field: checks ENV → DB → config.json
4. Passes resolved config to tool adapter

## Harness-owned runtime config (`runtime_config_fields`)

Most config is read by a module at run time. A harness sometimes needs one of its **own**
module's values *while the command line is being built* — a flag toggled per agent_view,
say. Those fields must be allow-listed in the harness declaration:

```json
{ "id": "example", "runtime_config_fields": ["builtin_tools"], ... }
```

They then resolve into `HarnessRunContext.harness_config` (readable by that harness's
`CommandBuilder`) and are also offered to `WorkspaceAdapter.prepare_workspace` as
`harness_config=…` for settings that have to be written into a build-time file — supplied
only when the adapter's signature accepts the keyword, so an older third-party adapter
keeps working. Three properties are deliberate, and they are a security
boundary rather than a convenience:

- **The namespace is the declaring MODULE, not the harness id.** Paths are
  `{module}/{field}`, and the two names are not interchangeable — the test fixture
  `fake_harness` declares harness id `fake`.
- **Resolution is per-path (`svc.get`), never `resolve_all()`.** `resolve_all()` resolves
  every declared path and decrypts every module's `obscure` values along the way; this
  dict is used to build a command line.
- **Secrets are refused**, at `module:validate` *and* at registration, before any DB
  change. A field is a secret when its schema is `{"type": "obscure"}`. A schema entry
  that is not an object is refused too — it carries no `type`, so it cannot be proven
  safe. A misconfiguration here fails the boot rather than silently skipping the harness.

See [architecture/harness-contract.md](../architecture/harness-contract.md).

## Agent-view runtime: harness / provider / model / priority

The per-job runtime profile (`agent_view/harness`, `agent_view/provider`, `agent_view/model`, `agent_view/scheduling/priority`) resolves through the same `ScopedConfigService`, so **ENV overrides apply**:

```bash
CONFIG__AGENT_VIEW__HARNESS=codex      # the program driving the agent
CONFIG__AGENT_VIEW__PROVIDER=openai    # the model/API vendor it talks to
CONFIG__AGENT_VIEW__MODEL=gpt-5.4-mini
CONFIG__AGENT_VIEW__SCHEDULING__PRIORITY=80
```

Setting `PROVIDER` alone is a **pre-0.15** shape: back then that key held what is now the
harness. It still works — a provider naming a registered harness is recognised as legacy
and mapped to that harness plus its default provider — but set both going forward. See
[harness-contract.md](../architecture/harness-contract.md#pre-015-compatibility).

Precedence for the model specifically: an explicit `--model` flag on `agento run` / `agento e2e` / replay **wins over** ENV/DB config; with no flag, `CONFIG__AGENT_VIEW__MODEL` (ENV) beats the DB value, which beats `config.json`.

### Workspace build honors ENV too

Workspace materialization (`.mcp.json`, `.codex/config.toml`, `.claude.json`, `AGENTS.md` / `SOUL.md`, `.ssh/`) is built from `ScopedConfigService.resolve_all()` — the **full effective config**, each path resolved ENV → DB → config.json. Its key set is the union of DB-override keys, `CONFIG__*` env keys, and every declared module config field — so provider-specific fields set only via ENV (`CONFIG__AGENT_VIEW__CODEX__APPROVAL_MODE`, `CONFIG__AGENT_VIEW__CLAUDE__PERSONALITY`, …) **and** `config.json`-only defaults (e.g. `agent_view/harness`) both participate. (Tool-field `config.json`-only defaults are excluded — they configure toolbox-side tools and never materialize into the build; tool overrides set via DB/ENV are still included.) The build's freshness checksum hashes that same resolved view, so changing any override or shipped default (then recreating the container, since `CONFIG__*` is read at process start) drifts the checksum and the next job-claim **rebuilds** the workspace. One resolver drives both the checksum and every materialized file — no separate DB-only path.

## Scope Restrictions (`showIn*`)

Fields declared in a module's `system.json` may restrict which scopes allow editing, using Magento-style flags:

```json
{
  "timezone": {
    "type": "string",
    "label": "IANA timezone",
    "showInDefault": true,
    "showInWorkspace": false,
    "showInAgentView": false
  }
}
```

Rules:

- All three flags are optional; missing flags default to `true` (editable at that scope).
- Fields without any `showIn*` declaration remain editable on every scope — backward compatible.
- Enforcement is in CLI (`config:set`) and in the admin TUI (edit blocked with a `[readonly]` badge). No DB constraint is applied.
- Applies equally to module-level fields (`module/field`) and tool fields (`module/tools/tool/field`).

## Testing a stored value

A field may declare a **tester** — a probe that answers "does this credential actually work?",
reachable as `agento config:test <path>` and as `t` in the admin TUI. The probe runs in the process
that already holds the credential (the toolbox for network credentials, the declaring module for the
SSH keypair), so the framework never decrypts a secret in order to test one. Results are four-state:
`ok`, `fail`, `not_configured`, `error` — `error` means "could not check", which is not the same
claim as "your credential is wrong".

See [Config Testers](testers.md).

## Further Reading

- [ENV Variables](env-vars.md) — naming convention and examples
- [core_config_data](core-config-data.md) — DB table and CLI
- [Encryption](encryption.md) — obscure fields and AES-256-CBC
- [Config Testers](testers.md) — declaring a "Test connection" for a field
