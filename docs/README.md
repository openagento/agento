# Agento Developer Documentation

Agento is an AI agent framework that automates Jira tasks using AI coding agents (Claude Code, OpenAI Codex, Pi) in Docker containers, with Magento-inspired modular architecture.

## Start Here

- [Getting Started](getting-started.md) — Install Agento and create your first module in 5 minutes

## Documentation

| Section | Description |
|---------|-------------|
| [CLI Reference](cli/) | `bin/agento` commands: install, modules, config, tokens |
| [Modules](modules/) | Creating and managing modules (module.json, config.json, knowledge base) |
| [Configuration](config/) | 3-level config fallback: ENV → DB → config.json |
| [Tools](tools/) | MCP tool adapters: built-in (MySQL, MSSQL, OpenSearch) and custom |
| [Architecture](architecture/) | Docker containers, zero-trust security, job queue |

## Key Concepts

- **Module** = self-contained package with tools, knowledge base, prompts, and config (like a Magento module)
- **Config resolution** = Magento-like 3-level fallback with encrypted secrets in DB
- **Reindex** = compile modules into runtime workspace (like `bin/magento setup:upgrade`)
- **Zero-trust** = Toolbox container holds all secrets; agent container has none

## Source Code References

| Component | Path | Language |
|-----------|------|----------|
| CLI | [bin/agento](../bin/agento) | Bash |
| Config loader | [docker/toolbox/config-loader.js](../docker/toolbox/config-loader.js) | Node.js |
| Tool adapters | [docker/toolbox/tools/](../docker/toolbox/tools/) | Node.js |
| Job consumer | [src/agento/framework/consumer.py](../src/agento/framework/consumer.py) | Python |
| Config CRUD | [src/agento/framework/core_config.py](../src/agento/framework/core_config.py) | Python |
| Example module | [modules/_example/](../modules/_example/) | JSON + Markdown |
