import { describe, it, expect } from 'vitest';
import { enqueueJob } from '../job-enqueue.js';

// conn.execute dispatches by SQL: dedupe SELECT -> [[dupRow]] or [[]]; job INSERT -> insertId
// (or ER_DUP_ENTRY on a lost race); outbox INSERT -> ok. No transaction calls: the caller owns it.
function fakeConn({ dupRow = null, insertDupError = false, insertId = 501 } = {}) {
  const executed = [];
  return {
    executed,
    async execute(sql, params) {
      executed.push({ sql, params });
      if (/job_event_outbox/i.test(sql)) return [{ affectedRows: 1 }];
      if (/^\s*SELECT/i.test(sql)) return [dupRow ? [dupRow] : []];
      if (insertDupError) {
        const err = new Error("Duplicate entry for key 'uq_jobs_idempotency'");
        err.code = 'ER_DUP_ENTRY';
        err.errno = 1062;
        throw err;
      }
      return [{ affectedRows: 1, insertId }];
    },
  };
}

const JOB = {
  type: 'subagent', source: 'subagent', agentViewId: 4, priority: 30, referenceId: '42',
  context: 'do the thing', idempotencyKey: 'subagent:42:abc',
};

function columnValues(call) {
  const cols = call.sql.match(/INSERT INTO job\s*\(([^)]*)\)/i)[1].split(',').map((c) => c.trim());
  const values = call.sql.match(/VALUES\s*\(([^)]*)\)/i)[1].split(',').map((v) => v.trim());
  let p = 0;
  return Object.fromEntries(cols.map((c, i) => [c, values[i].includes('?') ? call.params[p++] : values[i]]));
}

describe('enqueueJob', () => {
  it('inserts the job with parent_job_id/depth and announces it', async () => {
    const conn = fakeConn();
    const res = await enqueueJob(conn, { ...JOB, parentJobId: 42, depth: 2 });

    expect(res).toEqual({ jobId: 501, duplicate: false });
    expect(conn.executed[0].sql).toMatch(/SELECT id FROM job WHERE idempotency_key = \?/i);
    expect(conn.executed[0].params).toEqual(['subagent:42:abc']);
    const row = columnValues(conn.executed[1]);
    expect(row).toMatchObject({
      type: 'subagent', source: 'subagent', agent_view_id: 4, priority: 30, reference_id: '42',
      context: 'do the thing', idempotency_key: 'subagent:42:abc', status: "'TODO'", attempt: '0',
      max_attempts: 3, scheduled_after: null, parent_job_id: 42, depth: 2,
    });
    expect(conn.executed[1].sql).toMatch(/COALESCE\(\?, CURRENT_TIMESTAMP\)/);
    expect(conn.executed[2].sql).toMatch(/job_event_outbox/i);
    expect(conn.executed[2].params[0]).toBe(501);
    expect(JSON.parse(conn.executed[2].params[1])).toEqual({
      type: 'subagent', source: 'subagent', agent_view_id: 4, priority: 30,
    });
  });

  it('defaults to a root job (parent NULL, depth 0) and passes scheduledAfter/maxAttempts', async () => {
    const conn = fakeConn();
    await enqueueJob(conn, { ...JOB, agentViewId: null, maxAttempts: 5, scheduledAfter: '2099-01-01 08:00:00' });
    const row = columnValues(conn.executed[1]);
    expect(row).toMatchObject({
      parent_job_id: null, depth: 0, max_attempts: 5, scheduled_after: '2099-01-01 08:00:00', agent_view_id: null,
    });
    expect(JSON.parse(conn.executed[2].params[1]).agent_view_id).toBeNull();
  });

  it.each([
    ['dedupe hit', { dupRow: { id: 9 } }, 1],
    ['lost race on the unique key', { insertDupError: true }, 2],
  ])('%s -> duplicate, no outbox row', async (_label, opts, statements) => {
    const conn = fakeConn(opts);
    const res = await enqueueJob(conn, JOB);
    expect(res).toEqual({ jobId: null, duplicate: true });
    expect(conn.executed).toHaveLength(statements);
    expect(conn.executed.some((c) => /job_event_outbox/i.test(c.sql))).toBe(false);
  });

  it('rethrows any other insert error', async () => {
    const conn = { async execute(sql) { if (/^\s*INSERT/i.test(sql)) throw new Error('boom'); return [[]]; } };
    await expect(enqueueJob(conn, JOB)).rejects.toThrow('boom');
  });
});
