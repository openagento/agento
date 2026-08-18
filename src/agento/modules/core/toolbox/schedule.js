import { z } from 'zod';

export async function healthcheck({ db }) {
  const start = Date.now();
  try {
    const pool = db.getCronPool();
    await pool.query('SELECT 1');
    return [{ tool: 'schedule_followup', status: 'ok', ms: Date.now() - start }];
  } catch (err) {
    return [{ tool: 'schedule_followup', status: 'fail', ms: Date.now() - start, error: err.message }];
  }
}

export function register(server, { log, db, isToolEnabled, jobId }) {
  if (isToolEnabled && !isToolEnabled('schedule_followup')) return;
  server.tool(
    'schedule_followup',
    [
      'Schedule a follow-up task for yourself. The job will be executed at the specified time.',
      'Use this when you need to check something later, wait for a process to complete, or defer work.',
      'The instructions field should describe exactly what to do when the time comes.',
      'The follow-up automatically continues the SAME task you are working on now — it inherits the',
      'current task\'s channel and reference, so you only choose WHEN it runs and WHAT it should do.',
      'Examples:',
      '  scheduled_at: "2026-02-24T16:30:00", instructions: "Sprawdź czy reindeks się zakończył i odpisz."',
      '  scheduled_at: "2026-02-25T08:00:00", instructions: "Sprawdź czy klient odpowiedział na pytanie."',
    ].join('\n'),
    {
      user:          z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      scheduled_at:  z.string().describe('When to execute, ISO 8601 datetime in local timezone (e.g. "2026-02-24T16:30:00")'),
      instructions:  z.string().min(10).max(2000).describe('Detailed instructions for what to do when the follow-up fires'),
    },
    async ({ user, scheduled_at, instructions }) => {
      // NOTE: source/reference_id are NOT agent inputs — they are inherited from the current job
      // (looked up by the MCP session's job_id). A stray `source`/`reference_id` from an older agent
      // is simply ignored (zod strips unknown keys). See plan schedule-followup-inherit-source.
      try {
        const scheduledDate = new Date(scheduled_at);
        if (isNaN(scheduledDate.getTime())) {
          log('schedule_followup', 'ERROR', `user=${user} invalid date: ${scheduled_at}`);
          return {
            content: [{ type: 'text', text: `Error: Invalid datetime format "${scheduled_at}". Use ISO 8601 (e.g. "2026-02-24T16:30:00").` }],
            isError: true,
          };
        }

        if (scheduledDate.getTime() <= Date.now()) {
          log('schedule_followup', 'ERROR', `user=${user} scheduled_at is in the past: ${scheduled_at}`);
          return {
            content: [{ type: 'text', text: `Error: scheduled_at must be in the future. Got: ${scheduled_at}` }],
            isError: true,
          };
        }

        // The follow-up continues the CURRENT job — identified by the MCP session's job_id.
        // No active job (e.g. interactive `agento run`) => there is no task to continue.
        if (!jobId) {
          log('schedule_followup', 'ERROR', `user=${user} no active job context (interactive run?)`);
          return {
            content: [{ type: 'text', text:
              'Error: schedule_followup needs an active task context (a running job). ' +
              'It is not available in interactive `agento run`.' }],
            isError: true,
          };
        }

        // Format for MySQL TIMESTAMP
        const mysqlDatetime = scheduledDate.toISOString().slice(0, 19).replace('T', ' ');

        const pool = db.getCronPool();
        const conn = await pool.getConnection();
        try {
          // Inherit the channel + reference + execution scope (agent_view_id, priority) from the
          // current job, so the follow-up runs in the same context as its parent.
          const [rows] = await conn.execute(
            'SELECT source, reference_id, agent_view_id, priority FROM job WHERE id = ?',
            [jobId]
          );
          const job = rows[0];
          if (!job || !job.source || !job.reference_id) {
            log('schedule_followup', 'ERROR', `user=${user} job=${jobId} could not resolve current job`);
            return {
              content: [{ type: 'text', text: `Error: could not resolve the current job (id=${jobId}); follow-up not scheduled.` }],
              isError: true,
            };
          }
          const { source, reference_id, agent_view_id, priority } = job;

          // Idempotency key: minute-granular per source+reference
          const isoMinute = scheduledDate.toISOString().slice(0, 16).replace(':', '');
          const idempotencyKey = `followup:${source}:${reference_id}:${isoMinute}`;

          // Dedupe with a SELECT before INSERT: an INSERT IGNORE rejected by the
          // unique idempotency_key still burns an auto_increment id, growing the job
          // id counter on every duplicate (AG-22). Check first to keep it flat.
          const [dup] = await conn.execute(
            'SELECT id FROM job WHERE idempotency_key = ? LIMIT 1',
            [idempotencyKey]
          );
          if (dup.length > 0) {
            log('schedule_followup', 'DUP', `user=${user} source=${source} ref=${reference_id} key=${idempotencyKey}`);
            return {
              content: [{ type: 'text', text:
                `Follow-up already scheduled for ${reference_id} at that time (duplicate prevented).` }],
            };
          }

          const [result] = await conn.execute(
            `INSERT INTO job
               (type, source, agent_view_id, priority, reference_id, context, idempotency_key, status, attempt, max_attempts, scheduled_after)
             VALUES
               ('followup', ?, ?, ?, ?, ?, ?, 'TODO', 0, 3, ?)`,
            [source, agent_view_id, priority, reference_id, instructions, idempotencyKey, mysqlDatetime]
          );

          if (result.affectedRows > 0) {
            log('schedule_followup', 'OK', `user=${user} source=${source} ref=${reference_id} av=${agent_view_id} at=${mysqlDatetime} key=${idempotencyKey}`);
            return {
              content: [{ type: 'text', text:
                `Follow-up scheduled for ${reference_id} (${source}) at ${mysqlDatetime}.\n` +
                `Idempotency key: ${idempotencyKey}` }],
            };
          } else {
            log('schedule_followup', 'DUP', `user=${user} source=${source} ref=${reference_id} key=${idempotencyKey}`);
            return {
              content: [{ type: 'text', text:
                `Follow-up already scheduled for ${reference_id} at that time (duplicate prevented).` }],
            };
          }
        } finally {
          conn.release();
        }
      } catch (err) {
        log('schedule_followup', 'ERROR', `user=${user} job=${jobId} ${err.message}`);
        return {
          content: [{ type: 'text', text: `Schedule error: ${err.message}` }],
          isError: true,
        };
      }
    }
  );
}
