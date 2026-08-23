# The `pi` harness

[Pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) as a third Agento
harness, with two providers: **OpenRouter** (API key) and **Ollama** (no credential).

Pi differs from `claude` and `codex` in three ways that shape everything below: it takes
its prompt only on **stdin**, it has **no MCP client** of its own, and it resolves an
unrecognised model name by **silently substituting a different one**.

---

## Quick start (OpenRouter)

```bash
# 1. The key. Read from stdin — never as an argv value.
echo "$OPENROUTER_API_KEY" | agento credential:register openrouter primary --with-api-key
agento credential:list                       # expect scope=openrouter

# 2. Point an agent_view at it. `model` MUST be an exact catalogue id — see below.
agento config:set agent_view/harness pi        --scope=agent_view --scope-id=<n>
agento config:set agent_view/provider openrouter --scope=agent_view --scope-id=<n>
agento config:set agent_view/model anthropic/claude-sonnet-4.5 --scope=agent_view --scope-id=<n>

# 3. Build the workspace and run.
agento workspace:build --agent-view <code>
agento run <code> "say hello"
```

## Quick start (Ollama, no credential)

Add Ollama to `docker/docker-compose.override.yml` on `agento-net`:

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    networks: [agento-net]
    volumes: [ollama:/root/.ollama]
volumes:
  ollama:
```

```bash
agento config:set agent_view/provider ollama --scope=agent_view --scope-id=<n>
agento config:set agent_view/provider_options/base_url http://ollama:11434/v1 \
  --scope=agent_view --scope-id=<n>
