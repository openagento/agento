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

A provider may also declare `provider_options` — the `agent_view/provider_options/<name>`
config fields *it* needs. A self-hosted provider needs an endpoint override; a hosted one
does not, and an operator on the hosted one should not be shown an empty box that will never
apply to them. The matching `system.json` field opts in by naming the option:

```json
"provider_options/base_url": { "type": "string", "provider_option": "base_url" }
```

Admin then shows that field only when the effective provider declares `base_url`. The
condition lives with the **provider**, so no core module and no framework code names a
provider — the same agent-agnosticism rule as everywhere else. Visibility hides only on
positive knowledge: an unset or unresolvable harness/provider leaves the field visible,
because hiding a field the operator still needs is the worse failure. `module:validate`
rejects a `provider_option` no installed provider declares, since such a field would be
invisible forever with no error anywhere. That check reads **installed** providers, not
enabled ones, so disabling the module that declares an option cannot invalidate the
manifest of the module that uses it — every module stays safely disableable.

`sandbox_package` is rendered into the sandbox Dockerfile, so every field is validated
against a closed schema (regex per field, allow-list of managers) before any string
reaches the template. Note the fields are **not** `shlex.quote`d: the rendered line is
`"@openai/codex@${CODEX_VERSION}"`, and quoting would stop the `ARG` from expanding —
which is exactly what the pin exists for. Safety comes from validation, not quoting.

## Implementing a harness

The module supplies one object implementing `AgentHarnessAdapter`, which wires together:

| Protocol                  | Responsibility                                                    |
|---------------------------|-------------------------------------------------------------------|
| `CommandBuilder`          | `headless(ctx, request)`, `interactive(ctx, *, yolo)` and `stdin_payload(ctx, request)` — **the only** place that harness's CLI invocation exists |
| `WorkspaceAdapter`        | materializes config + credentials into a build/run dir; owns `owned_paths`, `persistent_home_paths`, `capture_refreshed_credentials`, `serialize_toolbox_connection` |
| `TranscriptReader`        | parses that harness's own session transcript (optional — `None` when it keeps none) |
| `StreamRenderer`          | renders one **live stdout event** as terminal text for `agento run --pretty` (optional — omit the member entirely and the run streams raw) |
| `CredentialAuthenticator` | one per credential-requiring scope: interactive OAuth + `register_from_secret(mode, secret)` |
| `create_runner(ctx)`      | builds a runner bound to the run context                          |

`descriptor` is deliberately **absent** from the adapter: the framework builds it from
`di.json` so it can be enumerated without importing the module's Python.

### Adding pretty rendering to a harness

`StreamRenderer` is the seam for `agento run --pretty`. `TranscriptReader` is **not** the
right one: it reads an on-disk transcript by `session_id`, while `--pretty` renders the
live stdout stream as it arrives.

A harness opts in with one class and one property — nothing to declare in `di.json`:

```python
# src/agento/modules/<harness>/src/stream_renderer.py
from agento.framework.harness.stream_style import BRANCH, BULLET, bold, dim, truncate

class MyStreamRenderer:
    def render(self, event: dict) -> str | None:
        ...   # return the line to print, or None to hide this event
```

```python
# src/agento/modules/<harness>/src/adapter.py
    @property
    def stream_renderer(self) -> MyStreamRenderer:
        return self._stream_renderer
```

The member is read with `getattr(adapter, "stream_renderer", None)` and is deliberately
**not** declared on `AgentHarnessAdapter`: that protocol is `runtime_checkable` and every
adapter is isinstance-checked at registration, so a declared member would be *required*
and an existing harness without one would stop loading. Omitting it is a supported state —
`--pretty` then streams the raw event JSON exactly as it does today.

Cron reports the renderer's dotted `module:Class` path in the `agent_view:prepare-run`
payload and the host imports it, so `run.py` never maps a harness id to a module. Only
paths under the `agento.` package are imported; a module loaded from `app/code/` gets a
synthetic module name that the host cannot import, and such a harness streams raw.

Contract for `render`: return the text to print, `None` to hide the event deliberately, and
raise if you must — the caller prints the raw line on any exception, so a renderer bug can
never swallow a run's output. Do not return raw JSON for an event type you do not know; a
short dim line keeps a silent format change visible.

### A command is argv *plus* stdin

`stdin_payload(ctx, request)` returns the text written to the process's stdin, which is
then closed; `None` keeps stdin at `DEVNULL`. It belongs to the `CommandBuilder` and not
to the runner because Agento has **two** spawn paths — the consumer's `SubprocessRunner`
and `agento run` on the host, via `prepare_run.py`'s JSON payload — and both must deliver
the invocation the same way. A harness whose CLI accepts its prompt only on stdin (because
an argv prompt beginning with `-` parses as a flag) would otherwise run with the prompt
going nowhere on one of the two paths.

Two implementation constraints that are easy to get wrong:

- The payload is written from its **own thread**, alongside the stdout/stderr drain
  threads. A CLI may read stdin only after startup work, so a payload larger than the
  pipe buffer would block the parent — and the timeout (`proc.wait`) runs on the main
  thread. `BrokenPipeError` is swallowed there so a process that died during startup
  still reports its real error through the normal non-zero-exit path.
- On the host path, `subprocess.run(..., input=payload)` requires `text=True`, and
  `input=None` does **not** mean `DEVNULL` — it inherits the caller's stdin. So the
  no-payload branch keeps `stdin=subprocess.DEVNULL` explicitly.

