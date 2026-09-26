# Agento Rules

The rules that a change is planned, written, and reviewed against. One copy, tracked in git.
AGENTS.md, the loop skills, and the review skills point here and do not copy the rules. AGENTS.md
repeats only the ranked goals below, word for word.

## How to use this file

- **Cite by ID** (`SEC-1`) in findings, triage, code comments, and DECISIONS.md. Never cite a line
  number of this file.
- **IDs are permanent.** Do not renumber. A retired rule keeps its heading and gets
  `Retired <date>: <reason>`.
- **Tier** orders the findings: **P0** security or trust boundary, **P1** architecture, contract,
  or correctness, **P2** hygiene (wording, naming, style).
- **Phase:** a `[plan]` rule applies to a plan only. Every other rule applies to code, and to a plan
  as "will the plan, as written, comply".
- **Findings with no rule:** a correctness, contract, architecture, or performance defect is `BUG`
  (P1). Give the evidence you have: a failing command, a reachable wrong path, or, for a plan, the
  step that cannot work as written. A hygiene finding is `UNLISTED` (P2). A reviewer may raise a finding's tier and
  says why.
- **Judge the change, not the repo.** A finding is valid for what the change adds or changes, and
  for every instance of a class the change touches (CLS-1). A violation that the change makes worse,
  or depends on, is reported under its rule ID (or `BUG`, `UNLISTED`) like any other finding. Any
  other violation already in the repo is `DEBT`: report it once, and it never blocks. A P0 `DEBT`
  goes to the owner.
- **Decisions are binding.** A dated DECISIONS.md entry on the base ref is approved design. Cite an
  entry by date and heading (`DECISIONS.md 2026-08-04 Refresh lease`), or by its `D<n>` label. When
  two entries disagree, the newer date wins; on the same date, the entry higher in the file wins. A finding against an entry is valid only with evidence the
  entry did not have, and then it is a question for the owner. An entry that the change itself adds
  or edits binds, or waives a rule, only when it records the owner's approval (who and when).

## What we care about most

Ranked. When two goals conflict, the higher goal wins.

1. **Zero trust** — the agent holds no tool credential. Tool secrets live only in the toolbox. (SEC)
2. **Mechanism vs meaning** — the framework is mechanism and names no vendor. Modules give
   meaning. (PLC)
3. **Least privilege** — every tool is declared, off unless enabled, and gated per tool. (TBX)
4. **Disableable modules** — a module can be turned off. Its dependencies are declared. (MOD)
5. **Simplicity** — the simplest thing that works. No dead code. Close the class, not the
   instance. (CODE, CLS)
6. **Tests and docs travel with the change.** (TST, DOC)

---

## SEC — Security (P0)

The security pass is mandatory. When there is no P0 finding, the report says so. Each branch's
`docs/architecture/zero-trust.md` has a `Known exceptions and debt` section. It is the one register
of security exceptions and gaps. An **accepted exception** is a row there that links a dated
DECISIONS.md entry with the owner's approval; nothing else accepts an exception. A listed item is
not a new finding, unless the change makes it worse or depends on it. A gap already in the repo that
is not listed is `DEBT`: the reviewer reports it, the loop records it for the owner, and the owner adds
the row. A gap the change adds is a finding under its SEC ID. A row marked `Part of the model` restates
what SEC-1 or CFG-1 allows; it is not an exception.

**SEC-1 Credential boundary.** The agent holds no tool credential (upstream API token, tool DB
user, SMTP password) and cannot read one. It holds only its own harness/provider credential in its
per-run HOME, and what zero-trust.md lists. Tool credentials live in the toolbox. A change makes cron
decrypt nothing beyond what a run needs to start, and passes no tool credential between containers.
An exception is an accepted exception (see above) whose DECISIONS.md entry lists every residual
channel it accepts, with its lifetime. Other docs point to that entry; they do not restate it.

