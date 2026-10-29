import { describe, it, expect, vi } from 'vitest';
import { enqueueJob } from '../job-enqueue.js';
import { executeTool } from '../dispatcher.js';
import { capabilityContext } from './capability-rows.js';
import { register } from '../../modules/subagent/toolbox/agent.js';

function fakeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

// A fake connection that answers by SQL, as the real tables would: the named lock, the caller
// row, the `agent_view` lookup, the scope read (IN_SCOPE: `scoped` rows by id), the open-line
// read (`lineOpen`), the cap read (`children` rows), the enqueueJob thread checks, its dedupe
// SELECT, the job INSERT and the outbox INSERT.
function fakeDb({ parent = { id: 42, agent_view_id: 7, priority: 20, depth: 0, thread_id: 5, line_id: null },
                  views = [], children = 0, scoped = [], lineOpen = false, head = null, failOn = null,
                  lockGot = 1 } = {}) {
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
      if (/GET_LOCK/i.test(sql)) return [[{ got: lockGot }]];
      if (/RELEASE_LOCK/i.test(sql)) return [[{ released: 1 }]];
      if (/FROM agent_view WHERE/i.test(sql)) return [views.filter(v => v.code === params[0] && v.is_active)];
      if (/x\.status/i.test(sql)) return [scoped.filter(x => x.id === params[1])];
      if (/parent_depth/i.test(sql)) return [head ? [head] : []];
      if (/status IN/i.test(sql)) return [lineOpen ? [{ 1: 1 }] : []];
      if (/LIMIT \d+/i.test(sql)) return [Array.from({ length: children }, () => ({ 1: 1 }))];
      if (/FROM thread/i.test(sql)) return [[{ agent_view_id: parent?.agent_view_id }]];
      if (/SELECT thread_id FROM job/i.test(sql)) return [[{ thread_id: parent?.thread_id }]];
      if (/idempotency_key = \?/i.test(sql)) return [[]];
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
    const { db, calls } = fakeDb({
      parent: { id: 42, agent_view_id: 7, priority: 20, depth, thread_id: 5, line_id: null }, views: [OWN] });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'Do the thing.' });
    expect(res.isError).toBeUndefined();
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status: 'TODO', depth: depth + 1 });
    const [, agentViewId, priority, referenceId, context, key, type, , parentJobId, childDepth, threadId, lineId] =
      jobInsert(calls).params;
    // The child lands in the caller's thread (and so its conversation), as a new line.
    expect({ agentViewId, priority, referenceId, context, type, parentJobId, childDepth, threadId, lineId })
      .toEqual({ agentViewId: 7, priority: 20, referenceId: '42', context: 'Do the thing.', type: 'subagent',
        parentJobId: 42, childDepth: depth + 1, threadId: 5, lineId: null });
    expect(jobInsert(calls).params[0]).toBe('subagent');
    expect(key).toMatch(/^subagent:42:[0-9a-f-]{36}$/);
    expect(calls.executed[0]).toEqual({ sql: 'SELECT GET_LOCK(?, 10) AS got', params: ['subagent:42'] });
    expect(calls.executed.some(c => /FOR UPDATE/.test(c.sql))).toBe(false);
    expect(calls.executed.at(-1)).toEqual({ sql: 'SELECT RELEASE_LOCK(?)', params: ['subagent:42'] });
    expect(calls.commit).toBe(1);
    expect(calls.released).toBe(1);
  });

  it.each([0, 1])('the child copies the parent dry_run %i', async (dryRun) => {
    const { db, calls } = fakeDb({
      parent: { id: 42, agent_view_id: 7, priority: 20, depth: 0, thread_id: 5, line_id: null, dry_run: dryRun },
      views: [OWN] });
    await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'Do the thing.' });
    expect(jobInsert(calls).sql).toMatch(/line_id, dry_run,/);
    expect(jobInsert(calls).params[12]).toBe(dryRun);
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
    expect(text(res)).toBe('max 10 sub-agents for this agent');
    expect(inserts(calls)).toEqual([]);
  });

  it('at depth 0 counts the subagent rows of the caller row only, reading at most the cap', async () => {
    const { db, calls } = fakeDb({ views: [OWN], children: 9 });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res.isError).toBeUndefined();
    const cap = calls.executed.find(c => /LIMIT \d+/.test(c.sql));
    expect(cap.sql).toMatch(/WHERE parent_job_id = \? AND type = 'subagent' LIMIT 10$/);
    expect(cap.params).toEqual([42]);
  });

  it('at depth > 0 counts the subagent rows under the whole caller line', async () => {
    const { db, calls } = fakeDb({
      parent: { id: 42, agent_view_id: 7, priority: 20, depth: 1, thread_id: 5, line_id: 30 }, views: [OWN] });
    await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    const cap = calls.executed.find(c => /LIMIT \d+/.test(c.sql));
    expect(cap.sql).toMatch(/\(p\.id = \? OR p\.line_id = \?\) AND p\.thread_id = \? AND x\.type = 'subagent' LIMIT 10$/);
    expect(cap.params).toEqual([30, 30, 5]);
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
    const { db, calls } = fakeDb({ views: [OWN], failOn: /LIMIT \d+/ });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res).toEqual({ content: [{ type: 'text', text: 'agent_run failed' }], isError: true });
    expect(calls.rollback).toBe(1);
    expect(calls.released).toBe(1);
  });
  it('refuses with the fixed text and inserts nothing when the parent lock is not taken', async () => {
    const { db, calls } = fakeDb({ views: [OWN], lockGot: 0 });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x' });
    expect(res).toEqual({ content: [{ type: 'text', text: 'agent_run failed' }], isError: true });
    expect(inserts(calls)).toEqual([]);
    expect(calls.executed.some(c => /RELEASE_LOCK/.test(c.sql))).toBe(false);
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

  it.each([['parent_job_id', 1], ['depth', 0], ['thread_id', 1], ['line_id', 1]])('rejects a call that adds %s', async (field, value) => {
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

describe('agent_run with job_id (continue a child)', () => {
  // 901 is the head of its line; 950 is a later row (a continuation or a wake) of line 901.
  const HEAD = { id: 901, status: 'SUCCESS', output: 'o', error_class: null, line_id: null, view_code: 'own' };
  const LATER = { ...HEAD, id: 950, line_id: 901 };
  const head = { agent_view_id: 7, depth: 1, thread_id: 5, line_id: null, parent_depth: 0, parent_thread: 5,
    parent_line: 42, caller_depth: 0, caller_thread: 5, caller_line: 42 };

  it.each([[HEAD, 901], [LATER, 901]])('inserts a new row of the child line', async (x, line) => {
    const { db, calls } = fakeDb({ views: [OWN], scoped: [x], head });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'Fix it.', job_id: x.id });
    expect(res.isError).toBeUndefined();
    expect(calls.executed.find(c => /x\.status/.test(c.sql)).params).toEqual([42, x.id]);
    expect(calls.executed.find(c => /status IN/.test(c.sql)).params).toEqual([line, line, 5]);
    const params = jobInsert(calls).params;
    expect({ parentJobId: params[8], depth: params[9], threadId: params[10], lineId: params[11] })
      .toEqual({ parentJobId: 42, depth: 1, threadId: 5, lineId: line });
  });

  it.each([
    ['a job out of scope', []],
    ['a child of another view', [{ ...HEAD, view_code: 'other' }]],
  ])('%s gives the not-found text and no INSERT', async (_n, scoped) => {
    const { db, calls } = fakeDb({ views: [OWN, OTHER], scoped });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x', job_id: 901 });
    expect(res).toEqual({ content: [{ type: 'text', text: 'job not found or not accessible' }], isError: true });
    expect(inserts(calls)).toEqual([]);
  });

  it('refuses while a row of the child line is open', async () => {
    const { db, calls } = fakeDb({ views: [OWN], scoped: [HEAD], lineOpen: true });
    const res = await tools(db).agent_run.handler({ agent_view: 'own', prompt: 'x', job_id: 901 });
    expect(text(res)).toBe('job is still running');
    expect(inserts(calls)).toEqual([]);
  });
});

describe('agent_run description', () => {
  const descriptionFor = (config) => {
    const server = fakeServer();
    register(server, ctx(fakeDb().db, { moduleConfigs: { subagent: config } }));
    return server.tools.agent_run.desc;
  };

  it.each([1, '1', 'true', 'yes'])('with wake_parent %j tells the agent to end its turn', (v) => {
    const desc = descriptionFor({ wake_parent: v });
    expect(desc).toContain('End your turn after delegating. Do not poll: you are resumed when all children finish.');
    expect(desc).not.toContain('Poll agent_result');
  });

  it.each([0, '0', '', undefined])('with wake_parent %j tells the agent to poll', (v) => {
    const desc = descriptionFor({ wake_parent: v });
    expect(desc).toContain('Poll agent_result, or use schedule_followup (root jobs only).');
    expect(desc).not.toContain('End your turn');
  });

  it('always names the shared directory and the job_id continuation', () => {
    const desc = descriptionFor({});
    expect(desc).toContain('`./shared` is shared with your parent and your children');
    expect(desc).toContain('Pass job_id of an earlier child to continue that child\'s session with a new prompt.');
  });
});

describe('agent_result', () => {
  const CHILD = { id: 901, status: 'SUCCESS', output: 'the answer', error_class: null, line_id: null, view_code: 'own' };

  it('returns status and output of a job in scope, binding the caller job from context', async () => {
    const { db, calls } = fakeDb({ scoped: [CHILD] });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status: 'SUCCESS', output: 'the answer' });
    expect(calls.executed[0].params).toEqual([42, 901]);
  });

  it.each(['FAILED', 'DEAD'])('a %s child carries error_class only', async (status) => {
    const { db } = fakeDb({ scoped: [{ ...CHILD, status, output: null, error_class: 'harness_error',
      error_message: 'quoted CLI output' }] });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status, error: 'harness_error' });
  });

  it.each(['TODO', 'RUNNING', 'PAUSED'])('a %s child gives status only', async (status) => {
    const { db } = fakeDb({ scoped: [{ ...CHILD, status, output: null }] });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(JSON.parse(text(res))).toEqual({ job_id: 901, status });
  });

  it('a job out of scope gives one identical text', async () => {
    const { db } = fakeDb({ scoped: [] });
    const res = await tools(db).agent_result.handler({ job_id: 901 });
    expect(res).toEqual({ content: [{ type: 'text', text: 'job not found or not accessible' }], isError: true });
  });

  it('refuses outside a job context before any DB call', async () => {
    const { db, calls } = fakeDb({ scoped: [CHILD] });
    const res = await tools(db, { jobId: null }).agent_result.handler({ job_id: 901 });
    expect(text(res)).toBe('agent_result requires an Agento job context');
    expect(calls.executed).toEqual([]);
  });
});
