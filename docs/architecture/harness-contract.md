# The harness contract

Adding an agent to Agento must not require editing framework code (RULES.md PLC-2).
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
        "default_range": "0.157.0"
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
| `WorkspaceAdapter`        | materializes config + credentials into a build/run dir; owns `owned_paths`, `persistent_home_paths`, `inject_runtime_params`, `capture_refreshed_credentials`, `serialize_toolbox_connection` |
| `StreamRenderer`          | renders one **live stdout event** as terminal text for `agento run --pretty` (optional — omit the member entirely and the run streams raw) |
| `CredentialAuthenticator` | one per credential-requiring scope: interactive OAuth + `register_from_secret(mode, secret)` |
| `create_runner(ctx)`      | builds a runner bound to the run context                          |

`descriptor` is deliberately **absent** from the adapter: the framework builds it from
`di.json` so it can be enumerated without importing the module's Python.

**Where it runs.** The runner that `create_runner(ctx)` builds, its output parser and its
`stream_event_mapper` run in a `runner-<i>` service, not in `cron`
([runner.md](runner.md)). The consumer uses `RemoteRunner`, which has the same `Runner`
protocol, so a workflow sees no difference. The runner has no database: a harness runner
reads no DB and no module config (it gets `harness_config` on the context). Usage goes back
as a `usage` event (`SubprocessRunner.observe(on_usage=…)`); the worker writes the row. A
vendor CLI that is not a run (a login, a model list) goes through `runner.client.run` or
`runner.client.pty`, never `subprocess` in the module (`test_spawn_guard.py`).

### `inject_runtime_params` — the capability injection point

```python
def inject_runtime_params(
    self,
    artifacts_dir: Path,
    *,
    job_id: int | None,
    run_id: str | None = None,
    capability_token: str | None = None,
    toolbox_url: str | None = None,
) -> None: ...
```

`capability_token` arrives with the trusted `toolbox_url` it belongs to, and only to an adapter that
names the keyword (or takes `**kwargs`); an adapter that cannot receive it gets no token and its
session is refused `401`. `job_id` / `run_id` still scope the run's desk (see below). The build
directory is shared by every run of the agent_view and therefore carries no claims; this call is where
the *per-run* credential enters the *copied* config.

The adapter's responsibility is narrow and is a security contract, not a style choice:

- Inject the capability into **the toolbox's own MCP entry only**. Match by **origin and path** —
  `(scheme, host, port)` equal to the toolbox's, and a path of exactly `/mcp` or `/sse`. Use the
  framework helpers `toolbox_origin()` / `is_toolbox_endpoint()` from
  `agento.framework.harness` rather than writing the comparison again.
- **Never match by substring.** Operators may add third-party MCP servers under
  `agent_view/mcp/servers`; a `"/mcp" in url` test would hand them the capability. An adapter that
  injects the token anywhere but our own endpoint is a security defect.
- **Validate the trusted input first.** `toolbox_origin()` raises on a `toolbox_url` that is not a
  usable http(s) origin, and the adapter must call it *before* touching any file, so a misconfigured
  `core/toolbox/url` fails the run loudly instead of scattering the token. Never compare a sentinel:
  a helper returning `None` on failure would make two failures compare equal and match everything.
- **Never log the token**, and never write it anywhere but the run's own config file.

An entry the operator added that shadows the `toolbox` key stays legal — it simply fails the endpoint
test, receives no capability, and is refused `401` by the toolbox. Fail-closed by construction.

### Adding pretty rendering to a harness

`StreamRenderer` is the seam for `agento run --pretty`. It renders the live stdout stream
as it arrives.

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

### Adding live timeline events

`StreamEventMapper` turns one parsed stdout event into **canonical fragments**, so the panel
timeline shows every harness the same way. It is optional, like `stream_renderer`: a harness
exposes it as a `stream_event_mapper` property, and a harness without one still runs (its thread
gets `run.started`, `run.finished` and the answer, but no live events).

```python
# src/agento/modules/<harness>/src/stream_event_mapper.py
class MyStreamEventMapper:
    def map_event(self, event: dict) -> dict | list[dict] | None:
        ...   # one fragment, several, or None to skip the event
```

| `kind` | Fields | Meaning |
| --- | --- | --- |
| `assistant.text` | `text` | assistant text, one message or one part of it |
| `assistant.partial` | `text` | a token-level piece of assistant text (coalesced, see below) |
| `assistant.reasoning` | `text` | the model's complete reasoning block (may be empty) |
| `reasoning.partial` | `text` | a token-level piece of reasoning (coalesced) |
| `tool.started` | `tool_name`, `data.call_id`, `data.input` | the harness calls a tool |
| `tool.completed` | `tool_name` (optional), `data.call_id`, `data.output`, `data.is_error` | the call's result |
| `error` | `text` | an error the harness reported |

