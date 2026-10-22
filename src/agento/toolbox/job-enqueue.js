// The one Node path that inserts a job (Python's twin: framework/publish_service.py publish_job).
// Caller owns the transaction (begin/commit/rollback); on `duplicate` nothing was written.

export class ThreadRefused extends Error {}

// A job made from another job copies that job's thread (and, at depth > 0, the line it
// continues); the caller reads both from the job row, never from a tool argument (SEC-7). The
// toolbox never creates a thread: it holds SELECT only on `thread`, and a job with no thread
// gets its default one at claim (framework/threads.py place_claim). These checks are the twin
// of threads.join_thread, plus two the trusted Python callers do not need: a new line at
// depth > 0 starts in its parent's thread, and `lineId` must name a line of this thread, view
// and depth whose head was delegated from the caller's own line, so no job continues the
// session of another view or of another branch.
async function checkThread(conn, { threadId, lineId, agentViewId, parentJobId, depth }) {
  if (threadId === null) {
    if (lineId !== null) throw new ThreadRefused('a line_id needs its thread');
    return;
  }
  if (depth === 0 && (agentViewId === null || agentViewId === undefined)) throw new ThreadRefused('a job with no agent_view cannot join a thread');
  const [threads] = await conn.execute('SELECT agent_view_id FROM thread WHERE id = ?', [threadId]);
  const thread = threads[0];
  if (!thread || (depth === 0 && Number(thread.agent_view_id) !== Number(agentViewId))) {
    throw new ThreadRefused(`thread ${threadId} is not a thread of this job's agent_view`);
  }
  if (lineId === null) {
    if (depth === 0) return;
    // A new line at depth > 0 starts in its parent's thread, never in a thread the parent is
    // not in: the depth-0 view check above does not apply to it.
    if (parentJobId === null) throw new ThreadRefused('a delegated job needs its parent');
    const [parents] = await conn.execute('SELECT thread_id FROM job WHERE id = ?', [parentJobId]);
    if (!parents[0] || Number(parents[0].thread_id) !== Number(threadId)) {
      throw new ThreadRefused(`thread ${threadId} is not the thread of job ${parentJobId}`);
    }
    return;
  }
  if (depth === 0 || parentJobId === null) throw new ThreadRefused('only a delegated job continues a line');
  // x = the line's head (its id is the line key), p = the job x was delegated from, c = the
  // caller (the new job's parent). One indexed read by primary keys.
  const [heads] = await conn.execute(
    `SELECT x.agent_view_id, x.depth, x.thread_id, x.line_id,
            p.depth AS parent_depth, p.thread_id AS parent_thread, COALESCE(p.line_id, p.id) AS parent_line,
            c.depth AS caller_depth, c.thread_id AS caller_thread, COALESCE(c.line_id, c.id) AS caller_line
       FROM job x JOIN job p ON p.id = x.parent_job_id JOIN job c ON c.id = ?
      WHERE x.id = ?`,
    [parentJobId, lineId]);
  const h = heads[0];
  // The caller's line: at depth 0 every depth-0 row of the thread, else its line key.
  const fromCallersLine = h && (Number(h.caller_depth) === 0
    ? Number(h.parent_depth) === 0
    : Number(h.parent_depth) === Number(h.caller_depth) && Number(h.parent_line) === Number(h.caller_line));
  if (!fromCallersLine || h.line_id !== null || Number(h.depth) !== depth
      || Number(h.agent_view_id) !== Number(agentViewId)
      || Number(h.thread_id) !== Number(threadId) || Number(h.parent_thread) !== Number(threadId)
      || Number(h.caller_thread) !== Number(threadId)) {
    throw new ThreadRefused(`line ${lineId} is not a line this job may continue`);
  }
}

export async function enqueueJob(conn, {
  type, source, agentViewId, priority, referenceId, context, idempotencyKey,
  maxAttempts = 3, scheduledAfter = null, parentJobId = null, depth = 0,
  threadId = null, lineId = null,
}) {
  // Dedupe with a SELECT before INSERT: an INSERT IGNORE rejected by the unique idempotency_key
  // still burns an auto_increment id, growing the job id counter on every duplicate (AG-22).
  const [dup] = await conn.execute('SELECT id FROM job WHERE idempotency_key = ? LIMIT 1', [idempotencyKey]);
  if (dup.length > 0) return { jobId: null, duplicate: true };
  await checkThread(conn, { threadId, lineId, agentViewId, parentJobId, depth });

  let result;
  try {
    [result] = await conn.execute(
      `INSERT INTO job
         (source, agent_view_id, priority, reference_id, context, idempotency_key, type, max_attempts,
          parent_job_id, depth, thread_id, line_id, status, attempt, scheduled_after)
       VALUES
         (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TODO', 0, COALESCE(?, CURRENT_TIMESTAMP))`,
      // The FK check on thread_id holds a shared lock on the thread row until commit, so a
      // claim in this thread (threads.lock_thread) waits for this job to commit: an earlier
      // turn is never still invisible when a later one is claimed.
      [source, agentViewId, priority, referenceId, context, idempotencyKey, type, maxAttempts,
        parentJobId, depth, threadId, lineId, scheduledAfter]
    );
  } catch (err) {
    // Race: another publisher inserted the same idempotency_key between the SELECT and this INSERT
    // (mirrors publisher.py).
    if (err && (err.code === 'ER_DUP_ENTRY' || err.errno === 1062)) return { jobId: null, duplicate: true };
    throw err;
  }

  // `job.queued`, the same kind and payload shape the Python publisher writes; the relay turns it
  // into the thread's first event (ROADMAP F14).
  await conn.execute(
    `INSERT INTO job_event_outbox (job_id, execution_id, kind, payload)
     VALUES (?, NULL, 'job.queued', ?)`,
    [result.insertId, JSON.stringify({ type, source, agent_view_id: agentViewId ?? null, priority })]
  );
  return { jobId: result.insertId, duplicate: false };
}
