# Contributing to Agento

Thank you for your interest in contributing to Agento. This guide will help you get started.

## Prerequisites

- Python 3.11+
- Node.js 22+
- Docker + Docker Compose V2
- [uv](https://docs.astral.sh/uv/) (Python package manager)

## Getting Started

1. Fork the repository and clone your fork:

```bash
git clone https://github.com/<your-username>/agento.git
cd agento
```

2. Install dependencies and verify everything works:

```bash
uv sync --group dev
bin/test
```

## Project Structure

```
src/agento/
  framework/       # Core framework (CLI, config, events, consumer, setup)
  modules/         # Core modules shipped with the framework (list: bin/agento module:list)
  toolbox/         # Node.js MCP server (credential broker)

app/code/          # User modules (deployment-specific, gitignored)
  _example/        # Example module template

tests/             # Python test suite
docker/            # Docker Compose deployment
docs/              # Developer documentation
```

## Module Development

The easiest way to start a new module is to use the built-in generator:

```bash
bin/agento module:add my-module \
  --description="My custom module" \
  --tool mysql:my_db:"Database description"
```

You can also copy `app/code/_example/` as a starting template. Each module must have a `module.json` manifest. See [docs/modules/creating-a-module.md](docs/modules/creating-a-module.md) for details.

## Testing

Run the full test suite (JSON validation, linting, type checking, Python tests, JS tests):

```bash
bin/test
```

Run individual test suites:

```bash
# Python tests
uv run pytest -q

# JavaScript tests
cd src/agento/toolbox && npm test
```

## Code Style

- **Python:** Formatted and linted with [ruff](https://docs.astral.sh/ruff/) (line-length 120). Type checked with basedpyright.
- **JavaScript:** Linted with ESLint. ES modules only (`import`/`export`).

Run linting manually:

```bash
uv run ruff check src/ tests/
cd src/agento/toolbox && npx eslint .
```

## Pull Request Process

1. Create a branch from `main`:

```bash
git checkout -b feature/my-change
```

2. Make your changes and add tests.

3. Run the full test suite:

```bash
bin/test
```

4. Submit a pull request against `main`.

## Commit Messages

Follow the commit-message format in [GIT-WORKFLOW.md](GIT-WORKFLOW.md).

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Please read it before participating.
