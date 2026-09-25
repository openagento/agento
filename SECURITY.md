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

- The **sandbox** (where interactive `agento run` executes) has no tool credentials and no direct database access. An agent's own harness credential (OAuth) lives in its per-run HOME. Headless jobs run the agent inside the `cron` container, which inherits the DB password and the encryption key — a known gap, listed with the others in [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt).
- The **toolbox** is designed to be the only container that holds tool credentials (known gaps: [zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt)), exposed via controlled MCP tool interfaces.
- Config encryption uses AES-256-CBC for sensitive fields.

See [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md) for the credential model and its known exceptions, and [docs/architecture/](docs/architecture/) for full details.
