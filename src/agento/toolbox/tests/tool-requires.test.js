import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

// Harness mirrors config-loader.test.js's "registerTools integration" block: a real temp
// module dir with module.json + toolbox/*.js, with db/log/adapters mocked out.

describe('registerTools: requires chain in the tool gate', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_req_' + Date.now() + '_' + Math.random().toString(36).slice(2));
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

  /**
   * @param {object} opts
   * @param {Array<{name: string, tools: object[], registers: string[], respectGate?: boolean}>} opts.modules
   * @param {Record<string, string>} [opts.dbValues] tools/<name>/is_enabled values as a path->value map
   */
  async function runRegisterTools({ modules, dbValues = {} }) {
    for (const mod of modules) {
      const dir = path.join(tmpDir, mod.name);
      fs.mkdirSync(path.join(dir, 'toolbox'), { recursive: true });
      fs.writeFileSync(
        path.join(dir, 'module.json'),
        JSON.stringify({ name: mod.name, version: '1.0.0', description: 'd', tools: mod.tools }),
      );
      const respect = mod.respectGate === undefined ? true : mod.respectGate;
      // A module that consults the gate (the normal case) vs one that registers blindly
      // (what drift detection exists for).
      fs.writeFileSync(
        path.join(dir, 'toolbox', 'tools.js'),
        `export function register(server, { isToolEnabled }) {\n` +
        `  const names = ${JSON.stringify(mod.registers)};\n` +
        `  const respect = ${respect};\n` +
        `  for (const name of names) {\n` +
        `    if (respect && isToolEnabled && !isToolEnabled(name)) continue;\n` +
        `    server.tool(name, 'desc', {}, async () => ({ content: [] }));\n` +
        `  }\n` +
        `}\n`,
      );
    }

    const rows = Object.entries(dbValues).map(([p, value]) => ({ path: p, value, encrypted: 0 }));
    const queryMock = vi.fn().mockResolvedValue([rows]);

    const logs = [];
    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: queryMock }) }));
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn((scope, level, msg) => logs.push(`${scope} ${level} ${msg}`)),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
    }));
    vi.doMock('../adapters/index.js', () => ({
      registerAdapterTools: vi.fn(() => ({ names: [], healthchecks: [] })),
    }));

    vi.resetModules();
    const mod = await import('../config-loader.js');
    const registered = [];
    const mockServer = { tool: (name) => { registered.push(name); } };
    const result = await mod.registerTools(
      mockServer,
      { log: vi.fn(), db: {}, playwright: {}, sqlPoolRegistry: { createPoolHandle: vi.fn() } },
    );
    return { registered, logs, ...result };
  }

  const MASTERED = [
    { type: 'mcp', name: 'jira', description: 'd', toolset: 'jira' },
    { type: 'mcp', name: 'jira_search', description: 'd', toolset: 'jira', requires: 'jira' },
    { type: 'mcp', name: 'jira_get_issue', description: 'd', toolset: 'jira', requires: 'jira' },
  ];

  it('registers a child when both the child key and its master resolve 1', async () => {
    const { registered } = await runRegisterTools({
      modules: [{ name: 'jira', tools: MASTERED, registers: ['jira_search'] }],
      dbValues: { 'tools/jira/is_enabled': '1', 'tools/jira_search/is_enabled': '1' },
    });
    expect(registered).toContain('jira_search');
  });

  it('skips a child whose master is 0 even when its own key is 1', async () => {
    const { registered } = await runRegisterTools({
      modules: [{ name: 'jira', tools: MASTERED, registers: ['jira_search'] }],
      dbValues: { 'tools/jira/is_enabled': '0', 'tools/jira_search/is_enabled': '1' },
    });
    expect(registered).not.toContain('jira_search');
  });

  it('skips a child whose own key is 0 even when the master is 1', async () => {
    const { registered } = await runRegisterTools({
      modules: [{ name: 'jira', tools: MASTERED, registers: ['jira_search'] }],
      dbValues: { 'tools/jira/is_enabled': '1', 'tools/jira_search/is_enabled': '0' },
    });
    expect(registered).not.toContain('jira_search');
  });

  it('gates siblings independently under one master', async () => {
    const { registered } = await runRegisterTools({
      modules: [{ name: 'jira', tools: MASTERED, registers: ['jira_search', 'jira_get_issue'] }],
      dbValues: {
        'tools/jira/is_enabled': '1',
        'tools/jira_search/is_enabled': '1',
        'tools/jira_get_issue/is_enabled': '0',
      },
    });
    expect(registered).toContain('jira_search');
    expect(registered).not.toContain('jira_get_issue');
  });

  it('honours a master set only via ENV', async () => {
    process.env.CONFIG__TOOLS__JIRA__IS_ENABLED = '0';
    const { registered } = await runRegisterTools({
      modules: [{ name: 'jira', tools: MASTERED, registers: ['jira_search'] }],
      dbValues: { 'tools/jira_search/is_enabled': '1' },
    });
    expect(registered).not.toContain('jira_search');
  });

  it('a tool without requires is unaffected', async () => {
    const { registered } = await runRegisterTools({
      modules: [{
        name: 'core',
        tools: [{ type: 'mcp', name: 'email_send', description: 'd', toolset: 'core' }],
        registers: ['email_send'],
      }],
      dbValues: { 'tools/email_send/is_enabled': '1' },
    });
    expect(registered).toContain('email_send');
  });

  it('denies an UNDECLARED name even when its key resolves 1', async () => {
    // tool:enable accepts any snake_case name, so a key can exist for a tool no manifest
    // declares. Without the declaration requirement a passthrough loop would register it —
    // live but invisible in admin, with the drift WARN arriving only after the fact.
    const { registered } = await runRegisterTools({
      modules: [{
        name: 'core',
        tools: [{ type: 'mcp', name: 'browser_navigate', description: 'd', toolset: 'browser' }],
        registers: ['browser_navigate', 'browser_future_tool'],
      }],
      dbValues: {
        'tools/browser_navigate/is_enabled': '1',
        'tools/browser_future_tool/is_enabled': '1',
      },
    });
    expect(registered).toContain('browser_navigate');
    expect(registered).not.toContain('browser_future_tool');
  });

  it('denies an UNDECLARED name enabled via ENV', async () => {
    process.env.CONFIG__TOOLS__BROWSER_FUTURE_TOOL__IS_ENABLED = '1';
    const { registered } = await runRegisterTools({
      modules: [{ name: 'core', tools: [], registers: ['browser_future_tool'] }],
    });
    expect(registered).not.toContain('browser_future_tool');
  });

  it('denies a declared tool whose requires target is undeclared', async () => {
    const { registered } = await runRegisterTools({
      modules: [{
        name: 'demo',
        tools: [{ type: 'mcp', name: 'demo_a', description: 'd', toolset: 'demo', requires: 'ghost' }],
        registers: ['demo_a'],
      }],
      dbValues: { 'tools/demo_a/is_enabled': '1', 'tools/ghost/is_enabled': '1' },
    });
    expect(registered).not.toContain('demo_a');
  });

  it('denies a gate-RESPECTING squatter the OWNER\'s declared and enabled name', async () => {
    // The gate must check the ASKING module's declarations, not the union: otherwise a module
    // can compute a name another module declares and enables, pass the gate, and register its
    // own handler under that module's grant. The per-module drift WARN only reports it after.
    const { registered } = await runRegisterTools({
      modules: [
        {
          name: 'owner',
          tools: [{ type: 'mcp', name: 'shared_tool', description: 'd', toolset: 'owner' }],
          registers: ['shared_tool'],
        },
        { name: 'squatter', tools: [], registers: ['shared_tool'] },
      ],
      dbValues: { 'tools/shared_tool/is_enabled': '1' },
    });
    // The owner registers it once; the squatter is denied, so no second registration.
    expect(registered.filter(n => n === 'shared_tool')).toEqual(['shared_tool']);
  });

  it('still allows a module its OWN declared name', async () => {
    const { registered } = await runRegisterTools({
      modules: [{
        name: 'owner',
        tools: [{ type: 'mcp', name: 'shared_tool', description: 'd', toolset: 'owner' }],
        registers: ['shared_tool'],
      }],
      dbValues: { 'tools/shared_tool/is_enabled': '1' },
    });
    expect(registered).toContain('shared_tool');
  });

  it('fails closed on a requires cycle', async () => {
    const { registered } = await runRegisterTools({
      modules: [{
        name: 'demo',
        tools: [
          { type: 'mcp', name: 'a', description: 'd', toolset: 'demo', requires: 'b' },
          { type: 'mcp', name: 'b', description: 'd', toolset: 'demo', requires: 'a' },
        ],
        registers: ['a'],
      }],
      dbValues: { 'tools/a/is_enabled': '1', 'tools/b/is_enabled': '1' },
    });
    expect(registered).not.toContain('a');
  });
});
