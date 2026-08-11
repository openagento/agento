# Getting Started

Install Agento and create your first module in 5 minutes.

## Prerequisites

- Python 3.11+
- Docker + Docker Compose V2

Run `agento doctor` after installation to verify prerequisites.

## Path A — Docker Compose (recommended)

```bash
# Install the CLI
uv tool install agento-core         # or: pip install agento-core

# Create a project directory and install
mkdir my-project && cd my-project
agento install                       # Interactive wizard — scaffolds, starts, migrates
```

## Add Your First Module

```bash
agento module:add my-ecommerce \
  --description="My e-commerce platform" \
  --tool mysql:mysql_ecom_prod:"Production MySQL (read-only)"
```

This creates `app/code/my-ecommerce/` with module.json, config.json, and knowledge/ directory.

## Set Credentials

```bash
# Set database password — omit the value so agento prompts instead of putting
# the secret into your bash history. Auto-encrypted in DB (field is "obscure").
agento config:set my_ecommerce/tools/mysql_ecom_prod/pass
# → Paste value…  <Ctrl+D>

# Set host (plain text) — fine to pass inline
agento config:set my_ecommerce/tools/mysql_ecom_prod/host 10.0.0.1

# Or use ENV vars (highest priority, no DB needed)
# CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__HOST=10.0.0.1
```

## Verify

```bash
agento module:list               # Shows your module with tool count
agento config:list               # Shows all config values
agento token list                # Shows registered agent tokens
agento setup:upgrade --dry-run   # Shows pending work (should be none)
```

## Add Knowledge Base

Place documentation in `app/code/my-ecommerce/knowledge/`:

```bash
# Add system documentation
echo "# My E-commerce\n\nArchitecture overview..." > app/code/my-ecommerce/knowledge/README.md

# Build workspace to make it visible to the agent
agento workspace:build --all
```

## Register an Agent Credential

Credentials are registered per **credential scope** (the pool a `(harness, provider)` pair
draws from) and stored encrypted in the database — never as a file path. Selection is LRU
over the healthy pool, so there is nothing to "activate".

```bash
# Interactive OAuth (needs a TTY):
agento credential:register claude my-token

# Or an API key, read from stdin so it never reaches argv/shell history:
echo "$ANTHROPIC_API_KEY" | agento credential:register claude my-token --with-api-key
```

Then point the agent_view at the harness and provider:

```bash
agento config:set agent_view/harness  claude    --agent-view dev_01
agento config:set agent_view/provider anthropic --agent-view dev_01
```

See [cli/credentials.md](cli/credentials.md) for the pool model and recovery commands.

## Next Steps

- [Module Guide](modules/creating-a-module.md) — detailed module creation tutorial
- [Config System](config/) — understand the 3-level fallback
- [CLI Reference](cli/) — all available commands
