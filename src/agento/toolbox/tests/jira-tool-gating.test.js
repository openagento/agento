import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { register } from '../../modules/jira/toolbox/jira.js';

const MODULE_JSON = fileURLToPath(new URL('../../modules/jira/module.json', import.meta.url));
const CONFIG_JSON = fileURLToPath(new URL('../../modules/jira/config.json', import.meta.url));

const ALL_TOOLS = [
  'jira_add_comment', 'jira_assign_issue', 'jira_attach_file', 'jira_create_issue',
  'jira_get_attachment', 'jira_get_issue', 'jira_search', 'jira_transition_issue',
  'jira_update_issue',
];

// On by default today; jira_get_attachment is deliberately opt-in-off.
const DEFAULT_ON = ALL_TOOLS.filter(n => n !== 'jira_get_attachment');

function makeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

function ctx(isToolEnabled) {
  return {
    log: vi.fn(),
    moduleConfigs: { jira: { jira_host: 'https://x.atlassian.net', jira_user: 'u@x.com', jira_token: 't' } },
    isToolEnabled,
    artifactsDir: '/tmp/art',
    fileManager: { downloadAndConvert: vi.fn() },
  };
}

describe('jira tools: per-tool opt-in gating', () => {
  it('registers all 9 tools when every tool key is enabled', () => {
    const s = makeServer();
    register(s, ctx(() => true));
    expect(Object.keys(s.tools).sort()).toEqual(ALL_TOOLS);
  });

  it('registers nothing when every tool key is disabled', () => {
    const s = makeServer();
    register(s, ctx(() => false));
    expect(Object.keys(s.tools)).toEqual([]);
  });

  it('never consults a module-level "jira" key — the master lives in requires', () => {
    const s = makeServer();
    const seen = [];
    register(s, ctx((n) => { seen.push(n); return true; }));
    expect(seen).not.toContain('jira');
  });

  it('disables jira_add_comment while keeping jira_get_issue and jira_assign_issue (the incident case)', () => {
    const s = makeServer();
    register(s, ctx((n) => n !== 'jira_add_comment'));
    expect(s.tools.jira_add_comment).toBeUndefined();
    expect(s.tools.jira_get_issue).toBeDefined();
    expect(s.tools.jira_assign_issue).toBeDefined();
  });

  it.each(ALL_TOOLS)('gates %s independently', (target) => {
    const s = makeServer();
    register(s, ctx((n) => n === target));
    expect(Object.keys(s.tools)).toEqual([target]);
  });

  it('registers everything when isToolEnabled is absent (startup REST-API pass)', () => {
    const s = makeServer();
    const c = ctx(() => true);
    delete c.isToolEnabled;
    register(s, c);
    expect(Object.keys(s.tools).sort()).toEqual(ALL_TOOLS);
  });
});

describe('jira manifest + config defaults', () => {
  it('declares the master and all 9 tools, each child requiring the master', () => {
    const manifest = JSON.parse(readFileSync(MODULE_JSON, 'utf8'));
    const byName = new Map(manifest.tools.map(t => [t.name, t]));
    expect([...byName.keys()].sort()).toEqual(['jira', ...ALL_TOOLS].sort());
    for (const t of manifest.tools) {
      expect(t.type).toBe('mcp');
      expect(t.toolset).toBe('jira');
      expect(typeof t.description).toBe('string');
      expect(t.description.length).toBeGreaterThan(0);
    }
    expect(byName.get('jira').requires).toBeUndefined();
    for (const n of ALL_TOOLS) {
      expect(byName.get(n).requires).toBe('jira');
    }
  });

  it('preserves current defaults: master on, 8 tools on, jira_get_attachment off', () => {
    const cfg = JSON.parse(readFileSync(CONFIG_JSON, 'utf8'));
    expect(cfg['tools/jira/is_enabled']).toBe('1');
    for (const n of DEFAULT_ON) {
      expect(cfg[`tools/${n}/is_enabled`]).toBe('1');
    }
    expect(cfg['tools/jira_get_attachment/is_enabled']).toBeUndefined();
  });
});
