# Running the panel

The panel is the HTTP API that the `web` service serves behind `proxy` (E2 ships no frontend yet;
see [ROADMAP.md](../../ROADMAP.md)). This page is
for the operator who deploys it. The design is in
[../architecture/panel.md](../architecture/panel.md).

## Restriction: agents share one UID (read this first)

All agent runs use the same `agent` UID and the same workspace mount. A shell-capable agent can
read another run's directory, including that run's live toolbox capability. RBAC controls what
the panel **API** lets a user do. It does not control what a process can read from the shared
mount.

**Until per-run UID or container isolation exists, expose the panel only where every user is
trusted with every agent_view that the agents can reach.** Do not use panel roles to separate
users who must not see each other's agent data. This is an open item in
[ROADMAP.md](../../ROADMAP.md).

## Hosts and ports

`proxy` publishes one port on the loopback interface, `127.0.0.1:${AGENTO_PROXY_PORT:-8443}`,
and serves three origins on it:

| Variable | Default | Origin |
|---|---|---|
| `AGENTO_PANEL_HOST` | `panel.localhost` | the panel API, `/api/*` |
| `AGENTO_APPS_HOST` | `apps.localhost` | launched artifact files, `/a/<code>/v/<id>/…` |
| `AGENTO_SHARE_HOST` | `share.localhost` | Basic-auth shares (E6; every request is denied until then) |
| `AGENTO_PROXY_PORT` | `8443` | the host port; `443` gives origins with no port |

Set the variables in `docker/.env`. The generated `docker-compose.yml` gives the same values
to `proxy` and to `web`. `web` needs them because it compares the `Origin` header of every
write with the exact panel origin, and it builds the launch redeem URL on the apps origin. If you
override one of them in `docker-compose.override.yml`, override it for both services.

TLS is `tls internal` (Caddy's own CA). A browser does not trust that CA until you install it,
or until you replace `tls internal` with a real certificate. The panel cookie has the `__Host-`
prefix, so the panel works only over HTTPS.

## First admin

Create the first user at the CLI. The password comes from a prompt, or from stdin when stdin is
not a terminal. It never comes from argv:

```bash
bin/agento user:create admin --role admin          # prompts twice for the password
bin/agento user:create ci-admin --role admin < pw  # from a mode-0600 file
```

A password has 12 to 1024 characters. A client signs in with `POST /api/session` on
`https://panel.localhost:8443`. See
[../cli/user.md](../cli/user.md) and [../cli/grant.md](../cli/grant.md).

## Giving a user role access

A `user` sees nothing until you grant its role access in a scope. A scope is one workspace, or
one agent_view:

```bash
bin/agento grant:add --role user --tool versioned_artifact_get_current --agent-view dev_01
bin/agento grant:add --role user --operation artifact.launch --agent-view dev_01
bin/agento tool:enable versioned_artifact --agent-view dev_01
bin/agento tool:enable versioned_artifact_get_current --agent-view dev_01
```

A grant never enables a tool. A tool that `is_enabled` turns off at that scope is refused for
every role, `admin` included. To launch an artifact, a role needs both grants above in the
artifact's scope, and the view must be allowed to use the artifact (its own, or listed in
`versioned_artifacts/allowed_artifacts`).

## Launch limits

`web/launch/max_concurrent` (default `5`, hard ceiling `20`) is how many live launches one user
may hold. A new launch past the limit ends the oldest one. Set it per workspace:

```bash
bin/agento config:set web/launch/max_concurrent 3 --scope=workspace --scope-id=2
```

The launch lifetime is `core/auth/launch_max_ttl`, and the session lifetime is
`core/auth/session_max_ttl` (see [../architecture/auth-context.md](../architecture/auth-context.md)).

## What `web` holds

`web` has no `env_file`, no `AGENTO_ENCRYPTION_KEY`, and it never runs `bootstrap()`. It
stores only SHA-256 hashes of session tokens, launch tokens and exchange codes. It mounts
`app/code` and `app/etc` read-only, so it sees the same module list as `cron` when it checks a
grant or a config write. The admin config form refuses every field that it cannot prove is not a
secret: set those with `bin/agento config:set`.

## Checking a deployment

```bash
bash docker/smoke/proxy-smoke.sh
```

Step 7 of the smoke signs in, launches an artifact, and redeems it through the real proxy. It
seeds a user named `e2-smoke-user` and an artifact named `e2-smoke` at `SMOKE_AGENT_VIEW`
(default `dev_01`). Run it only on a development stack.
