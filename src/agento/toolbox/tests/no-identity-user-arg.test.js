import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../modules/core/toolbox/email-transport.js', () => ({ createTransporter: vi.fn() }));

import { register as registerJira } from '../../modules/jira/toolbox/jira.js';
import { register as registerEmail } from '../../modules/core/toolbox/email.js';
import { register as registerSchedule } from '../../modules/core/toolbox/schedule.js';
import { register as registerBrowser } from '../../modules/core/toolbox/browser.js';
import { registerMysqlTools, registerMysqlRootTools } from '../adapters/mysql.js';
import { registerMssqlTools } from '../adapters/mssql.js';
import { registerOpensearchTools } from '../adapters/opensearch.js';
import { SqlPoolRegistry } from '../adapters/sql-pool-registry.js';
import { matchesWhitelist } from '../email-match.js';
import { createScopedLogger } from '../log.js';
import { executeTool } from '../dispatcher.js';
import { capabilityContext } from './capability-rows.js';
import { jiraCtx } from './jira-register-ctx.js';

// The caller's identity comes from the verified capability, never from a tool argument: no
// shipped tool takes a `user`, and the session logger names the caller on every line.

const SQL = [{ name: 'db_x', description: 'X', config: { host: 'h.test', user: 'r', pass: 'p', database: 'd' } }];

function capture() {
  const tools = new Map();
  return { tools, tool: (name, desc, schema, handler) => tools.set(name, { desc, schema, handler }) };
}

// Every shipped tool, registered the way a session registers it.
function registerAll(log = vi.fn()) {
  const server = capture();
  const sqlPoolRegistry = new SqlPoolRegistry();
  registerJira(server, { ...jiraCtx(), log });
  registerEmail(server, {
    log, isToolEnabled: () => true, matchesWhitelist,
    moduleConfigs: { core: { smtp_host: 'smtp.test', smtp_user: 's', email_whitelist: '*@allowed.com', smtp_reply_to: '' } },
  });
  registerSchedule(server, { log, db: {}, isToolEnabled: () => true, jobId: null, enqueueJob: vi.fn() });
  registerBrowser(server, {
    log, isToolEnabled: () => true, artifactsDir: '/workspace/tmp', moduleConfigs: { core: {} },
    playwright: {
      getClient: () => null,
      getTools: () => [{ name: 'browser_click', inputSchema: { properties: { ref: { type: 'string' } }, required: ['ref'] } }],
      getViewport: () => ({ width: 1366, height: 768 }),
    },
  });
  registerMysqlTools(server, SQL, { sqlPoolRegistry, log, tier: 'read-only' });
  registerMysqlRootTools(server, SQL.map(t => ({ ...t, name: 'db_root' })), { sqlPoolRegistry, log });
  registerMssqlTools(server, SQL.map(t => ({ ...t, name: 'mssql_x' })), { sqlPoolRegistry, log });
  registerOpensearchTools(server, SQL.map(t => ({ ...t, name: 'os_x' })), { log });
  return server.tools;
}

describe('no shipped tool takes an identity argument', () => {
  it('no registered tool schema has a `user` key', () => {
    const tools = registerAll();
    expect(tools.size).toBeGreaterThan(20);
    const withUser = [...tools].filter(([, t]) => Object.hasOwn(t.schema, 'user')).map(([name]) => name);
    expect(withUser).toEqual([]);
  });

  it('a call that still sends `user` is invalid_arguments and runs nothing', async () => {
    const tool = registerAll().get('jira_get_issue');
    const handler = vi.fn(tool.handler);
    const ctx = capabilityContext('mcp_job', { agent_view_id: 1 });
    const response = await executeTool(ctx, 'jira_get_issue', { user: 'human@corp.com', issue_key: 'AI-1' }, {
      endpoint: 'mcp',
      audit: { insert: vi.fn(async () => {}), finalize: vi.fn(async () => {}) },
      consume: vi.fn(async () => true),
      reverify: vi.fn(async () => ({ context: ctx, single_use: false, permitted_tools: null })),
      loadRegistry: async () => ({
        tools: new Map([['jira_get_issue', { schema: tool.schema, handler }]]),
        unavailableTools: new Set(), isEnabled: () => true, isSensitive: () => false,
      }),
      loadOverrides: vi.fn(async () => ({})),
      log: vi.fn(),
    });
    expect(response.error.code).toBe('invalid_arguments');
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('the session logger names the verified caller on a tool line', () => {
  const PREFIX = '[Mieszko (id: 1)] [actor=agent subject=1 job=42] ';
  let lines;

  beforeEach(() => {
    lines = [];
    vi.spyOn(console, 'log').mockImplementation(line => lines.push(line));
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, text: async () => 'down', json: async () => ({}) })));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([
    ['jira_get_issue', { issue_key: 'AI-1' }],
    ['db_x', { query: 'DELETE FROM orders' }],
    ['email_send', { to: ['stranger@evil.com'], subject: 's', body_html: '<p>b</p>' }],
  ])('%s', async (name, args) => {
    const log = createScopedLogger({ id: 1, label: 'Mieszko' }, capabilityContext('mcp_job', { agent_view_id: 1 }));
    await registerAll(log).get(name).handler(args);
    const line = lines.find(l => l.includes(`[${name}]`));
    expect(line).toContain(`${PREFIX}[${name}]`);
    expect(line).not.toContain('user=');
  });
});
