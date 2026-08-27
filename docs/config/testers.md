# Config Testers — "Test connection" for a config field

A field in a module's `system.json` can declare a **tester**: a probe that answers "do these stored
values actually work?" It surfaces in two places, both of which run the same probe:

- `agento config:test <path>` — the CLI, `--all` for every declared field
- `t` on a highlighted field in the admin TUI Config screen

## Why this exists

Two incidents, both the same shape — a credential that was wrong and a system that said nothing:

1. **SMTP.** `app_monitor` alerting was dead for weeks. The credential returned
   `535 5.7.8 authentication failed`, the observer swallowed the exception, and the warning went to a
   logger with no handler. Nothing was broken loudly enough to notice.
2. **SSH.** An interactively pasted private key was stored truncated to 36 bytes. `config:set`
   reported success, `identity:show` printed a fingerprint (the old fingerprint was a hash of the
   raw text — deterministic for any garbage), and four workspace builds failed with
   `Permission denied (publickey)`.

A tester makes both answerable in one keystroke, and makes the answer specific: not "broken" but
`535 5.7.8 authentication failed` / `PAIR_MISMATCH`.

## The four states

Never a boolean. `error` is not `fail`, and the difference is the entire point:

| State | Means | In the TUI |
|---|---|---|
| `ok` | The probe reached the system and the credential worked | information |
| `fail` | The probe reached the system and the credential was rejected | error |
| `not_configured` | A value the probe needs is not set — nothing to test | warning |
| `error` | The probe could not find out (toolbox unreachable, timeout, undecryptable value) | error |

`config:test` exits 0 for `ok` and `not_configured`, 1 for `fail` and `error`. An optional
integration nobody configured must not fail a deploy check; a credential nobody managed to test
must not be reported as working.

**A stored value that cannot be decrypted is `error`, never `not_configured`.** Reporting a stored
secret as absent is the misdiagnosis this feature exists to end.

## Where a probe runs

**A tester runs in the process that already holds the credential.** That is the whole architecture,
and it is why there is no "let the framework decrypt this for a tester" permission to argue about:

