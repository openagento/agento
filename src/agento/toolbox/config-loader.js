import fs from 'fs';
import path from 'path';
import { getCronPool } from './db.js';
import { decrypt, hasEncryptionKey } from './crypto.js';
import { registerAdapterTools } from './adapters/index.js';
import { wrapHandler } from './adapters/large-result.js';
import { FileManager, ConverterRegistry } from './file-manager.js';
import { logToolboxRest } from './log.js';

const CORE_MODULES_DIR = process.env.CORE_MODULES_DIR || '/app/modules/core';
const USER_MODULES_DIR = process.env.USER_MODULES_DIR || '/app/modules/user';

/**
 * Scan a single modules directory and return parsed module manifests.
 */
function scanDir(dir) {
  if (!fs.existsSync(dir)) return [];

  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const modules = [];

  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith('_')) continue;

    const manifestPath = path.join(dir, entry.name, 'module.json');
    if (!fs.existsSync(manifestPath)) continue;

    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));
      manifest._path = path.join(dir, entry.name);
      modules.push(manifest);
    } catch (err) {
      console.error(`[config-loader] Failed to load ${manifestPath}: ${err.message}`);
    }
  }

  return modules;
}

/**
 * Scan all module directories (core + user) and return parsed manifests.
 */
export function scanModules() {
  return [...scanDir(CORE_MODULES_DIR), ...scanDir(USER_MODULES_DIR)];
}

/**
 * Read config.json defaults for a module.
 */
function readConfigDefaults(modulePath) {
  const configPath = path.join(modulePath, 'config.json');
  if (!fs.existsSync(configPath)) return {};

  try {
    return JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  } catch (err) {
    console.warn(`[config-loader] Failed to read ${configPath}: ${err.message}`);
    return {};
  }
}

/**
 * Load all core_config_data overrides from DB into a map.
 * Returns { 'path': { value, encrypted } }
 */
async function loadDbOverrides() {
  const overrides = {};
  try {
    const pool = getCronPool();
    const [rows] = await pool.query(
      "SELECT path, value, encrypted FROM core_config_data WHERE scope = 'default' AND scope_id = 0"
    );
    for (const row of rows) {
      overrides[row.path] = { value: row.value, encrypted: !!row.encrypted };
    }
  } catch (err) {
    console.warn(`[config-loader] Failed to load core_config_data: ${err.message}`);
  }
  return overrides;
}

/**
 * Load DB overrides with scoped fallback: global → workspace → agent_view.
 * Returns { overrides, agentViewMeta }.
 */
export async function loadScopedDbOverrides(agentViewId) {
  const overrides = await loadDbOverrides();
  let agentViewMeta = null;

  if (!agentViewId) return { overrides, agentViewMeta };

  try {
    const pool = getCronPool();

    const [avRows] = await pool.query(
      `SELECT av.id, av.workspace_id, av.label, av.code AS agent_view_code, w.code AS workspace_code
       FROM agent_view av
       JOIN workspace w ON w.id = av.workspace_id
       WHERE av.id = ?`,
      [agentViewId]
    );
    if (avRows.length === 0) {
      console.warn(`[config-loader] agent_view_id=${agentViewId} not found, using global config`);
      return { overrides, agentViewMeta };
    }

    const av = avRows[0];
    agentViewMeta = {
      id: av.id,
      label: av.label,
      workspaceId: av.workspace_id,
      workspaceCode: av.workspace_code,
      agentViewCode: av.agent_view_code,
    };

    // Layer workspace overrides
    const [wsRows] = await pool.query(
      "SELECT path, value, encrypted FROM core_config_data WHERE scope = 'workspace' AND scope_id = ?",
      [av.workspace_id]
    );
    for (const row of wsRows) {
      overrides[row.path] = { value: row.value, encrypted: !!row.encrypted };
    }

    // Layer agent_view overrides (highest priority)
    const [avConfigRows] = await pool.query(
      "SELECT path, value, encrypted FROM core_config_data WHERE scope = 'agent_view' AND scope_id = ?",
      [agentViewId]
    );
    for (const row of avConfigRows) {
      overrides[row.path] = { value: row.value, encrypted: !!row.encrypted };
    }
  } catch (err) {
    console.warn(`[config-loader] Failed to load scoped overrides: ${err.message}`);
  }

  return { overrides, agentViewMeta };
}