agento config:set agent_view/model qwen2.5-coder:32b --scope=agent_view --scope-id=<n>
```

No credential is registered, and none is needed: the provider declares
`credential_required: false`, so `usage_log.credential_id` is `NULL` for these runs and
attribution falls back to `(harness, provider)`.

`base_url` is Ollama's alone — the provider declares `"provider_options": ["base_url"]` in
`di.json`, and admin shows the *Provider base URL* field only for providers that do. On
OpenRouter it is hidden rather than shown empty. See
[harness-contract.md](../architecture/harness-contract.md).

---

## `agent_view/model` must be an exact catalogue id

This is the single most important operational rule, and it is not a style preference.

Pi resolves a model in two steps (`dist/core/model-resolver.js:104-127`). It tries an
exact match; failing that it does a **substring** match against each model's `id` *or*
`name`, and returns the highest-sorting alias among the hits — **emitting nothing at
all**. Its own "not found" warning fires only when there was no match whatsoever. So
`gpt-4` can quietly run whatever else in the catalogue contains that substring.

Agento therefore checks positively rather than watching for a warning:

| Layer | What it does |
| --- | --- |
| `PiSubprocessRunner` | compares **both** `provider` and `model` on **every** assistant message (`docs/session-format.md:85-86`) against the request — not just the last, so a mid-run switch cannot hide — and **fails the job** on any mismatch. A stream with **no** assistant identity also fails: absence cannot prove the right model ran |
| the bridge extension | the same comparison in-process, on every spawn path including interactive. A **missing** actual field counts as a mismatch, and a malformed expectation is **rejected** rather than silently disabling the guard. Records an `agento-model-mismatch` entry and sets `process.exitCode = 1` when headless (`ctx.hasUI !== true`), which survives a clean finish so scripts and CI see the failure |
| stderr scan | Pi's own anchored "not found" warning is also treated as fatal |

The expectations reach the bridge through `expected_provider`/`expected_model` in
`.pi/agento-toolbox.json`. `prepare_workspace` writes them from the agent_view config at
build time, and `inject_runtime_params` **sets them per run** from the effective values —
otherwise a legitimate `--model` override would be failed by a stale build-time
expectation. Per-run injection runs on every spawn path, including a string-id
`agento run` that has no job scope at all. If an expectation is absent *and* the run names
no model, that half of the guard is inactive, so a partially configured agent_view leaves
it off rather than comparing against an empty string.

Whether the model half is **enforced** is a separate, explicit key —
`allow_model_substitution` (below) — not the absence of `expected_model`. Absence has two
causes (a router opt-out and an agent_view with no model configured) and conflating them
disabled the guard for the case that wanted it on.

**Every Pi assistant error fails the job.** Pi's `--mode json` does *not* set a non-zero
exit for an assistant error — that check is text-mode only
(`dist/modes/print-mode.js:110`) — so an unrecognised error would otherwise be recorded as
a success with the work undone. Credential classification (poison / throttle / fail over)
applies **only** when the provider requires a credential; an Ollama connection failure is
an ordinary run error, because there is no credential to act on.

A mismatch is an ordinary run failure, never a credential failure — the credential is
fine, the configuration is not.

### Router models: `pi/allow_model_substitution`

Some OpenRouter models are **routers** that dispatch to a different model by design —
`openrouter/free`, `openrouter/auto`, and friends, identifiable by
`architecture.tokenizer == "Router"` in <https://openrouter.ai/api/v1/models>. Pi then
reports the model that actually ran, which the identity check above correctly reads as a
mismatch. Verified live: `openrouter/free` ran `poolside/laguna-xs-2.1:free`.

For those models only, disable the model check:

```bash
agento config:set pi/allow_model_substitution 1 --scope=agent_view --scope-id=<n>
agento workspace:build --agent-view <code> --force   # the flag is applied at build time
```

Semantics, precisely:

* `prepare_workspace` writes `"allow_model_substitution": true` into the bridge's
  connection file. The `expected_model` value is still written — it records what was
  configured — but the marker tells the bridge not to enforce it, so per-run injection
  cannot undo the opt-out by refreshing that value.
* the **provider** check stays active: a wrong provider still fails the run.
* the runner's model comparison is skipped for the same reason.
* a connection file written before this key existed has no marker, and enforcing is the
  default — old builds keep behaving exactly as they did.

It is deliberately explicit rather than inferred from the model id: guessing "this looks
like a router" would produce both false positives and false negatives, and the runtime is
offline with no dependencies, so it cannot consult OpenRouter's catalogue.

⚠ So the "exact catalogue id" rule above has one exception: **a router id is exact for
configuration purposes, but the model that runs will differ.** That is the case this flag
exists for.

### Validating a model id before a run

`agent_view/model` is free text, and Pi substitutes silently, so a typo only surfaces at run
time. The authoritative check is **Pi's own catalogue** — not OpenRouter's API — because Pi
does the matching:

```bash
docker compose exec sandbox pi --offline --list-models | grep '<your model>'
```

(OpenRouter's `GET /api/v1/models` lists 406 ids and is useful for browsing, but a model
present there and absent from Pi's catalogue will still be substituted.)

## Tools reach Pi through a bridge extension

Pi ships no MCP client, so `src/agento/modules/pi/bridge/agento-toolbox.js` is one. It is
copied into each run directory and loaded with `-e`, and it registers every Toolbox tool
as `mcp__toolbox__<name>` — the same shape `claude` produces, so
`job.toolbox_mcp_calls` telemetry works with no change.

It has **zero runtime dependencies** by necessity: Node resolves bare imports by walking
up from the importing file, and a per-job build directory has no `node_modules` above it.
Neither the MCP SDK nor Zod can be imported there, so validation is hand-written: every
trust boundary (the config file, the JSON-RPC envelope, `tools/list` entries, `tools/call`
results) is checked explicitly and malformed input is rejected at the edge. See
[../../DECISIONS.md](../../DECISIONS.md) (D2c) for the reasoning.

The bridge's `agento-toolbox-init` record is read from the **session transcript**, not from
stdout. Pi calls `session.bindExtensions()` — which fires `session_start` synchronously —
*before* attaching its JSON-stream subscriber (`dist/modes/print-mode.js:53` vs `:84`), so
the resulting `entry_appended` event has no stdout listener and can never appear there. It
does reach the session file, which is what `PiTranscriptReader.read_toolbox_init` reads.

All handshake work happens in the extension **factory**, deliberately. A throw there is
fatal (`Failed to load extension` → `exit 1`), which is exactly what should happen if the
Toolbox is unreachable. A throw from `session_start` would be swallowed by Pi's extension
runner and the job would continue with zero tools and report success.

## Least privilege: `--no-builtin-tools`

Pi can run with its built-in tools switched off, keeping only Toolbox tools — tighter than
`claude` or `codex` can express:

```bash
agento config:set pi/builtin_tools 0 --scope=agent_view --scope-id=<n>
```

The agent then has no `bash`, `read`, `edit` or `write`, and reaches the outside world
only through gated Toolbox tools.

---

## Known limitations and gotchas

**`yolo` does nothing.** Pi has no approval prompts by design, so there is no flag to
bypass. `agento run --yolo` is accepted and ignored rather than mapped to something
plausible.

**Cost is not recorded, though OpenRouter runs do carry one.** `cost_reporting` is
`false` and `RunResult.cost_usd` stays `None`. The reason is per-provider, not universal: a
*generated* `models.json` (Ollama) has no rates, so a figure there would be fiction —
whereas Pi **does** price OpenRouter (a live haiku call reported
`usage.cost.total = 0.002204`). Capabilities are declared per **harness**, not per provider,
so `false` is the conservative value. Recording cost for credentialed providers is a
reasonable follow-up; see DECISIONS.md (D2d). `usage_log` records input/output tokens either
way.

**`num_turns` is not comparable across harnesses.** It counts Pi `turn_end` events, which
do not mean the same thing as claude's or codex's turns. Compare Pi runs to Pi runs.

**Session directories accumulate.** Each job leaves one bucket named after its cwd slug in
the persistent session store, permanently. This is parity with claude's `.claude/projects`
and not a defect.

**Resume works, and it needs a non-empty prompt to do so.** The consumer resumes with an
*empty* prompt, and Pi's print mode only prompts when the initial message is non-empty, so
an empty payload would open the session, print the header and exit `0` having done nothing
— recorded as a success. `PiCommandBuilder.stdin_payload` therefore substitutes an explicit
continuation prompt.

Verified live (spike **S4**): two runs sharing one `--session-id`, the second carrying that
continuation prompt, produced a **new `turn_end`** and the model recalled a fact
established in the first run. Both runs wrote to one session file with both exchanges
accumulated in order. `capabilities.resume` is `true` on that evidence.

Also verified as a **pipeline** (`job:pause` → `job:resume` → consumer resume branch): the
paused job kept its `session_id`, `resume_job` returned it to `TODO`, and the resumed run
continued the SAME session file — records grew from 9 to 13 with the pre-pause turn and its
tool call still present, across the `prepare_artifacts_dir` rmtree that wipes the run dir
between attempts. That is what `persistent_home_paths` (`.pi/agent/sessions`) buys.

⚠ **A pause during the FIRST turn loses that turn, silently.** Pi writes the session file
when a turn completes, so a run SIGTERMed before its first `turn_end` has persisted nothing.
The resume then finds no session and **creates a fresh one under the same id** — same
`session_id` in the job row, a transcript that grows, and rc=0, so every signal an operator
would check still looks like a successful resume. Measured directly: pausing in turn 1 gave a
transcript with 0 occurrences of the pre-pause prompt; pausing after a completed tool-call
turn preserved it. Nothing is corrupted and no work that *reached* the model is lost — but
"resumed" does not imply "continued" when the pause lands inside the opening turn.

**Configuration lives in the per-run `$HOME`, not the project.** Non-interactive runs never
see a trust prompt, and Pi's default `defaultProjectTrust: "ask"` makes an untrusted
project's `.pi/settings.json` and `.pi/skills` **ignored**. Writing to
`$HOME/.pi/agent/` sidesteps the gate. `AGENTS.md` is deliberately *not* written there —
`workspace_build` already places it in the build dir, which is Pi's cwd, and a second copy
would be concatenated, duplicating every instruction.

**Only `--session-id` is used for resume.** `--session <bare id>` landing in a different
cwd bucket makes Pi ask "Fork this session into current directory?" **on stdin**, which a
headless run would answer with its own prompt. `--session <path>` inherits the *old* cwd.
All three are also mutually exclusive with `--session-id`.

## Spikes

All four spikes are **green**, run against the live API and a real credential:

| # | Question | Status |
| --- | --- | --- |
| S1 | Does a raw `tools/list` JSON Schema work as `ToolDefinition.parameters`? | **GREEN** — yes, directly. Nested object + string `enum` + optional field all worked; no `Type.Object` conversion and no `StringEnum` workaround needed |
| S2 | Is `OPENROUTER_API_KEY` in env enough headless? | **GREEN** — yes, no `auth.json` and no `/login` |
| S3 | Does `tools/list` return real `inputSchema`? | **GREEN** — see below |
| S4 | Does `--session-id` + a continuation prompt actually resume work? | **GREEN** — new `turn_end` and recalled context |

Two defects were found *by running them*, neither of which any unit test had caught:

* **S1** — `pi.getAllTools()` is an *action method* and throws during extension loading
  (`core/extensions/loader.js:135-155`; only `registerTool()` is valid at load time). The
  bridge called it for a collision check, so the extension failed to load and **every Pi
  job exited 1**. It now dedupes against a local set; the `mcp__toolbox__` prefix already
  makes shadowing a built-in impossible.
* **S2** — Pi reports the canonical catalogue id, which may carry a `~` alias marker
  (request `anthropic/claude-haiku-latest`, get `~anthropic/claude-haiku-latest`). Strict
  equality **failed a legitimate run**; only the marker is normalised, so a genuine
  substitution is still caught.

**S3 (verified against a live Toolbox):** responses are `text/event-stream`, *not* JSON —
an `Accept` header does not change it, because the Toolbox constructs its transport
without `enableJsonResponse`. The session id arrives as the `mcp-session-id` **header**;
`notifications/initialized` answers **202**; `tools/list` returns real JSON Schema with
`type: "object"`; tool entries carry an **extra `execution` key** beyond the spec'd
fields, so a validator that rejected unknown keys would drop every tool; and `DELETE`
genuinely frees the session (reusing the id afterwards gives
`400 "Server not initialized"`).

S1 came back positive, so **no schema conversion layer exists and none should be added** —
the `Type.Object` / `StringEnum` contingency the plan hedged on is dead. See DECISIONS.md
(D2g).

## Adding more Pi providers

Pi supports 40+ providers, but a credential scope has exactly **one** owning harness, so
`pi` cannot claim the `claude` or `codex` scopes (`DuplicateCredentialScopeError`).
Further providers need their own scopes — `pi_anthropic`, and so on. Out of scope here.

## See also

- [../architecture/harness-contract.md](../architecture/harness-contract.md) — the contract
  this module implements, including the stdin channel and `runtime_config_fields`
- [../cli/credentials.md](../cli/credentials.md) — the credential pool

---

## Verified live through the consumer queue

`agento run` execs Pi directly; only a queued job goes through `PiSubprocessRunner`, the
credential pool, `usage_log` and the telemetry observers. Running one found two defects no
unit test could:

| What | Why no test caught it |
| --- | --- |
| `PiTranscriptReader` searched an **invented** root (`AGENTO_BUILD_DIR`, default `/var/agento/builds`) — a variable nothing sets and a path no container has. Every lookup missed, so `job.toolbox_mcp_connected` and `job.toolbox_mcp_calls` were NULL for every Pi job. | every unit test passed `build_root=tmp_path`, so none exercised the default. It now derives from `workspace_paths.BUILD_DIR` |
| The Ollama catalogue carried **no `apiKey`**, and Pi hides a model whose provider has no key — every Ollama run exited with `No API key found for ollama.` before reaching the model, i.e. the credential-free provider never worked. | the catalogue's *shape* was asserted; that Pi requires a placeholder key is stated only in Pi's `models.md` |

A third failure was environmental but worth knowing: **the cron image is built `FROM` the
sandbox image**, so adding a harness CLI to `sandbox` needs *both* rebuilt. Until cron is
rebuilt the consumer fails every job of the new harness with `rc=127 exec: pi: not found`,
while `agento run` (which uses the sandbox) works — an asymmetry that reads like a code bug.

Results, credential 25 (`openrouter`) and `qwen2.5:0.5b` (Ollama):

* **consumer queue** — SUCCESS, `usage_log.credential_id=25`, `harness=pi`,
  `toolbox_mcp_connected=1`, `toolbox_mcp_calls=1` from a real `schedule_followup` call,
  `session_id` persisted, and the runner accepted the router dispatch
  (`openrouter/free` → `poolside/laguna-xs-2.1:free`)
* **pause/resume** — see the resume section above
* **Ollama** — SUCCESS with **no credential**: `usage_log.credential_id IS NULL`,
  attribution by `(harness, provider)`, exact model id, toolbox connected

Also confirmed incidentally: an OpenRouter free-tier `429` classifies as `UsageLimitError`
(throttle + retry), **not** as an authentication failure, so a rate limit never poisons a
healthy credential. Clearing it early is `agento credential:reset <id>`.

To reproduce the Ollama run, put the service in `docker/docker-compose.override.yml` (see the
Ollama section) — or, for a throwaway check against the dev stack, which starts with
`-f docker-compose.dev.yml` and therefore does **not** merge the override:

```bash
docker run -d --name ollama --network <project>_agento-net --network-alias ollama \
  -v ollama:/root/.ollama ollama/ollama:latest
docker exec ollama ollama pull qwen2.5:0.5b
```