**SEC-2 Decryption path.** For each `obscure` field, trace where it is decrypted, not only who can
see it. `bootstrap()` decrypts DEFAULT-scope obscure config. `resolve_all()` decrypts every obscure field in
its scope chain. `get_module(name)` decrypts every obscure field of that module, agent_view scope
included. `ScopedConfigService.get(path)` decrypts only that path. A dataclass that drops the field is not enough. A Python caller reads
non-secret fields with per-path `.get()` or `get_module(name, include_obscure=False)`. A secret that
a channel keeps toolbox-only is kept out of the Python resolver with the branch's mechanism
(`"access": "toolbox_only"` where it exists, else scope flags plus an ENV guard on both sides, as in
the github module).

**SEC-3 Harness runtime config.** A harness reads only its own module's fields listed in
`runtime_config_fields`, resolved per path as `{module}/{field}`. The harness context is never built
with `resolve_all()`. An `obscure` field, or a field whose schema is not an object, is not allowed in
the list.

**SEC-4 Secrets and PII in changed lines.** No hardcoded credential, token, key, or connection
string. No real PII (email, user name, customer id); reserved domains such as `example.com` are fine.
No internal host, IP, or company domain. No value that must come from ENV or config. A secret found
stops the review.

**SEC-5 Input safety.** PyMySQL queries use `%s` parameters only. No unsanitized input to
`subprocess`, `os.system`, or a shell. Validate paths against traversal. Validate a user-, agent-, or
externally supplied URL before an httpx call. Escape rendered user content. A value rendered into a
generated file (Dockerfile, config) is validated against its schema, not shell-quoted (DECISIONS.md
D9). A pattern that an operator or an external party writes, and that is matched against external
input, has a time bound (Python: `regex` with `timeout=`, DECISIONS.md 2026-07-24). A fixed pattern,
or a glob with every metacharacter escaped, needs none.

**SEC-6 Logs and errors.** No secret in a log line or an error response. No stack trace, path, config
value, or SQL to an external caller. An external person's address, or an allow-list pattern, is logged
as a domain or a short hash. An id the operator configures (mailbox UPN, agent_view code, binding id)
may be logged. Where a message can quote a credential, log the exception class and a sanitized
message, not `exc_info=True` (example: `_log_alert_failure` in `modules/app_monitor/src/observers.py`).
A logger namespace joins `log.ATTACHED_NAMESPACES` only after its
`exc_info=True` sites are audited.

**SEC-7 Authorization and scope.** Scope (agent_view, job, thread, message) comes from a trusted
source: the capability row (where the branch has it), the trigger, the run. A change adds no path that takes scope from an id
the agent or a caller supplies. Caller arguments can narrow scope, never grant it. Where the branch
has no trusted source yet (the toolbox caller-id row in zero-trust.md), a new path uses the pattern
of the Outlook and Bitbucket REST handlers: the caller's id selects that view's config, an unknown id
fails closed with 404 (DECISIONS.md 2026-06-19 D-5), and the plan names the row. A change adds no
path that lets agent_view A read agent_view B's config, credential, or data. Credentials in the DB are
encrypted or hashed, and a change adds no plaintext copy that zero-trust.md does not list. A
single-use credential is consumed atomically and has a replay test. A change to the harness
credential flow (register, lease, refresh, disable, deregister) writes a rotated token back only to
its own `credential` row, never re-enables a credential the operator disabled
(`update_refreshed_credentials`), and leaves no disabled or deregistered credential in a run's HOME
(`remove_credentials`).

**SEC-8 Two languages.** Node (the toolbox) holds tool credential code. Python (cron and consumer)
runs agents and harness/provider credential code. Keep each kind of code on its own side.

**SEC-9 Fail closed.** A value that is missing, empty, or cannot be parsed (agent_view_id, kinds list,
allow-list, DMARC verdict) grants nothing, unless it is an accepted exception in zero-trust.md. An
explicit
operator opt-out is not a missing value: it has a security warning in its `system.json` description
and a safe default. No sentinel value means "all" or "global". A scan or scrub that did not finish
never reports clean. A change that removes a guard, or makes it match less, lists each invariant the
guard enforces and where each one is enforced after the change.

**SEC-10 Web origin.** A browser-facing endpoint puts no credential or one-time code in a URL. Its
cookies are `__Host-`, `HttpOnly`, and `SameSite`. A state-changing request checks `Origin`. CORS is
an explicit allow-list.

