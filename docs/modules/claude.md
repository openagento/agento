# The `claude` harness

[Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code) as an Agento
harness, with one provider: **Anthropic** (interactive OAuth or API key).

At workspace-build time the adapter writes `.claude.json` (model, system prompt, login
state) and `.claude/settings.json` (permissions and everything else Claude Code reads
from its settings file), plus `.mcp.json` for the Toolbox.

## `claude/settings` — native settings.json passthrough

**Never put a credential in this field.** It is stored unencrypted in `core_config_data`
(a `runtime_config_fields` entry may not be `obscure`), it is readable with
`config:get` / `config:list`, and it is written into a file inside the agent container —
which by design holds no credential. Native keys that take a secret (`env`) have no
supported use here; MCP credentials belong in the Toolbox.

| | |
|---|---|
| Path | `claude/settings` |
| Format | raw Claude Code `settings.json` (JSON object) |
| Destination | `.claude/settings.json` in the build |
| Merge rule | deep-merged **over** the block Agento generates — the operator wins on a key collision |
| Invalid value | fails the workspace build (it is never silently skipped) |

```bash
# Block the built-in web tools for one agent_view. permissions.deny is honoured even
# under --dangerously-skip-permissions.
agento config:set claude/settings '{"permissions":{"deny":["WebSearch","WebFetch"]}}' \
  --scope=agent_view --scope-id=<id>

# Pick the model Claude's advisor tool uses.
agento config:set claude/settings '{"advisorModel":"fable"}' \
  --scope=agent_view --scope-id=<id>
```

The merge is nested, so a `permissions` block from the operator does not wipe the
`permissions.dangerouslySkipPermissions` Agento derives from `claude/trust_level`.

In `agento admin` the field is on the **agent_view** node, beside the harness selector —
it is listed only when this view's harness is `claude` (see
[harness contract](../architecture/harness-contract.md#harness_option--showing-the-field-where-it-is-set)).

## `claude/permissions` (legacy)

`agent_view/claude/permissions` still works, but it now lands in
`.claude/settings.json` instead of `.claude.json` — Claude **ignores** a `permissions`
key in `.claude.json`, so the value used to have no effect. It is merged together with
`claude/settings`.

**Breaking change:** an **invalid** JSON value there used to be logged and skipped. It
now fails the workspace build. A silently dropped permissions block is a deny-list the
operator believes is in place and is not. Remedy:

```bash
agento config:get agent_view/claude/permissions --scope=agent_view --scope-id=<id>
# then fix the JSON, or unset the value
```

## See also

- [Harness contract](../architecture/harness-contract.md) — `runtime_config_fields` and the rest of the contract
- [The `codex` harness](codex.md), [the `pi` harness](pi.md)