/**
 * List the ids of all active agent_views (view AND its workspace active), id order.
 * The JS mirror of Python's get_active_agent_views — used by the Outlook channel to
 * auto-derive the fleet mailbox set. Swallows DB errors (warns + returns []) so callers
 * fail safe, matching loadDbOverrides / loadScopedDbOverrides.
 */
export async function listActiveAgentViewIds() {
  try {
    const pool = getCronPool();
    const [rows] = await pool.query(
      `SELECT av.id FROM agent_view av
       JOIN workspace w ON w.id = av.workspace_id
       WHERE av.is_active = 1 AND w.is_active = 1
       ORDER BY av.id`
    );
    return rows.map((r) => r.id);
  } catch (err) {
    console.warn(`[config-loader] Failed to list active agent_views: ${err.message}`);
    return [];
  }
}

/**
 * Resolve a single config value by raw path through the standard fallback:
 * 1. ENV var: CONFIG__{PATH_WITH___} (slash -> __, dash -> _, uppercased)
 * 2. DB: core_config_data merged across the scope chain (decrypt-aware)
 * 3. config.json defaults (merged across modules, keyed by literal path)
 *
 * This is the toolbox's single config-resolution entrypoint for raw paths —
 * the mirror of Python's ScopedConfigService.get(). Callers must NOT index
 * dbOverrides directly, so ENV and config.json fallbacks always apply.
 */
export function resolveConfigValue(configPath, dbOverrides = {}, configDefaults = {}) {
  // 1. ENV var (highest priority)
  const envKey = `CONFIG__${configPath.replace(/\//g, '__')}`.toUpperCase().replace(/-/g, '_');
  if (process.env[envKey] !== undefined) return process.env[envKey];

  // 2. DB override (merged scope chain)
  const override = dbOverrides[configPath];
  if (override) {
    if (override.encrypted) {
      if (!hasEncryptionKey()) {
        console.warn(`[config-loader] Cannot decrypt ${configPath}: AGENTO_ENCRYPTION_KEY not set`);
        return null;
      }
      try {
        return decrypt(override.value);
      } catch (err) {
        console.error(`[config-loader] Failed to decrypt ${configPath}: ${err.message}`);
        return null;
      }
    }
    return override.value;
  }

  // 3. config.json default
  const def = configDefaults?.[configPath];
  return def !== undefined ? String(def) : null;
}

/**
 * Merge every module's config.json into one path-keyed defaults map.
 * Tool names are globally unique, so first-class defaults like
 * `tools/email_send/is_enabled` declared in a module's config.json resolve
 * without needing to know the owning module.
 */
export function loadConfigDefaults() {
  const merged = {};
  for (const mod of scanModules()) {
    Object.assign(merged, readConfigDefaults(mod._path));
  }
  return merged;
}

/**
 * Check if a tool is enabled via the `tools/{toolName}/is_enabled` config path.
 * Opt-in: enabled only when the resolved value is explicitly '1'. Resolution
 * goes through resolveConfigValue (ENV -> DB -> config.json), so a module may
 * ship a first-class tool enabled-by-default in its config.json while a missing
 * value (and explicit '0') both mean disabled. The scope chain is already merged
 * into dbOverrides, so an agent_view '0' overriding an inherited '1' resolves to
 * disabled.
 */
export function isToolEnabled(toolName, dbOverrides, configDefaults = {}) {
  return resolveConfigValue(`tools/${toolName}/is_enabled`, dbOverrides, configDefaults) === '1';
}

/**
 * Resolve a module-level field using 3-level fallback:
 * 1. ENV var: CONFIG__{MODULE}__{FIELD}
 * 2. DB: core_config_data at path {module}/{field}
 * 3. config.json defaults (top-level)
 */
