# Creating a Module

Step-by-step guide to add a new system integration to Agento.

## 1. Create the Module

```bash
bin/agento module:add my-crm \
  --description="My CRM system" \
  --repo=git@github.com:org/my-crm.git \
  --tool mysql:mysql_crm_prod:"CRM Production MySQL (read-only)" \
  --tool mysql:mysql_crm_staging:"CRM Staging MySQL"
```

This creates `app/code/my-crm/` with module.json, config.json, and knowledge/ directory.

## 2. Edit config.json (Non-Secret Defaults)

```bash
cat > modules/my-crm/config.json << 'EOF'
{
  "tools": {
    "mysql_crm_prod": {
      "host": "10.0.1.50",
      "port": 3306,
      "user": "crm_reader",
      "database": "crm_production"
    },
    "mysql_crm_staging": {
      "host": "10.0.1.51",
      "port": 3306,
      "user": "crm_reader",
      "database": "crm_staging"
    }
  }
}
EOF
```

## 3. Set Credentials

```bash
# Omit the value → agento prompts, you paste, press Ctrl+D.
# Keeps the password out of `ps aux` and your shell history.
bin/agento config:set my_crm/tools/mysql_crm_prod/pass
bin/agento config:set my_crm/tools/mysql_crm_staging/pass
```

