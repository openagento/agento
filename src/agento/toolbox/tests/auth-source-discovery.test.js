import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

describe('discoverAuthSources', () => {
  let tmpDir;
  const logs = [];

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_auth_src_' + Date.now() + '_' + Math.random().toString(36).slice(2));
    fs.mkdirSync(tmpDir, { recursive: true });
    process.env.CORE_MODULES_DIR = tmpDir;
    process.env.USER_MODULES_DIR = path.join(tmpDir, '_nonexistent_');
    logs.length = 0;
    vi.doMock('../log.js', () => ({
      logToolboxMcp: vi.fn(),
      logToolboxRest: vi.fn((scope, level, msg) => logs.push(`${level} ${msg}`)),
      logPublisher: vi.fn(),
      createScopedLogger: vi.fn(),
      errorCategory: () => 'Error',
    }));
  });

  afterEach(() => {
    delete process.env.CORE_MODULES_DIR;
    delete process.env.USER_MODULES_DIR;
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.resetModules();
  });

  function moduleWith(name, source) {
    const dir = path.join(tmpDir, name);
    fs.mkdirSync(path.join(dir, 'toolbox'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name, version: '1.0.0', description: 'd' }));
    fs.writeFileSync(path.join(dir, 'toolbox', 'auth.js'), source);
  }

  async function discover() {
    vi.resetModules();
    const { discoverAuthSources } = await import('../config-loader.js');
    return discoverAuthSources();
  }

  it('collects the source checkers modules export', async () => {
    moduleWith('sessions', "export const authSources = [['session', async () => null]];\n");
    const entries = await discover();
    expect(entries.map(([kind]) => kind)).toEqual(['session']);
    expect(typeof entries[0][1]).toBe('function');
  });

  it('refuses a source kind claimed by two modules, and skips a malformed entry', async () => {
    moduleWith('a', "export const authSources = [['session', async () => null], ['launch', 'nope']];\n");
    moduleWith('b', "export const authSources = [['session', async () => ({})]];\n");
    expect(await discover()).toEqual([]);
    expect(logs.join('\n')).toMatch(/'session' is claimed by more than one module/);
    expect(logs.join('\n')).toMatch(/malformed authSources/);
  });

  it('finds nothing when no module exports authSources (E1 ships none)', async () => {
    moduleWith('plain', 'export function register() {}\n');
    expect(await discover()).toEqual([]);
  });
});
