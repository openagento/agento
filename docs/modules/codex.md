# The `codex` harness

[OpenAI Codex](https://www.npmjs.com/package/@openai/codex) as an Agento harness, with
one provider: **OpenAI** (interactive OAuth, API key or access token).

At workspace-build time the adapter writes `.codex/config.toml` — model, the optional
`codex/approval_mode`, and the Toolbox MCP entry (plus anything from `codex/config`,
below).

## `codex/config` — native config.toml passthrough

**Never put a credential in this field.** It is stored unencrypted in `core_config_data`
(a `runtime_config_fields` entry may not be `obscure`), it is readable with
`config:get` / `config:list`, and it is written into a file inside the agent container —
which by design holds no credential. Native keys that take a secret
(`mcp_servers.*.http_headers`) have no supported use here; MCP credentials belong in the
Toolbox.

| | |
|---|---|
| Path | `codex/config` |
| Format | raw Codex `config.toml` (TOML) |
| Destination | `.codex/config.toml` in the build |
| Merge rule | deep-merged **over** the block Agento generates — the operator wins on a key collision |
| Invalid value | fails the workspace build (it is never silently skipped) |

Agento writes **no** `sandbox_mode` and **no** `approval_policy` of its own. Headless
runs bypass the sandbox through a CLI flag (below), and the same file is the HOME an
*interactive* `agento run` uses — a generated "never ask" there would silently remove
that session's approval prompts. Only this field puts those keys in the file.

In `agento admin` the field is on the **agent_view** node, beside the harness selector —
it is listed only when this view's harness is `codex` (see
[harness contract](../architecture/harness-contract.md#harness_option--showing-the-field-where-it-is-set)).

```bash
# Block all network access at the OS sandbox level for one agent_view.
agento config:set codex/config 'sandbox_mode = "workspace-write"' \
  --scope=agent_view --scope-id=<id>
```

### Setting `sandbox_mode` also drops a CLI flag

Headless runs normally carry `--dangerously-bypass-approvals-and-sandbox`, which
overrides `config.toml` wholesale. Agento therefore **omits** that flag for a run whose
`codex/config` sets `sandbox_mode`; without that, the operator's sandbox would be
silently dead. With no blob, or a blob that sets no `sandbox_mode`, the flag stays and
behaviour is unchanged.

**Unverified on the pinned CLI:** whether `codex exec` honours `sandbox_mode` from
`config.toml`, and whether the OS sandbox actually blocks network in the shipped image,
could not be confirmed here — no Codex credential in the pool is accepted by the backend
(`403 Forbidden` from `chatgpt.com/backend-api/codex/*`, on both 0.137.0 and 0.157.0), so
the probe run ends before any command executes. The reporter observed the behaviour on
0.145.0, which the pinned 0.157.0 is past. Treat the network block as untested until
someone re-runs the probe with a working credential.

### Shapes the writer cannot emit

Agento serialises `config.toml` itself with a small TOML subset: scalars, nested tables
and arrays of scalars. An array of tables (`[[profiles]]`) is rejected with an error
naming the key — express it as nested tables instead.

## See also

- [Harness contract](../architecture/harness-contract.md)
- [The `claude` harness](claude.md), [the `pi` harness](pi.md)
