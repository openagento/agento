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

- The **sandbox** (where AI agents run) holds no tool or database credential and has no database
  access. It does receive the **harness/provider** API credential its own run needs — that is what
  runs the model — and one further documented exception: the SSH private key used for git, delivered per run into a private `ssh-agent`
  and never written to disk (a dated, scoped waiver — see [DECISIONS.md](DECISIONS.md) D-SSH-1).
- The **toolbox** is the only container holding the credential store (API keys, tokens, DB credentials), exposed via controlled MCP tool interfaces (known gaps: [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt)). The SSH exception above is the one credential that does not come from it.
- Config encryption uses AES-256-CBC for sensitive fields, with the key derived from `AGENTO_ENCRYPTION_KEY` by scrypt and a per-value salt.

See [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md) for the credential model and its known exceptions, and [docs/architecture/](docs/architecture/) for full details.
