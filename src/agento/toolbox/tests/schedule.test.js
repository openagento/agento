import { describe, it, expect, vi } from 'vitest';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCHEDULE_PATH = path.resolve(__dirname, '../../modules/core/toolbox/schedule.js');

// Captures name + desc + schema + handler so we can assert on the registered tool surface.
function fakeServer() {
  const tools = {};
  const names = [];
  return {
    tools,
    names,
    tool(name, desc, schema, handler) {
      names.push(name);
      tools[name] = { desc, schema, handler };
    },
  };
}

// db.getCronPool() -> pool.getConnection() -> conn { execute, release }.
// execute dispatches by SQL:
//   parent-job SELECT (WHERE id = ?) -> [ [jobRow] ] (or [[]] when missing)
//   dedupe SELECT (WHERE idempotency_key = ?) -> [ [dupRow] ] when simulating a
//     duplicate, else [[]]
//   INSERT -> [ {affectedRows} ]
function fakeDb(jobRow, { affectedRows = 1, failExecute = false, dupRow = null } = {}) {
  const calls = { getConnection: 0, executed: [], released: 0 };
  const conn = {
    async execute(sql, params) {
      calls.executed.push({ sql, params });
      if (failExecute) throw new Error('db boom');
      if (/^\s*SELECT/i.test(sql) && /idempotency_key/i.test(sql)) return [dupRow ? [dupRow] : []];
      if (/^\s*SELECT/i.test(sql)) return [jobRow ? [jobRow] : []];
      return [{ affectedRows }];
    },
    release() { calls.released += 1; },
  };
  const pool = { async getConnection() { calls.getConnection += 1; return conn; } };
  return { db: { getCronPool: () => pool }, calls };
}

const FUTURE = '2099-01-01T08:00:00';

function baseCtx(overrides = {}) {
  return { log: vi.fn(), db: {}, isToolEnabled: () => true, jobId: 42, ...overrides };
}

async function loadRegister() {
  const { register } = await import(SCHEDULE_PATH);
  return register;
}

describe('schedule_followup opt-in gating', () => {
  it('does NOT register when is_enabled is missing/disabled (opt-in)', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    register(server, baseCtx({ isToolEnabled: () => false }));
    expect(server.names).not.toContain('schedule_followup');
  });

  it('registers when explicitly enabled', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    register(server, baseCtx({ isToolEnabled: (name) => name === 'schedule_followup' }));
    expect(server.names).toContain('schedule_followup');
  });
});

describe('schedule_followup tool surface', () => {
  it('description names no channels and no "teams"', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    register(server, baseCtx());
    const desc = server.tools.schedule_followup.desc;
    expect(desc).not.toMatch(/teams/i);
    expect(desc).not.toMatch(/\bjira\b/i);
    expect(desc).not.toMatch(/\boutlook\b/i);
    expect(desc).not.toMatch(/communication channel/i);
  });

  it('schema is slimmed to { user, scheduled_at, instructions } — no source/reference_id', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    register(server, baseCtx());
    const keys = Object.keys(server.tools.schedule_followup.schema).sort();
    expect(keys).toEqual(['instructions', 'scheduled_at', 'user']);
    expect(keys).not.toContain('source');
    expect(keys).not.toContain('reference_id');
  });
});