The framework adds `gap` and `truncated` itself. It drops an unknown kind (DEBUG log), reads a
kindless fragment or the pre-E9 `delta` kind as `assistant.text`, redacts the run's capability
tokens from `text` and from every string in `data`, and cuts each string at 64 KiB. Pair a
`tool.started` and its `tool.completed` by the same `call_id`; use `""` when the harness gives
none. The mapper names no other harness and does no I/O: it runs on the stdout drain thread, in
the runner. The runner sends each fragment to the worker, and the worker does the rest below.

**Partials are coalesced, not written one per token.** The framework keeps one buffer per run and
flushes it as one row every 250 ms or 4 KiB, and the delta writer commits at most 4 times a second
(plus one per 200 rows), so 200 parallel streaming runs do not multiply the write rate by the token
rate (RULES.md SCL-1). The complete fragment supersedes the buffer of its kind
(`assistant.text` ends `assistant.partial`, `assistant.reasoning` ends `reasoning.partial`); a
fragment of another kind flushes it first, so the rows keep stream order. Redaction runs on the
joined buffer and holds back a tail that could be the start of a secret, so a token split over two
deltas never reaches a row. A full queue drops partials without a gap marker: the complete fragment
follows. Emit partials only when the harness streams text and also sends the complete message
(claude `--include-partial-messages`, pi `message_update`); codex sends whole items only
(DECISIONS.md D-E9-5).

A harness's `raw_output` is the **final answer text** — the last assistant message — never the
stream: it is what the answer bubble shows (DECISIONS.md D-E9-4).

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

A conversation's next turn resumes too, but with the new message as the prompt: the
`CommandBuilder` must send `req.prompt` when it is set, and its own "continue" text only
when it is empty (a retry). That turn is a new job, so it runs in a new working directory.
A CLI that files sessions under a slug of its cwd (claude, pi) cannot see the earlier
session from there, so its runner overrides the optional `SubprocessRunner.prepare_resume(
session_id) -> bool`: it moves the session file into this run's folder (`move_session_into`),
or answers `False` when the session is gone. On `False` the conversation workflow starts a
fresh session with the whole thread. The hook is not part of the `Runner` protocol; a runner
without it counts as "found".

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

Each shipped harness uses this seam for one **native-config passthrough** field, so an
operator can hand the CLI its own config without new Agento code: `claude/settings`
(JSON → `.claude/settings.json`), `codex/config` (TOML → `.codex/config.toml`) and
`pi/settings` (JSON → `$HOME/.pi/agent/settings.json`). Each adapter parses its own
format, deep-merges the blob **over** the block Agento generates, and **raises** on a
malformed one — a silently dropped blob is a silently dropped deny-list. See
[claude](../modules/claude.md), [codex](../modules/codex.md) and [pi](../modules/pi.md).

#### `harness_option` — showing the field where it is set

The passthrough is set per agent_view, so `agento admin` lists it under the **agent_view**
node, next to the harness selector, and hides the passthroughs belonging to the other
harnesses. The `system.json` field asks for that itself:

```json
"settings": { "type": "textarea", "harness_option": true, "label": "…" }
```

The path does **not** move: it stays `{module}/{field}`, which is what
`runtime_config_fields` resolves and what `config:set` writes — only the display moves, so
the field still appears on its own module node too. The condition is the declaring
module's own `agent_harnesses` entry, read off disk (`modules_declaring`), so no framework
file names a harness; an unset or unresolvable harness shows every passthrough rather than
hiding one the operator still needs. Same shape as `provider_option`, one axis up.

Every site that builds a `HarnessRunContext` must carry the resolved dict, or the
harness's own build-time settings silently revert on that path. That is enforced by an
AST guard (`tests/unit/framework/test_harness_config_wiring.py`) over every
`HarnessRunContext(` call in `src/agento/`, with an empty allow-list.

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

### Optional authenticator members: limits and panel re-login

`CredentialAuthenticator` has two **optional** members. The framework reads them with
`getattr`, like `account_label`, so an out-of-tree authenticator without them keeps working:
its credentials show "no data" for limits, and a panel re-login of its credential ends as
`failed` with `unsupported`. `web` loads no harness module, so it cannot see the member: it
answers 400 only when the scope's `di.json` provider declares no `interactive_oauth`
registration mode. A scope that declares the mode but whose authenticator has no
`start_web_login` gets 201, and the worker then ends the login with `unsupported`.