**SEC-11 Dependencies (P1).** A new dependency has no known CVE and is maintained. Add none where the
stdlib or an installed dependency does the job.

## TBX — Toolbox and tools

**TBX-1 Session isolation (P0).** Trigger: a change under `src/agento/toolbox/**` or
`src/agento/modules/*/toolbox/**`. The toolbox imports a module once per process but calls
`register(server, context)` once per session. State that is per-session in meaning is a `const`
inside `register()` or comes from `context`. Module scope may hold process-wide state that is shared
by design: the server's session table, a lazily created pool or client, a static table. The defect
is module-scope state that grows with the number of sessions or differs between them, including an
exported getter that closes over a `let` that `register()` rebinds. Check:
```bash
grep -rnE "^(let|const) +[A-Za-z_][A-Za-z0-9_]* *= *(\[\]|new (Map|Set|WeakMap|WeakSet)\(\)|\{\})|^let " \
  --include='*.js' --exclude-dir=node_modules --exclude-dir=tests \
  src/agento/toolbox src/agento/modules/*/toolbox
# for each hit: grep -nE "\b<name>\b\.(push|set|add|delete)|\b<name>\s*=" <file>
# a write inside register() or a handler it registers is the defect
```
The grep is a first pass: it misses filled initializers and index, property, or method writes. Then
read each module-scope binding in the touched file for a write inside `register()` or a handler it
registers (assignment, index or property write, `unshift`, `splice`, `pop`, `clear`, `Object.assign`).
Incident: `core/toolbox/browser.js` `registeredPassthroughNames` grew for 9 h until `tools/list`
timed out and the agent reported `SUCCESS` for work it never did.

**TBX-2 Off unless enabled, gated per tool (P0).** A missing `is_enabled` means disabled. A
`config.json` default of `1` is only for a tool of the agent's built-in toolkit (DECISIONS.md
2026-06-04 lists them); a new one needs a DECISIONS.md entry. A datastore or customer adapter tool,
and a skill, ships no default of `1`. Each registration is gated on its own `tools/<name>/is_enabled`.
Never gate on a module-level `is_enabled` key (an early return for a missing credential is fine). A
shared switch is a declared key that each member points at with `requires`. `is_enabled` is the only
switch that decides whether a tool is registered or callable: do not add a second tool allow-list. A
data allow-list that limits what an enabled tool may reach (`core/email_whitelist`, `allowed_senders`)
is a different thing, and SEC-9 applies to it.

**TBX-3 Declared tools (P1).** Every tool a module can register is in its `module.json` `tools[]`,
including a proxied or computed name. Tool names are globally unique. A `config.json`
`tools/<name>/is_enabled` default belongs to a tool that the same module declares.

**TBX-4 Injected services (P1).** Toolbox module code gets framework services from `context` (for
example `context.fileManager.download()`), not from an import of framework JS.

**TBX-5 Agent-side extensions (P1).** A file that an agent CLI loads from a build directory (the Pi
bridge) has zero runtime dependencies. Zod does not apply there, so validate every trust boundary by
hand.

## PLC — Placement: framework or module

The framework provides mechanics, core modules define meaning, modules deliver features.

**Terms.**
- **framework** — `src/agento/framework/` (Python) and `src/agento/toolbox/` (the JS server).
- **core module** — any module under `src/agento/modules/`. It ships with the framework.
- **the `core` module** — `src/agento/modules/core/` only.
- **user module** — `app/code/<name>/`, per deployment. With a PyPI module, it is a
  **third-party module**. Its **publisher** is the owner prefix in its event names (EVT-4).
- **vendor** — an external product that agento drives or connects to as a harness, provider,
  channel, or tool service (Claude, Codex, Jira, Outlook, GitHub, Playwright). A generic protocol or
  datastore type (the SMTP and HTTP config-tester probes; MySQL, MSSQL, OpenSearch in the toolbox
  `ADAPTERS` map) is not a vendor.

**PLC-1 Where the logic goes [plan] (P1).** The plan has a `Placement` line for each new unit of
logic: where it runs, which module owns it, and the question that decided each.

