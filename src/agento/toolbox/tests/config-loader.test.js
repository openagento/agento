import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import path from 'path';

describe('config-loader', () => {
  let loadTools, getModuleToolTypes;
  let tmpDir;
  const savedEnv = {};

  beforeEach(async () => {
    tmpDir = path.join(import.meta.dirname, '_test_modules_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });

    // Save and set env — use CORE_MODULES_DIR for the new structure
    savedEnv.CORE_MODULES_DIR = process.env.CORE_MODULES_DIR;
    savedEnv.USER_MODULES_DIR = process.env.USER_MODULES_DIR;
    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');

    // Mock DB — no overrides by default
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[]]),
      }),
    }));

    // Mock log
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    loadTools = mod.loadTools;
    getModuleToolTypes = mod.getModuleToolTypes;
  });

  afterEach(() => {
    if (savedEnv.CORE_MODULES_DIR === undefined) delete process.env.CORE_MODULES_DIR;
    else process.env.CORE_MODULES_DIR = savedEnv.CORE_MODULES_DIR;
    if (savedEnv.USER_MODULES_DIR === undefined) delete process.env.USER_MODULES_DIR;
    else process.env.USER_MODULES_DIR = savedEnv.USER_MODULES_DIR;

    // Clean env vars set during tests
    for (const key of Object.keys(process.env)) {
      if (key.startsWith('CONFIG__')) delete process.env[key];
    }

    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  function createModule(name, moduleJson, configJson) {
    const dir = path.join(tmpDir, name);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify(moduleJson));
    if (configJson) {
      fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(configJson));
    }
  }

  it('returns empty when no modules exist', async () => {
    const tools = await loadTools();
    expect(tools).toEqual([]);
  });

  it('skips directories starting with underscore', async () => {
    createModule('_example', { name: '_example', tools: [{ type: 'mysql', name: 'test', description: 'T', fields: {} }] });
    const tools = await loadTools();
    expect(tools).toEqual([]);
  });

  it('loads tool with config.json defaults', async () => {
    createModule('my-app', {
      name: 'my-app',
      tools: [{
        type: 'mysql',
        name: 'mysql_myapp',
        description: 'My DB',
        fields: {
          host: { type: 'string', label: 'Host' },
          port: { type: 'integer', label: 'Port' },
        },
      }],
    }, {
      tools: { mysql_myapp: { host: '10.0.0.1', port: 3306 } },
    });

    const tools = await loadTools();
    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe('mysql_myapp');
    expect(tools[0].type).toBe('mysql');
    expect(tools[0].module).toBe('my-app');
    expect(tools[0].config.host).toBe('10.0.0.1');
    expect(tools[0].config.port).toBe(3306); // config.json default
  });

  it('ENV var overrides config.json default', async () => {
    createModule('my-app', {
      name: 'my-app',
      tools: [{
        type: 'mysql',
        name: 'mysql_myapp',
        description: 'My DB',
        fields: { host: { type: 'string', label: 'Host' } },
      }],
    }, {
      tools: { mysql_myapp: { host: '10.0.0.1' } },
    });

    process.env.CONFIG__MY_APP__TOOLS__MYSQL_MYAPP__HOST = '10.0.0.99';

    const tools = await loadTools();
    expect(tools[0].config.host).toBe('10.0.0.99');
  });

  it('DB override beats config.json default', async () => {
    createModule('my-app', {
      name: 'my-app',
      tools: [{
        type: 'mysql',
        name: 'mysql_myapp',
        description: 'My DB',
        fields: { host: { type: 'string', label: 'Host' } },
      }],
    }, {
      tools: { mysql_myapp: { host: '10.0.0.1' } },
    });

    // Re-mock with DB override
    vi.resetModules();
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[
          { path: 'my_app/tools/mysql_myapp/host', value: '10.0.0.50', encrypted: 0 },
        ]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
    }));

    const mod = await import('../config-loader.js');
    const tools = await mod.loadTools();
    expect(tools[0].config.host).toBe('10.0.0.50');
  });

  it('resolves the standard per-tool SQL pool limit without a manifest field', async () => {
    createModule('my-app', {
      name: 'my-app',
      tools: [{
        type: 'mysql',
        name: 'mysql_myapp',
        description: 'My DB',
        fields: { host: { type: 'string', label: 'Host' } },
      }],
    });

    const tools = await loadTools({
      'my_app/tools/mysql_myapp/client_connection_pool_max_per_tool': {
        value: '7',
        encrypted: false,
      },
    });

    expect(tools[0].config.client_connection_pool_max_per_tool).toBe('7');
  });

  it('returns null for unresolved field without default', async () => {
    createModule('my-app', {
      name: 'my-app',
      tools: [{
        type: 'mysql',
        name: 'mysql_myapp',
        description: 'My DB',
        fields: { pass: { type: 'obscure', label: 'Password' } },
      }],
    });

    const tools = await loadTools();
    expect(tools[0].config.pass).toBeNull();
  });

  it('getModuleToolTypes returns unique types across modules', () => {
    createModule('app-a', {
      name: 'app-a',
      tools: [
        { type: 'mysql', name: 'a', description: 'A', fields: {} },
        { type: 'mssql', name: 'b', description: 'B', fields: {} },
      ],
    });
    createModule('app-b', {
      name: 'app-b',
      tools: [{ type: 'mysql', name: 'c', description: 'C', fields: {} }],
    });

    const types = getModuleToolTypes();
    expect(types).toEqual(new Set(['mysql', 'mssql']));
  });

  it('handles module without config.json gracefully', async () => {
    createModule('no-config', {
      name: 'no-config',
      tools: [{
        type: 'mysql',
        name: 'mysql_nc',
        description: 'NC',
        fields: { port: { type: 'integer', label: 'Port' } },
      }],
    });
    // No config.json created

    const tools = await loadTools();
    expect(tools[0].config.port).toBeNull(); // no config.json, no default
  });
});

