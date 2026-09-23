# Agent Identity (SSH + Credentials)

Each `agent_view` has its own identity: SSH private key, optional public key, optional `~/.ssh/config`, the git commit author (`git_author_name`/`git_author_email`), and (via `credential:register`) its agent CLI credentials. **Secrets** (the SSH private key, OAuth credentials) are stored **encrypted in the database** (`obscure` fields); **non-secret public metadata** like the git author name/email is stored as plaintext (`string` fields) — it ends up in public commit metadata anyway. Workspace builds materialize reusable identity/config templates and carry **no** `.ssh/` directory; each run copies that build into an artifacts directory, writes the **non-secret** SSH files there, and uses the artifacts directory as the agent's `$HOME`. The **SSH private key is never written to disk** anywhere: it is resolved per run and loaded into a per-run `ssh-agent` (see [How It Reaches the Agent Process](#how-it-reaches-the-agent-process)).

## Registering a New Agent — Quick Start

Minimum viable onboarding for a fresh `agent_view`, using the interactive paste flow (no host paths leaked into Docker, no volume mounts):

```bash
# 1. Register the agent CLI credential for its SCOPE (Claude / Codex OAuth).
#    It joins that scope's LRU pool automatically — there is no primary flag.
agento credential:register claude dev_01
# 1b. Bind BOTH axes: the harness alone does not identify the model vendor, and the
#     consumer resolves the credential pool from the (harness, provider) pair.
agento config:set agent_view/harness  claude    --scope=agent_view --scope-id=<agent_view_id>
agento config:set agent_view/provider anthropic --scope=agent_view --scope-id=<agent_view_id>

# 2. Paste the SSH private key into the encrypted DB field
agento config:set agent_view/identity/ssh_private_key --agent-view dev_01
# → "Paste value for agent_view/identity/ssh_private_key, then press Ctrl+D…"
# <paste the key, press Enter, then Ctrl+D>

# 3. Paste the matching public key (optional but recommended — enables fingerprint display)
agento config:set agent_view/identity/ssh_public_key --agent-view dev_01
# <paste the .pub line, Ctrl+D>

# 4. Verify — show prints the fingerprint, check proves the key parses and pairs
agento agent_view:identity:show dev_01
agento agent_view:identity:check dev_01

# 5. Materialize the workspace build template
agento workspace:build --agent-view dev_01
```

Everything below explains the mechanism.

### Verifying the key

`agent_view:identity:show` prints a real OpenSSH SHA256 fingerprint derived from the key material,
and `agent_view:identity:check` goes further: it parses the stored private key, derives its public
key, and compares that with the stored `ssh_public_key`. It **never prints the private key**. The
same check runs on `config:set agent_view/identity/ssh_private_key`, which now refuses a value that
does not parse.

| Result | Meaning |
|---|---|
| `OK` | the private key parses and its derived public key matches `ssh_public_key` |
| `NOT_SET` | no private key is stored for this scope |
| `INVALID_KEY` | a value is stored but it does not parse as a private key — the incident's shape |
| `ENCRYPTED_KEY` | the key is passphrase-protected, so the build cannot use it unattended |
| `NO_PUBLIC_KEY` | the private key is fine, but no `ssh_public_key` is stored to compare it with |
| `PAIR_MISMATCH` | both are stored and they are not a pair — a stale `.pub` from an older key |
| `DECRYPT_FAILED` | a row exists and did not decrypt: `AGENTO_ENCRYPTION_KEY` is not the one it was stored with (reported by `identity:check`, which reads the stored row) |
| `CHECK_FAILED` | the stored identity could not be READ at all — the DB is down, a manifest is broken. Says nothing about the key; the exception type is printed, never the value (`identity:check` only) |

`DECRYPT_FAILED` is the one result that says nothing about the key itself — fix the encryption key
before regenerating anything.

This exists because a truncated paste used to be stored silently: the pre-0.16 fingerprint was a
hash of the raw stored text, so it printed happily for a 36-byte fragment and four workspace builds
failed with `Permission denied (publickey)` before anyone connected the two.

## Why DB, Not Filesystem