| Member | Called by | Returns |
|---|---|---|
| `fetch_limits(credentials, credential_type)` | `credential:limits` (cron, every 10 min) | `CredentialLimits(windows, balance_usd)` of `LimitWindow(label, used_pct, resets_at)`, or `None` when this type has no endpoint. Raise on a failed call; the framework stores `NULL` and logs the exception class only. It also stores `NULL` when a `used_pct` is outside 0–100 or a number is not finite. Use `httpx` with a 10 s timeout. |
| `start_web_login(tmp_home, logger)` | `credential:web-login` (cron, every minute) | an `InteractiveLogin`: `prompt: LoginPrompt(url, user_code, needs_code)`, `submit_code(code)`, `poll() -> AuthResult \| None`, `close()` |

`start_web_login` starts the vendor CLI with `HOME=tmp_home`, a fresh temp dir. The framework's
`pty_login.spawn(cmd, home)` and `PtyLogin(proc, prompt, parse)` do the PTY work (minimal
environment, wide terminal, ANSI stripping, the CLI dies with the worker); the module gives
only the command, the patterns, and `parse`, which reads the files the CLI wrote into
`tmp_home`. `PtyProcess.read_until(pattern, timeout)` returns a match only once more output
follows it or the CLI ended, so a pattern may end in an open token (`\S+`) and still get the
whole URL or code when the CLI writes it in parts. `poll()` raises `AuthenticationError` when the CLI fails. The worker checks that
`prompt.url` is `https`, keeps the code in memory only, and saves the result through
`register_credential_and_dispatch`, the same function as `credential:register`. See
[credentials.md](../cli/credentials.md#re-login-from-the-panel).

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

### Checking a model id

`check_model` is optional, like `stream_renderer`, and read with `getattr`. The `agent_view/model`
config tester ([testers.md](../config/testers.md#3-a-module-local-python-class)) calls it after it
has checked the harness, its CLI, the provider and the credential pool:

```python
def check_model(self, provider: str, model: str, credential: CredentialRecord | None,
                *, timeout_s: float) -> TestResult | None: ...
```

- `credential` is the decrypted pool credential, or `None` when the provider needs none.
- Return `None` when this harness cannot check this provider's models; the tester answers
  `error` / `MODEL_NOT_CHECKED`.
- Otherwise return `ok` / `MODEL_OK`, `fail` / `MODEL_UNKNOWN`, `error` / `MODEL_CHECK_FAILED` or
  `error` / `MODEL_CHECK_TIMEOUT`. Answer `fail` only on proof that the model is unknown; a list that
  could not be read is `error`.
- `timeout_s` is a total budget: take one deadline at entry and give each subprocess or request only
  the time that is left. Run network calls in a child process, so the deadline also covers DNS.
- Write nothing to the DB. Put no secret, CLI stderr or response body in the message. Start a CLI
  in a runner (`runner.client.run`) in a temporary HOME under `client.shared_tmp()`, never with
  the secret on argv.

The shipped harnesses: Pi reads `pi --list-models`, Codex reads `codex debug models` (the account's
list), Claude asks `GET /v1/models/{id}`. A guard test requires `check_model` on every in-tree
harness.

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
call carries per-run scoping (`?agent_view_id=…&job_id=…`, or `…&run_id=…` for a run with no
job). Two methods divide that work:

1. `prepare_workspace(...)` writes the build-time configuration. **Today every shipped
   adapter writes its Toolbox wiring directly here** — they do not route it through
   `serialize_toolbox_connection`.
2. `inject_runtime_params(artifacts_dir, job_id=…)` rewrites that configuration inside the
   per-run directory, adding the run's scope. Without this step a run has no scope at
   all — its tool calls simply are not attributed to a job (there is no misattribution to
   another job, since no job builds the agent_view workspace), and the toolbox can give it
   no per-run directory of its own: it falls back to `/workspace/artifacts/_fallback`,
   which every unscoped session shares, so the `versioned_artifacts` desk tools refuse it
   with `WORKSPACE_UNAVAILABLE`.

   `job_id` is `int | None`: `None` means the run has no job scope, which is what a
   string-id `agento run` has. Such a run names itself with the optional `run_id` keyword
   instead — its own unique id, already the last segment of its artifacts dir. Build the
   scope with `agento.framework.harness.run_scope.scope_toolbox_url(url, job_id, run_id)`
   rather than formatting the query by hand; it rejects anything that is not one plain path
   segment, and `_fallback` itself. An adapter MAY also accept `effective_model` /
   `effective_provider` — the per-run values, where a `--model` override beats build-time
   config. Each is passed to any adapter that can receive it, whether by a named parameter
   or by `**kwargs`.
   **The rules interact:** `job_id=None` is passed *only* to an adapter declaring a
   **named** `run_id`, `effective_model` or `effective_provider` matching what is actually
   supplied, because otherwise the call has nothing to do. `**kwargs` does not qualify — it is a
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