describe('schedule_followup inherits identity from the current job', () => {
  it('copies source, reference_id, agent_view_id, priority into the INSERT', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb({ source: 'jira', reference_id: 'AI-9', agent_view_id: 7, priority: 20 });
    register(server, baseCtx({ db, jobId: 42 }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Sprawdz status zadania.' }
    );

    expect(res.isError).toBeFalsy();
    const select = calls.executed[0];
    expect(select.sql).toMatch(/^\s*SELECT/i);
    expect(select.params).toEqual([42]);
    const dedup = calls.executed[1];
    expect(dedup.sql).toMatch(/SELECT id FROM job WHERE idempotency_key/i);
    const insert = calls.executed[2];
    expect(insert.sql).toMatch(/INSERT INTO job/i);
    expect(insert.sql).not.toMatch(/INSERT IGNORE/i);
    // params order: [source, agent_view_id, priority, reference_id, context, idempotencyKey, scheduled_after]
    expect(insert.params[0]).toBe('jira');
    expect(insert.params[1]).toBe(7);
    expect(insert.params[2]).toBe(20);
    expect(insert.params[3]).toBe('AI-9');
    expect(insert.params.some((p) => typeof p === 'string' && p.startsWith('followup:jira:AI-9:'))).toBe(true);
    expect(calls.released).toBe(1);
  });

  it('carries an Outlook compound reference_id verbatim (no parsing in the tool)', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const ref = 're-subject::AAAMkAGI2==';
    const { db, calls } = fakeDb({ source: 'outlook', reference_id: ref, agent_view_id: null, priority: 50 });
    register(server, baseCtx({ db }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Sprawdz odpowiedz klienta.' }
    );

    expect(res.isError).toBeFalsy();
    const insert = calls.executed[2];
    expect(insert.params[3]).toBe(ref);
  });

  it('passes a NULL agent_view_id through (global scope is valid)', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb({ source: 'jira', reference_id: 'AI-1', agent_view_id: null, priority: 50 });
    register(server, baseCtx({ db }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Kontynuuj zadanie.' }
    );

    expect(res.isError).toBeFalsy();
    const insert = calls.executed[2];
    expect(insert.params[1]).toBeNull();
  });

  it('idempotency key already exists → returns duplicate message without INSERT', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb(
      { source: 'jira', reference_id: 'AI-9', agent_view_id: 7, priority: 20 },
      { dupRow: { id: 123 } }
    );
    register(server, baseCtx({ db, jobId: 42 }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Sprawdz status zadania.' }
    );

    expect(res.isError).toBeFalsy();
    expect(res.content[0].text).toMatch(/duplicate prevented/i);
    // parent-job SELECT + dedupe SELECT, but NO INSERT
    expect(calls.executed).toHaveLength(2);
    expect(calls.executed[0].sql).toMatch(/^\s*SELECT/i);
    expect(calls.executed[1].sql).toMatch(/idempotency_key/i);
    expect(calls.executed.some((c) => /INSERT/i.test(c.sql))).toBe(false);
    expect(calls.released).toBe(1);
  });
});

describe('schedule_followup rejects when it has no job to continue', () => {
  it('no job_id (interactive run) → isError, no DB access, no job created', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb({ source: 'jira', reference_id: 'AI-9', agent_view_id: 1, priority: 50 });
    register(server, baseCtx({ db, jobId: null }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Nie powinno sie wykonac.' }
    );

    expect(res.isError).toBe(true);
    expect(calls.getConnection).toBe(0);
    expect(calls.executed).toHaveLength(0);
  });

  it('missing job row → isError, no INSERT', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb(null); // SELECT returns []
    register(server, baseCtx({ db, jobId: 99 }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Brak zadania w bazie.' }
    );

    expect(res.isError).toBe(true);
    expect(calls.executed).toHaveLength(1); // only the SELECT
    expect(calls.executed[0].sql).toMatch(/^\s*SELECT/i);
    expect(calls.released).toBe(1);
  });

  it('job row found but with null source/reference_id → isError, no INSERT', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const { db, calls } = fakeDb({ source: null, reference_id: null, agent_view_id: 1, priority: 50 });
    register(server, baseCtx({ db, jobId: 42 }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Niekompletny wiersz zadania.' }
    );

    expect(res.isError).toBe(true);
    expect(calls.executed).toHaveLength(1); // only the SELECT, no INSERT
    expect(calls.released).toBe(1);
  });

  it('DB error is caught gracefully (no ReferenceError) and logs job=<id>', async () => {
    const register = await loadRegister();
    const server = fakeServer();
    const log = vi.fn();
    const { db } = fakeDb({ source: 'jira', reference_id: 'AI-9', agent_view_id: 1, priority: 50 }, { failExecute: true });
    register(server, baseCtx({ db, jobId: 42, log }));

    const res = await server.tools.schedule_followup.handler(
      { user: 'agent@example.com', scheduled_at: FUTURE, instructions: 'Wywola blad bazy.' }
    );

    expect(res.isError).toBe(true);
    const errLog = log.mock.calls.find((c) => c[1] === 'ERROR' && /job=42/.test(c[2]));
    expect(errLog).toBeTruthy();
    expect(log.mock.calls.some((c) => /reference_id/.test(String(c[2])))).toBe(false);
  });
});