- **Per-agent_view identity** — each agent_view can have its own git/SSH identity without manual file juggling (a separate identity, not a security boundary between views — see [What this does and does not confine](#what-this-does-and-does-not-confine))
- **3-level fallback** — identity resolves through `agent_view → workspace → default` like any other scoped config
- **Encryption at rest** — `ssh_private_key` is marked `type: "obscure"` in `system.json` and is auto-encrypted by `config:set` (AES-256-CBC, same mechanism as other obscure fields in `core_config_data`)
- **Backups** — your SQL backup already captures identity; no separate key-management dance

## Storing an SSH Key

There is no SSH-specific CLI — identity fields go through the generic `config:set`, which reads the value from the argument, a pipe, or an interactive paste (when stdin is a TTY). Because the field is declared `type: "obscure"`, the value is encrypted automatically.

```bash
# Interactive paste (recommended — no key material in shell history / ps aux)
agento config:set agent_view/identity/ssh_private_key --agent-view dev_01
# Paste key, press Ctrl+D

# Pipe (scripts / CI)
cat ~/.ssh/agent_dev_01 | agento config:set agent_view/identity/ssh_private_key --agent-view dev_01

# Public key (plaintext — same mechanism, different field)
cat ~/.ssh/agent_dev_01.pub | agento config:set agent_view/identity/ssh_public_key --agent-view dev_01
```

### Scope shortcuts

- `--agent-view <code>` — expands to `--scope agent_view --scope-id <lookup>`; mutually exclusive with `--scope-id`.
- `--scope default` / `--scope workspace --scope-id <id>` — for a workspace-wide or global fallback key. Plain `config:set` flags, nothing identity-specific.

## Inspecting Identity

```bash
agento agent_view:identity:show <agent_view_code>
```

Shows the public key (if stored), a fingerprint tag for the private key, and preview lines of `ssh_config` / `known_hosts`. **The private key is never printed.**

## Removing Identity

Identity rows are removed via generic `config:remove`:

```bash
agento config:remove agent_view/identity/ssh_private_key --agent-view dev_01
agento config:remove agent_view/identity/ssh_public_key  --agent-view dev_01
agento config:remove agent_view/identity/ssh_config      --agent-view dev_01
agento config:remove agent_view/identity/ssh_known_hosts --agent-view dev_01
```

## Configuration Fields

Defined in `src/agento/modules/agent_view/system.json`:

| Path | Type | Notes |
|---|---|---|
| `agent_view/identity/ssh_private_key` | `obscure` | Encrypted at rest; decrypted **per run** and loaded into a per-run `ssh-agent`. Never written to a file. Limited to **16 KiB** (see [Size limit](#size-limit-on-the-private-key)). |
| `agent_view/identity/ssh_public_key` | `textarea` | Plaintext; written **per run** to `<artifacts_dir>/.ssh/id_rsa.pub` — never into the build. |
| `agent_view/identity/ssh_config` | `textarea` | Optional — contents of `~/.ssh/config` (Host/IdentityFile blocks for multi-host setups). |
| `agent_view/identity/ssh_known_hosts` | `textarea` | Optional — pre-populated trust entries. |
| `agent_view/identity/git_author_name` | `string` | Optional — git commit author `user.name`. Written to `<build_dir>/.gitconfig` `[user]`. |
| `agent_view/identity/git_author_email` | `string` | Optional — git commit author `user.email`. **Must be a verified email on the target Bitbucket/Git account, or commits will not link to it.** |

All six support the standard 3-level scope fallback: `agent_view → workspace → default`.

### Git commit author identity

The agent commits with `git` inside the sandbox; the SSH key authenticates the *push* but does **not**
set the commit author. The configured identity is applied **two ways**, so it always wins:

1. **`~/.gitconfig` `[user]`** — `workspace:build` materializes it from `git_author_name` /
   `git_author_email` (each value is single-line-encoded with git's own double-quoting, so control
   chars / `#` / `\` cannot corrupt or inject config).
2. **`GIT_AUTHOR_NAME/EMAIL` + `GIT_COMMITTER_NAME/EMAIL` env vars** — exported on the agent process at
   run time (by the consumer and by `agento run`). Git env vars override **every** gitconfig level —
   including a **repo-local `.git/config [user]`**, which would otherwise beat the global `~/.gitconfig`
   (e.g. a reused clone that carries a stale identity). This is what makes the author deterministic.

If neither field is set, no `.gitconfig` is written, no git env is exported, and the author falls back to
whatever the base image/CLI provides (often wrong) — i.e. behaviour is unchanged for unconfigured views.

**Linking rule:** Bitbucket (and GitHub) link a commit to an account *only* when the commit author email
matches a **verified** email on that account — set `git_author_email` accordingly. For Bitbucket views,
`bitbucket` onboarding seeds both fields automatically from the verified account (see
[bitbucket.md](../modules/bitbucket.md)); override anytime with `config:set`. For GitHub views, `github`
onboarding seeds them from the verified account too, defaulting the email to
`<id>+<login>@users.noreply.github.com` — the `users.noreply` form **always** links, so no separately
verified address is needed (see [github.md](../modules/github.md)).

## How It Reaches the Agent Process

1. `agento workspace:build --agent-view <code>` resolves scoped overrides and writes **no SSH files at all** — neither the private key nor the public ones are part of the build. It also **prunes** any private key a pre-0.15 build left in that view's generations, and **fails** (rather than building) if one cannot be removed.
2. `agento run` and the consumer copy the current build into a per-run artifacts directory, write the non-secret SSH files into `<artifacts_dir>/.ssh/` (0700 dir, 0600 files) and delete any `id_rsa` found there, then set `HOME=<artifacts_dir>` on the agent subprocess. The **private key** is resolved from config into process memory, passed to the wrapper as `AGENTO_SSH_PRIVATE_KEY`, and the wrapper ([`agento.framework.ssh_prelude`](../../src/agento/framework/ssh_prelude.py)) starts a **per-run `ssh-agent`**, feeds the key to `ssh-add` over an inherited file descriptor (never argv, never a file), scrubs the key from the environment with `env -u`, and `exec`s the agent command with `SSH_AUTH_SOCK` as its only secret-bearing variable — the non-secret `GIT_SSH_COMMAND`, `AGENTO_SSH_TTL` and `SSH_AGENT_PID` stay in that environment; `AGENTO_SSH_PRIVATE_KEY` is what `env -u` removes. The agent therefore holds a **signing socket**, not a key.
   - `GIT_SSH_COMMAND` is exported so `git` uses the run's identity directory and the agent socket.
   - **Caveat:** the wrapper no longer symlinks anything into the process's passwd home (`/root`, `/home/agent`) — those directories are actively kept empty by the container entrypoints. OpenSSH expands `~/.ssh/` via `getpwuid(getuid())->pw_dir`, not `$HOME`, so a bare `ssh` invoked **without** `GIT_SSH_COMMAND` sees no `~/.ssh` and no key file; it still authenticates through `SSH_AUTH_SOCK`, but it will not pick up `ssh_config` or `ssh_known_hosts`. Use `git`, or pass `-F <HOME>/.ssh/config`.
   - If the wrapper cannot deliver the key safely it **drops** it (warns, unsets `SSH_AUTH_SOCK`) rather than falling back to a file; and if it finds a planted identity in the passwd home that it cannot remove, it refuses to run (exit 78).
3. `git_author_*` are materialized into `<build_dir>/.gitconfig` `[user]`. git reads `~/.gitconfig` via `$HOME`, which is already set to the artifacts dir. `.gitconfig` is **copied** (not symlinked) into each per-run artifacts dir, so a run-time `git config` write stays private to that run instead of corrupting the shared build.
4. The same `git_author_*` values are also exported as `GIT_AUTHOR_*`/`GIT_COMMITTER_*` on the agent process (consumer: subprocess env; `agento run`: `docker exec -e`). These env vars take precedence over **all** gitconfig files — so even a repo-local `.git/config [user]` in a reused clone cannot override the configured author. Derived via [`agento.framework.git_identity`](../../src/agento/framework/git_identity.py).

See [workspace-build.md](../cli/workspace-build.md) for the full build flow.

### Size limit on the private key

`agent_view/identity/ssh_private_key` declares `maxLength: 16384` in `system.json`, enforced at every
config entry point (`config:set`, the admin editor) and re-checked when the run resolves it. It is an
input **sanity bound** on a secret that travels through the process environment — nothing more. It is
**not** the mechanism that keeps the key off disk; that is the per-run `ssh-agent` above. See
[the generic `maxLength` constraint](README.md#maxlength-on-text-fields).

### The `CONFIG__*` ENV scope cannot carry the private key

The 3-level fallback is ENV → DB → `config.json` for every field, with **one refused path**: the
**cron** entrypoint writes every `CONFIG__*` value to its credential-store file, so a PEM arriving
that way would be the key on disk — which is exactly what this release removed. The **sandbox** entrypoint has no such file (it `exec`s `gosu agent`, so the environment
passes through intact); it refuses the variable for the second reason below, not this one. Both
entrypoints **refuse to start** when
`CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY` is set in the container environment (exit 78) and
tell you to set it in the DB instead:

```bash
cat id_rsa | bin/agento config:set agent_view/identity/ssh_private_key \
  --scope agent_view --scope-id <id>
```

Pipe it — never pass the key as an argument (`"$(cat id_rsa)"`): an argument is visible in
`/proc/<pid>/cmdline` to every process of that uid and lands in the shell history file.

The same entrypoints refuse an ambient `AGENTO_SSH_PRIVATE_KEY`, `AGENTO_SSH_TTL`, `SSH_AUTH_SOCK`,
`SSH_AGENT_PID` or `GIT_SSH_COMMAND`: a run's identity is resolved per run, and an ambient value
would be inherited by **every** agent_view's runs — including views granted no key at all. The
framework strips the same names from the environment it hands each spawn, so the consumer process's
own environment cannot leak into a job either.

Other multiline fields (`ssh_public_key`, `ssh_config`, `ssh_known_hosts`, `agents_md`, `soul_md`)
share the cron transport limitation but are not secrets and are not refused — set them in the DB if a
`CONFIG__*` value arrives truncated.

### What this does and does not confine

The key is never a file — not on the shared `/workspace` mount, not in the container's `/tmp`. It is
**not** isolated from another agent_view: every view runs as the same uid in the same container, so a
same-uid peer can still reach the wrapper's pre-`exec` environ, the inherited descriptor, the live
agent socket, and the consumer process's heap. The complete enumeration, with lifetimes, is in
[DECISIONS.md](../../DECISIONS.md) (D-SSH-1). Per-view OS uids are the tracked follow-up
([ROADMAP.md](../../ROADMAP.md)).

## Clearing keys older versions wrote to disk

Versions before this change wrote the private key into every build generation. Those copies stay
readable until something deletes them, and a rebuild only cleans the view it rebuilds — so the purge
is an **ordered upgrade step**, not an optional cleanup:

1. **Deploy this version** (`agento upgrade`, which runs `setup:upgrade`). Purging first is pointless:
   the old code writes the key again on the next build.
2. **Purge:** [`agento workspace:ssh-purge`](../cli/workspace-build.md#workspacessh-purge) (deprecated, removal in v0.17+) —
   `--dry-run` first if you want to see the list before confirming. The consumer may keep running:
   on this version no run writes a key file, and the command is reached by exec'ing into the
   **running** cron container, so stopping cron stops the purge too.
3. **Rotate** every affected `agent_view/identity/ssh_private_key`, and install the new public key
   **on the remote** — that is what authenticates: the run forces `-o IdentitiesOnly=no`, so git offers
   the agent-loaded identity whatever the local `id_rsa.pub` contains. Set
   `agent_view/identity/ssh_public_key` too (a separate config value, written into the run HOME) so the
   files describing the identity match the identity in use. Deleting a copy cannot un-leak a key that
   sat readable on a shared mount.

To purge a deployment that is still on the old code, cron must be stopped and the sweep run in a
one-off container — the exact commands are in
[the CLI reference](../cli/workspace-build.md#where-it-belongs-in-an-upgrade--the-order-matters).

## Agent Credentials (per credential scope)

The same DB-obscured pattern applies to OAuth credentials registered via `credential:register`. Credentials are stored inside the `credential.credentials` column (encrypted) instead of referencing a JSON file on disk. See [credentials.md](../cli/credentials.md) for the CLI.
