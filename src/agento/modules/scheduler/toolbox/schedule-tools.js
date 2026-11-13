import { Buffer } from 'node:buffer';
import { z } from 'zod';

// The schedule_* tools (AG-67 plan §10, docs/modules/scheduler.md). The caller's view and job
// come from the capability (SEC-7), never from an argument. A tool reaches only rows of its own
// view that its view or its job's account user owns (OP-1), and writes only `scheduler` rows.
// The RRULE check here is syntax only (S-3): the tick (Python engine) computes the first fire,
// and a rule it refuses is disabled with `last_error`.

const SOURCE = 'scheduler';
const MAX_TITLE = 500;
const MAX_RRULE = 1024;
const MAX_LIST = 200;
// Twin of framework/schedule/engine.py _parse (keep the two in step: rrule_vectors.json).
const MAX_COUNT = 10000;
const FREQS = new Set(['YEARLY', 'MONTHLY', 'WEEKLY', 'DAILY', 'HOURLY', 'MINUTELY', 'SECONDLY']);
const INT_LISTS = {
  BYMONTH: [1, 12, false], BYMONTHDAY: [1, 31, true], BYYEARDAY: [1, 366, true],
  BYWEEKNO: [1, 53, true], BYSETPOS: [1, 366, true], BYHOUR: [0, 23, false], BYMINUTE: [0, 59, false],
};
const PARTS = new Set(['FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY', 'WKST', ...Object.keys(INT_LISTS)]);
const START_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;
const LIMIT_LOCK = { user: 'scheduler:limit:user:', view: 'scheduler:limit:view:' }; // + id + ':' + DATABASE()

/**
 * The engine's syntax refusals, as far as Node can tell: `{ rule }` (the normalized text) or
 * `{ code }` (`bad_rule` | `too_frequent`). It never refuses a rule the engine accepts.
 */
export function parseRrule(text, minIntervalMinutes) {
  const lines = String(text ?? '').trim().split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  if (lines.length !== 1) return { code: 'bad_rule' };
  let rule = lines[0].toUpperCase();
  if (rule.startsWith('RRULE:')) rule = rule.slice(6);
  if (rule.includes(':')) return { code: 'bad_rule' };
  const parts = new Map();
  for (const item of rule.split(';')) {
    const at = item.indexOf('=');
    const key = at < 0 ? item : item.slice(0, at);
    const value = at < 0 ? '' : item.slice(at + 1);
    if (at < 0 || !value || !PARTS.has(key) || parts.has(key)) return { code: 'bad_rule' };
    parts.set(key, value);
  }
  if (!FREQS.has(parts.get('FREQ'))) return { code: 'bad_rule' };
  for (const key of ['INTERVAL', 'COUNT']) {
    if (parts.has(key) && !(/^\d+$/.test(parts.get(key)) && Number(parts.get(key)) >= 1)) return { code: 'bad_rule' };
  }
  for (const [key, [lo, hi, negative]] of Object.entries(INT_LISTS)) {
    for (const v of parts.has(key) ? parts.get(key).split(',') : []) {
      if (!/^-?\d+$/.test(v)) return { code: 'bad_rule' };
      const n = Number(v);
      if (!(lo <= (negative ? Math.abs(n) : n) && (negative ? Math.abs(n) : n) <= hi) || (n < 0 && !negative)) {
        return { code: 'bad_rule' };
      }
    }
  }
  if (parts.get('FREQ') === 'SECONDLY') return { code: 'too_frequent' };
  if (Number(parts.get('COUNT') ?? 1) > MAX_COUNT) return { code: 'bad_rule' };
  // MINUTELY fires every INTERVAL minutes, unless BYMINUTE picks the minutes, or COUNT/UNTIL
  // may leave one fire only (the engine then sees no gap): Node cannot tell those, so it leaves them.
  if (parts.get('FREQ') === 'MINUTELY' && !parts.has('BYMINUTE') && !parts.has('UNTIL')
      && Number(parts.get('COUNT') ?? 2) >= 2 && Number(parts.get('INTERVAL') ?? 1) < minIntervalMinutes) {
    return { code: 'too_frequent' };
  }
  return { rule: [...parts].map(([k, v]) => `${k}=${v}`).join(';') };
}