*Where it runs* — the first yes:
1. An MCP tool, or uses a tool credential, or makes the authenticated call with it? → Node, in the
   owning module's `toolbox/` (SEC-1).
2. Else → Python. (Module Node that is not a tool and must stay out of the secrets container runs as
   its own compose service; the plan says so, because that needs a framework compose-template entry.)

*Who owns it* — the first yes:
3. Only for one deployment? → a user module.
4. Names a vendor? → that vendor's module, or a feature module that lists it in `sequence`
   (`jira_periodic_tasks`). When the framework must act on it, the framework gets a vendor-free
   protocol or an empty registry, and the module declares its data in `di.json`.
5. A contract that modules plug into: protocol, registry, lifecycle step, event class, loader,
   validator, a table that several modules share, or a CLI group over those? → the framework
   (PLC-3). The dispatch call sits where the transition happens (EVT-2).
6. Vendor-free logic that a second module must import or call as a hard dependency now? → promote it
   to the framework, and register its default before the module loop so a module can override it
   (PLC-4). A soft dependency (MOD-1) does not count.
7. Has its own tables, commands, or observers? → its own core module (MOD-1).
8. A single vendor-free tool, converter, default router, or seed data patch that needs a module
   slot? → the `core` module.

Old code that does not fit (`tool:*` in agent_view, `ingress:*` in core, the Playwright client in
`src/agento/toolbox/`) stays where it is. New work follows the questions.

**PLC-2 The framework names no vendor (P1).** Framework code, CLI help text, and examples have no
vendor id, file name (`.claude.json`, `.codex/config.toml`, `CLAUDE.md`), CLI flag list, or sandbox
CLI pin. No `if provider == "claude"` branch. Docstrings and comments do not count. A file rendered
from module declarations and pinned by a test (`framework/docker/sandbox/Dockerfile`) does not count
either. A framework event about a channel carries a `channel` field; it is not named after one
channel. Adding a harness needs no hand edit of framework code: one `agent_harnesses` entry in
`di.json`, an `AgentHarnessAdapter`, and a re-render (docs/architecture/harness-contract.md).

**PLC-3 Extending a contract (P1).** A change to a protocol, registry, or event that modules implement
or observe lands vendor-free first. The same change migrates every in-tree implementation and keeps
`bin/test` green. Out-of-tree implementations keep loading: add a new member as CODE-3 says.

**PLC-4 Promote or keep (P1).** Promote vendor-free logic to the framework when a second user needs it
now. Code that looks alike but differs per vendor stays in each module. When you find a
framework-wide defect while you build a module, fix it once in the framework, or ship the module with
a local guard plus a DECISIONS.md entry with the owner's approval and a ROADMAP.md follow-up. That
local guard is the one allowed exception to CLS-1. A module-only variant of a shared fix without that
entry is a finding.

## MOD — Modules

**MOD-1 Dependencies and disabling (P1).** A module lists another module in `sequence` when it imports
that module's code or needs its tables. `sequence` is a hard dependency: `bootstrap()` raises when a
listed module is disabled, so a cold consumer start and `setup:upgrade` fail, other CLI commands only
warn, and a running consumer's hot-reload is left with empty registries. Do not list a module you do not need. A static `import` or
`from agento.modules.<x>` is always listed, even inside `try` (`test_no_cross_module_imports` checks
it). A string import (`importlib.import_module(...)` in a `try` whose `except ImportError` has a
working fallback) is a soft dependency and is not listed. The fallback covers an absent module, not a
disabled one. Reading `core/*` config needs no entry. Prefer
framework code and events to a module import. Disabling a module and its dependents leaves the system
working. Exception: the framework reads `core/*` and `agent_view/*` config, so those two modules must
not be disabled (nothing enforces this yet).

**MOD-2 One module, one package (P1).** An integration is one module: channel, workflows, Python and
JS tools, config, and knowledge. Do not split it into typed micro-modules.

**MOD-3 Setup files (P1).** Schema in `sql/*.sql`, data in `data_patch.json`, cron jobs in `cron.json`,
onboarding in `di.json`. A shipped SQL file or data patch does not change (migrations are keyed by file
name, so an edit never runs): add a new one. A data patch does not depend on which modules are enabled
at upgrade time (DECISIONS.md D16).

