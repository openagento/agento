# Config Commands

Magento-like config management via `core_config_data` DB table.

## config:set

```bash
# Plain value — fine to pass on the command line
bin/agento config:set my_ecommerce/tools/mysql_ecom_prod/host 10.0.0.1
```

### Secrets — Never Pass on the Command Line

Anything that ends up as a positional `value` arg is visible to `ps aux` while the command runs and is written to your shell history (`.bash_history`, `.zsh_history`). **For any `type: "obscure"` field (passwords, tokens, SSH keys, OAuth secrets) omit the `value` — agento reads it from stdin instead.**

```bash
# Interactive paste (TTY): agento prompts, you paste, press Ctrl+D
bin/agento config:set my_ecommerce/tools/mysql_ecom_prod/pass
# → "Paste value for my_ecommerce/tools/mysql_ecom_prod/pass, then press Ctrl+D…"
# <paste>, <Ctrl+D>

# Pipe (scripts / CI): no prompt, stdin read verbatim
cat /path/to/token.txt | bin/agento config:set jira/jira_token
echo -n "$SECRET" | bin/agento config:set my_ecommerce/tools/mysql_ecom_prod/pass
```

Behavior:

- **When stdin is a TTY** — agento prints a one-line prompt to stderr and strips a single trailing `\n` (the Enter before Ctrl+D). Multi-line content (SSH keys, certs) is preserved intact.
- **When stdin is piped** — silent read, no stripping, bytes go to the DB verbatim. Use `echo -n` to avoid a trailing newline on single-value secrets.
- **Encryption is automatic** — if the field is declared `type: "obscure"` in `module.json` / `system.json`, the stored value is AES-256-CBC encrypted regardless of whether you typed, pasted, or piped. See [encryption docs](../config/encryption.md).

### Scope Shortcuts

```bash
# Explicit scope + id
bin/agento config:set my_app/field value --scope agent_view --scope-id 42

# --agent-view <code> resolves the id for you (looks up agent_view.code)
bin/agento config:set my_app/field value --agent-view dev_01
```

`--agent-view` is mutually exclusive with `--scope-id` and with a non-matching `--scope`.

### Path Format

`{module}/tools/{tool_name}/{field_name}` or `{module}/{field_name}` for module-level fields.

Module name uses underscores (hyphens converted): `my-ecommerce` → `my_ecommerce`.

### Scope Restrictions

Fields can declare Magento-style `showInDefault` / `showInWorkspace` / `showInAgentView` flags in `system.json` to restrict which scopes allow editing. Missing flags default to `true` (editable at all scopes — backward compatible).

```bash
# Global-only field rejects lower scopes
bin/agento config:set core/timezone UTC --scope agent_view --scope-id 1
# Error: Field 'timezone' cannot be set at scope 'agent_view' (allowed: default)

bin/agento config:set core/timezone UTC
# Set: core/timezone
```

See [config/README.md — Scope Restrictions](../config/README.md#scope-restrictions-showin) for details.

### Exit Codes

`config:set` exits **1** on every rejection — malformed path, unknown field, scope
restriction, and an invalid `select` value — and **0** only when the value was written.
Scripts and CI can therefore trust the exit code alone:

```bash
# provider options depend on the agent_view's harness (claude → anthropic)
bin/agento config:set agent_view/provider codex --agent-view qa_01
# Error: Invalid value 'codex' for select field 'provider'
#   Allowed values: anthropic
echo $?   # 1 — nothing was written
```

## config:remove

```bash
bin/agento config:remove my_ecommerce/tools/mysql_ecom_prod/pass
bin/agento config:remove agent_view/identity/ssh_private_key --agent-view dev_01
```

Falls back to lower-scope / `config.json` default after removal. `--agent-view` works here the same way it does for `config:set`.

## config:get

```bash
bin/agento config:get my_ecommerce/tools/mysql_ecom_prod/host
# Output: 10.0.0.1
```

Returns the DB override value only. Does not show ENV or config.json defaults.

## config:list

```bash
# List all config
bin/agento config:list

# Filter by module prefix
bin/agento config:list my_ecommerce
```

Output:
```
  my_ecommerce/tools/mysql_ecom_prod/host = 10.0.0.1
  my_ecommerce/tools/mysql_ecom_prod/pass = **** [encrypted]
```

## config:test

Run the tester declared on a config field.

```bash
# One field
agento config:test core/smtp_pass

# At an agent_view's scope
agento config:test agent_view/identity/ssh_private_key --agent-view dev_01

# Every field that declares a tester — a post-deploy smoke check
agento config:test --all
```

Output is one line per field:

```
OK  core/smtp_pass: 250 login accepted (412 ms)
FAIL [AUTH_FAILED]  app_monitor/alerts/smtp_password: 535 5.7.8 authentication failed
NOT_CONFIGURED  outlook/outlook_client_secret: no credentials are configured
ERROR [TOOLBOX_UNREACHABLE]  jira/jira_token: toolbox unreachable at http://toolbox:3001
```

Exit codes: `0` = every run was `ok` or `not_configured`; `1` = any `fail` or `error`; `2` = usage
(no path given, or the field declares no tester).

`ERROR` is not `FAIL`. `FAIL` means the system answered and rejected the credential; `ERROR` means
the check did not happen. Do not rotate a credential on an `ERROR`.

### Scopes

`--agent-view CODE` runs the test against that view's resolved values. Which scopes a field supports
depends on where its probe runs:

| Tester | Runs in | Scopes |
|---|---|---|
| `smtp`, `http`, a named module probe | the toolbox | `default` and `agent_view` |
| `local` (e.g. the SSH keypair) | the framework | every scope, including `workspace` |

A network credential is probed inside the toolbox because that is the only container holding
secrets, and the toolbox's scoped reader for tests (`loadStrictScopedOverrides`) resolves an
`agent_view`. There is no workspace chain there, so `--scope workspace --scope-id N` on a
toolbox-tested field returns `ERROR [SCOPE_UNSUPPORTED]` rather than silently answering about the
default scope — an answer about credentials you did not name is worse than no answer. That loader is
strict on purpose: unlike the lenient one every MCP session uses, a failed query or an unknown
agent_view is `ERROR [CONFIG_UNAVAILABLE]`, never a quiet fall back to global config.

`--scope`/`--scope-id` are checked before either arm runs: `default` takes no id, `workspace` and
`agent_view` need a positive one, and anything else is `ERROR [SCOPE_UNSUPPORTED]`.

Which fields are testable, and how a module declares one: [config/testers.md](../config/testers.md).

## ENV Var Override

ENV vars have the highest priority and override both DB and config.json:

```bash
CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__HOST=10.0.0.99
```

Convention: `CONFIG__{MODULE}__{PATH}` — all uppercase, hyphens → underscores, path separators → `__`.

See [ENV var docs](../config/env-vars.md) for details.
