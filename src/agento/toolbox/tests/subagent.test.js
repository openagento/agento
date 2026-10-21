import { describe, it, expect, vi } from 'vitest';
import { enqueueJob } from '../job-enqueue.js';
import { executeTool } from '../dispatcher.js';
import { capabilityContext } from './capability-rows.js';
import { register } from '../../modules/subagent/toolbox/agent.js';

function fakeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

// A fake connection that answers by SQL, as the real tables would:
//   parent `SELECT … FOR UPDATE`, `agent_view` lookup, child COUNT, the enqueueJob dedupe
//   SELECT, the job INSERT, the outbox INSERT, and the agent_result child read.
function fakeDb({ parent = { id: 42, agent_view_id: 7, priority: 20, depth: 0 }, views = [], children = 0,
                  child = null, failOn = null } = {}) {
  const calls = { executed: [], begin: 0, commit: 0, rollback: 0, released: 0 };
  let nextId = 900;
  const conn = {
    async beginTransaction() { calls.begin += 1; },
    async commit() { calls.commit += 1; },
    async rollback() { calls.rollback += 1; },
    release() { calls.released += 1; },
    async execute(sql, params) {
      calls.executed.push({ sql, params });
      if (failOn && failOn.test(sql)) throw new Error('connect ECONNREFUSED 10.0.0.1 SELECT secret');
      if (/FROM agent_view/i.test(sql)) return [views.filter(v => v.code === params[0] && v.is_active)];
      if (/COUNT\(\*\)/i.test(sql)) return [[{ n: children }]];
      if (/idempotency_key = \?/i.test(sql)) return [[]];
      if (/parent_job_id = \?/i.test(sql) && /^\s*SELECT/i.test(sql)) {
        return [child && child.id === params[0] && child.parent_job_id === params[1] ? [child] : []];
      }
      if (/^\s*SELECT/i.test(sql)) return [parent && parent.id === params[0] ? [parent] : []];
      if (/INSERT INTO job_event_outbox/i.test(sql)) return [{ affectedRows: 1 }];
      if (/INSERT INTO job/i.test(sql)) { nextId += 1; return [{ affectedRows: 1, insertId: nextId }]; }
      throw new Error(`unexpected SQL ${sql}`);
    },
  };
  const pool = { async getConnection() { return conn; }, execute: (...a) => conn.execute(...a) };
  return { db: { getCronPool: () => pool }, calls };
}