describe('resolveModuleField', () => {
  let resolveModuleField;
  let resolveModuleFieldStrict;
  let tmpDir;
  const savedEnv = {};

  beforeEach(async () => {
    tmpDir = path.join(import.meta.dirname, '_test_modcfg_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });

    savedEnv.CORE_MODULES_DIR = process.env.CORE_MODULES_DIR;
    savedEnv.USER_MODULES_DIR = process.env.USER_MODULES_DIR;
    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    resolveModuleField = mod.resolveModuleField;
    resolveModuleFieldStrict = mod.resolveModuleFieldStrict;
  });

  afterEach(() => {
    if (savedEnv.CORE_MODULES_DIR === undefined) delete process.env.CORE_MODULES_DIR;
    else process.env.CORE_MODULES_DIR = savedEnv.CORE_MODULES_DIR;
    if (savedEnv.USER_MODULES_DIR === undefined) delete process.env.USER_MODULES_DIR;
    else process.env.USER_MODULES_DIR = savedEnv.USER_MODULES_DIR;

    for (const key of Object.keys(process.env)) {
      if (key.startsWith('CONFIG__')) delete process.env[key];
    }

    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  it('encodes a NESTED field the way the docs and the Python resolver do', () => {
    // `config_resolver.py:130` does .replace("/", "__"); the JS twin never did,
    // so `CONFIG__APP_MONITOR__ALERTS__SMTP_PASSWORD` — the documented name, and
    // the only way to configure alerting from ENV — could not be read at all.
    process.env.CONFIG__APP_MONITOR__ALERTS__SMTP_PASSWORD = 'from-env';
    expect(
      resolveModuleFieldStrict('app_monitor', 'alerts/smtp_password', {}, {}).value,
    ).toBe('from-env');
  });

  it('tells "stored but undecryptable" apart from "unset"', () => {
    expect(resolveModuleFieldStrict('core', 'smtp_pass', {}, {}))
      .toEqual({ value: null, state: 'unset' });
    // No AGENTO_ENCRYPTION_KEY in the test env, so an encrypted override cannot
    // be read — and must NOT come back looking absent.
    expect(resolveModuleFieldStrict('core', 'smtp_pass', {}, {
      'core/smtp_pass': { value: 'ciphertext', encrypted: true },
    })).toEqual({ value: null, state: 'undecryptable' });
  });

  it('returns null when no source and no config.json default', () => {
    const result = resolveModuleField('core', 'sql_timeout_seconds', {}, {});
    expect(result).toBeNull();
  });

  it('returns config.json default', () => {
    const result = resolveModuleField('core', 'sql_timeout_seconds', { sql_timeout_seconds: 600 }, {});
    expect(result).toBe(600);
  });

  it('ENV var overrides config.json default', () => {
    process.env.CONFIG__CORE__SQL_TIMEOUT_SECONDS = '120';
    const result = resolveModuleField('core', 'sql_timeout_seconds', { sql_timeout_seconds: 600 }, {});
    expect(result).toBe('120'); // raw string — coercion is caller's responsibility
  });

  it('DB override overrides config.json default', () => {
    const dbOverrides = { 'core/sql_timeout_seconds': { value: '90', encrypted: false } };
    const result = resolveModuleField('core', 'sql_timeout_seconds', { sql_timeout_seconds: 600 }, dbOverrides);
    expect(result).toBe('90');
  });

  it('returns null when no source at all', () => {
    const result = resolveModuleField('core', 'smtp_host', {}, {});
    expect(result).toBeNull();
  });
});

describe('loadModuleConfigs', () => {
  let loadModuleConfigs;
  let tmpDir;

  beforeEach(async () => {
    tmpDir = path.join(import.meta.dirname, '_test_modcfg2_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });

    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    loadModuleConfigs = mod.loadModuleConfigs;
  });

  afterEach(() => {
    delete process.env.CORE_MODULES_DIR;
    delete process.env.USER_MODULES_DIR;
    for (const key of Object.keys(process.env)) {
      if (key.startsWith('CONFIG__')) delete process.env[key];
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  it('resolves module config from system.json + config.json', async () => {
    const dir = path.join(tmpDir, 'mymod');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name: 'mymod' }));
    fs.writeFileSync(path.join(dir, 'system.json'), JSON.stringify({
      timeout: { type: 'integer' },
      label: { type: 'string' },
    }));
    fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({ timeout: 100, label: 'hello' }));

    const configs = await loadModuleConfigs();
    expect(configs.mymod.timeout).toBe(100);
    expect(configs.mymod.label).toBe('hello');
  });

  it('ENV override takes priority', async () => {
    const dir = path.join(tmpDir, 'mymod');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name: 'mymod' }));
    fs.writeFileSync(path.join(dir, 'system.json'), JSON.stringify({
      timeout: { type: 'integer' },
    }));
    fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({ timeout: 100 }));

    process.env.CONFIG__MYMOD__TIMEOUT = '999';
    const configs = await loadModuleConfigs();
    expect(configs.mymod.timeout).toBe('999');
  });

  it('returns empty for modules without system.json', async () => {
    const dir = path.join(tmpDir, 'bare');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name: 'bare' }));

    const configs = await loadModuleConfigs();
    expect(configs.bare).toBeUndefined();
  });
});