export function resolveModuleField(moduleName, fieldName, configDefaults, dbOverrides) {
  // 1. ENV var (highest priority)
  const envKey = `CONFIG__${moduleName}__${fieldName}`
    .toUpperCase()
    .replace(/-/g, '_');
  if (process.env[envKey] !== undefined) {
    return process.env[envKey];
  }

  // 2. DB override
  const dbPath = `${moduleName}/${fieldName}`.replace(/-/g, '_');
  const override = dbOverrides[dbPath];
  if (override) {
    if (override.encrypted) {
      if (!hasEncryptionKey()) {
        console.warn(`[config-loader] Cannot decrypt ${dbPath}: AGENTO_ENCRYPTION_KEY not set`);
        return null;
      }
      try {
        return decrypt(override.value);
      } catch (err) {
        console.error(`[config-loader] Failed to decrypt ${dbPath}: ${err.message}`);
        return null;
      }
    }
    return override.value;
  }

  // 3. config.json default (top-level, not nested under tools)
  const defaultValue = configDefaults?.[fieldName];
  if (defaultValue !== undefined) return defaultValue;

  return null;
}

/**
 * Resolve a single tool field value using 3-level fallback:
 * 1. ENV var: CONFIG__{MODULE}__TOOLS__{TOOL}__{FIELD}
 * 2. DB: core_config_data (explicit overrides)
 * 3. config.json defaults
 */
function resolveField(moduleName, toolName, fieldName, configDefaults, dbOverrides) {
  // 1. ENV var (highest priority)
  const envKey = `CONFIG__${moduleName}__TOOLS__${toolName}__${fieldName}`
    .toUpperCase()
    .replace(/-/g, '_');
  if (process.env[envKey] !== undefined) {
    return process.env[envKey];
  }

  // 2. DB override
  const dbPath = `${moduleName}/tools/${toolName}/${fieldName}`.replace(/-/g, '_');
  const override = dbOverrides[dbPath];
  if (override) {
    if (override.encrypted) {
      if (!hasEncryptionKey()) {
        console.warn(`[config-loader] Cannot decrypt ${dbPath}: AGENTO_ENCRYPTION_KEY not set`);
        return null;
      }
      try {
        return decrypt(override.value);
      } catch (err) {
        console.error(`[config-loader] Failed to decrypt ${dbPath}: ${err.message}`);
        return null;
      }
    }
    return override.value;
  }

  // 3. config.json default
  const defaultValue = configDefaults?.tools?.[toolName]?.[fieldName];
  if (defaultValue !== undefined) return defaultValue;

  return null;
}

/**
 * Load all tools from all modules with resolved config.
 * Each tool has: { name, type, description, module, config: { host, port, user, pass, database } }
 */
export async function loadTools(dbOverrides = null) {
  const modules = scanModules();
  if (!dbOverrides) dbOverrides = await loadDbOverrides();
  const tools = [];

  for (const mod of modules) {
    const configDefaults = readConfigDefaults(mod._path);

    for (const tool of mod.tools || []) {
      const config = {};
      for (const fieldName of Object.keys(tool.fields || {})) {
        config[fieldName] = resolveField(mod.name, tool.name, fieldName, configDefaults, dbOverrides);
      }
      if ((tool.type === 'mysql' || tool.type === 'mssql')
          && config.client_connection_pool_max_per_tool === undefined) {
        config.client_connection_pool_max_per_tool = resolveField(
          mod.name,
          tool.name,
          'client_connection_pool_max_per_tool',
          configDefaults,
          dbOverrides
        );
      }

      tools.push({
        name: tool.name,
        type: tool.type,
        description: tool.description,
        module: mod.name,
        config,
      });
    }
  }

  return tools;
}

/**
 * Get tool adapter types registered across all modules.
 */
export function getModuleToolTypes() {
  const modules = scanModules();
  const types = new Set();
  for (const mod of modules) {
    for (const tool of mod.tools || []) {
      types.add(tool.type);
    }
  }
  return types;
}

/**
 * Discover toolbox/*.js files in a module directory.
 * Convention-based: any .js file in toolbox/ is auto-discovered.
 */