const inserts = (calls) => calls.executed.filter(c => /^\s*INSERT/i.test(c.sql));
const jobInsert = (calls) => calls.executed.find(c => /INSERT INTO job\s*\(/i.test(c.sql));

function ctx(db, over = {}) {
  return {
    log: vi.fn(), db, isToolEnabled: () => true, jobId: 42, agentViewId: 7, enqueueJob,
    errorCategory: (err) => err?.constructor?.name ?? 'Error',
    moduleConfigs: { subagent: { allowed_agent_views: '', max_children_per_job: '10' } },
    ...over,
  };
}

function tools(db, over) {
  const server = fakeServer();
  register(server, ctx(db, over));
  return server.tools;
}

const OWN = { id: 7, code: 'own', is_active: 1 };
const OTHER = { id: 8, code: 'other', is_active: 1 };
const text = (res) => res.content[0].text;

describe('subagent gating (TBX-2)', () => {
  it('registers each tool only when its own key is enabled', () => {
    const server = fakeServer();
    register(server, ctx(fakeDb().db, { isToolEnabled: (n) => n === 'agent_result' }));
    expect(Object.keys(server.tools)).toEqual(['agent_result']);
    const none = fakeServer();
    register(none, ctx(fakeDb().db, { isToolEnabled: () => false }));
    expect(Object.keys(none.tools)).toEqual([]);
  });
});

describe('agent_run', () => {
  it.each([0, 1, 2])('from depth %i creates a child one level down, bound to the parent', async (depth) => {
    const { db, calls } = fakeDb({ parent: { id: 42, agent_view_id: 7, priority: 20, depth }, views: [OWN] });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'Do the thing.' });
    expect(res.isError).toBeUndefined();
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status: 'TODO', depth: depth + 1 });
    const [, agentViewId, priority, referenceId, context, key, type, , parentJobId, childDepth] = jobInsert(calls).params;
    expect({ agentViewId, priority, referenceId, context, type, parentJobId, childDepth })
      .toEqual({ agentViewId: 7, priority: 20, referenceId: '42', context: 'Do the thing.', type: 'subagent',
        parentJobId: 42, childDepth: depth + 1 });
    expect(jobInsert(calls).params[0]).toBe('subagent');
    expect(key).toMatch(/^subagent:42:[0-9a-f-]{36}$/);
    expect(calls.executed[0].sql).toMatch(/FOR UPDATE/);
    expect(calls.commit).toBe(1);
    expect(calls.released).toBe(1);
  });

  it('refuses at depth 3 with the exact text and inserts nothing', async () => {
    const { db, calls } = fakeDb({ parent: { id: 42, agent_view_id: 7, priority: 20, depth: 3 }, views: [OWN] });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res).toEqual({ content: [{ type: 'text', text: 'sorry, max 3 nested subagents levels' }], isError: true });
    expect(inserts(calls)).toEqual([]);
    expect(calls.rollback).toBe(1);
  });

  it('refuses outside a job context before any DB call', async () => {
    const { db, calls } = fakeDb();
    const res = await tools(db, { jobId: null }).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(text(res)).toBe('agent_run requires an Agento job context');
    expect(res.isError).toBe(true);
    expect(calls.executed).toEqual([]);
  });

  it.each([
    ['parent row missing', { parent: null }],
    ['parent belongs to another view', { parent: { id: 42, agent_view_id: 99, priority: 0, depth: 0 } }],
  ])('fails closed when the %s', async (_name, db0) => {
    const { db, calls } = fakeDb({ ...db0, views: [OWN] });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(text(res)).toBe('agent_run requires an Agento job context');
    expect(inserts(calls)).toEqual([]);
  });

  it.each([
    ['unknown', [OWN], 'ghost', 'other'],
    ['inactive', [OWN, { ...OTHER, is_active: 0 }], 'other', 'other'],
    ['not allowed', [OWN, OTHER], 'other', ''],
  ])('an %s target gives one identical text and no INSERT', async (_n, views, target, allowed) => {
    const { db, calls } = fakeDb({ views });
    const res = await tools(db, {
      moduleConfigs: { subagent: { allowed_agent_views: allowed, max_children_per_job: 10 } },
    }).agent_run.handler({ agent_view: target, prompt: 'x' });
    expect(text(res)).toBe('agent_view not found or not allowed');
    expect(res.isError).toBe(true);
    expect(inserts(calls)).toEqual([]);
  });

  it('delegates to a view in allowed_agent_views (trimmed list)', async () => {
    const { db, calls } = fakeDb({ views: [OWN, OTHER] });
    const res = await tools(db, {
      moduleConfigs: { subagent: { allowed_agent_views: ' x , other ,', max_children_per_job: 10 } },
    }).agent_run.handler({ agent_view: 'other', prompt: 'x' });
    expect(res.isError).toBeUndefined();
    expect(jobInsert(calls).params[1]).toBe(8);
  });

  it('refuses at the child cap', async () => {
    const { db, calls } = fakeDb({ views: [OWN], children: 10 });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(text(res)).toBe('max 10 child jobs per job reached');
    expect(inserts(calls)).toEqual([]);
  });

  it.each([undefined, '', 'abc', '0', '-1', '1.5'])('an invalid max_children_per_job %j refuses', async (v) => {
    const { db, calls } = fakeDb({ views: [OWN] });
    const c = ctx(db, { moduleConfigs: { subagent: { allowed_agent_views: '', max_children_per_job: v } } });
    const server = fakeServer();
    register(server, c);
    const res = await server.tools.agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(text(res)).toBe('agent_run is not configured');
    expect(inserts(calls)).toEqual([]);
    expect(c.log).toHaveBeenCalledWith('agent_run', 'WARN', expect.stringContaining('av=7'));
  });

  it('answers a fixed text on an unexpected DB error and leaks nothing', async () => {
    const { db, calls } = fakeDb({ views: [OWN], failOn: /COUNT/ });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res).toEqual({ content: [{ type: 'text', text: 'agent_run failed' }], isError: true });
    expect(calls.rollback).toBe(1);
    expect(calls.released).toBe(1);
  });
  it('answers the same fixed text when no connection can be taken', async () => {
    const db = { getCronPool: () => ({ async getConnection() { throw new Error('connect ECONNREFUSED 10.0.0.1'); } }) };
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res).toEqual({ content: [{ type: 'text', text: 'agent_run failed' }], isError: true });
  });
});

