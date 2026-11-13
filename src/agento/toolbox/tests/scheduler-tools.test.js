import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, vi } from 'vitest';
import { executeTool } from '../dispatcher.js';
import { capabilityContext } from './capability-rows.js';
import { register, parseRrule } from '../../modules/scheduler/toolbox/schedule-tools.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(fs.readFileSync(
  path.join(here, '../../../../tests/fixtures/scheduler/rrule_vectors.json'), 'utf8'));
const TOOLS = ['schedule_create', 'schedule_update', 'schedule_list', 'schedule_delete'];

function fakeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

const ROW = { id: 5, source: 'scheduler', enabled: 1, agent_view_id: 7, owner_agent_view_id: 7, owner_user_id: null };
const JIRA = { ...ROW, id: 6, source: 'jira_periodic' };
const OTHER_VIEW = { ...ROW, id: 8, agent_view_id: 9, owner_agent_view_id: 9 };
const USERS_ROW = { ...ROW, id: 10, owner_agent_view_id: null, owner_user_id: 3 };

// A fake connection that answers by SQL as the tables would: the caller's job, user, the
// scheduler.own and reach grants, the limit lock and count, the reach-checked row reads.
function fakeDb({ job = { agent_view_id: 7, requester_trust: 'claimed', requester_key: null },
                  user = { role: 'user', is_active: 1 }, own = true, reach = true, count = 0,
                  rows = [ROW, JIRA, OTHER_VIEW, USERS_ROW] } = {}) {
  const calls = { executed: [], commit: 0, rollback: 0, released: 0 };
  const reachable = (p) => rows.filter(r => r.agent_view_id === p[0]
    && (r.owner_agent_view_id === p[1] || (p[2] !== null && r.owner_user_id === p[2])));
  const conn = {
    async beginTransaction() {},
    async commit() { calls.commit += 1; },
    async rollback() { calls.rollback += 1; },
    release() { calls.released += 1; },
    async execute(sql, params) {
      calls.executed.push({ sql, params });
      if (/requester_trust/.test(sql)) return [job ? [job] : []];
      if (/FROM `user`/.test(sql)) return [user ? [user] : []];
      if (/name = 'scheduler.own'/.test(sql)) return [own ? [{ 1: 1 }] : []];
      if (/JOIN agent_view v/.test(sql)) return [reach ? [{ 1: 1 }] : []];
      if (/GET_LOCK/.test(sql)) return [[{ got: 1 }]];
      if (/RELEASE_LOCK/.test(sql)) return [[{ r: 1 }]];
      if (/COUNT\(\*\)/.test(sql)) return [[{ n: count }]];
      if (/FOR UPDATE/.test(sql)) return [reachable(params.slice(1)).filter(r => r.id === params[0])];
      if (/^SELECT id, source, title/.test(sql)) return [reachable(params).map(r => ({ ...r, title: 't' }))];
      if (/^INSERT INTO schedule \(/.test(sql)) return [{ insertId: 77 }];
      if (/^(INSERT INTO schedule_audit|UPDATE schedule)/.test(sql)) return [{ affectedRows: 1 }];
      throw new Error(`unexpected SQL ${sql}`);
    },
  };
  return { db: { getCronPool: () => ({ async getConnection() { return conn; } }) }, calls };
}

function ctx(db, over = {}) {
  return {
    log: vi.fn(), db, isToolEnabled: () => true, jobId: 42, agentViewId: 7,
    errorCategory: (err) => err?.constructor?.name ?? 'Error',
    moduleConfigs: {
      core: { 'scheduler/min_interval_minutes': '5', 'scheduler/default_timezone': 'Europe/Warsaw' },
      scheduler: { 'limits/max_per_user': '10', 'limits/max_per_agent_view': '20' },
      conversation: { 'limits/max_message_bytes': '32768' },
    },
    ...over,
  };
}

function tools(db, over) {
  const server = fakeServer();
  register(server, ctx(db, over));
  return server.tools;
}

const text = (res) => res.content[0].text;
const sqlOf = (calls, re) => calls.executed.filter(c => re.test(c.sql));
const insertOf = (calls) => sqlOf(calls, /^INSERT INTO schedule \(/)[0];
const ACCOUNT = { agent_view_id: 7, requester_trust: 'account', requester_key: 'user:3' };
const NEW = { title: 'Report', prompt: 'Write it.', rrule: 'freq=daily;byhour=9;byminute=0' };

describe('gates (TBX-2)', () => {
  it.each(TOOLS)('registers %s only when its own key is enabled', (name) => {
    const server = fakeServer();
    register(server, ctx(fakeDb().db, { isToolEnabled: (n) => n === name }));
    expect(Object.keys(server.tools)).toEqual([name]);
  });

  it('refuses without a job of the caller view', async () => {
    for (const over of [{ jobId: null }, { agentViewId: 9 }]) {
      const res = await tools(fakeDb().db, over).schedule_list.handler({});
      expect(res).toEqual({ content: [{ type: 'text', text: 'the schedule tools need an Agento job context' }],
        isError: true });
    }
  });
});

describe('schedule_create owner (D-11, OP-2)', () => {
  it('a job with no account user makes a view-owned schedule under the view limit', async () => {
    const { db, calls } = fakeDb();
    const res = await tools(db).schedule_create.handler({ ...NEW, starts_at: '2027-01-01T09:00' });
    expect(JSON.parse(text(res)).id).toBe(77);
    const [source, title, prompt, view, ownerUser, ownerView, rrule, startsAt, tz, enabled, createdJob] =
      insertOf(calls).params;
    expect({ source, title, prompt, view, ownerUser, ownerView, rrule, startsAt, tz, enabled, createdJob }).toEqual({
      source: 'scheduler', title: 'Report', prompt: 'Write it.', view: 7, ownerUser: null, ownerView: 7,
      rrule: 'FREQ=DAILY;BYHOUR=9;BYMINUTE=0', startsAt: '2027-01-01 09:00:00', tz: 'Europe/Warsaw', enabled: 1,
      createdJob: 42 });
    expect(sqlOf(calls, /GET_LOCK/)[0].params).toEqual(['scheduler:limit:view:7:']);
    expect(sqlOf(calls, /COUNT/)[0].sql).toMatch(/owner_agent_view_id = \?/);
    expect(sqlOf(calls, /schedule_audit/)[0].params).toEqual([77, 'create', 'job:42', null]);
    expect(calls.commit).toBe(1);
    expect(sqlOf(calls, /RELEASE_LOCK/)).toHaveLength(1);
  });

  it('an account job makes a user-owned schedule under the user limit', async () => {
    const { db, calls } = fakeDb({ job: ACCOUNT });
    await tools(db).schedule_create.handler(NEW);
    const params = insertOf(calls).params;
    expect([params[4], params[5]]).toEqual([3, null]);
    expect(params[7]).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:00$/); // default start: local now
    expect(sqlOf(calls, /GET_LOCK/)[0].params).toEqual(['scheduler:limit:user:3:']);
  });

  it('an admin user is not limited', async () => {
    const { db, calls } = fakeDb({ job: ACCOUNT, user: { role: 'admin', is_active: 1 }, own: false, reach: false });
    expect((await tools(db).schedule_create.handler(NEW)).isError).toBeUndefined();
    expect(sqlOf(calls, /COUNT|GET_LOCK/)).toEqual([]);
  });

  it.each([
    [{ user: { role: 'user', is_active: 0 } }, 'the requesting user is not active'],
    [{ user: null }, 'the requesting user is not active'],
    [{ own: false }, 'the requesting user may not own schedules'],
    [{ reach: false }, 'the requesting user cannot reach this agent_view'],
    [{ count: 10 }, 'limit: at most 10 schedules'],
  ])('refuses %o with no fallback to the view', async (over, message) => {
    const { db, calls } = fakeDb({ job: ACCOUNT, ...over });
    expect(await tools(db).schedule_create.handler(NEW)).toEqual({ content: [{ type: 'text', text: message }],
      isError: true });
    expect(insertOf(calls)).toBeUndefined();
  });

  it('refuses when a limit or the minimum interval is not configured', async () => {
    const base = ctx(null).moduleConfigs;
    for (const moduleConfigs of [
      { ...base, scheduler: {} },
      { ...base, core: { 'scheduler/default_timezone': 'UTC' } },
    ]) {
      const { db, calls } = fakeDb();
      const res = await tools(db, { moduleConfigs }).schedule_create.handler(NEW);
      expect(res.isError).toBe(true);
      expect(text(res)).toMatch(/not configured/);
      expect(insertOf(calls)).toBeUndefined();
    }
  });

  it.each([
    [{ rrule: 'FREQ=SECONDLY' }, 'rrule refused: too_frequent'],
    [{ rrule: 'FREQ=MINUTELY;INTERVAL=2' }, 'rrule refused: too_frequent'],
    [{ rrule: 'FREQ=DAILY;BYHOUR=25' }, 'rrule refused: bad_rule'],
    [{ timezone: 'Mars/Base' }, 'timezone refused: bad_timezone (use a canonical IANA name)'],
    [{ starts_at: '2027-02-30T09:00' }, 'starts_at refused: not a real local time'],
  ])('refuses %o', async (over, message) => {
    const { db, calls } = fakeDb();
    expect(text(await tools(db).schedule_create.handler({ ...NEW, ...over }))).toBe(message);
    expect(insertOf(calls)).toBeUndefined();
  });
});

describe('prompt size: conversation/limits/max_message_bytes in UTF-8 bytes, as the panel (CFG-1)', () => {
  const limited = (bytes) => ({ moduleConfigs: { ...ctx(null).moduleConfigs,
    conversation: { 'limits/max_message_bytes': String(bytes) } } });
  const call = (name, prompt, over) => {
    const { db, calls } = fakeDb();
    const args = name === 'schedule_create' ? { ...NEW, prompt } : { id: 5, prompt };
    return tools(db, over)[name].handler(args).then(res => ({ res, calls }));
  };

  it.each([
    ['ASCII over the limit', 'a'.repeat(32769)],
    ['multibyte: 12000 characters, 36000 bytes', '\u20ac'.repeat(12000)],
    ['blank', ' \n\t '],
  ])('%s: both tools refuse it', async (_, prompt) => {
    for (const name of TOOLS.slice(0, 2)) {
      const { res, calls } = await call(name, prompt);
      expect(text(res)).toBe('prompt refused: it must be 1 to 32768 bytes and not blank');
      expect(sqlOf(calls, /^UPDATE schedule|^INSERT INTO schedule \(/)).toEqual([]);
    }
  });

  it('follows a changed setting', async () => {
    for (const name of TOOLS.slice(0, 2)) {
      expect(text((await call(name, 'a'.repeat(2049), limited(2048))).res)).toMatch(/1 to 2048 bytes/);
      expect((await call(name, 'a'.repeat(2048), limited(2048))).res.isError).toBeUndefined();
    }
  });

  it('refuses when the limit is not configured', async () => {
    const { res, calls } = await call('schedule_create', 'p', limited(''));
    expect(text(res)).toMatch(/not configured \(conversation\/limits\/max_message_bytes\)/);
    expect(insertOf(calls)).toBeUndefined();
  });
});

describe('reach (OP-1) and the read-only source', () => {
  it('lists only reachable rows and marks a Jira row not editable', async () => {
    const { db, calls } = fakeDb();
    const listed = JSON.parse(text(await tools(db).schedule_list.handler({})));
    expect(listed.map(r => [r.id, r.editable, r.owner])).toEqual([[5, true, 'agent_view'], [6, false, 'agent_view']]);
    expect(sqlOf(calls, /^SELECT id, source, title/)[0].params).toEqual([7, 7, null]);
  });

  it('an account job also reaches its user\'s rows in this view', async () => {
    const listed = JSON.parse(text(await tools(fakeDb({ job: ACCOUNT }).db).schedule_list.handler({})));
    expect(listed.map(r => r.id)).toEqual([5, 6, 10]);
  });

  it.each([['schedule_update', { title: 'x' }], ['schedule_delete', {}]])('%s refuses other rows', async (name, args) => {
    for (const [id, message] of [[8, 'schedule not found'], [10, 'schedule not found'], [999, 'schedule not found'],
      [6, 'this schedule is read-only (it belongs to another module)']]) {
      const { db, calls } = fakeDb();
      expect(text(await tools(db)[name].handler({ id, ...args }))).toBe(message);
      expect(sqlOf(calls, /^UPDATE|^INSERT/)).toEqual([]);
      expect(calls.rollback).toBe(1);
    }
  });

  it('every write carries the reach in its WHERE clause', async () => {
    for (const [name, args] of [['schedule_update', { id: 5, title: 'x' }], ['schedule_delete', { id: 5 }]]) {
      const { db, calls } = fakeDb();
      await tools(db)[name].handler(args);
      const update = sqlOf(calls, /^UPDATE schedule/)[0];
      expect(update.sql).toMatch(/WHERE id = \? AND source = \? AND agent_view_id = \? AND deleted_at IS NULL AND \(owner_agent_view_id = \? OR owner_user_id = \?\)/);
      expect(update.params.slice(-5)).toEqual([5, 'scheduler', 7, 7, null]);
      expect(sqlOf(calls, /schedule_audit/)[0].params.slice(0, 3)).toEqual([5, name === 'schedule_update' ? 'update' : 'delete', 'job:42']);
    }
  });
});

describe('schedule_update timing', () => {
  it('a timing change leaves the next fire and the anchor to the tick', async () => {
    const { db, calls } = fakeDb();
    await tools(db).schedule_update.handler({ id: 5, starts_at: '2027-01-01T10:00' });
    const update = sqlOf(calls, /^UPDATE schedule/)[0];
    expect(update.sql).toMatch(/^UPDATE schedule SET starts_at = \?, next_fire_at = \?, anchor_local = \?, last_error = \? WHERE/);
    expect(update.params.slice(0, 4)).toEqual(['2027-01-01 10:00:00', null, null, null]);
  });

  it('a title change leaves the timing', async () => {
    const { db, calls } = fakeDb();
    await tools(db).schedule_update.handler({ id: 5, title: 'New' });
    expect(sqlOf(calls, /^UPDATE schedule/)[0].sql).toMatch(/^UPDATE schedule SET title = \? WHERE/);
  });

  it('a re-enable leaves the next fire to the tick', async () => {
    const { db, calls } = fakeDb({ rows: [{ ...ROW, enabled: 0 }] });
    await tools(db).schedule_update.handler({ id: 5, enabled: true });
    expect(sqlOf(calls, /^UPDATE schedule/)[0].sql).toMatch(/^UPDATE schedule SET enabled = \?, next_fire_at = \?/);
  });
});

describe('strict arguments through the real dispatcher', () => {
  function deps(registered) {
    return {
      endpoint: 'mcp',
      audit: { insert: async () => {}, finalize: async () => {} },
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

  it.each([['harness', 'claude'], ['model', 'x'], ['agent_view_id', 9], ['owner_user_id', 1]])(
    'schedule_create rejects %s', async (field, value) => {
      const { db, calls } = fakeDb();
      const t = tools(db);
      const res = await executeTool(capabilityContext('mcp_job'), 'schedule_create', { ...NEW, [field]: value },
        deps({ schedule_create: t.schedule_create }));
      expect(res.error.code).toBe('invalid_arguments');
      expect(calls.executed).toEqual([]);
    });

  it.each([['schedule_create', NEW], ['schedule_update', { id: 5 }]])(
    '%s takes a 20000-byte ASCII prompt under the default byte limit', async (name, args) => {
      const t = tools(fakeDb().db);
      const res = await executeTool(capabilityContext('mcp_job'), name, { ...args, prompt: 'a'.repeat(20000) },
        deps({ [name]: t[name] }));
      expect(res.ok).toBe(true);
    });

  it('accepts a valid call', async () => {
    const t = tools(fakeDb().db);
    const res = await executeTool(capabilityContext('mcp_job'), 'schedule_create', NEW,
      deps({ schedule_create: t.schedule_create }));
    expect(res.ok).toBe(true);
  });
});

describe('shared RRULE vectors (S-3)', () => {
  it.each(VECTORS.vectors.map(v => [v.rrule, v]))('%j', (_, v) => {
    expect(parseRrule(v.rrule, VECTORS.min_interval_minutes).rule !== undefined).toBe(v.node_ok);
  });
});
