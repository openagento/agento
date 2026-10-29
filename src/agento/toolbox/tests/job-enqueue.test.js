import { describe, it, expect } from 'vitest';
import { enqueueJob, ThreadRefused } from '../job-enqueue.js';

// conn.execute dispatches by SQL: dedupe SELECT -> [[dupRow]] or [[]]; job INSERT -> insertId
// (or ER_DUP_ENTRY on a lost race); outbox INSERT -> ok. No transaction calls: the caller owns it.
function fakeConn({ dupRow = null, insertDupError = false, insertId = 501, thread = null, head = null, parent = null } = {}) {
  const executed = [];
  return {
    executed,
    async execute(sql, params) {
      executed.push({ sql, params });
      if (/job_event_outbox/i.test(sql)) return [{ affectedRows: 1 }];
      if (/FROM thread/i.test(sql)) return [thread ? [thread] : []];
      if (/FROM job x/i.test(sql)) return [head ? [head] : []];
      if (/SELECT thread_id FROM job/i.test(sql)) return [parent ? [parent] : []];
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
      max_attempts: 3, scheduled_after: null, parent_job_id: 42, depth: 2, thread_id: null, line_id: null,
      dry_run: 0,
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
    await enqueueJob(conn, {
      ...JOB, agentViewId: null, maxAttempts: 5, scheduledAfter: '2099-01-01 08:00:00', dryRun: true,
    });
    const row = columnValues(conn.executed[1]);
    expect(row).toMatchObject({
      parent_job_id: null, depth: 0, max_attempts: 5, scheduled_after: '2099-01-01 08:00:00', agent_view_id: null,
      dry_run: 1,
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

  describe('thread and line', () => {
    // The head x of line 70 (depth 2, view 4, thread 5) was delegated from p, a row of the
    // caller c's line (key 60, depth 1).
    const HEAD = {
      agent_view_id: 4, depth: 2, thread_id: 5, line_id: null,
      parent_depth: 1, parent_thread: 5, parent_line: 60,
      caller_depth: 1, caller_thread: 5, caller_line: 60,
    };
    const CHILD = { ...JOB, parentJobId: 42, depth: 2, threadId: 5, lineId: 70 };

    it('a copied thread of the same view is written, and no thread is created', async () => {
      const conn = fakeConn({ thread: { agent_view_id: 4 } });
      await enqueueJob(conn, { ...JOB, threadId: 5 });
      const insert = conn.executed.find((c) => /INSERT INTO job\b/i.test(c.sql) && !/outbox/i.test(c.sql));
      expect(columnValues(insert)).toMatchObject({ thread_id: 5, line_id: null });
      expect(conn.executed.some((c) => /INSERT INTO thread|UPDATE thread/i.test(c.sql))).toBe(false);
    });

    it.each([
      ['another view', { thread: { agent_view_id: 9 } }, {}],
      ['a missing thread', {}, {}],
      ['a job with no view', { thread: { agent_view_id: null } }, { agentViewId: null }],
      ['a line with no thread', {}, { lineId: 70 }],
      ['a line at depth 0', { thread: { agent_view_id: 4 } }, { lineId: 70 }],
    ])('at depth 0 it refuses %s and inserts nothing', async (_label, opts, job) => {
      const conn = fakeConn(opts);
      const args = { ...JOB, threadId: 5, ...job };
      if (job.lineId && !opts.thread) args.threadId = null;
      await expect(enqueueJob(conn, args)).rejects.toBeInstanceOf(ThreadRefused);
      expect(conn.executed.some((c) => /INSERT/i.test(c.sql))).toBe(false);
    });

    it('a delegated job may join its parent thread of another view', async () => {
      const conn = fakeConn({ thread: { agent_view_id: 9 }, parent: { thread_id: 5 } });
      await enqueueJob(conn, { ...JOB, parentJobId: 42, depth: 1, threadId: 5 });
      expect(conn.executed.find((c) => /SELECT thread_id FROM job/i.test(c.sql)).params).toEqual([42]);
      const insert = conn.executed.find((c) => /INSERT INTO job\b/i.test(c.sql) && !/outbox/i.test(c.sql));
      expect(columnValues(insert)).toMatchObject({ thread_id: 5, depth: 1 });
    });

    it.each([
      ['a thread its parent is not in', { parent: { thread_id: 6 } }, { parentJobId: 42 }],
      ['a parent that does not exist', {}, { parentJobId: 42 }],
      ['no parent', { parent: { thread_id: 5 } }, { parentJobId: null }],
    ])('a new line at depth > 0 refuses %s', async (_label, opts, job) => {
      const conn = fakeConn({ thread: { agent_view_id: 9 }, ...opts });
      await expect(enqueueJob(conn, { ...JOB, depth: 1, threadId: 5, ...job }))
        .rejects.toBeInstanceOf(ThreadRefused);
      expect(conn.executed.some((c) => /INSERT/i.test(c.sql))).toBe(false);
    });

    it('continues a line delegated from the caller line', async () => {
      const conn = fakeConn({ thread: { agent_view_id: 9 }, head: HEAD });
      await enqueueJob(conn, CHILD);
      const head = conn.executed.find((c) => /FROM job x/i.test(c.sql));
      expect(head.params).toEqual([42, 70]);
      const insert = conn.executed.find((c) => /INSERT INTO job\b/i.test(c.sql) && !/outbox/i.test(c.sql));
      expect(columnValues(insert)).toMatchObject({ thread_id: 5, line_id: 70, depth: 2 });
    });

    it('a depth-0 caller continues a line delegated from any depth-0 turn of its thread', async () => {
      const conn = fakeConn({ thread: { agent_view_id: 4 }, head: {
        ...HEAD, depth: 1, parent_depth: 0, parent_line: 11, caller_depth: 0, caller_line: 42 } });
      await expect(enqueueJob(conn, { ...CHILD, depth: 1 })).resolves.toMatchObject({ duplicate: false });
    });

    it.each([
      ['an unknown line', null],
      ['a row that is not a line head', { line_id: 66 }],
      ['another view', { agent_view_id: 8 }],
      ['another depth', { depth: 3 }],
      ['a head in another thread (detached)', { thread_id: 6 }],
      ['a head delegated from another thread', { parent_thread: 6 }],
      ['a sibling branch', { parent_line: 61 }],
      ['a head delegated from another depth', { parent_depth: 0 }],
      ['a caller in another thread', { caller_thread: 6 }],
    ])('refuses %s', async (_label, change) => {
      const conn = fakeConn({ thread: { agent_view_id: 9 }, head: change === null ? null : { ...HEAD, ...change } });
      await expect(enqueueJob(conn, CHILD)).rejects.toBeInstanceOf(ThreadRefused);
      expect(conn.executed.some((c) => /INSERT/i.test(c.sql))).toBe(false);
    });

    it('a line needs the parent job', async () => {
      const conn = fakeConn({ thread: { agent_view_id: 9 }, head: HEAD });
      await expect(enqueueJob(conn, { ...CHILD, parentJobId: null })).rejects.toBeInstanceOf(ThreadRefused);
    });
  });
});
