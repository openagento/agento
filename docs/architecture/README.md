# Architecture

Agento runs as Docker containers with strict security separation between the AI agent and credential-holding services.

```
┌─────────────┐     JQL search      ┌─────────────┐    secrets.env     ┌──────────┐
│  Jira Cloud │◄────────────────────►│   Toolbox   │◄──────────────────►│  MySQL   │
│             │     REST API v2      │  (MCP+REST) │    credentials     │ cron_db  │
└─────────────┘                      │  port 3001  │                    └──────────┘
       ▲                             └──────┬──────┘                         ▲
       │ @mention, comment                  │ MCP (SSE/streamable_http)      │ jobs table
       │                                    ▼                                │
┌──────┴──────┐                      ┌─────────────┐    publish/consume  ┌──────────┐
│   Human     │                      │  Agent      │                    │   Cron   │
│  (reporter) │                      │  (sandbox)  │                    │ consumer │
└─────────────┘                      └─────────────┘                    └──────────┘
```

## Design Principles

1. **Two languages = security boundary** — Python (cron) runs the AI agent, Node.js (toolbox) holds secrets. You cannot accidentally mix credential code with agent execution code.

2. **Zero-trust credentials** — Agent container has NO secrets. All database/API access goes through Toolbox MCP tools. See [zero-trust.md](zero-trust.md).

3. **Magento-inspired modules** — Self-contained packages with config, tools, knowledge base. See [modules docs](../modules/).

## Further Reading

- [Containers](containers.md) — what runs where, volume mounts, networking
- [Harness Contract](harness-contract.md) — the harness / provider / model split and the agent plugin boundary
- [Workspace](workspace.md) — theme / build / runtime layers, what lives where, agent's view
- [Zero-Trust Security](zero-trust.md) — credential isolation model
- [Publisher-Consumer](publisher-consumer.md) — job queue architecture
- [Cron Env Contract](cron-env-contract.md) — `AGENTO_*` env-var prefix convention for the cron container
- [Event-Observer System](events.md) — cross-module communication via events
- [FileManager](file-manager.md) — external file handling, conversion, and security
