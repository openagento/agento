import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

// Same harness shape as tool-requires.test.js / config-loader.test.js's registerTools block.
// Drift has two shapes and both are covered: a module that registers WITHOUT consulting the gate
// (`respectGate: false` — caught at the server.tool wrapper) and one that consults it and is
// denied (`respectGate: true` — caught by recording the gate lookup, which is what the browser
// passthrough actually does).

describe('registerTools: drift detection', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_drift_' + Date.now() + '_' + Math.random().toString(36).slice(2));
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

  async function runRegisterTools({ modules }) {
    for (const mod of modules) {
      const dir = path.join(tmpDir, mod.name);
      fs.mkdirSync(path.join(dir, 'toolbox'), { recursive: true });
      fs.writeFileSync(
        path.join(dir, 'module.json'),
        JSON.stringify({ name: mod.name, version: '1.0.0', description: 'd', tools: mod.tools }),
      );
      const respect = mod.respectGate === undefined ? true : mod.respectGate;
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

    const logs = [];
    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: vi.fn().mockResolvedValue([[]]) }) }));
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
    const result = await mod.registerTools(
      { tool: () => {} },
      { log: vi.fn(), db: {}, playwright: {}, sqlPoolRegistry: { createPoolHandle: vi.fn() } },
    );
    return { logs, ...result };
  }

  it('reports and WARNs about a registered tool not declared in module.json', async () => {
    const { undeclaredToolNames, logs } = await runRegisterTools({
      modules: [{ name: 'demo', tools: [], registers: ['ghost_tool'], respectGate: false }],
    });
    expect(undeclaredToolNames).toEqual([{ name: 'ghost_tool', module: 'demo' }]);
    expect(logs.join('\n')).toMatch(/ghost_tool.*does not declare/s);
  });

  it('stays silent when every registered tool is declared', async () => {
    const { undeclaredToolNames, logs } = await runRegisterTools({
      modules: [{
        name: 'demo',
        tools: [{ type: 'mcp', name: 'good_tool', description: 'd', toolset: 'demo' }],
        registers: ['good_tool'],
        respectGate: false,
      }],
    });
    expect(undeclaredToolNames).toEqual([]);
    expect(logs.join('\n')).not.toMatch(/not declared/);
  });

  it('does NOT warn about declared-but-unregistered tools (a disabled tool is normal)', async () => {
    const { logs } = await runRegisterTools({
      modules: [{
        name: 'demo',
        tools: [
          { type: 'mcp', name: 'on_tool', description: 'd', toolset: 'demo' },
          { type: 'mcp', name: 'off_tool', description: 'd', toolset: 'demo' },
        ],
        registers: ['on_tool'],
        respectGate: false,
      }],
    });
    expect(logs.join('\n')).not.toMatch(/off_tool/);
  });

  it('stays silent for a declared tool registered under a computed name', async () => {
    // core declares all 36 browser tools, so passthrough registration is not drift.
    const { undeclaredToolNames, logs } = await runRegisterTools({
      modules: [{
        name: 'core',
        tools: [
          { type: 'mcp', name: 'browser_navigate', description: 'd', toolset: 'browser' },
          { type: 'mcp', name: 'browser_snapshot', description: 'd', toolset: 'browser' },
        ],
        registers: ['browser_navigate', 'browser_snapshot'],
        respectGate: false,
      }],
    });
    expect(undeclaredToolNames).toEqual([]);
    expect(logs.join('\n')).not.toMatch(/not declared/);
  });

  it('warns about a computed name nobody declared (e.g. a new upstream tool)', async () => {
    const { undeclaredToolNames } = await runRegisterTools({
      modules: [{
        name: 'core',
        tools: [{ type: 'mcp', name: 'browser_navigate', description: 'd', toolset: 'browser' }],
        registers: ['browser_navigate', 'browser_brand_new'],
        respectGate: false,
      }],
    });
    expect(undeclaredToolNames).toEqual([{ name: 'browser_brand_new', module: 'core' }]);
  });

  it('attributes drift to the REGISTERING module, not the whole manifest set', async () => {
    const { undeclaredToolNames } = await runRegisterTools({
      modules: [
        { name: 'owner', tools: [{ type: 'mcp', name: 'shared_tool', description: 'd', toolset: 'owner' }], registers: [], respectGate: false },
        { name: 'squatter', tools: [], registers: ['shared_tool'], respectGate: false },
      ],
    });
    expect(undeclaredToolNames).toEqual([{ name: 'shared_tool', module: 'squatter' }]);
  });

  it('warns for a gate-RESPECTING module whose computed name is undeclared', async () => {
    // The case the browser passthrough actually hits: the module consults the gate, the gate
    // denies the undeclared name, so server.tool() is never called. Without recording the gate
    // LOOKUP this drift would be invisible — which is precisely the drift the WARN promises.
    const { undeclaredToolNames, logs } = await runRegisterTools({
      modules: [{
        name: 'core',
        tools: [{ type: 'mcp', name: 'browser_navigate', description: 'd', toolset: 'browser' }],
        registers: ['browser_navigate', 'browser_brand_new'],
        respectGate: true,
      }],
    });
    expect(undeclaredToolNames).toEqual([{ name: 'browser_brand_new', module: 'core' }]);
    expect(logs.join('\n')).toMatch(/browser_brand_new.*does not declare/s);
  });

  it('reports a repeated (module, name) registration once', async () => {
    const { undeclaredToolNames } = await runRegisterTools({
      modules: [{ name: 'demo', tools: [], registers: ['dup_tool', 'dup_tool'], respectGate: false }],
    });
    expect(undeclaredToolNames).toEqual([{ name: 'dup_tool', module: 'demo' }]);
  });
});
