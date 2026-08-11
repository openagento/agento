# The harness contract

Adding an agent to Agento must not require editing framework code (AGENTS.md rule #6).
This document describes the contract that makes that true, and why it is shaped the way
it is.

## Three axes, not one

Before v0.15 a single closed enum, `AgentProvider(CLAUDE|CODEX)`, keyed **five** separate
registries: runners, config writers, CLI invokers, auth strategies and transcript
readers. One value therefore had to mean three unrelated things at once, and adding a
third agent meant editing the enum — i.e. editing the framework.

The one axis is now three independent ones:

| Axis         | What it is                                                       | Example            |
|--------------|------------------------------------------------------------------|--------------------|
| **harness**  | the *program* that drives the agent — its CLI flags, workspace layout, transcript format, sandbox package | `claude`, `codex`  |
| **provider** | the *model/API vendor* the harness talks to — whether a credential is required and which pool it comes from | `anthropic`, `openai` |
| **model**    | the model identifier passed to that provider                      | `claude-opus-4-7`  |

They are genuinely independent: one harness can offer several providers, only some of
which need a credential (a locally-hosted model needs none), and the same provider can be
reachable from more than one harness.

## Declaring a harness

Everything static lives in the module's `di.json`, under one `agent_harnesses` entry:

```json
{
  "agent_harnesses": [
    {
      "id": "codex",
      "label": "OpenAI Codex",
      "class": "src.adapter.CodexHarnessAdapter",
      "default_provider": "openai",
      "providers": [
        {
          "id": "openai",
          "label": "OpenAI",
          "credential_required": true,
          "registration_modes": ["interactive_oauth", "api_key", "access_token"],
          "credential_scope": "codex"
        }
      ],
      "capabilities": { "interactive": true, "resume": true, "transcripts": true },
      "sandbox_package": {
        "manager": "npm",
        "package": "@openai/codex",
        "binary": "codex",
        "version_env_key": "CODEX_VERSION",
        "default_range": "0.137.0"
      }
    }
  ]
}
```

**Why a manifest rather than Python?** Three callers need to enumerate harnesses *before*
any Python can be imported or any DB touched: `config:set` validating a `select` value,
`enumerate_sandbox_packages` during `install`/`upgrade`/`doctor`, and `module:validate`
inside `setup:upgrade` (which must fail *before* the first schema change). So descriptors
are pure data, parsed straight off disk by `framework/harness/manifest.py`.

`credential_required` is the single source of truth, and the other two credential fields
must agree with it **in both directions** — `true` needs a scope and at least one
registration mode, `false` must declare neither. A half-declared provider fails to load
rather than failing later at `credential:register`.

`sandbox_package` is rendered into the sandbox Dockerfile, so every field is validated
against a closed schema (regex per field, allow-list of managers) before any string
reaches the template. Note the fields are **not** `shlex.quote`d: the rendered line is
`"@openai/codex@${CODEX_VERSION}"`, and quoting would stop the `ARG` from expanding —
which is exactly what the pin exists for. Safety comes from validation, not quoting.

## Implementing a harness

The module supplies one object implementing `AgentHarnessAdapter`, which wires together:

| Protocol                  | Responsibility                                                    |
|---------------------------|-------------------------------------------------------------------|
| `CommandBuilder`          | `headless(ctx, request)` and `interactive(ctx, *, yolo)` — **the only** place that harness's CLI flags exist |
| `WorkspaceAdapter`        | materializes config + credentials into a build/run dir; owns `owned_paths`, `persistent_home_paths`, `capture_refreshed_credentials`, `serialize_toolbox_connection` |
| `TranscriptReader`        | parses that harness's own session transcript (optional — `None` when it keeps none) |
| `CredentialAuthenticator` | one per credential-requiring scope: interactive OAuth + `register_from_secret(mode, secret)` |
| `create_runner(ctx)`      | builds a runner bound to the run context                          |

`descriptor` is deliberately **absent** from the adapter: the framework builds it from
`di.json` so it can be enumerated without importing the module's Python.

### One CommandBuilder, not two

Claude's flags used to live in two places — `TokenClaudeRunner._build_command` and
`ClaudeCliInvoker.headless_command` — and had already drifted: the invoker omitted
`--mcp-config .mcp.json --strict-mcp-config`, so `agento run <view> "<prompt>"` started
the agent *without* the per-job MCP config the consumer path always injected. The agent
silently had no toolbox. Collapsing both into one `CommandBuilder` per harness makes that
class of drift unrepresentable, and
`tests/unit/framework/harness/test_command_builder_parity.py` pins it.

## Credential scopes

A **scope** names one credential pool. `resolve_credential_scope(harness, provider)`
returns it, or `None` when that provider needs no credential.

One scope has exactly **one** owning harness. That is a deliberate restriction: `di.json`
carries only a class path, so authenticator identity cannot be checked statically, and
`module:validate` must work without importing Python. Two harnesses sharing a pool would
need an explicit manifest field; until something needs it, a collision is a hard error at
registration (`DuplicateCredentialScopeError`).

The **caller** claims the credential — exactly once per run — and passes it in through
`HarnessRunContext`. The runner has no pool access at all, so the command it builds and
the process it spawns can never end up on two different credentials.

A provider needing no credential still records usage: `usage_log.credential_id` is
nullable and the row is attributed by `(harness, provider)`.

## Scoped config

Two `agent_view` config paths, both `select` fields whose options come from the
declarations rather than a hardcoded list:

```
agent_view/harness    options_source: agent_harness_registry
agent_view/provider   options_source: agent_harness_providers, depends_on: agent_view/harness
```

### Pre-0.15 compatibility

Before the split, `agent_view/provider` held what is now the *harness* id. Since the new
`config.json` always ships a default harness, "harness unset" never happens — so the
fallback cannot test presence. It compares the two values' **origins**:

```
ENV (40) > DB agent_view (30) > DB workspace (20) > DB default (10) > config.json (0)
```

- A provider that is **valid for the effective harness** is taken at face value (checked
  first, so `anthropic` is never mistaken for a legacy value).
- Otherwise a provider that names a **registered harness** is legacy. That test is
  structural on purpose — a hardcoded `claude`/`codex` list here would put back in the
  framework precisely the branch this contract removes. Registering a third harness makes
  its id a recognised legacy value with zero framework changes.
- On a tie the **legacy provider wins**: an equal origin means the operator only ever set
  the old key at that scope, so honouring it is what keeps a pre-0.15 deployment working.
  The harness wins only when set at a **strictly stronger** origin, in which case the stale
  provider is ignored in favour of the harness's own `default_provider` — so
  `(claude, openai)` can never be produced. (Implementation: `provider_origin >=
  harness_origin` selects the legacy branch.)
- A provider that is neither valid nor legacy **raises**. The old code silently fell back
  to Claude.

Stored rows are migrated once by the `SplitProviderIntoHarness` data patch. Its
harness→provider map is **frozen in the patch**, not read from the registry: a data patch
is applied once and tracked permanently, so reading the registry would skip
`provider=codex` rows on a deployment with the codex module disabled — and a later
`module:enable codex` would never re-run the patch, leaving that config permanently
unmigrated.

## What the framework no longer contains

Deleted with the split: `framework/cli_invoker.py`, `framework/config_writer.py`,
`framework/transcript_reader.py`, `framework/runner.py`, `framework/runner_factory.py`,
`framework/agent_manager/runner.py`, and the per-module `src/cli.py` invokers — five
registries and five loaders collapsed into one registry and one loader
(`bootstrap._load_agent_harnesses`).

## Adding a harness: checklist

1. `module.json` + `di.json` with one `agent_harnesses` entry.
2. An `AgentHarnessAdapter` implementation.
3. `agento module:enable <name>` then `agento setup:upgrade`.
4. `agento credential:register <scope> <label>` for each credential-requiring provider.
5. `agento config:set agent_view/harness <id> --scope=agent_view --scope-id=<n>`.

No framework file changes at any step. `tests/fixtures/modules/fake_harness/` is a
working third harness used by the test suite to keep that claim honest — including a
test asserting no framework source file names it.

## See also

- [docs/cli/credentials.md](../cli/credentials.md) — the credential pool and CLI
- [docs/architecture/events.md](events.md) — `credential_*` events and their deprecated `token_*` aliases
- [DECISIONS.md](../../DECISIONS.md) — D1–D16, the decisions taken during this refactor
