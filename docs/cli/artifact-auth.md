# artifact:auth

Set, rotate, show or disable an artifact's HTTP Basic auth.

```bash
uv run bin/agento artifact:auth <artifact_code> [--user <u>] [--pass-stdin] [--disable] [--show] [--actor <who>]
```

Shortcut: `ar:au`.

| Flag | Meaning |
|---|---|
| `--user <u>` | Basic auth user. Empty defaults to the artifact code |
| `--pass-stdin` | Read the password from stdin (`printf '%s' "$PW" \| … --pass-stdin`), or from a prompt, typed twice, on a terminal. Without it the password is a strong random one |
| `--disable` | Turn Basic auth off — removes the sidecar the server gates on |
| `--show` | Print the current credential instead of changing it |
| `--actor <who>` | Recorded in the audit row (default `admin`) |

Leaving out `--user` and `--pass-stdin` enables auth with the artifact code as the user
and a fresh strong password. The password is printed **only** by the command that sets it
(or by `--show`), so pass it on when you see it.

It also prints the artifact's **share URL**, `https://<token>.<AGENTO_SHARE_HOST>[:port]/`: the
only address the credential opens (PRD E6 §9.1). The token stays the same when you rotate the
password; `--disable` removes it, and the old URL answers `404`. With `AGENTO_SHARE_HOST` empty
or invalid the line reads `share: not configured`. See
[versioned-artifacts.md → Shares](../modules/versioned-artifacts.md#shares).

**There is no tool equivalent, and there will not be one.** `agent_view_id` is asserted by
the caller, so ownership *scopes* and does not authorize — a self-asserted identity must not
be able to change who may read a published tree. The toolbox refuses the operation for any
caller that is not the admin CLI.

## How auth is stored

The password is kept in two forms that never meet: a one-way scrypt hash in the served tree
(`published/<code>/.auth`) that the secret-free serving container verifies against, and an
AES-encrypted copy in the `versioned_artifact` row that `--show` reads back. Setting auth
needs `AGENTO_ENCRYPTION_KEY`; without it the command fails with `AUTH_UNAVAILABLE`. See
[versioned-artifacts.md → Basic auth](../modules/versioned-artifacts.md#basic-auth).

## How it runs

Host-local (`_LOCAL_MODULE_COMMANDS`), exec'ing into the toolbox with `--op auth`
(or `--op auth-show` for `--show`).

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.