| Credential | Runs in | Why |
|---|---|---|
| SMTP, HTTP APIs, Graph | the **toolbox** | the only container with secrets — it already resolves and decrypts them for every tool |
| the agent_view SSH keypair | the **framework** (the module's own Python) | Node cannot parse an OpenSSH private key (`crypto.createPrivateKey` → `DECODER routines::unsupported`); `cryptography.load_ssh_private_key` can, and `agent_view:identity:show` already decrypts that field in-process |

A toolbox probe proves the **toolbox's** vantage point. That is the right one — the toolbox is what
sends the mail and calls the API — but it is not the cron container's, so a network path broken only
outside the toolbox will still read `ok`.

## The three declaration forms

All three go on the field, in `system.json`. **None of them touches `di.json`.**

### 1. A built-in kind

Two ship with the framework: `smtp` and `http`. Other fields are interpolated with
`{module/path}` — your **own** module's paths only.

```json
"smtp_pass": {
  "type": "obscure",
  "label": "SMTP password",
  "tester": {
    "kind": "smtp",
    "host": "{core/smtp_host}",
    "port": "{core/smtp_port}",
    "user": "{core/smtp_user}",
    "pass": "{core/smtp_pass}"
  }
}
```

`smtp` requires `host`; it connects, STARTTLSes (implicit TLS on port 465), and calls LOGIN.
**It never sends a message.**

```json
"jira_token": {
  "type": "obscure",
  "label": "Jira API token",
  "tester": {
    "kind": "http",
    "url": "{jira/jira_host}/rest/api/2/myself",
    "basic": ["{jira/jira_user}", "{jira/jira_token}"],
    "expect": 200
  }
}
```

`http` requires `url`; `basic: [user, pass]` or `bearer: "…"` for auth, `headers` for anything else,
`expect` for the status code (default 200). Only `http:` and `https:` URLs, credentials in the URL
are refused, and redirects are a failure rather than a silent success.

### 2. A named probe your module exports

For a credential no generic kind expresses — Outlook's Graph token, where the credential's *shape*
decides whether the secret flow or the certificate flow runs:

```json
"outlook_client_secret": { "type": "obscure", "tester": "graph_credentials" }
```

The bare string is sugar. `{"kind": "toolbox", "name": "graph_credentials"}` is the same
declaration written out, and both validate and run identically.

and in any `.js` file in your module's `toolbox/` directory:

```javascript
export const configTests = [{
  name: 'graph_credentials',
  paths: ['outlook/outlook_tenant_id', 'outlook/outlook_client_secret', /* … */],
  // `config` arrives keyed by FIELD name — the shape your existing helper takes.
  // `signal` is aborted when `timeoutMs` runs out: any network call you make MUST
  // forward it, or your request outlives the answer and keeps authenticating
  // against the remote account after the caller gave up.
  run: async ({ config, timeoutMs, signal }) => {
    // return { status: 'ok' | 'fail' | 'not_configured' | 'error', code, detail, ms }
  },
}];
```

The returned `status` must be one of those four strings — anything else is reported as
`ERROR [BAD_RESULT]`. `code` is printed unredacted, so it must be a label (`^[A-Z0-9_]{1,40}$`) and
never the credential; a code that carries one is replaced with `UNSPECIFIED`.

The name must be globally unique and the paths must belong to your module. These are checked by
`src/agento/toolbox/tests/config-test-declaration.test.js` (run by `bin/test`), which imports the
export and can therefore see it — `module:validate` reads manifests only and cannot know whether a
JavaScript export exists. So the shape is enforced again at registration: a declaration without a
`name` or a `run` function, or with a `paths` member that is not a `module/path` string, is DROPPED
(the field then answers `UNKNOWN_TESTER`), and one name registered twice fails CLOSED — both
declarations are discarded and the field answers `DUPLICATE_TESTER` rather than the toolbox picking
whichever file `readdir` returned first. A path outside your module answers `FOREIGN_PATH`, and a
name registered by another module answers `FOREIGN_TESTER`. Being
in `toolbox/` **is** the registration — do not re-export it from a second file there.

### 3. A module-local Python class

For a credential that must be inspected rather than used — the SSH keypair, where nothing is
contacted at all:

```json
"ssh_private_key": {
  "type": "obscure",
  "tester": { "kind": "local", "class": "src.testers.ssh_identity.SshIdentityTester" }
}
```

The class is imported from the declaring module's directory and gets `run(conn, *, scope, scope_id)`.
It resolves its own config. Reach for this only when the probe genuinely cannot run in the
toolbox — `local` is the exception in this design, not the default.

## Rules the framework enforces

- **Own-module paths only.** A tester may read its declaring module's config and nothing else
  (`FOREIGN_PATH`). A named probe must be registered by the module that names it
  (`FOREIGN_TESTER`).
- **No secret leaves a probe.** Every obscure value **this declaration** resolved is redacted out of
  the response. That is the complete set a probe can have seen: a named probe receives only its
  declared paths and no database handle, and a built-in probe receives only the interpolated spec.
- **A `tester` under `tools/` is rejected.** Tool fields are not testable paths.
- **Every placeholder must name a field that exists**, and a built-in kind must supply its required
  field. `module:validate` reports both, before any DB change.

## Why not a bash `test_cmd`

The original proposal was a shell string with the same `{module/path}` interpolation. The
interpolation survived; the shell did not:

- `$(…)`, backticks and `;` in a config value are code execution — and `core_config_data` is
  writable by anyone who can run `config:set`.
- The command line is visible to `ps` inside the container, so the secret leaks to any process there.
- `curl` exits **0** on a 401. A shell test's exit code cannot distinguish "rejected" from
  "unreachable", which is precisely the `fail` vs `error` distinction that makes the result useful.
- A string offers no place to redact from.

A typed spec keeps the ergonomics of the proposal and none of that.