## EVT — Events

Events let a module react to a change without the framework or another module knowing it exists.
Scope: Python code. The Node toolbox has no event mechanism; do not add one.

**EVT-1 Extension points [plan] (P1).** A plan for a framework feature or a core module has an
`Extension points` section when it adds a state change: a status or lifecycle transition of a
persisted thing (job, credential, workspace build, config value, schedule, channel poll), or a hold
that silently stops work (`mailbox_stall_after`). A telemetry column, a loop step, a retry attempt
inside one operation, or a registry operation is not a state change. For each state change, write
one of:
- `emit <name>: payload <fields>; observer: <one sentence>` — the observer is an in-tree module (for
  example an `app_monitor` alert) or a concrete `app/code` use case; or
- `no event: <reason>` — no observer can be named, or EVT-3 applies.

A missing section is P1. A reviewer who disagrees with a `no event` reason writes a P2 finding. EVT-8
decides the shape of each `emit` line.

**EVT-2 Family consistency (P1).** When a transition has an event, every Python path that the change
adds or changes for that transition dispatches it: CLI, admin TUI, recovery, cron. Put the dispatch in
the shared function that all callers use. A Node path that makes the same transition (the
`schedule_followup` tool inserts a `job` row) cannot dispatch: the plan routes it through Python or
lists the gap.

**EVT-3 Unsafe events (P0).** Do not dispatch from a process that runs as root: observers are module
code, so they would run as root. A new payload field carries ids, paths, counts, or a reason enum. It
carries no secret, token, key, config value, or raw external input (email body, a routing payload);
the observer re-reads through its own gated path. A new member of a family carries the family's
existing fields (`job_*` events after claim carry `job`); the limits above apply to new fields. A new
secret-bearing event needs a DECISIONS.md entry. Existing payloads
that break this are `DEBT`: `credential_register_after`, `credential_refresh_after` and their
`token_*` aliases, `routing_*_after`, `module_register_before`, `security_breach_after`.

**EVT-4 Shape (P1).** Name: `{subject}_{verb}_{before|after}`, with no publisher prefix for framework
and core-module events. `{publisher}_{module}_…` is for third-party modules. Class: a dataclass
`{Subject}…Event`, usually past tense (`JobClaimedEvent`); a before/after pair may share one class
(`JobFinalizeEvent`). It lives in `framework/events.py` for framework and core-module events, in its
own module for a third-party event. Check prefix and suffix, not grammar. The name says what happened
(a throttle does not dispatch `*_auth_failed_after`). The start and finish events of one run fire
together or not at all. A new event is internal: do not re-export it from `framework/contracts/` until
an observer outside `src/agento/` needs it (a use case named under EVT-1 counts). A rename
dual-dispatches the old name with the old payload (DECISIONS.md D14). Observers are declared in
`events.json`, run synchronously, and never crash the caller.

**EVT-5 Explicit events (P1).** Use explicit domain and lifecycle events. Do not add an interception or
plugin layer, and do not add a generic before/after hook around every function.

**EVT-6 Docs row (P2).** A new or changed event has a row in `docs/architecture/events.md`: name,
class, fields, when. Its dispatch test is TST-1.

**EVT-7 Unobserved is not dead (P1).** A documented event that marks a state change (EVT-1) is not dead
code, even with zero in-tree observers: observers in `app/code` live outside this repo. A "remove this
event" finding is valid only when the event is undocumented and marks no state change, or its dispatch
point is gone.

**EVT-8 Event or protocol, before or after (P1).** An event fits when the reaction is a side effect, a
veto, or a field that any observer may set and the dispatcher reads back: the dispatcher needs no
single answer. When the caller needs exactly one answer, use a `di.json` protocol. Use `_after` by
default. Use `_before` only when the observer must act before the action commits, and say which case:
it sets a field that the dispatcher reads back (`job_finalize_before.verdict`), or it does a side
effect that must come first (`agent_view_run_start_before` writes AGENTS.md).

## CFG — Config