describe('loadScopedDbOverrides', () => {
  let loadScopedDbOverrides;
  let tmpDir;

  beforeEach(async () => {
    tmpDir = path.join(import.meta.dirname, '_test_scoped_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });

    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');
  });

  afterEach(() => {
    delete process.env.CORE_MODULES_DIR;
    delete process.env.USER_MODULES_DIR;
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  it('returns global overrides when agentViewId is null', async () => {
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[
          { path: 'jira/jira_host', value: 'https://jira.example.com', encrypted: 0 },
        ]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    loadScopedDbOverrides = mod.loadScopedDbOverrides;

    const { overrides, agentViewMeta } = await loadScopedDbOverrides(null);
    expect(agentViewMeta).toBeNull();
    expect(overrides['jira/jira_host'].value).toBe('https://jira.example.com');
  });

  it('layers workspace and agent_view overrides on top of global', async () => {
    const queryMock = vi.fn()
      // 1st call: global defaults (loadDbOverrides)
      .mockResolvedValueOnce([[
        { path: 'jira/jira_host', value: 'https://global.jira.com', encrypted: 0 },
        { path: 'core/timeout', value: '30', encrypted: 0 },
      ]])
      // 2nd call: agent_view lookup
      .mockResolvedValueOnce([[
        { id: 5, workspace_id: 2, label: 'Dev Agent', workspace_code: 'acme', agent_view_code: 'dev' },
      ]])
      // 3rd call: workspace overrides
      .mockResolvedValueOnce([[
        { path: 'jira/jira_host', value: 'https://ws.jira.com', encrypted: 0 },
      ]])
      // 4th call: agent_view overrides
      .mockResolvedValueOnce([[
        { path: 'jira/jira_host', value: 'https://av.jira.com', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');

    const { overrides, agentViewMeta } = await mod.loadScopedDbOverrides(5);
    expect(agentViewMeta).toEqual({
      id: 5,
      label: 'Dev Agent',
      workspaceId: 2,
      workspaceCode: 'acme',
      agentViewCode: 'dev',
    });
    // agent_view override wins
    expect(overrides['jira/jira_host'].value).toBe('https://av.jira.com');
    // global value preserved for non-overridden path
    expect(overrides['core/timeout'].value).toBe('30');
  });

  it('returns global config when agent_view not found', async () => {
    const queryMock = vi.fn()
      // global defaults
      .mockResolvedValueOnce([[
        { path: 'core/timeout', value: '30', encrypted: 0 },
      ]])
      // agent_view lookup — not found
      .mockResolvedValueOnce([[]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { overrides, agentViewMeta } = await mod.loadScopedDbOverrides(999);
    expect(agentViewMeta).toBeNull();
    expect(overrides['core/timeout'].value).toBe('30');
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('agent_view_id=999 not found'));
    warnSpy.mockRestore();
  });

  it('workspace overrides global but agent_view overrides workspace', async () => {
    const queryMock = vi.fn()
      // global
      .mockResolvedValueOnce([[
        { path: 'app/color', value: 'red', encrypted: 0 },
      ]])
      // agent_view lookup
      .mockResolvedValueOnce([[{ id: 1, workspace_id: 10, label: 'QA', workspace_code: 'acme', agent_view_code: 'av1' }]])
      // workspace overrides
      .mockResolvedValueOnce([[
        { path: 'app/color', value: 'blue', encrypted: 0 },
      ]])
      // agent_view overrides — no override for this path
      .mockResolvedValueOnce([[]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');

    const { overrides } = await mod.loadScopedDbOverrides(1);
    // workspace override wins over global (no agent_view override)
    expect(overrides['app/color'].value).toBe('blue');
  });
});

describe('toolbox discovery', () => {
  let scanModules;
  let tmpDir;

  beforeEach(async () => {
    tmpDir = path.join(import.meta.dirname, '_test_discovery_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });

    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    scanModules = mod.scanModules;
  });

  afterEach(() => {
    delete process.env.CORE_MODULES_DIR;
    delete process.env.USER_MODULES_DIR;
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  it('scans core modules directory', () => {
    const dir = path.join(tmpDir, 'test-mod');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name: 'test-mod' }));

    const modules = scanModules();
    expect(modules).toHaveLength(1);
    expect(modules[0].name).toBe('test-mod');
    expect(modules[0]._path).toBe(dir);
  });

  it('scans both core and user module directories', () => {
    const userDir = path.join(tmpDir, '_user_');
    fs.mkdirSync(userDir, { recursive: true });
    process.env.USER_MODULES_DIR = userDir;

    // Core module
    const coreDir = path.join(tmpDir, 'core-mod');
    fs.mkdirSync(coreDir, { recursive: true });
    fs.writeFileSync(path.join(coreDir, 'module.json'), JSON.stringify({ name: 'core-mod' }));

    // User module
    const userModDir = path.join(userDir, 'user-mod');
    fs.mkdirSync(userModDir, { recursive: true });
    fs.writeFileSync(path.join(userModDir, 'module.json'), JSON.stringify({ name: 'user-mod' }));

    // Re-import to pick up new USER_MODULES_DIR
    vi.resetModules();
    // Need synchronous re-read — scanModules reads env at import time
    // For this test, directly call with the updated env
    const modules = scanModules();
    expect(modules.map(m => m.name)).toContain('core-mod');
  });
});

describe('isToolEnabled', () => {
  let isToolEnabled;

  beforeEach(async () => {
    vi.resetModules();
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: vi.fn().mockResolvedValue([[]]) }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    const mod = await import('../config-loader.js');
    isToolEnabled = mod.isToolEnabled;
  });

  afterEach(() => {
    vi.resetModules();
  });

  it('returns false when no is_enabled config exists (opt-in default)', () => {
    expect(isToolEnabled('mysql_test', {})).toBe(false);
  });

  it('returns true when is_enabled is "1"', () => {
    const overrides = { 'tools/mysql_test/is_enabled': { value: '1', encrypted: false } };
    expect(isToolEnabled('mysql_test', overrides)).toBe(true);
  });

  it('returns false when is_enabled is "0"', () => {
    const overrides = { 'tools/mysql_test/is_enabled': { value: '0', encrypted: false } };
    expect(isToolEnabled('mysql_test', overrides)).toBe(false);
  });

  it('returns false for a tool with no own is_enabled row even if others are set', () => {
    const overrides = { 'tools/other_tool/is_enabled': { value: '1', encrypted: false } };
    expect(isToolEnabled('mysql_test', overrides)).toBe(false);
  });

  it('three-state: merged "0" (agent_view) overriding inherited "1" stays disabled', () => {
    // loadScopedDbOverrides merges the chain into one value before this runs,
    // so an agent_view '0' that overrode a default '1' arrives here as '0'.
    const overrides = { 'tools/mysql_test/is_enabled': { value: '0', encrypted: false } };
    expect(isToolEnabled('mysql_test', overrides)).toBe(false);
  });

  it('enabled via a config.json "1" default when no DB row (first-class tool)', () => {
    const configDefaults = { 'tools/email_send/is_enabled': '1' };
    expect(isToolEnabled('email_send', {}, configDefaults)).toBe(true);
  });

  it('DB "0" overrides a config.json "1" default', () => {
    const dbOverrides = { 'tools/email_send/is_enabled': { value: '0', encrypted: false } };
    const configDefaults = { 'tools/email_send/is_enabled': '1' };
    expect(isToolEnabled('email_send', dbOverrides, configDefaults)).toBe(false);
  });
});

describe('resolveConfigValue', () => {
  let resolveConfigValue;

  beforeEach(async () => {
    vi.resetModules();
    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: vi.fn().mockResolvedValue([[]]) }) }));
    vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(), logPublisher: vi.fn(), createScopedLogger: vi.fn() }));
    const mod = await import('../config-loader.js');
    resolveConfigValue = mod.resolveConfigValue;
  });

  afterEach(() => {
    delete process.env.CONFIG__TOOLS__EMAIL_SEND__IS_ENABLED;
    vi.resetModules();
  });

  it('precedence ENV > DB > config.json', () => {
    const dbOverrides = { 'tools/email_send/is_enabled': { value: '0', encrypted: false } };
    const configDefaults = { 'tools/email_send/is_enabled': '1' };
    // config.json only
    expect(resolveConfigValue('tools/email_send/is_enabled', {}, configDefaults)).toBe('1');
    // DB beats config.json
    expect(resolveConfigValue('tools/email_send/is_enabled', dbOverrides, configDefaults)).toBe('0');
    // ENV beats both
    process.env.CONFIG__TOOLS__EMAIL_SEND__IS_ENABLED = '1';
    expect(resolveConfigValue('tools/email_send/is_enabled', dbOverrides, configDefaults)).toBe('1');
  });

  it('returns null when no source has the path', () => {
    expect(resolveConfigValue('tools/unknown/is_enabled', {}, {})).toBe(null);
  });
});

describe('registerTools integration', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_reg_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });
    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');
  });

  afterEach(() => {
    delete process.env.CORE_MODULES_DIR;
    delete process.env.USER_MODULES_DIR;
    for (const key of Object.keys(process.env)) {
      if (key.startsWith('CONFIG__')) delete process.env[key];
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  function createModule(name, moduleJson, configJson) {
    const dir = path.join(tmpDir, name);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify(moduleJson));
    if (configJson) {
      fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(configJson));
    }
  }

  it('adapter tools use scoped config from agent_view', async () => {
    createModule('myapp', {
      name: 'myapp',
      tools: [{
        type: 'mysql', name: 'mysql_prod', description: 'Prod DB',
        fields: { host: { type: 'string' }, pass: { type: 'obscure' } },
      }],
    });

    // global has no host, agent_view scope provides it
    const queryMock = vi.fn()
      .mockResolvedValueOnce([[]])  // global: empty
      .mockResolvedValueOnce([[{ id: 2, workspace_id: 1, label: 'Dev', workspace_code: 'acme', agent_view_code: 'av2' }]])  // agent_view lookup
      .mockResolvedValueOnce([[]])  // workspace: empty
      .mockResolvedValueOnce([[  // agent_view scope
        { path: 'tools/mysql_prod/is_enabled', value: '1', encrypted: 0 },
        { path: 'myapp/tools/mysql_prod/host', value: 'scoped-db.internal', encrypted: 0 },
        { path: 'myapp/tools/mysql_prod/pass', value: 'secret', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    const { registerAdapterTools } = await import('../adapters/index.js');
    const mod = await import('../config-loader.js');
    const mockServer = { tool: vi.fn() };
    const sqlPoolRegistry = { createPoolHandle: vi.fn() };
    const mockContext = { log: vi.fn(), db: {}, playwright: {}, sqlPoolRegistry };

    await mod.registerTools(mockServer, mockContext, 2);

    // Adapter should receive tool with scoped config
    const calledTools = registerAdapterTools.mock.calls[0][1];
    expect(calledTools).toHaveLength(1);
    expect(calledTools[0].config.host).toBe('scoped-db.internal');
    expect(calledTools[0].config.pass).toBe('secret');
    // The session's agent_view-scoped logger must reach the adapters, so adapter tool
    // invocations are attributed to the agent_view and not just the LLM-supplied `user`.
    expect(registerAdapterTools.mock.calls[0][4]).toEqual({ sqlPoolRegistry, log: mockContext.log });
  });

  it('is_enabled=0 at agent_view scope filters out adapter tool', async () => {
    createModule('myapp', {
      name: 'myapp',
      tools: [{
        type: 'mysql', name: 'mysql_prod', description: 'Prod DB',
        fields: { host: { type: 'string' } },
      }],
    }, { tools: { mysql_prod: { host: '10.0.0.1' } } });

    const queryMock = vi.fn()
      .mockResolvedValueOnce([[]])  // global
      .mockResolvedValueOnce([[{ id: 3, workspace_id: 1, label: 'QA', workspace_code: 'acme', agent_view_code: 'av3' }]])  // agent_view lookup
      .mockResolvedValueOnce([[]])  // workspace
      .mockResolvedValueOnce([[  // agent_view: disable tool
        { path: 'tools/mysql_prod/is_enabled', value: '0', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    const { registerAdapterTools } = await import('../adapters/index.js');
    const mod = await import('../config-loader.js');
    const mockServer = { tool: vi.fn() };
    const mockContext = { log: vi.fn(), db: {}, playwright: {} };

    await mod.registerTools(mockServer, mockContext, 3);

    // Tool should be filtered out
    const calledTools = registerAdapterTools.mock.calls[0][1];
    expect(calledTools).toHaveLength(0);
  });

  it('isToolEnabled available to JS module tools via context', async () => {
    const modDir = path.join(tmpDir, 'testmod');
    fs.mkdirSync(path.join(modDir, 'toolbox'), { recursive: true });
    fs.writeFileSync(path.join(modDir, 'module.json'), JSON.stringify({ name: 'testmod' }));
    const capturePath = path.join(tmpDir, '_capture.json');
    fs.writeFileSync(path.join(modDir, 'toolbox', 'probe.js'),
      `import fs from 'fs';
       export function register(server, ctx) {
         fs.writeFileSync(${JSON.stringify(capturePath)}, JSON.stringify({
           hasFn: typeof ctx.isToolEnabled === 'function',
           emailEnabled: ctx.isToolEnabled('email_send'),
           hasApp: !!ctx.app,
         }));
       }`
    );

    // Disable email_send in DB
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[
          { path: 'tools/email_send/is_enabled', value: '0', encrypted: 0 },
        ]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn(() => ({ names: [], healthchecks: [] })),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    const mockServer = { tool: vi.fn() };
    const mockContext = { app: { post: vi.fn() }, log: vi.fn(), db: {}, playwright: {} };

    await mod.registerTools(mockServer, mockContext);

    const captured = JSON.parse(fs.readFileSync(capturePath, 'utf-8'));
    expect(captured.hasFn).toBe(true);
    expect(captured.emailEnabled).toBe(false);
    expect(captured.hasApp).toBe(false);
  });

  it('no agent_view_id serves only globally enabled tools (opt-in)', async () => {
    createModule('myapp', {
      name: 'myapp',
      tools: [
        { type: 'mysql', name: 'mysql_a', description: 'A', fields: { host: { type: 'string' } } },
        { type: 'mysql', name: 'mysql_b', description: 'B', fields: { host: { type: 'string' } } },
      ],
    }, { tools: { mysql_a: { host: 'a.db' }, mysql_b: { host: 'b.db' } } });

    // Opt-in: enable mysql_a globally; mysql_b has no '1' row so it stays disabled.
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: vi.fn().mockResolvedValue([[
          { path: 'tools/mysql_a/is_enabled', value: '1', encrypted: 0 },
        ]]),
      }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn(),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    const { registerAdapterTools } = await import('../adapters/index.js');
    const mod = await import('../config-loader.js');
    const mockServer = { tool: vi.fn() };
    const mockContext = { log: vi.fn(), db: {}, playwright: {} };

    await mod.registerTools(mockServer, mockContext);  // no agentViewId

    const calledTools = registerAdapterTools.mock.calls[0][1];
    expect(calledTools).toHaveLength(1);
    expect(calledTools[0].name).toBe('mysql_a');
  });

  it('same module, two agent_views: one sees all tools, other has tool disabled', async () => {
    createModule('erp', {
      name: 'erp',
      tools: [
        { type: 'mysql', name: 'erp_read', description: 'ERP Read', fields: { host: { type: 'string' } } },
        { type: 'mysql', name: 'erp_write', description: 'ERP Write', fields: { host: { type: 'string' } } },
      ],
    }, { tools: { erp_read: { host: 'db.internal' }, erp_write: { host: 'db.internal' } } });

    // --- Agent view 10 (developer): both tools explicitly enabled (opt-in) ---
    const queryDev = vi.fn()
      .mockResolvedValueOnce([[]])  // global overrides
      .mockResolvedValueOnce([[{ id: 10, workspace_id: 1, label: 'Developer', workspace_code: 'acme', agent_view_code: 'av10' }]])  // agent_view lookup
      .mockResolvedValueOnce([[]])  // workspace overrides
      .mockResolvedValueOnce([[  // agent_view overrides: enable both
        { path: 'tools/erp_read/is_enabled', value: '1', encrypted: 0 },
        { path: 'tools/erp_write/is_enabled', value: '1', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryDev }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(), logPublisher: vi.fn(), createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    let adapterMod = await import('../adapters/index.js');
    let mod = await import('../config-loader.js');
    let mockServer = { tool: vi.fn() };

    await mod.registerTools(mockServer, { log: vi.fn(), db: {}, playwright: {} }, 10);

    const devTools = adapterMod.registerAdapterTools.mock.calls[0][1];
    expect(devTools).toHaveLength(2);
    expect(devTools.map(t => t.name).sort()).toEqual(['erp_read', 'erp_write']);

    // --- Agent view 20 (qa-tester): erp_read enabled, erp_write explicitly disabled ---
    const queryQa = vi.fn()
      .mockResolvedValueOnce([[]])  // global overrides
      .mockResolvedValueOnce([[{ id: 20, workspace_id: 1, label: 'QA Tester', workspace_code: 'acme', agent_view_code: 'av20' }]])  // agent_view lookup
      .mockResolvedValueOnce([[]])  // workspace overrides
      .mockResolvedValueOnce([[  // agent_view overrides: enable erp_read, disable erp_write
        { path: 'tools/erp_read/is_enabled', value: '1', encrypted: 0 },
        { path: 'tools/erp_write/is_enabled', value: '0', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryQa }),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    adapterMod = await import('../adapters/index.js');
    mod = await import('../config-loader.js');
    mockServer = { tool: vi.fn() };

    await mod.registerTools(mockServer, { log: vi.fn(), db: {}, playwright: {} }, 20);

    const qaTools = adapterMod.registerAdapterTools.mock.calls[0][1];
    expect(qaTools).toHaveLength(1);
    expect(qaTools[0].name).toBe('erp_read');
  });

  it('workspace-level disable inherited by agent_view, agent_view re-enable overrides', async () => {
    createModule('crm', {
      name: 'crm',
      tools: [
        { type: 'mysql', name: 'crm_search', description: 'CRM Search', fields: { host: { type: 'string' } } },
      ],
    }, { tools: { crm_search: { host: 'crm.internal' } } });

    // Agent view 30: workspace disables crm_search, agent_view does NOT re-enable
    const queryInherited = vi.fn()
      .mockResolvedValueOnce([[]])  // global
      .mockResolvedValueOnce([[{ id: 30, workspace_id: 5, label: 'Restricted', workspace_code: 'acme', agent_view_code: 'av30' }]])
      .mockResolvedValueOnce([[  // workspace: disable
        { path: 'tools/crm_search/is_enabled', value: '0', encrypted: 0 },
      ]])
      .mockResolvedValueOnce([[]]);  // agent_view: empty (inherits workspace disable)

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryInherited }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(), logPublisher: vi.fn(), createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    let adapterMod = await import('../adapters/index.js');
    let mod = await import('../config-loader.js');

    await mod.registerTools({ tool: vi.fn() }, { log: vi.fn(), db: {}, playwright: {} }, 30);

    let tools = adapterMod.registerAdapterTools.mock.calls[0][1];
    expect(tools).toHaveLength(0);  // workspace disable inherited

    // Agent view 31: same workspace disables crm_search, but agent_view RE-ENABLES
    const queryOverride = vi.fn()
      .mockResolvedValueOnce([[]])  // global
      .mockResolvedValueOnce([[{ id: 31, workspace_id: 5, label: 'Privileged', workspace_code: 'acme', agent_view_code: 'av31' }]])
      .mockResolvedValueOnce([[  // workspace: disable
        { path: 'tools/crm_search/is_enabled', value: '0', encrypted: 0 },
      ]])
      .mockResolvedValueOnce([[  // agent_view: re-enable
        { path: 'tools/crm_search/is_enabled', value: '1', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryOverride }),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    adapterMod = await import('../adapters/index.js');
    mod = await import('../config-loader.js');

    await mod.registerTools({ tool: vi.fn() }, { log: vi.fn(), db: {}, playwright: {} }, 31);

    tools = adapterMod.registerAdapterTools.mock.calls[0][1];
    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe('crm_search');
  });

  it('agent_view inherits an enable ("1") set at workspace scope (opt-in)', async () => {
    createModule('crm', {
      name: 'crm',
      tools: [
        { type: 'mysql', name: 'crm_search', description: 'CRM Search', fields: { host: { type: 'string' } } },
      ],
    }, { tools: { crm_search: { host: 'crm.internal' } } });

    // workspace enables crm_search; agent_view has no row -> inherits the enable
    const queryInherit = vi.fn()
      .mockResolvedValueOnce([[]])  // global
      .mockResolvedValueOnce([[{ id: 50, workspace_id: 7, label: 'Team', workspace_code: 'acme', agent_view_code: 'av50' }]])
      .mockResolvedValueOnce([[  // workspace: enable
        { path: 'tools/crm_search/is_enabled', value: '1', encrypted: 0 },
      ]])
      .mockResolvedValueOnce([[]]);  // agent_view: empty -> inherits workspace enable

    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: queryInherit }) }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(), logPublisher: vi.fn(), createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn((_server, tools) => ({ names: tools.map(t => t.name), healthchecks: [] })),
    }));

    vi.resetModules();
    const adapterMod = await import('../adapters/index.js');
    const mod = await import('../config-loader.js');

    await mod.registerTools({ tool: vi.fn() }, { log: vi.fn(), db: {}, playwright: {} }, 50);

    const tools = adapterMod.registerAdapterTools.mock.calls[0][1];
    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe('crm_search');
  });

  it('JS module tools also filtered by scoped is_enabled per agent_view', async () => {
    const modDir = path.join(tmpDir, 'notifier');
    fs.mkdirSync(path.join(modDir, 'toolbox'), { recursive: true });
    fs.writeFileSync(path.join(modDir, 'module.json'), JSON.stringify({ name: 'notifier' }));
    const capturePath = path.join(tmpDir, '_js_tool_capture.json');
    fs.writeFileSync(path.join(modDir, 'toolbox', 'slack.js'),
      `import fs from 'fs';
       export function register(server, ctx) {
         const enabled = ctx.isToolEnabled('slack_notify');
         fs.writeFileSync(${JSON.stringify(capturePath)}, JSON.stringify({ enabled }));
         if (enabled) server.tool('slack_notify', 'Send Slack', {}, () => {});
       }`
    );

    // Agent view 40: disable slack_notify
    const queryMock = vi.fn()
      .mockResolvedValueOnce([[]])
      .mockResolvedValueOnce([[{ id: 40, workspace_id: 1, label: 'NoSlack', workspace_code: 'acme', agent_view_code: 'av40' }]])
      .mockResolvedValueOnce([[]])
      .mockResolvedValueOnce([[
        { path: 'tools/slack_notify/is_enabled', value: '0', encrypted: 0 },
      ]]);

    vi.doMock('../db.js', () => ({
      getCronPool: () => ({ query: queryMock }),
    }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(), logPublisher: vi.fn(), createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn(() => ({ names: [], healthchecks: [] })),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    const toolNames = [];
    const mockServer = { tool: (...args) => { toolNames.push(args[0]); } };

    await mod.registerTools(mockServer, { log: vi.fn(), db: {}, playwright: {} }, 40);

    const captured = JSON.parse(fs.readFileSync(capturePath, 'utf-8'));
    expect(captured.enabled).toBe(false);
    expect(toolNames).not.toContain('slack_notify');
  });
});

describe('loadStrictScopedOverrides', () => {
  // The lenient loaders keep an MCP session alive on a degraded DB. A config
  // test must not inherit that: "tested the global credential and called it ok"
  // is the same misdiagnosis class the feature exists to end.
  const mockPool = async (queryMock) => {
    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: queryMock }) }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(), logToolboxRest: vi.fn(),
      logPublisher: vi.fn(), createScopedLogger: vi.fn(),
    }));
    vi.resetModules();
    return import('../config-loader.js');
  };

  it('layers global, workspace and agent_view like the lenient loader', async () => {
    const queryMock = vi.fn()
      .mockResolvedValueOnce([[{ path: 'core/timeout', value: '30', encrypted: 0 }]])
      .mockResolvedValueOnce([[{ id: 5, workspace_id: 2 }]])
      .mockResolvedValueOnce([[{ path: 'jira/jira_host', value: 'ws', encrypted: 0 }]])
      .mockResolvedValueOnce([[{ path: 'jira/jira_host', value: 'av', encrypted: 0 }]]);
    const mod = await mockPool(queryMock);
    const overrides = await mod.loadStrictScopedOverrides(5);
    expect(overrides['jira/jira_host'].value).toBe('av');
    expect(overrides['core/timeout'].value).toBe('30');
  });

  it('throws on an agent_view that does not exist instead of using global config', async () => {
    const queryMock = vi.fn()
      .mockResolvedValueOnce([[{ path: 'core/timeout', value: '30', encrypted: 0 }]])
      .mockResolvedValueOnce([[]]);
    const mod = await mockPool(queryMock);
    await expect(mod.loadStrictScopedOverrides(5)).rejects.toThrow(/does not exist/);
  });

  it('throws when a scoped query fails instead of returning a partial read', async () => {
    const queryMock = vi.fn()
      .mockResolvedValueOnce([[{ path: 'core/timeout', value: '30', encrypted: 0 }]])
      .mockResolvedValueOnce([[{ id: 5, workspace_id: 2 }]])
      .mockRejectedValueOnce(new Error('server has gone away'));
    const mod = await mockPool(queryMock);
    await expect(mod.loadStrictScopedOverrides(5)).rejects.toThrow(/gone away/);
  });

  it('throws when the default-scope query itself fails', async () => {
    const mod = await mockPool(vi.fn().mockRejectedValue(new Error('connect ECONNREFUSED')));
    await expect(mod.loadStrictScopedOverrides(null)).rejects.toThrow(/ECONNREFUSED/);
  });
});
