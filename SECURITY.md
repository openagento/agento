# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | Yes                |

## Reporting a Vulnerability

We take security seriously. If you discover a vulnerability, please report it responsibly.

**Preferred method:** [GitHub Security Advisories](https://github.com/saipix/agento/security/advisories/new)

This allows us to collaborate privately on a fix before public disclosure.

## Response Timeline

- **Acknowledgement:** Within 48 hours of report
- **Initial assessment:** Within 1 week
- **Fix target:** Within 90 days of confirmed vulnerability

## What Qualifies

The following are considered security vulnerabilities:

- Credential exposure (secrets leaking from the toolbox container)
- Container escape (sandbox breaking out of its isolation)
- Authentication or authorization bypass
- SQL injection or other injection attacks
- Unauthorized access to agent_view-scoped config or data

## What Does Not Qualify

- Denial of service requiring local access
- Issues in dependencies (report upstream, but let us know)
- Theoretical attacks without proof of concept

## Security Architecture

Agento uses a zero-trust container architecture:

- A **runner** (`runner-<i>`, where every agent CLI runs, `agento run` too) holds no tool or database credential and has no
  database access. It receives the **harness/provider** API credential its own run needs, and one documented
  exception: the SSH private key used for git, delivered per run into a private `ssh-agent` and never written
  to disk (a dated, scoped waiver — see [DECISIONS.md](DECISIONS.md) D-SSH-1). Known gaps: [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt).
- Headless jobs run the agent in a **runner** container (`runner-<i>`). The runner has no `env_file`, no
  database credential, no encryption key and no route to `web`. On Linux it also has no network route to
  MySQL; on Docker Desktop it can reach MySQL through `host.docker.internal`, so there the guard is that it
  holds no DB credential (see the debt rows in zero-trust.md). The consumer in `cron`
  sends each run over a Unix socket ([runner.md](docs/architecture/runner.md)).
- The database has three users: a migration user that only `setup:upgrade` uses, one DML user for the
  backend (`cron` and `web`), and one for the toolbox with only the tables its SQL uses (no `credential`).
- The **toolbox** is designed to be the only container that holds tool credentials (known gaps: [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt)), exposed via controlled MCP tool interfaces. The SSH exception above is the one credential that does not come from it.
- The toolbox `/admin/*` routes (artifact store administration) take only a **viewless** `internal_rest` capability. Only the backend can mint one; no agent capability reaches them ([auth-context.md](docs/architecture/auth-context.md#profiles)). Their users are the panel artifact API in `web` (each route checks its admin leaf) and the cron `artifact:auth` command. An artifact share password is stored only as a one-way hash: it is shown once and never read back.
- **Sub-agent delegation** (`agent_run`, opt-in): a job may queue a child job only in its own agent_view or in one the operator lists in `subagent/allowed_agent_views`, so delegation is not a path from a low-privilege view to another view's tools without consent. The parent job, view and depth come from the capability row, and `agent_result` (or a continuation by `job_id`) reaches only a job delegated from the caller's own line, in its thread ([docs/modules/subagent.md](docs/modules/subagent.md)).
- **Thread directory** (`/workspace/artifacts/_threads/<key>/`): the runs of one thread share its `shared/` dir, and the toolbox writes the thread's downloads (Jira attachments) to its `downloads/`, which runs only read. The framework gives a run only its own thread's `./shared` link; the boundary is the 128-bit random key plus one group (`agento_thread`) that every run has, so a run that knows another thread's absolute path can open it. `downloads/` is tool-owned: no run can add, rename or remove an entry on the path the toolbox writes through ([DECISIONS.md](DECISIONS.md) D-THREAD-2, [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt)).
- Config encryption uses AES-256-CBC for sensitive fields, with the key derived from `AGENTO_ENCRYPTION_KEY` by scrypt and a per-value salt.
- `web` (the panel API) and `cron` are one trusted backend: the same env file, with `AGENTO_ENCRYPTION_KEY`, and one DB user. The trust boundary is the runner, not the backend: the backend may decrypt what it needs, and no keyholder service is planned (DECISIONS.md D-BACKEND-2). A compromise of `web` therefore exposes the key and the plaintext credentials. A harness credential re-login started in the panel runs the vendor CLI in a runner; `web` encrypts the pasted code with the key ([zero-trust.md](docs/architecture/zero-trust.md#what-the-backend-holds), DECISIONS.md D-BACKEND-1).

See [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md) for the credential model and its known exceptions, and [docs/architecture/](docs/architecture/) for full details.
