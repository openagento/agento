# versioned-folder:init

Create a versioned folder, optionally importing a directory as its first version.

```bash
uv run bin/agento versioned-folder:init <folder_code> [--source <dir>] [--actor <who>]
```

Shortcut: `vf:init`.

| Flag | Meaning |
|---|---|
| `--source <dir>` | Host directory to import as version 1. Omit for an empty folder |
| `--actor <who>` | Recorded in the audit row (default `admin`) |

## Why this is not an MCP tool

Creating a folder is administrative. The agent must be able to work inside folders an
administrator granted it, never to create new ones (PRD §10). Exposing creation as a
tool would also mean exposing an arbitrary host path to the model.

## How it runs

Unlike most commands, this one is **not** proxied into the cron container — cron can see
neither the host source directory nor the toolbox-only storage volume. It runs on the
host, walks `--source` itself, and pipes a JSON payload into the toolbox:

```
docker compose <flags> exec -T toolbox node \
  /app/modules/core/versioned_folders/toolbox/cli.js --actor <who>
```

Before walking `--source` it makes one extra call, `… cli.js --print-limits`, and uses the
answer as its pre-flight budget. The host cannot resolve module config itself — the
ENV → DB → `config.json` fallback reads a database on the container network — so asking
the toolbox is what keeps `config:set versioned_folders/limits/...` effective here instead
of being overridden by a constant compiled into the command. If the toolbox does not
answer, the command stops rather than guessing a default.

The toolbox side is the *same* service the MCP tools use, so the administrative path
gets the same locking, limits and audit row as an agent-driven change.

Because the command is host-local but provided by a module, it is listed in
`_LOCAL_MODULE_COMMANDS` rather than `_LOCAL_COMMANDS`: the former stops the Docker
proxy, the latter would also skip the module bootstrap that registers the command with
argparse.

## Source handling

- A top-level `.git` directory is skipped.
- A symbolic link **aborts** the import rather than being silently dropped, so an
  administrator learns their source contained one.
- A source `.gitignore` is ignored: every file walked is imported.
- Files are read on the host and sent base64-encoded; the toolbox never opens an
  arbitrary path.

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a
traceback. Re-running after a failed import is safe: a failed creation leaves no folder
behind.
