import { randomUUID } from 'node:crypto';
import { z } from 'zod';

// Hard limit (PRD §7), not config: a root job delegates at most three levels down.
const MAX_DEPTH = 3;

const NO_JOB = 'agent_run requires an Agento job context';
const NOT_ALLOWED = 'agent_view not found or not allowed';
const NOT_FOUND = 'job not found or not accessible';

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

function truthy(value) {
  return ['1', 'true', 'yes'].includes(String(value ?? '').trim().toLowerCase());
}

// The scope rule (docs/modules/subagent.md): the caller C may address row X when X was
// delegated from a row of C's line, in C's thread. C's line at depth 0 is every depth-0 row of
// its thread; at depth > 0 the rows keyed COALESCE(line_id, id). So a later turn of the line (a
// wake, a new Jira comment) reaches the children an earlier turn started, and a sibling branch
// is out of scope. X is a child's first row, a continuation or a wake of a child's line: every
// row with a parent is one of these. C comes from the capability (SEC-7), never from an
// argument. Three reads by primary key.
const IN_SCOPE = `
  SELECT x.id, x.status, x.output, x.error_class, x.line_id, v.code AS view_code
    FROM job x
    JOIN job p ON p.id = x.parent_job_id
    JOIN job c ON c.id = ?
    LEFT JOIN agent_view v ON v.id = x.agent_view_id
   WHERE x.id = ? AND x.thread_id = c.thread_id AND p.thread_id = c.thread_id
     AND ((c.depth = 0 AND p.depth = 0)
          OR (c.depth > 0 AND COALESCE(p.line_id, p.id) = COALESCE(c.line_id, c.id)))`;

// The caller view's config decides how a parent learns that its children finished.
function describeRun(config) {
  return [
    'Delegate a task to an Agento agent_view as a child job. Returns at once with the child job_id.',
    'The child gets only the prompt you write, not your context.',
    '`./shared` is shared with your parent and your children.',
    truthy(config.wake_parent)
      ? 'End your turn after delegating. Do not poll: you are resumed when all children finish.'
      : 'Poll agent_result, or use schedule_followup (root jobs only).',
    'Pass job_id of an earlier child to continue that child\'s session with a new prompt.',
    `Nesting stops at ${MAX_DEPTH} levels.`,
  ].join('\n');
}

export function register(server, { log, db, isToolEnabled, jobId, agentViewId, moduleConfigs, enqueueJob, errorCategory }) {
  const config = moduleConfigs?.subagent ?? {};

  if (isToolEnabled('agent_run')) {
    server.tool(
      'agent_run',
      describeRun(config),
      {
        agent_view: z.string().regex(/^[A-Za-z0-9_-]{1,50}$/).describe('Code of the agent_view that runs the task'),
        prompt: z.string().min(1).max(16000).describe('The full task for the child; it sees nothing else'),
        job_id: z.number().int().positive().optional()
          .describe('An earlier child whose session continues with this prompt; agent_view must be its agent_view'),
      },
      async ({ agent_view, prompt, job_id }) => {
        if (!jobId) return refuse(NO_JOB);
        let conn;
        let lock = null;
        try {
          conn = await db.getCronPool().getConnection();
          // A named lock serializes the agent_run calls of one parent, so the child cap holds.
          // Not `SELECT … FOR UPDATE`: a locking read needs UPDATE on `job`, which the toolbox
          // user does not hold (db_grants.py). Taken before the transaction, so its snapshot
          // sees every child a previous holder committed. At depth > 0 the cap counts the
          // whole line, and two rows of one line never run at once (threads.line_blocked).
          const [[{ got }]] = await conn.execute('SELECT GET_LOCK(?, 10) AS got', [`subagent:${jobId}`]);
          if (Number(got) !== 1) throw new Error('subagent lock timeout');
          lock = `subagent:${jobId}`;
          await conn.beginTransaction();
          const fail = async (text) => { await conn.rollback(); return refuse(text); };

          const [parents] = await conn.execute(
            'SELECT id, agent_view_id, priority, depth, thread_id, line_id, dry_run FROM job WHERE id = ?', [jobId]);
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

          // A continuation is a new row of the earlier child's line: the claim resumes the
          // line's latest session (framework/threads.py resolve_resume).
          let lineId = null;
          if (job_id !== undefined) {
            const [found] = await conn.execute(IN_SCOPE, [parent.id, job_id]);
            const x = found[0];
            if (!x || x.view_code !== target.code) return await fail(NOT_FOUND);
            lineId = Number(x.line_id ?? x.id);
            const [open] = await conn.execute(
              `SELECT 1 FROM job WHERE (id = ? OR line_id = ?) AND thread_id = ?
                  AND status IN ('TODO', 'RUNNING', 'PAUSED') LIMIT 1`,
              [lineId, lineId, parent.thread_id]);
            if (open.length > 0) return await fail('job is still running');
          }

          const limit = parseLimit(config.max_children_per_job);
          if (limit === null) {
            log('agent_run', 'WARN', `av=${agentViewId} job=${jobId} subagent/max_children_per_job is not a valid integer >= 1`);
            return await fail('agent_run is not configured');
          }
          // The cap: at depth 0 per row (a long Jira history must not use it up), at depth > 0
          // per line (it bounds the wakes under one parent). Wake rows are `followup` and do
          // not count. Reads at most `limit` index entries; `limit` is a checked integer.
          const callerLine = Number(parent.line_id ?? parent.id);
          const [taken] = Number(parent.depth) === 0
            ? await conn.execute(
              `SELECT 1 FROM job WHERE parent_job_id = ? AND type = 'subagent' LIMIT ${limit}`, [parent.id])
            : await conn.execute(
              `SELECT 1 FROM job p JOIN job x ON x.parent_job_id = p.id
                WHERE (p.id = ? OR p.line_id = ?) AND p.thread_id = ? AND x.type = 'subagent' LIMIT ${limit}`,
              [callerLine, callerLine, parent.thread_id]);
          if (taken.length >= limit) return await fail(`max ${limit} sub-agents for this agent`);

          const depth = parent.depth + 1;
          const { jobId: childId } = await enqueueJob(conn, {
            type: 'subagent', source: 'subagent', agentViewId: target.id, priority: parent.priority,
            referenceId: String(parent.id), context: prompt,
            idempotencyKey: `subagent:${parent.id}:${randomUUID()}`, parentJobId: parent.id, depth,
            threadId: parent.thread_id, lineId, dryRun: Number(parent.dry_run) === 1,
          });
          await conn.commit();
          log('agent_run', 'OK', `job=${jobId} child=${childId} av=${target.code} depth=${depth} line=${lineId ?? childId}`);
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
      'Read the status of a job delegated from your line (agent_run, or a later turn of that child), and its output once it is SUCCESS.',
      { job_id: z.number().int().positive().describe('The job_id that agent_run returned') },
      async ({ job_id }) => {
        if (!jobId) return refuse('agent_result requires an Agento job context');
        try {
          const [rows] = await db.getCronPool().execute(IN_SCOPE, [jobId, job_id]);
          const child = rows[0];
          if (!child) return refuse(NOT_FOUND);
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