Passwords are auto-encrypted in the database (field type = `obscure`). See [docs/cli/config.md#secrets](../cli/config.md#secrets--never-pass-on-the-command-line).

## 4. Add Knowledge Base

Create documentation that the AI agent will use during tasks:

```bash
cat > modules/my-crm/knowledge/README.md << 'EOF'
# CRM System

## Architecture
- MySQL database with 3 main tables: contacts, deals, activities
- REST API at api.crm.example.com

## Key Tables
- `contacts` — id, email, name, company_id, created_at
- `deals` — id, contact_id, value, stage, closed_at
- `activities` — id, deal_id, type, description, created_at

## Common Queries
- Find contact: `SELECT * FROM contacts WHERE email = 'user@example.com'`
- Deal pipeline: `SELECT stage, COUNT(*) FROM deals GROUP BY stage`
EOF
```

## 5. Add Diagnostic Prompts (Optional)

```bash
cat > modules/my-crm/prompts/diagnose-sync-issue.md << 'EOF'
# Diagnose CRM Sync Issues

1. Check last sync timestamp: `SELECT MAX(synced_at) FROM contacts`
2. Look for error records: `SELECT * FROM sync_log WHERE status = 'error' ORDER BY created_at DESC LIMIT 20`
3. Compare counts: source vs CRM
EOF
```

## 6. Build Workspace

```bash
agento workspace:build --all
```

This compiles module content into per-agent_view workspace builds. The agent reads knowledge/ and prompts/ from the materialized build directory.

## 7. Verify

```bash
# Check module is listed
bin/agento module:list

# Check config values
bin/agento config:list my_crm

# Restart toolbox to load new tools (if containers are running)
cd docker && docker compose restart toolbox
```

The agent can now query your CRM database using the `mysql_crm_prod` MCP tool and reference the knowledge base during task execution.

## 8. Subscribe to Events (Optional)

Create `events.json` to react to job lifecycle events:

```bash
cat > modules/my-crm/events.json << 'EOF'
{
  "job_succeeded": [
    {"name": "crm_job_succeeded", "class": "src.observers.JobSucceededObserver"}
  ]
}
EOF

mkdir -p modules/my-crm/src
cat > modules/my-crm/src/observers.py << 'EOF'
import logging

logger = logging.getLogger(__name__)

class JobSucceededObserver:
    def execute(self, event):
        logger.info("Job %d completed in %dms", event.job.id, event.elapsed_ms)
EOF
```

See [Event-Observer System](../architecture/events.md) for all available events.

## 9. Register Capabilities (Optional)

If your module provides channels, workflows, or CLI commands, create `di.json`:

```bash
cat > modules/my-crm/di.json << 'EOF'
{
  "channels": [
    {"name": "crm", "class": "src.channel.CrmChannel"}
  ],
  "commands": [
    {"name": "crm-sync", "class": "src.commands.sync.CrmSyncCommand"}
  ]
}
EOF
```

Channel classes implement the `Channel` protocol. Command classes implement the `Command` protocol. Import both from `agento.framework.contracts`:

```python
# src/channel.py
from agento.framework.contracts import Channel, PromptFragments

class CrmChannel:
    @property
    def name(self) -> str:
        return "crm"

    def get_prompt_fragments(self, reference_id: str) -> PromptFragments:
        return PromptFragments(
            read_context=f"Read CRM ticket {reference_id}.",
            respond="Post the result as a comment.",
        )
```

```python
# src/commands/sync.py
import argparse

class CrmSyncCommand:
    @property
    def name(self) -> str:
        return "crm-sync"

    @property
    def help(self) -> str:
        return "Sync CRM tickets to job queue"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def execute(self, args: argparse.Namespace) -> None:
        print("Syncing CRM tickets...")
```

After bootstrap, your channel is available via `get_channel("crm")` and your command as `agento crm-sync`.

If your channel routes inbound requests to different agent_views by a **regex sender pattern**
(rather than an exact identity), declare the identity type in `di.json` so both the runtime router
and the `ingress:bind` CLI treat its bindings as regexes:

```json
{ "regex_identity_types": ["crm_sender"] }
```

Operators then `agento ingress:bind crm_sender '<regex>' <view> --priority <n>`; highest priority
wins, a tie between different views is ambiguous. See [di.json format](module-json.md#dijson) for
all capability types.

## 10. Declare Config Schema (Optional)

If your module has configurable fields, create `system.json`:

```bash
cat > modules/my-crm/system.json << 'EOF'
{
  "api_url": {"type": "string", "label": "CRM API URL", "default": "https://api.crm.example.com"},
  "api_token": {"type": "obscure", "label": "API Token"},
  "sync_interval": {"type": "integer", "label": "Sync interval (minutes)", "default": 15}
}
EOF
```

Set sensitive values via CLI (auto-encrypted). Omit the value so it isn't saved in bash history:

```bash
bin/agento config:set my_crm/api_token
# Paste…  <Ctrl+D>
```

Read config at runtime:

```python
from agento.framework.bootstrap import get_module_config

config = get_module_config("my-crm")
api_url = config.get("api_url")  # resolved via 3-level fallback
```

See [Config System](../config/README.md) for the full fallback chain.

## 11. Add Custom Toolbox Tools (Optional)

Create `toolbox/` directory with JS files that export `register(server, context)`:

```bash
mkdir -p modules/my-crm/toolbox
cat > modules/my-crm/toolbox/crm-api.js << 'EOF'
import { z } from 'zod';

export function register(server, { log }) {
  server.tool(
    'crm_search_contacts',
    'Search CRM contacts by name or email.',
    {
      user: z.string().email().describe('Agent email from SOUL.md'),
      query: z.string().describe('Search term'),
    },
    async ({ user, query }) => {
      // Your tool logic — use process.env for credentials
      log('crm_search_contacts', 'OK', `user=${user} query="${query}"`);
      return { content: [{ type: 'text', text: JSON.stringify([]) }] };
    }
  );
}
EOF
```

Convention-based discovery: any `.js` file in `toolbox/` is auto-discovered by the Toolbox at startup. The `context` object provides `{ app, log, db, playwright, fileManager }` -- no imports from framework files needed.

Always log tool activity via the injected `log` (never import a logger or hardcode a file path). The Toolbox auto-routes it: handlers registered as MCP tools (`server.tool(...)`) log to `toolbox_mcp.log`, while REST route handlers (`app.post('/api/...')`) log to `toolbox_rest.log`. You don't pass a type -- the correct log is injected per registration path.

Restart toolbox after adding tools:

```bash
cd docker && docker compose restart toolbox
```

**Security note:** When handling files from external systems, always use `context.fileManager.download()` -- never download files directly with `fetch` + `writeFile`.

## 12. Register File Converters (Optional)

If your module needs to convert file formats (e.g., DOCX to plain text), create a `toolbox/converters.js` file that exports a `converters` array:

```bash
cat > modules/my-crm/toolbox/converters.js << 'EOF'
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

async function convertDocx(inputPath) {
  const outputPath = inputPath.replace(/\.docx$/i, '.txt');
  await execFileAsync('pandoc', [inputPath, '-t', 'plain', '-o', outputPath]);
  return outputPath;
}

export const converters = [
  { fromExt: '.docx', toExt: '.txt', convert: convertDocx },
];
EOF
```

Each converter implements: `{ fromExt, toExt, convert(srcPath) -> convertedPath }`. The framework auto-discovers and registers converters during module loading. See [FileManager](../architecture/file-manager.md) for the full architecture.

## 13. Declare Schema Migrations (Optional)

If your module needs database tables, create a `sql/` directory with numbered SQL files:

```bash
mkdir -p modules/my-crm/sql
cat > modules/my-crm/sql/001_create_crm_cache.sql << 'EOF'
CREATE TABLE IF NOT EXISTS crm_cache (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    contact_id VARCHAR(255) NOT NULL,
    data JSON,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_contact (contact_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
EOF
```

Applied by `setup:upgrade` in module dependency order. Tracked in the `schema_migration` table.

## 14. Declare Data Patches (Optional)

Create `data_patch.json` to seed or transform data:

```bash
cat > modules/my-crm/data_patch.json << 'EOF'
{
    "patches": [
        {"name": "SeedDefaults", "class": "src.patches.seed_defaults.SeedDefaults"}
    ]
}
EOF

mkdir -p modules/my-crm/src/patches
cat > modules/my-crm/src/patches/seed_defaults.py << 'EOF'
class SeedDefaults:
    def apply(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO core_config_data (scope, path, value) "
                "VALUES ('default', 'my_crm/sync_interval', '15')"
            )
        conn.commit()

    def require(self):
        return []  # No dependencies; use ["other_module/PatchName"] for ordering
EOF
```

Patch classes implement `apply(conn)` and `require()`. Applied by `setup:upgrade` in topological order.

## 15. Declare Cron Jobs (Optional)

Create `cron.json` to schedule CLI commands:

```bash
cat > modules/my-crm/cron.json << 'EOF'
{
    "jobs": [
        {"name": "crm_sync", "schedule": "*/15 * * * *", "command": "crm-sync"}
    ]
}
EOF
```

The `command` references a CLI subcommand contributed via `di.json`. Installed by `setup:upgrade` into the system crontab.

## 16. Declare Onboarding (Optional)

If your module needs to configure external systems (create API resources, set up custom fields, etc.) before it can work, declare an interactive onboarding flow in `di.json`:

```json
{
  "onboarding": "src.onboarding.CrmOnboarding"
}
```

```python
# src/onboarding.py
import pymysql
import logging

from agento.framework.bootstrap import get_module_config
from agento.framework.config_resolver import load_db_overrides
from agento.framework.core_config import config_set

class CrmOnboarding:
    def is_complete(self, conn: pymysql.Connection) -> bool:
        overrides = load_db_overrides(conn)
        return "my_crm/api_workspace_id" in overrides

    def describe(self) -> str:
        return "Configure CRM API workspace"

    def run(self, conn: pymysql.Connection, config: dict, logger: logging.Logger) -> None:
        workspace_id = input("  CRM workspace ID: ").strip()
        if not workspace_id:
            print("  Error: Workspace ID is required.")
            return

        config_set(conn, "my_crm/api_workspace_id", workspace_id)
        conn.commit()
        print(f"  Saved workspace ID: {workspace_id}")
```

Onboarding runs as step 5 of `setup:upgrade` — after migrations, data patches, and cron. It's skipped when already complete, in `--dry-run`, or with `--skip-onboarding` (for CI/CD). The user is prompted before each module's onboarding runs.

See [di.json onboarding](module-json.md#onboarding) for the full protocol reference.

## Module Structure (Final)

```
modules/my-crm/
  module.json                 # Tool definitions + metadata
  config.json                 # Default non-secret values (hosts, ports)
  di.json                     # Capability bindings: channels, workflows, commands, onboarding (optional)
  system.json                 # Config field schemas with types (optional)
  events.json                 # Event observer declarations (optional)
  data_patch.json             # Data patch declarations (optional)
  cron.json                   # Cron job declarations (optional)
  sql/                        # Schema migrations (optional)
    001_create_tables.sql
  src/                        # Python code (runs in cron container)
    channel.py                # Channel implementation (optional)
    onboarding.py             # Interactive onboarding (optional)
    observers.py              # Observer classes (optional)
    patches/
      seed_defaults.py        # Data patch classes (optional)
    commands/
      sync.py                 # CLI command classes (optional)
  toolbox/                    # JS code (runs in toolbox container)
    crm-api.js                # Custom MCP tools (optional)
  knowledge/
    README.md                 # System overview + schema docs
  prompts/
    diagnose-sync-issue.md    # Diagnostic methodology
  skills/
```
