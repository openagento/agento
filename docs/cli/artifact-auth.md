# artifact:auth

Set, rotate or disable an artifact's HTTP Basic auth.

```bash
uv run bin/agento artifact:auth <artifact_code> [--user <u>] [--pass-stdin] [--disable] [--actor <who>]
```

Shortcut: `ar:au`.

| Flag | Meaning |
|---|---|
| `--user <u>` | Basic auth user. Empty defaults to the artifact code |
| `--pass-stdin` | Read the password from stdin (`printf '%s' "$PW" \| … --pass-stdin`), or from a prompt, typed twice, on a terminal. Without it the password is a strong random one |
| `--disable` | Turn Basic auth off — removes the sidecar the server gates on |
| `--actor <who>` | Recorded in the audit row as `service:cli:<who>` (default `admin`) |

Leaving out `--user` and `--pass-stdin` enables auth with the artifact code as the user
and a fresh strong password. The command prints the password **once**. No copy is kept that
can be read back, so pass it on when you see it. To get a new one, run the command again.

It also prints the artifact's **share URL**, `https://<token>.<AGENTO_SHARE_HOST>[:port]/`: the
only address the credential opens (PRD E6 §9.1). The token stays the same when you rotate the
password; `--disable` removes it, and the old URL answers `404`. With `AGENTO_SHARE_HOST` empty
or invalid the line reads `share: not configured`. See
[versioned-artifacts.md → Shares](../modules/versioned-artifacts.md#shares).

**There is no tool equivalent, and there will not be one.** `agent_view_id` is asserted by
the caller, so ownership *scopes* and does not authorize — a self-asserted identity must not
be able to change who may read a published tree. Share auth is not in the panel either: a
password never goes to a browser (DECISIONS.md D-MINIAPP-2, O-4).

## How auth is stored

The password is kept in one form only: a one-way scrypt hash in the served tree
(`published/<code>/.auth`). The secret-free serving container verifies against it. See
[versioned-artifacts.md → Basic auth](../modules/versioned-artifacts.md#basic-auth).

## How it runs

In the `cron` container (the host CLI proxies it there; a terminal is passed through for the
password prompt). It mints a viewless `internal_rest` capability and calls
`POST /admin/versioned-artifacts/<code>/auth` on the toolbox, the same `admin` endpoint the
panel uses. No `docker compose exec` into the toolbox.

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.