**CFG-1 Three-level fallback (P1).** Module config resolves ENV (`CONFIG__MODULE__PATH`, unless the
field sets `allowEnv: false`, where the branch has it) → DB (`core_config_data`, scoped agent_view → workspace → default) →
`config.json`. A new default goes in `config.json`, and a change adds no code fallback that repeats it.
A new module setting uses this system. A new process-level framework knob is an `AGENTO_*` env var
read in a `from_env()` classmethod.

**CFG-2 Cron env (P1).** An env var that cron or the consumer reads from compose is `AGENTO_*`. A name
that follows an external convention (driver, SDK) is added to the entrypoint whitelist and to the table
in `docs/architecture/cron-env-contract.md` in the same change. A name the whitelist does not match is
dropped.

**CFG-3 Config testers (P1).** A tester probe runs where the credential already lives and tests
own-module paths only. `error` ("could not check") is never shown as `fail`, and a stored value that
cannot be decrypted is `error`, never `not_configured`.

## CODE — Code

**CODE-1 Python (P1).** httpx (not requests), dataclasses (not Pydantic), PyMySQL (not
mysql-connector). Get the current time with `datetime.now(timezone.utc)`, never a naive local
`datetime.now()`. To compare with a PyMySQL `DATETIME` value, use naive UTC (`.replace(tzinfo=None)`)
(DECISIONS.md 2026-03-31 UTC-everywhere).

**CODE-2 Style (P2).** Python: relative imports inside the framework; absolute `agento.framework.*`
from modules and tests. Node: ES modules, `node:` prefix for built-ins, Zod for schema validation
(except TBX-5), camelCase names and UPPERCASE constants, `export function register(server,
context)`.

**CODE-3 Simplicity (P1).** Use the simplest solution that works. Three similar lines are better than
an early abstraction. Depend on protocols, not concretes. A member of a Protocol that registration
checks with `isinstance` (`AgentHarnessAdapter`) is required for every implementation. An optional
member of such a Protocol is read with `getattr(x, name, None)` or a signature check, with a comment
that says why (`stream_renderer` in `harness/protocols.py`). A member of a nested Protocol that
registration does not check (`WorkspaceAdapter`) is declared on it and called directly inside a
fail-safe branch, so an out-of-tree implementation without it keeps working (`credential_ttl_seconds`,
DECISIONS.md 2026-08-04 Refresh lease; D13).

**CODE-4 No dead code (P1).** No unused method, class, config file, or "maybe later" code. Reuse an
unused enum value before you add a parallel one. Exceptions: EVT-7, and compatibility code that CODE-5
keeps and ROADMAP.md lists for removal.

**CODE-5 Compatibility (P1).** Keep shipped contracts working: event names, config paths, CLI commands,
`di.json` keys, tool output shapes. A change that breaks one keeps the old form beside the new one, with
a removal entry in ROADMAP.md. It drops the old form at once when keeping it would break a P0 rule (a
second tool allow-list, a gate that can be bypassed): a data patch migrates what it can, and a
DECISIONS.md entry says why. An old form that never worked needs no alias. A change to a tool's output
shape is in the plan.

**CODE-6 Surgical (P2).** Change only what the task needs. Add no comments, docstrings, or type hints to
untouched code. Surgical limits unrelated edits; it does not allow leaving siblings of a defect
(CLS-1).

**CODE-7 Names and prompts (P2).** Singular table names (`job`, `schedule`; exception
`core_config_data`). Magento terms: observer, `di.json`, dispatch. Choices use `terminal.select()`;
text input uses `input()` with the default in brackets.

**CODE-8 Performance (P1).** Review the change for performance. In particular: no unbounded growth (memory, rows, files per job). No query per item in a
loop where one batch query does the same work; a per-item idempotent insert on a unique key is fine.
The consumer poll loop makes no call to a third-party service and no wait without a timeout.

## CLS — Defect classes

**CLS-1 Close the class (P1).** A class is a rename, a removed flow, a renamed config key, a changed
meaning of an existing value, event, or config contract, or every call site and twin (layer, adapter)
of a defective helper. It puts the whole repo in scope, including files the diff does not touch (docs,
`system.json` labels, tool descriptions, manifests). The reviewer reports one finding per class with
every instance and the search that found them. The fixer sweeps the whole class and adds one guard
test for it.