describe('agent_run through the real dispatcher (strict arguments)', () => {
  function deps(registered) {
    const rows = new Map();
    return {
      endpoint: 'mcp',
      audit: { insert: async (r) => rows.set(r.execution_id, r), finalize: async () => {} },
      consume: async () => true,
      reverify: async () => ({ context: capabilityContext('mcp_job'), single_use: false, permitted_tools: null }),
      loadRegistry: async () => ({
        toolNames: Object.keys(registered), tools: new Map(Object.entries(registered)),
        unavailableTools: new Set(), isEnabled: () => true,
      }),
      loadOverrides: async () => ({}),
      log: vi.fn(),
    };
  }

  it.each([['parent_job_id', 1], ['depth', 0], ['job_id', 1]])('rejects a call that adds %s', async (field, value) => {
    const { db, calls } = fakeDb({ views: [OWN] });
    const t = tools(db);
    const res = await executeTool(capabilityContext('mcp_job'), 'agent_run',
      { agent_view: 'own', prompt: 'x', [field]: value }, deps({ agent_run: t.agent_run }));
    expect(res.ok).toBe(false);
    expect(res.error.code).toBe('invalid_arguments');
    expect(calls.executed).toEqual([]);
  });

  it.each([['bad code!'], ['x'.repeat(51)]])('rejects agent_view %j', async (code) => {
    const { db } = fakeDb({ views: [OWN] });
    const res = await executeTool(capabilityContext('mcp_job'), 'agent_run',
      { agent_view: code, prompt: 'x' }, deps({ agent_run: tools(db).agent_run }));
    expect(res.error.code).toBe('invalid_arguments');
  });

  it('accepts a valid call', async () => {
    const { db } = fakeDb({ views: [OWN] });
    const res = await executeTool(capabilityContext('mcp_job'), 'agent_run',
      { agent_view: 'own', prompt: 'x' }, deps({ agent_run: tools(db).agent_run }));
    expect(res.ok).toBe(true);
  });
});

describe('agent_result', () => {
  const CHILD = { id: 901, parent_job_id: 42, status: 'SUCCESS', output: 'the answer', error_class: null };

  it('returns status and output of a SUCCESS child, binding the caller job from context', async () => {
    const { db, calls } = fakeDb({ child: CHILD });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status: 'SUCCESS', output: 'the answer' });
    expect(calls.executed[0].params).toEqual([901, 42]);
  });

  it.each(['FAILED', 'DEAD'])('a %s child carries error_class only', async (status) => {
    const { db } = fakeDb({ child: { ...CHILD, status, output: null, error_class: 'harness_error',
      error_message: 'quoted CLI output' } });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status, error: 'harness_error' });
  });

  it.each(['TODO', 'RUNNING', 'PAUSED'])('a %s child gives status only', async (status) => {
    const { db } = fakeDb({ child: { ...CHILD, status, output: null } });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status });
  });

  it.each([
    ['sibling', { ...CHILD, parent_job_id: 41 }],
    ['grandchild', { ...CHILD, parent_job_id: 900 }],
    ['unrelated', { ...CHILD, parent_job_id: null }],
    ['nonexistent', null],
  ])('a %s job gives one identical text', async (_n, child) => {
    const { db } = fakeDb({ child });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(res).toEqual({ content: [{ type: 'text', text: 'job not found or not accessible' }], isError: true });
  });

  it('refuses outside a job context before any DB call', async () => {
    const { db, calls } = fakeDb({ child: CHILD });
    const res = await tools(db, { jobId: null }).agent_result.handler({ job_id: 901 });
    expect(text(res)).toBe('agent_result requires an Agento job context');
    expect(calls.executed).toEqual([]);
  });
});
