import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { register } from '../../modules/jira/toolbox/jira.js';

// The miniapp-build skill is the agent's only guide to a miniapp; its examples must work as written.
const SKILL = readFileSync(
  fileURLToPath(new URL('../../modules/miniapps/skills/miniapp-build/SKILL.md', import.meta.url)), 'utf8');

function jiraSchemas() {
  const tools = {};
  register({ tool(name, desc, schema) { tools[name] = schema; } }, {
    log: vi.fn(),
    moduleConfigs: { jira: { jira_host: 'https://x.atlassian.net', jira_user: 'u@x.com', jira_token: 't' } },
    isToolEnabled: () => true,
    artifactsDir: '/tmp/art',
    fileManager: { downloadAndConvert: vi.fn() },
  });
  return tools;
}

// The same escape the skill tells the agent to apply.
const bake = (data) => JSON.stringify(data).replace(/</g, '\\u003c');

describe('miniapp-build skill examples', () => {
  it('every callAction example gives each required argument of its tool', () => {
    const schemas = jiraSchemas();
    const calls = [...SKILL.matchAll(/callAction\("([a-z0-9_]+)",\s*\{([^}]*)\}/g)];
    expect(calls.length).toBeGreaterThan(0);
    for (const [, tool, body] of calls) {
      expect(schemas[tool], `${tool} is not a Jira tool`).toBeDefined();
      const given = new Set([...body.matchAll(/(\w+)\s*:/g)].map((m) => m[1]));
      const required = Object.entries(schemas[tool]).filter(([, z]) => !z.isOptional()).map(([k]) => k);
      expect(required.filter((k) => !given.has(k)), tool).toEqual([]);
    }
  });

  it('the data block example holds no raw "<"', () => {
    // A block whose body starts as JSON; the prose that names the tag is not an example.
    const blocks = [...SKILL.matchAll(/<script type="application\/json" id="data">([[{][\s\S]*?)<\/script>/g)];
    expect(blocks.length).toBeGreaterThan(0);
    for (const [, json] of blocks) {
      expect(json).not.toContain('<');
      expect(() => JSON.parse(json)).not.toThrow();
    }
  });

  it('the escape rule keeps hostile text inside the block and round-trips it', () => {
    const hostile = [{ summary: '</script><script>alert(1)</script><!--' }];
    const baked = bake(hostile);
    expect(baked).not.toContain('<');
    expect(JSON.parse(baked)).toEqual(hostile);
  });
});