## TST — Tests

**TST-1 Red first (P1).** Each behaviour change has a test that fails without it: pytest + respx for
Python, vitest for JS, fixtures in `tests/fixtures/`. A new or changed event has a test that asserts
the dispatch and the payload fields an observer reads. Where no automated test is possible (entrypoint
shell, TUI layout), the change says how it was verified.

**TST-2 Guard the shape (P1).** A class guard test checks structure (AST, behaviour, import layers).
Use a scoped text search with an allow-list only where no structural check is possible.

**TST-3 Honest doubles (P1).** A test double for an external API or process fails where the real one
fails (DECISIONS.md D2h).

## DOC — Documentation

**DOC-1 Docs travel with the change (P2).** A doc that tells an operator or an implementer something
wrong (a command, flag, path, default, security step, runbook, contract) is a `DOC-1` finding at P1.

| Change | Update |
|---|---|
| CLI command added, removed, renamed | `docs/cli/`; `README.md` where it shows that command |
| Config path, or the fallback itself | `docs/config/`; AGENTS.md for a change to the fallback |
| Module added, renamed, removed | `docs/modules/`, the `README.md` module list |
| Container or trust boundary | `docs/architecture/` (incl. zero-trust.md), AGENTS.md, SECURITY.md |
| Event | `docs/architecture/events.md` (EVT-6) |
| Tool | `docs/tools/` |
| Roadmap item done or moved | `ROADMAP.md` |
| A rule | this file, in the same change |

**DOC-2 Record decisions (P2).** Add each non-obvious technical choice to DECISIONS.md. Do not rewrite
an entry already on the base ref: add a dated reversal bullet to it. A rename or pointer note in
parentheses is not a rewrite. An entry that another rule
requires (SEC-1 exception, EVT-3, PLC-4) has that rule's tier.

## PLN — Plans

**PLN-1 Verified facts [plan] (P1).** The plan lists each runtime fact its design depends on, with its
proof: `file:line` at the base ref for this repo's code, or the command, doc, or version for a
third-party fact (CLI flag, API field, library, shell, or browser behaviour). A fact without proof is
marked `ASSUMPTION`, with a spike step before the work that depends on it.

**PLN-2 One statement per contract [plan] (P1).** State each contract, number, and claim once, and point
to it from other places. Each pointer resolves to text that exists. A revision replaces the old plan
text; it never appends a correction. After a revision, sweep every summary of it in the plan and in the
files the change adds: definition of done, headers, tables. An obligation moved to another document or
phase exists at its destination, in a phase that has what it needs.

**PLN-3 Coverage (P1).** The plan covers every requirement of the task. The diff implements every
in-scope plan item. A missing or divergent item is a finding.

**PLN-4 Scope and settled items (P1).** The plan states what is in and out of scope, and ends with a
`Settled` list. Each settled item quotes or links the owner's words, with the date. An item without
that is a proposal, and findings against it are valid. An out-of-scope item becomes a follow-up in
ROADMAP.md or the plan. A finding that reopens a quoted settled item without new evidence is invalid,
in the plan and in the implementation review. Neither list waives a rule or a task requirement: only a
DECISIONS.md entry with the owner's approval waives a rule.

**PLN-5 No full code [plan] (P2).** A plan gives signatures, schemas, DDL, and a short snippet for the
hard part only. Full code is reviewed once, in the implementation phase.

---

## Report format

Three sections, in this order: **🔴 P0**, **P1**, **P2**. Each finding starts with `[ID] <claim>`, where
`ID` is a rule ID, `BUG`, `UNLISTED`, or `LINT` (a ruff, basedpyright, or eslint error; P1). A debt
finding starts with `[DEBT <ID>]` (a rule ID, `BUG`, or `UNLISTED`), goes in that tier's section, and
says "does not block". A violation the change makes worse, or depends on, is not debt: tag it with its
plain ID. Then give the evidence (`file:line`, or a command
and its output) and one fix where the fix is clear. One finding per class (CLS-1).
