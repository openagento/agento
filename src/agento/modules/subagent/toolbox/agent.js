import { randomUUID } from 'node:crypto';
import { z } from 'zod';

// Hard limit (PRD §7), not config: a root job delegates at most three levels down.
const MAX_DEPTH = 3;

const NO_JOB = 'agent_run requires an Agento job context';
const NOT_ALLOWED = 'agent_view not found or not allowed';

function refuse(text) {
  return { content: [{ type: 'text', text }], isError: true };
}

function reply(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }] };
}

// ENV/DB values arrive as strings (the JS resolver does no type check). A limit that does not
// parse to an integer >= 1 means "not configured", never "no limit" (SEC-9).
function parseLimit(value) {
  const n = value === '' || value === null || value === undefined ? NaN : Number(value);
  return Number.isSafeInteger(n) && n >= 1 ? n : null;
}

export function register(server, { log, db, isToolEnabled, jobId, agentViewId, moduleConfigs, enqueueJob, errorCategory }) {
  const config = moduleConfigs?.subagent ?? {};

  if (isToolEnabled('agent_run')) {
    server.tool(
      'agent_run',
      [
        'Delegate a task to an Agento agent_view as a child job. Returns at once with the child job_id;',
        'read the result later with agent_result. The child gets only the prompt you write, not your context.',
        `Nesting stops at ${MAX_DEPTH} levels.`,
      ].join('\n'),
      {
        agent_view: z.string().regex(/^[A-Za-z0-9_-]{1,50}$/).describe('Code of the agent_view that runs the task'),
        prompt: z.string().min(1).max(16000).describe('The full task for the child; it sees nothing else'),
      },
      async ({ agent_view, prompt }) => {
        if (!jobId) return refuse(NO_JOB);
        let conn;
        let lock = null;
        try {
          conn = await db.getCronPool().getConnection();
          // A named lock serializes the agent_run calls of one parent, so the child cap holds.
          // Not `SELECT … FOR UPDATE`: a locking read needs UPDATE on `job`, which the toolbox
          // user does not hold (db_grants.py). Taken before the transaction, so its snapshot
          // sees every child a previous holder committed.
          const [[{ got }]] = await conn.execute('SELECT GET_LOCK(?, 10) AS got', [`subagent:${jobId}`]);
          if (Number(got) !== 1) throw new Error('subagent lock timeout');
          lock = `subagent:${jobId}`;
          await conn.beginTransaction();
          const fail = async (text) => { await conn.rollback(); return refuse(text); };

          const [parents] = await conn.execute(
            'SELECT id, agent_view_id, priority, depth FROM job WHERE id = ?', [jobId]);
          const parent = parents[0];
          if (!parent || Number(parent.agent_view_id) !== Number(agentViewId)) return await fail(NO_JOB);
          if (parent.depth >= MAX_DEPTH) return await fail(`sorry, max ${MAX_DEPTH} nested subagents levels`);

          const [targets] = await conn.execute(
            'SELECT id, code FROM agent_view WHERE code = ? AND is_active = 1', [agent_view]);
          const target = targets[0];
          const allowed = String(config.allowed_agent_views ?? '').split(',').map(s => s.trim()).filter(Boolean);
          if (!target || (Number(target.id) !== Number(parent.agent_view_id) && !allowed.includes(target.code))) {
            return await fail(NOT_ALLOWED);
          }

          const limit = parseLimit(config.max_children_per_job);
          if (limit === null) {
            log('agent_run', 'WARN', `av=${agentViewId} job=${jobId} subagent/max_children_per_job is not a valid integer >= 1`);
            return await fail('agent_run is not configured');
          }
          const [[{ n }]] = await conn.execute('SELECT COUNT(*) AS n FROM job WHERE parent_job_id = ?', [parent.id]);
          if (Number(n) >= limit) return await fail(`max ${limit} child jobs per job reached`);

          const depth = parent.depth + 1;
          const { jobId: childId } = await enqueueJob(conn, {
            type: 'subagent', source: 'subagent', agentViewId: target.id, priority: parent.priority,
            referenceId: String(parent.id), context: prompt,
            idempotencyKey: `subagent:${parent.id}:${randomUUID()}`, parentJobId: parent.id, depth,
          });
          await conn.commit();
          log('agent_run', 'OK', `job=${jobId} child=${childId} av=${target.code} depth=${depth}`);
          return reply({ job_id: childId, status: 'TODO', depth });
        } catch (err) {
          await conn?.rollback().catch(() => {});
          log('agent_run', 'ERROR', `job=${jobId} ${errorCategory(err)}`);
          return refuse('agent_run failed');
        } finally {
          if (lock) await conn.execute('SELECT RELEASE_LOCK(?)', [lock]).catch(() => {});
          conn?.release();
        }
      }
    );
  }

  if (isToolEnabled('agent_result')) {
    server.tool(
      'agent_result',
      'Read the status of a child job you created with agent_run, and its output once it is SUCCESS.',
      { job_id: z.number().int().positive().describe('The job_id that agent_run returned') },
      async ({ job_id }) => {
        if (!jobId) return refuse('agent_result requires an Agento job context');
        try {
          // Only the direct parent reads a child: the caller's job binds from the capability row.
          const [rows] = await db.getCronPool().execute(
            'SELECT id, status, output, error_class FROM job WHERE id = ? AND parent_job_id = ?', [job_id, jobId]);
          const child = rows[0];
          if (!child) return refuse('job not found or not accessible');
          const result = { job_id: Number(child.id), status: child.status };
          if (child.status === 'SUCCESS') result.output = child.output;
          // error_class only: error_message can quote CLI output and stays admin-only (SEC-6).
          if (child.status === 'FAILED' || child.status === 'DEAD') result.error = child.error_class;
          return reply(result);
        } catch (err) {
          log('agent_result', 'ERROR', `job=${jobId} ${errorCategory(err)}`);
          return refuse('agent_result failed');
        }
      }
    );
  }
}