function discoverToolboxFiles(modulePath) {
  const toolboxDir = path.join(modulePath, 'toolbox');
  if (!fs.existsSync(toolboxDir)) return [];

  return fs.readdirSync(toolboxDir)
    .filter(f => f.endsWith('.js'))
    .map(f => path.join(toolboxDir, f));
}

/**
 * Register all tools:
 * 1. Config-driven adapter tools (mysql, mssql, opensearch) from module.json
 * 2. Convention-discovered JS tools from module toolbox/ directories
 *
 * @param {object} server - MCP server instance
 * @param {object} context - Shared context { app, log, db, playwright }
 * @returns {string[]} All registered tool names (adapter tools only; module tools are self-registering)
 */
/**
 * Resolve module-level config for all modules that have system.json.
 * Returns { moduleName: { field: resolvedValue } }
 */
export async function loadModuleConfigs(dbOverrides = null) {
  const modules = scanModules();
  if (!dbOverrides) dbOverrides = await loadDbOverrides();
  const moduleConfigs = {};

  for (const mod of modules) {
    const systemPath = path.join(mod._path, 'system.json');
    if (!fs.existsSync(systemPath)) continue;

    let system;
    try {
      system = JSON.parse(fs.readFileSync(systemPath, 'utf-8'));
    } catch {
      continue;
    }
    if (!system || Object.keys(system).length === 0) continue;

    const configDefaults = readConfigDefaults(mod._path);
    const resolved = {};
    for (const fieldName of Object.keys(system)) {
      resolved[fieldName] = resolveModuleField(mod.name, fieldName, configDefaults, dbOverrides);
    }
    moduleConfigs[mod.name] = resolved;
  }

  return moduleConfigs;
}

/**
 * Create a FileManager instance from module configs.
 * Parses allowed extensions and max file size from core config.
 */
function createFileManager(moduleConfigs, log) {
  const coreConfig = moduleConfigs.core || {};
  const extString = coreConfig['toolbox/file_manager/allowed_extensions']
    || '.pdf,.xlsx,.xls,.csv,.txt,.md,.json,.xml,.html,.png,.jpg,.jpeg,.gif,.svg,.webp';
  const allowedExtensions = new Set(extString.split(',').map(e => e.trim().toLowerCase()));
  const maxFileSize = parseInt(coreConfig['toolbox/file_manager/max_file_size'], 10) || 524288000;

  const converterRegistry = new ConverterRegistry();
  return new FileManager({ converterRegistry, allowedExtensions, maxFileSize, log });
}

/**
 * Register module REST API routes on the Express app at startup.
 * This ensures endpoints like /api/jira/request are available before any
 * MCP session connects (needed by setup:upgrade onboarding).
 */
export async function registerModuleRestApis(context) {
  const modules = scanModules();
  const dbOverrides = await loadDbOverrides();
  const moduleConfigs = await loadModuleConfigs(dbOverrides);
  const fileManager = createFileManager(moduleConfigs, context.log);
  const enrichedContext = { ...context, moduleConfigs, loadModuleConfigs, loadScopedDbOverrides, listActiveAgentViewIds, fileManager };

  const sorted = [...modules].sort((a, b) => (a.order || 100) - (b.order || 100));

  for (const mod of sorted) {
    const files = discoverToolboxFiles(mod._path);
    for (const file of files) {
      try {
        const toolModule = await import(file);
        if (Array.isArray(toolModule.converters)) {
          for (const conv of toolModule.converters) {
            fileManager.converterRegistry.register(conv);
          }
          logToolboxRest('discovery', 'OK', `Registered ${toolModule.converters.length} converter(s) from ${mod.name}/toolbox/${path.basename(file)}`);
        }
        if (typeof toolModule.register === 'function') {
          // Pass a stub server — only Express routes matter at startup
          const stubServer = { tool: () => {} };
          await toolModule.register(stubServer, enrichedContext);
          logToolboxRest('startup', 'OK', `Registered REST APIs from ${mod.name}/toolbox/${path.basename(file)}`);
        }
      } catch (err) {
        logToolboxRest('startup', 'ERROR', `Failed to load ${file}: ${err.message}`);
      }
    }
  }
}