### Capabilities are enforced, not decorative

`capabilities.resume` gates the consumer's resume branch (`_should_resume`). The consumer
resumes with an **empty** prompt, so a CLI that merely re-opens a session without
continuing work would exit successfully having done nothing — a silent false success. A
harness that declares `resume: false` therefore starts fresh instead.

### `runtime_config_fields` — the harness's own config, at command-build time

A harness may need one of its **own** module config values to build a command (a flag
toggled per agent_view, say). The declaration allow-lists them:

```json
{ "id": "example", "runtime_config_fields": ["builtin_tools"], ... }
```

`get_harness_config()` resolves exactly those paths as `{module}/{field}` via
`svc.get()`. The result lands on `HarnessRunContext.harness_config` for command building,
and is also offered to `WorkspaceAdapter.prepare_workspace(..., harness_config=…)` for
settings that must be baked into a build-time file. That keyword is supplied **only when
the adapter's signature accepts it** (`supply_harness_config`): a default in this Protocol
does not make an existing third-party implementation tolerate an unknown keyword, so the
caller inspects first. Three properties
matter:

- **The namespace comes from the declaring module, never the harness id.** They are not
  interchangeable — `tests/fixtures/modules/fake_harness/` is module `fake_harness`
  declaring harness id `fake`. `RegisteredHarness` therefore carries `module`.
- **It never calls `resolve_all()`.** That resolves every declared path and decrypts every
  module's `obscure` values on the way; this dict is used to build argv.
- **Secrets are refused at registration and by `module:validate`**, before any DB change.
  A field is a secret when its schema is `{"type": "obscure"}` — there is no `obscure: true`
  form anywhere, so checking for one would never match and would admit the field. A schema
  entry that is not an object is also refused: it carries no `type`, so it cannot be proven
  safe.

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

### Adding a harness vs. extending the contract

That promise covers steps 1–5 above: *adding a harness*. It does **not** mean the contract
itself never changes. Those are two different activities and only the first is free:

| | Adding a harness | Extending the contract |
|---|---|---|
| What changes | one module under `src/agento/modules/` | `src/agento/framework/` **and** every existing adapter |
| Framework edits | none — enforced by the test above | yes, by definition |
| Obligations | implement the protocols | migrate all sibling adapters, add compatibility tests, and keep `bin/test` green **with the new capability alone**, before any harness uses it |
| Names a harness | n/a | never — an extension is harness-agnostic or it is not an extension |

`stdin_payload`, the `capabilities.resume` gate and `runtime_config_fields` were all
contract *extensions*: the framework genuinely lacked the capability, so no amount of
module-side code could have supplied it. Each was landed first, harness-agnostically, with
`claude` and `codex` migrated and the fixture harness exercising it — and only then was it
available to a new harness. When you find yourself editing the framework to add an agent,
that is the signal you are doing this second thing, and it carries the obligations in the
right-hand column rather than being a reason to weaken the left one.

### `serialize_toolbox_connection` — declared, not yet on the call path

Stating the current state precisely, because the protocol table alone reads as though this
hook were already load-bearing:

A workspace build is materialized once per **agent_view**, while the Toolbox URL a run must
call carries per-job scoping (`?agent_view_id=…&job_id=…`). Two methods divide that work:

1. `prepare_workspace(...)` writes the build-time configuration. **Today every shipped
   adapter writes its Toolbox wiring directly here** — they do not route it through
   `serialize_toolbox_connection`.
2. `inject_runtime_params(artifacts_dir, job_id=…)` rewrites that configuration inside the
   per-run directory, adding the job id. Without this step a run has no job scope at
   all — its tool calls simply are not attributed to a job (there is no misattribution to
   another job, since no job builds the agent_view workspace).

   `job_id` is `int | None`: `None` means the run has no job scope, which is what a
   string-id `agento run` has. An adapter MAY also accept `effective_model` /
   `effective_provider` — the per-run values, where a `--model` override beats build-time
   config. Each is passed to any adapter that can receive it, whether by a named parameter
   or by `**kwargs`.
   **The two rules interact:** `job_id=None` is passed *only* to an adapter declaring a
   **named** `effective_model` or `effective_provider` matching the override supplied,
   because otherwise the call has nothing to do. `**kwargs` does not qualify — it is a
   forward-compatibility idiom, and an adapter carrying it may still declare `job_id: int`.
   So an adapter that names an override parameter must also widen `job_id` to `int | None`.
   Both shipped siblings accept `int | None` and return early on `None`.

`serialize_toolbox_connection` is the **declared seam** for making step 1
transport-agnostic — the framework would hand over a plain `ToolboxConnectionSpec` and each
harness would render it into whatever its CLI reads (an MCP JSON file, CLI flags, an
extension install, env vars). That rewrite is deferred; the method is implemented and tested
on every shipped adapter so the seam cannot rot into unexercised surface in the meantime.
See `protocols.py` and `tests/unit/framework/harness/test_toolbox_connection.py`, which say
the same thing at the source.

## See also

- [docs/cli/credentials.md](../cli/credentials.md) — the credential pool and CLI
- [docs/architecture/events.md](events.md) — `credential_*` events and their deprecated `token_*` aliases
- [DECISIONS.md](../../DECISIONS.md) — D1–D16, the decisions taken during this refactor
