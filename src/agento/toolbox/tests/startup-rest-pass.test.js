import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// The class: a module register() that assumes a per-session-only context field (isToolEnabled)
// throws in the startup REST pass, and the toolbox logs a false "[startup] ERROR Failed to load".
// This runs the real pass over every core module, so a new module that misses the guard fails here.
describe('registerModuleRestApis over every core module', () => {
  const saved = {};
  let tmp;

  beforeEach(() => {
    for (const k of ['CORE_MODULES_DIR', 'USER_MODULES_DIR', 'APP_ETC_DIR']) saved[k] = process.env[k];
    // The container layout: modules beside the toolbox's packages. In the source tree a module
    // file cannot resolve express/nodemailer, so its import fails and its register() never runs.
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'startup-pass-'));
    fs.cpSync(path.join(here, '..', '..', 'modules'), path.join(tmp, 'modules'), {
      recursive: true,
      filter: (src) => !src.includes('node_modules') && !src.includes('__pycache__'),
    });
    fs.symlinkSync(path.join(here, '..', 'node_modules'), path.join(tmp, 'node_modules'));
    process.env.CORE_MODULES_DIR = path.join(tmp, 'modules');
    process.env.USER_MODULES_DIR = path.join(tmp, '_none_');
    process.env.APP_ETC_DIR = path.join(tmp, '_none_');
  });

  afterEach(() => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    fs.rmSync(tmp, { recursive: true, force: true });
    vi.doUnmock('../db.js');
    vi.doUnmock('../log.js');
    vi.resetModules();
  });

  it('loads every toolbox file and runs every register() without an ERROR', async () => {
    const restLog = vi.fn();
    vi.doMock('../db.js', () => ({ getCronPool: () => ({ query: vi.fn().mockResolvedValue([[]]) }) }));
    vi.doMock('../log.js', async (orig) => ({ ...(await orig()), logToolboxRest: restLog }));
    vi.resetModules();
    const { registerModuleRestApis } = await import('../config-loader.js');

    // The shape server.js passes, with no isToolEnabled: that absence is what marks the startup pass.
    const app = new Proxy({}, { get: () => () => {} });
    const playwright = { getClient: () => null, getTools: () => [], getState: () => ({}), getViewport: () => null };
    await registerModuleRestApis({ app, log: vi.fn(), db: {}, playwright });

    expect(restLog.mock.calls.filter(([, status]) => status === 'ERROR')).toEqual([]);
    expect(restLog.mock.calls.some(([, status, msg]) => status === 'OK' && msg.includes('subagent/toolbox/agent.js'))).toBe(true);
  });
});