function refuse(text) {
  return { content: [{ type: 'text', text }], isError: true };
}

function reply(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }] };
}

// ENV/DB values arrive as strings. Not an integer >= 1 means "not configured", never "no limit".
function positiveInt(value) {
  const n = value === '' || value === null || value === undefined ? NaN : Number(value);
  return Number.isSafeInteger(n) && n >= 1 ? n : null;
}

// Only canonical IANA names (and UTC): an alias Intl knows may be one Python's zoneinfo does not.
function zoneOk(tz) {
  return typeof tz === 'string' && (tz === 'UTC' || Intl.supportedValuesOf('timeZone').includes(tz));
}

function parseStart(value) {
  const m = START_RE.exec(value);
  if (!m) return null;
  const [, y, mo, d, h, mi, s = '00'] = m;
  const date = new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s));
  const same = date.getUTCFullYear() === +y && date.getUTCMonth() === +mo - 1 && date.getUTCDate() === +d
    && date.getUTCHours() === +h && date.getUTCMinutes() === +mi && date.getUTCSeconds() === +s;
  return same ? `${y}-${mo}-${d} ${h}:${mi}:${s}` : null;
}

// The local wall time now in `tz`, to the minute, as `starts_at` stores it.
function localNow(tz) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date()).map(p => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:00`;
}

const TIME = (col) => `DATE_FORMAT(${col}, '%Y-%m-%dT%H:%i:%s')`;
const LIST_SQL = `SELECT id, source, title, prompt, rrule, ${TIME('starts_at')} AS starts_at, timezone, enabled,
    ${TIME('next_fire_at')} AS next_fire_at, ${TIME('last_fire_at')} AS last_fire_at, last_error,
    owner_user_id, owner_agent_view_id
  FROM schedule`;
// OP-1, in every read and write: the caller's view, owned by the view or by the job's user.
// `=`, never `<=>`: a job with no account user passes NULL, which must match no row.
const REACH = 'agent_view_id = ? AND deleted_at IS NULL AND (owner_agent_view_id = ? OR owner_user_id = ?)';

const timing = {
  rrule: z.string().min(1).max(MAX_RRULE)
    .describe('An RFC 5545 RRULE line, e.g. "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"'),
  starts_at: z.string().regex(START_RE)
    .describe('First local start, "YYYY-MM-DDTHH:MM" in `timezone`; default: now'),
  timezone: z.string().min(1).max(64).describe('IANA timezone, e.g. "Europe/Warsaw"; default: the deployment one'),
};
const fields = {
  title: z.string().trim().min(1).max(MAX_TITLE).describe('A short name of the task'),
  prompt: z.string().describe('The full task each run gets; it sees nothing else. At most conversation/limits/max_message_bytes UTF-8 bytes'),
  enabled: z.boolean().describe('false pauses the schedule'),
};

export function register(server, { log, db, isToolEnabled, jobId, agentViewId, moduleConfigs, errorCategory }) {
  const config = moduleConfigs?.scheduler ?? {};
  const core = moduleConfigs?.core ?? {};
  const conversation = moduleConfigs?.conversation ?? {};

  function defaultZone() {
    const tz = String(core['scheduler/default_timezone'] ?? '').trim() || process.env.TZ || 'UTC';
    return zoneOk(tz) ? tz : 'UTC';
  }

  // The caller (D-11, OP-2): the job's account user, checked, or else the view. A user who
  // fails a check gets a refusal, never the view as a fallback (SEC-9).
  async function caller(conn) {
    if (!jobId) return { error: 'the schedule tools need an Agento job context' };
    const [jobs] = await conn.execute(
      'SELECT agent_view_id, requester_trust, requester_key FROM job WHERE id = ?', [jobId]);
    const job = jobs[0];
    if (!job || Number(job.agent_view_id) !== Number(agentViewId)) {
      return { error: 'the schedule tools need an Agento job context' };
    }
    const m = /^user:(\d+)$/.exec(job.requester_key ?? '');
    if (job.requester_trust !== 'account' || !m) {
      return { userId: null, admin: false, lock: `${LIMIT_LOCK.view}${agentViewId}:`, limitPath: 'limits/max_per_agent_view' };
    }
    const userId = Number(m[1]);
    const [users] = await conn.execute('SELECT role, is_active FROM `user` WHERE id = ?', [userId]);
    const user = users[0];
    if (!user || !Number(user.is_active)) return { error: 'the requesting user is not active' };
    const admin = user.role === 'admin';
    if (!admin) {
      const [own] = await conn.execute(
        `SELECT 1 FROM role_grant WHERE role = ? AND grant_kind = 'operation' AND name = 'scheduler.own'
            AND workspace_id IS NULL AND agent_view_id IS NULL LIMIT 1`, [user.role]);
      if (own.length === 0) return { error: 'the requesting user may not own schedules' };
      const [reach] = await conn.execute(
        `SELECT 1 FROM role_grant g JOIN agent_view v ON v.id = ?
          WHERE g.role = ? AND ((g.agent_view_id = v.id AND g.workspace_id IS NULL)
             OR (g.agent_view_id IS NULL AND g.workspace_id = v.workspace_id)) LIMIT 1`, [agentViewId, user.role]);
      if (reach.length === 0) return { error: 'the requesting user cannot reach this agent_view' };
    }
    return { userId, admin, lock: `${LIMIT_LOCK.user}${userId}:`, limitPath: 'limits/max_per_user' };
  }

  // Timing arguments, checked: { values } or { error }.
  function checkTiming(args, { creating }) {
    const values = {};
    if (args.rrule !== undefined) {
      const min = positiveInt(core['scheduler/min_interval_minutes']);
      if (min === null) return { error: 'the schedule tools are not configured (scheduler/min_interval_minutes)' };
      const parsed = parseRrule(args.rrule, min);
      if (parsed.code) return { error: `rrule refused: ${parsed.code}` };
      values.rrule = parsed.rule;
    }
    if (args.timezone !== undefined) {
      if (!zoneOk(args.timezone)) return { error: 'timezone refused: bad_timezone (use a canonical IANA name)' };
      values.timezone = args.timezone;
    } else if (creating) {
      values.timezone = defaultZone();
    }
    if (args.starts_at !== undefined) {
      values.starts_at = parseStart(args.starts_at);
      if (values.starts_at === null) return { error: 'starts_at refused: not a real local time' };
    } else if (creating) {
      values.starts_at = localNow(values.timezone);
    }
    return { values };
  }

  // The panel's rule (scheduler/src/routes.py): not blank, at most the conversation message
  // limit in UTF-8 bytes. A run's prompt is a message in the schedule's conversation.
  function promptError(prompt) {
    const limit = positiveInt(conversation['limits/max_message_bytes']);
    if (limit === null) return 'the schedule tools are not configured (conversation/limits/max_message_bytes)';
    if (!prompt.trim() || Buffer.byteLength(prompt, 'utf8') > limit) {
      return `prompt refused: it must be 1 to ${limit} bytes and not blank`;
    }
    return null;
  }

  async function audit(conn, scheduleId, action, detail = null) {
    await conn.execute('INSERT INTO schedule_audit (schedule_id, action, actor, detail) VALUES (?, ?, ?, ?)',
      [scheduleId, action, `job:${jobId}`, detail === null ? null : JSON.stringify(detail)]);
  }

  // One transaction on one connection; `work` returns the tool answer. A refusal rolls back.
  async function run(name, work) {
    let conn;
    let lock = null;
    try {
      conn = await db.getCronPool().getConnection();
      const who = await caller(conn);
      if (who.error) return refuse(who.error);
      const takeLock = async (key) => {
        const [[{ got }]] = await conn.execute('SELECT GET_LOCK(CONCAT(?, DATABASE()), 10) AS got', [key]);
        if (Number(got) !== 1) throw new Error('schedule limit lock timeout');
        lock = key;
      };
      await conn.beginTransaction();
      const answer = await work(conn, who, takeLock);
      if (answer.isError) await conn.rollback(); else await conn.commit();
      return answer;
    } catch (err) {
      await conn?.rollback().catch(() => {});
      log(name, 'ERROR', `job=${jobId} av=${agentViewId} ${errorCategory(err)}`);
      return refuse(`${name} failed`);
    } finally {
      if (lock) await conn.execute('SELECT RELEASE_LOCK(CONCAT(?, DATABASE()))', [lock]).catch(() => {});
      conn?.release();
    }
  }

  // The row to write, locked: reachable (OP-1) and of the editable source.
  async function writable(conn, who, id) {
    const [rows] = await conn.execute(
      `SELECT id, source, enabled FROM schedule WHERE id = ? AND ${REACH} FOR UPDATE`,
      [id, agentViewId, agentViewId, who.userId]);
    if (rows.length === 0) return { error: 'schedule not found' };
    if (rows[0].source !== SOURCE) return { error: 'this schedule is read-only (it belongs to another module)' };
    return { row: rows[0] };
  }

  const firstFire = 'The first fire time is set within a minute; see schedule_list.';

  if (isToolEnabled('schedule_create')) {
    server.tool(
      'schedule_create',
      'Create a recurring schedule. Each fire runs `prompt` as a new task in this agent_view, in the '
        + 'schedule\'s own conversation. The schedule belongs to the user you work for, or else to this agent_view.',
      { title: fields.title, prompt: fields.prompt, rrule: timing.rrule, starts_at: timing.starts_at.optional(),
        timezone: timing.timezone.optional(), enabled: fields.enabled.optional() },
      async (args) => run('schedule_create', async (conn, who, takeLock) => {
        const checked = checkTiming(args, { creating: true });
        if (checked.error) return refuse(checked.error);
        const badPrompt = promptError(args.prompt);
        if (badPrompt) return refuse(badPrompt);
        if (!who.admin) {
          const limit = positiveInt(config[who.limitPath]);
          if (limit === null) return refuse(`the schedule tools are not configured (scheduler/${who.limitPath})`);
          await takeLock(who.lock);
          const [[{ n }]] = who.userId === null
            ? await conn.execute('SELECT COUNT(*) AS n FROM schedule WHERE owner_agent_view_id = ? AND deleted_at IS NULL',
              [agentViewId])
            : await conn.execute('SELECT COUNT(*) AS n FROM schedule WHERE owner_user_id = ? AND deleted_at IS NULL',
              [who.userId]);
          if (Number(n) >= limit) return refuse(`limit: at most ${limit} schedules`);
        }
        const { rrule, starts_at, timezone } = checked.values;
        const [result] = await conn.execute(
          `INSERT INTO schedule (source, title, prompt, agent_view_id, owner_user_id, owner_agent_view_id,
              rrule, starts_at, timezone, enabled, created_job_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [SOURCE, args.title, args.prompt, agentViewId, who.userId, who.userId === null ? agentViewId : null,
            rrule, starts_at, timezone, args.enabled === false ? 0 : 1, jobId]);
        await audit(conn, result.insertId, 'create');
        log('schedule_create', 'OK', `job=${jobId} av=${agentViewId} schedule=${result.insertId}`);
        return reply({ id: result.insertId, note: firstFire });
      }),
    );
  }

  if (isToolEnabled('schedule_update')) {
    server.tool(
      'schedule_update',
      'Change a schedule you reach (see schedule_list). Set only the fields to change.',
      { id: z.number().int().positive().describe('The schedule id'),
        title: fields.title.optional(), prompt: fields.prompt.optional(), rrule: timing.rrule.optional(),
        starts_at: timing.starts_at.optional(), timezone: timing.timezone.optional(), enabled: fields.enabled.optional() },
      async (args) => run('schedule_update', async (conn, who) => {
        const checked = checkTiming(args, { creating: false });
        if (checked.error) return refuse(checked.error);
        const badPrompt = args.prompt === undefined ? null : promptError(args.prompt);
        if (badPrompt) return refuse(badPrompt);
        const found = await writable(conn, who, args.id);
        if (found.error) return refuse(found.error);
        const changes = { ...checked.values };
        for (const key of ['title', 'prompt']) if (args[key] !== undefined) changes[key] = args[key];
        if (args.enabled !== undefined) changes.enabled = args.enabled ? 1 : 0;
        const fieldsSet = Object.keys(changes).sort();
        if (fieldsSet.length === 0) return refuse('nothing to change');
        // A timing change, or a re-enable, leaves the next fire to the tick (plan §5.1, D-5).
        if (Object.keys(checked.values).length > 0 || (args.enabled === true && !Number(found.row.enabled))) {
          Object.assign(changes, { next_fire_at: null, anchor_local: null, last_error: null });
        }
        const set = Object.keys(changes).map(k => `${k} = ?`).join(', ');
        await conn.execute(`UPDATE schedule SET ${set} WHERE id = ? AND source = ? AND ${REACH}`,
          [...Object.values(changes), args.id, SOURCE, agentViewId, agentViewId, who.userId]);
        await audit(conn, args.id, 'update', { fields: fieldsSet });
        log('schedule_update', 'OK', `job=${jobId} schedule=${args.id} fields=${fieldsSet.join(',')}`);
        return reply({ id: args.id, updated: fieldsSet, note: changes.next_fire_at === null ? firstFire : undefined });
      }),
    );
  }

  if (isToolEnabled('schedule_list')) {
    server.tool(
      'schedule_list',
      'List the schedules you reach in this agent_view. Times: starts_at is local in `timezone`; '
        + 'next_fire_at and last_fire_at are UTC. `editable: false` rows belong to another module.',
      {},
      async () => run('schedule_list', async (conn, who) => {
        const [rows] = await conn.execute(`${LIST_SQL} WHERE ${REACH} ORDER BY id LIMIT ${MAX_LIST}`,
          [agentViewId, agentViewId, who.userId]);
        return reply(rows.map(r => ({
          id: Number(r.id), title: r.title, prompt: r.prompt, rrule: r.rrule, starts_at: r.starts_at,
          timezone: r.timezone, enabled: Boolean(Number(r.enabled)),
          next_fire_at: r.next_fire_at && `${r.next_fire_at}Z`, last_fire_at: r.last_fire_at && `${r.last_fire_at}Z`,
          last_error: r.last_error, editable: r.source === SOURCE,
          owner: r.owner_user_id === null ? 'agent_view' : 'user',
        })));
      }),
    );
  }

  if (isToolEnabled('schedule_delete')) {
    server.tool(
      'schedule_delete',
      'Delete a schedule you reach. Its conversation stays.',
      { id: z.number().int().positive().describe('The schedule id') },
      async ({ id }) => run('schedule_delete', async (conn, who) => {
        const found = await writable(conn, who, id);
        if (found.error) return refuse(found.error);
        // A soft delete: the tick finalizes it (retitles the conversation, schedule_delete_after).
        await conn.execute(
          `UPDATE schedule SET enabled = 0, deleted_at = UTC_TIMESTAMP() WHERE id = ? AND source = ? AND ${REACH}`,
          [id, SOURCE, agentViewId, agentViewId, who.userId]);
        await audit(conn, id, 'delete');
        log('schedule_delete', 'OK', `job=${jobId} schedule=${id}`);
        return reply({ id, deleted: true });
      }),
    );
  }
}
