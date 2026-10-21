// The one Node path that inserts a job (Python's twin: framework/publish_service.py publish_job).
// Caller owns the transaction (begin/commit/rollback); on `duplicate` nothing was written.
export async function enqueueJob(conn, {
  type, source, agentViewId, priority, referenceId, context, idempotencyKey,
  maxAttempts = 3, scheduledAfter = null, parentJobId = null, depth = 0,
}) {
  // Dedupe with a SELECT before INSERT: an INSERT IGNORE rejected by the unique idempotency_key
  // still burns an auto_increment id, growing the job id counter on every duplicate (AG-22).
  const [dup] = await conn.execute('SELECT id FROM job WHERE idempotency_key = ? LIMIT 1', [idempotencyKey]);
  if (dup.length > 0) return { jobId: null, duplicate: true };

  let result;
  try {
    [result] = await conn.execute(
      `INSERT INTO job
         (source, agent_view_id, priority, reference_id, context, idempotency_key, type, max_attempts,
          parent_job_id, depth, status, attempt, scheduled_after)
       VALUES
         (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TODO', 0, COALESCE(?, CURRENT_TIMESTAMP))`,
      [source, agentViewId, priority, referenceId, context, idempotencyKey, type, maxAttempts,
        parentJobId, depth, scheduledAfter]
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