export async function registerTools(server, context, agentViewId = null, preloadedOverrides = null) {
  const modules = scanModules();

  // Use pre-loaded overrides if available, otherwise resolve from DB
  let dbOverrides;
  let agentViewMeta = null;
  if (preloadedOverrides) {
    dbOverrides = preloadedOverrides;
  } else if (agentViewId) {
    ({ overrides: dbOverrides, agentViewMeta } = await loadScopedDbOverrides(agentViewId));
  } else {
    dbOverrides = await loadDbOverrides();
  }

  // Resolve module-level config (system.json fields) — passed to JS tools via context
  const moduleConfigs = await loadModuleConfigs(dbOverrides);
  const configDefaults = loadConfigDefaults();
  // `requires` on a module.json tools[] entry declares a toolset-master relationship: the tool
  // is available only if it AND every tool up its requires chain resolve '1'. Declaring it beats
  // a hand-written module-level early-return, which was invisible to Python (the admin Tools
  // screen would show a child enabled while the runtime denied it) and made per-tool keys dead.
  const requiresByTool = new Map();
  const declaredToolNames = new Set();          // union: used by the unscoped adapter/startup gate
  const declaredByModule = new Map();           // per module: used while a module's register() runs
  for (const mod of modules) {
    const owned = new Set();
    for (const t of mod.tools || []) {
      if (t.name) { declaredToolNames.add(t.name); owned.add(t.name); }
      if (t.requires) requiresByTool.set(t.name, t.requires);
    }
    declaredByModule.set(mod.name, owned);
  }
  // Set while a module's register() runs, so both the gate and the server.tool wrapper can
  // attribute what they see to the module responsible.
  let currentModule = null;
  // Names a module ASKED about but nobody declares. A well-behaved module consults the gate and
  // skips registration, so it never reaches the server.tool wrapper below — without recording the
  // lookup, drift in a computed-name group (the browser passthrough) would be invisible: exactly
  // the case the runtime WARN exists for.
  const undeclaredLookups = [];

  // Declaration is REQUIRED, not just a display concern: `tool:enable` accepts any snake_case
  // name, so without this an operator could enable `browser_something_new` and a passthrough
  // loop would register a tool no manifest declares — invisible in admin.
  //
  // The declaration must belong to the module ASKING. Against the union, a module could compute
  // a name another module declares and enables, pass the gate, and register its own handler under
  // that module's grant — the per-module drift WARN only reports it afterwards, too late. Outside
  // a module's register() (`currentModule === null`: adapter tools, the startup REST pass) the
  // union is correct, since those names come from manifests by construction.
  const enabledCheck = (toolName) => {
    const owned = currentModule ? declaredByModule.get(currentModule) : declaredToolNames;
    const declared = owned || new Set();
    const seen = new Set();
    let current = toolName;
    while (current) {
      if (seen.has(current)) return false;   // cycle: fail closed
      seen.add(current);
      if (!declared.has(current)) {
        if (currentModule) undeclaredLookups.push({ name: toolName, module: currentModule });
        return false;
      }
      if (!isToolEnabled(current, dbOverrides, configDefaults)) return false;
      current = requiresByTool.get(current) || null;
    }
    return true;
  };
  const fileManager = createFileManager(moduleConfigs, context.log);
  const enrichedContext = {
    ...context,
    app: undefined,
    moduleConfigs,
    isToolEnabled: enabledCheck,
    fileManager,
    agentViewId,
  };

  // Build offload config for the result offload middleware
  const offloadConfig = {
    artifactsDir: context.artifactsDir,
    threshold: parseInt(moduleConfigs?.core?.['toolbox/result_offload/threshold'] || '20000', 10),
    sampleRows: parseInt(moduleConfigs?.core?.['toolbox/result_offload/sample_rows'] || '5', 10),
    textPreviewChars: parseInt(moduleConfigs?.core?.['toolbox/result_offload/text_preview_chars'] || '200', 10),
  };

  // Track all tool names registered on the server (adapter + JS module tools). currentModule is
  // set only while a module's register() runs, so a name is attributed to the module that
  // actually registered it — a name declared by a DIFFERENT module must not excuse it.
  const allToolNames = [];
  const registeredBy = [];
  const originalTool = server.tool.bind(server);
  server.tool = (name, desc, schema, handler, options = {}) => {
    allToolNames.push(name);
    if (currentModule) registeredBy.push({ name, module: currentModule });
    const strategy = options.resultStrategy !== undefined ? options.resultStrategy : 'text';
    const wrapped = wrapHandler(handler, name, strategy, offloadConfig);
    return originalTool(name, desc, schema, wrapped);
  };

  // 1. Register config-driven adapter tools (filtered by is_enabled)
  const allTools = await loadTools(dbOverrides);
  const enabledTools = allTools.filter(t => enabledCheck(t.name));
  const moduleToolTypes = getModuleToolTypes();
  const { healthchecks: adapterHealthchecks } = registerAdapterTools(
    server,
    enabledTools,
    moduleToolTypes,
    moduleConfigs,
    { sqlPoolRegistry: context.sqlPoolRegistry, log: context.log }
  );

  const healthchecks = [...adapterHealthchecks];

  // 2. Discover and register JS tools from modules
  // Sort modules by order field for deterministic registration
  const sorted = [...modules].sort((a, b) => (a.order || 100) - (b.order || 100));

  for (const mod of sorted) {
    const files = discoverToolboxFiles(mod._path);
    for (const file of files) {
      try {
        const toolModule = await import(file);
        if (Array.isArray(toolModule.converters)) {
          for (const conv of toolModule.converters) {
            fileManager.converterRegistry.register(conv);
          }
          logToolboxRest('discovery', 'OK', `Registered ${toolModule.converters.length} converter(s) from ${mod.name}/toolbox/${path.basename(file)}`);
        }
        if (typeof toolModule.register === 'function') {
          currentModule = mod.name;
          try {
            await toolModule.register(server, enrichedContext);
          } finally {
            currentModule = null;
          }
          logToolboxRest('discovery', 'OK', `Registered toolbox/${path.basename(file)} from ${mod.name}`);
        } else if (!Array.isArray(toolModule.converters)) {
          logToolboxRest('discovery', 'SKIP', `${file} has no register() export`);
        }
        if (typeof toolModule.healthcheck === 'function') {
          healthchecks.push(() => toolModule.healthcheck(enrichedContext));
        }
      } catch (err) {
        logToolboxRest('discovery', 'ERROR', `Failed to load ${file}: ${err.message}`);
      }
    }
  }

  // Manifests are the admin Tools screen / tool:list source of truth (both read module.json
  // tools[] only), so a name registered here but absent there is live yet invisible and its
  // tools/<name>/is_enabled key cannot be flipped in admin. module:validate catches the
  // string-literal cases statically; this catches names computed at runtime too (upstream MCP
  // passthrough). Compared PER MODULE: a name declared by a DIFFERENT module must not excuse it.
  const reported = new Map();   // module -> Set(name), so a repeat is reported once
  const undeclaredToolNames = [];
  // Both paths matter: a module that registers without consulting the gate (caught by
  // registeredBy) and one that consults it and is denied (caught by undeclaredLookups).
  for (const { name, module } of [...registeredBy, ...undeclaredLookups]) {
    let seenForModule = reported.get(module);
    if (!seenForModule) {
      seenForModule = new Set();
      reported.set(module, seenForModule);
    }
    if (seenForModule.has(name)) continue;
    seenForModule.add(name);
    if ((declaredByModule.get(module) || new Set()).has(name)) continue;
    undeclaredToolNames.push({ name, module });
  }
  undeclaredToolNames.sort((a, b) => a.name.localeCompare(b.name));
  for (const { name, module } of undeclaredToolNames) {
    logToolboxRest('drift', 'WARN',
      `Module '${module}' exposed or queried tool '${name}', which it does not declare in its ` +
      `module.json tools[] — the gate denies undeclared names, and an undeclared tool would be ` +
      `invisible in the admin Tools screen and in tool:list. Add it to that module's tools[].`);
  }

  return { toolNames: allToolNames, healthchecks, agentViewMeta, undeclaredToolNames };
}
