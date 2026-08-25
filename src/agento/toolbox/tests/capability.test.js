import { describe, it, expect, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { tokenHash, extractToken, createVerifier, createRequireCapability, rejectScopeMismatch } from '../capability.js';

const hash = (t) => createHash('sha256').update(t).digest('hex');
const fakeQuery = (rows) => vi.fn(async () => [rows]);

describe('tokenHash', () => {
  it('is sha256 hex', () => expect(tokenHash('abc')).toBe(hash('abc')));
});

describe('extractToken', () => {
  it('prefers the Authorization bearer header', () => {
    expect(extractToken({ headers: { authorization: 'Bearer tok1' }, query: { cap: 'tok2' } })).toBe('tok1');
  });
  it('falls back to the cap query param', () => {
    expect(extractToken({ headers: {}, query: { cap: 'tok2' } })).toBe('tok2');
  });
  it('returns null when neither is present', () => {
    expect(extractToken({ headers: {}, query: {} })).toBeNull();
  });
  it('ignores a non-bearer authorization scheme', () => {
    expect(extractToken({ headers: { authorization: 'Basic zzz' }, query: {} })).toBeNull();
  });
});

describe('verifyCapability', () => {
  const row = { kind: 'mcp_job', agent_view_id: 7, job_id: '42' };

  it('looks the token up by HASH, never by raw value', async () => {
    const query = fakeQuery([row]);
    await createVerifier(query)('rawtok', { kinds: ['mcp_job'] });
    const params = query.mock.calls[0][1];
    expect(params).toContain(hash('rawtok'));
    expect(params).not.toContain('rawtok');
  });

  it('returns server-side claims', async () => {
    expect(await createVerifier(fakeQuery([row]))('t', { kinds: ['mcp_job'] }))
      .toEqual({ kind: 'mcp_job', agentViewId: 7, jobId: '42' });
  });

  it('rejects an mcp_job row whose job_id is NULL, zero, or malformed', async () => {
    // Not "returns jobId: null" — REJECTS. A null jobId is Outlook's interactive escape hatch.
    for (const bad of [null, 0, '0', '-1', 'abc', '', '007']) {
      const verify = createVerifier(fakeQuery([{ kind: 'mcp_job', agent_view_id: 7, job_id: bad }]));
      expect(await verify('t', { kinds: ['mcp_job'] })).toBeNull();
    }
  });

  it('selects job_id as an exact string in SQL, not as a rounded JS number', async () => {
    // A fake query returning a string would prove the FAKE, not mysql2 — by then the driver
    // has already rounded a BIGINT past 2^53. The conversion must happen in the database, so
    // assert the CAST is in the statement. The LIVE-driver half (a real row, the real pool, the
    // real verifier) is step 8 of docker/smoke/toolbox-capability-smoke.sh — this suite has no
    // database, so it cannot be asserted here.
    const query = fakeQuery([row]);
    await createVerifier(query)('t', { kinds: ['mcp_job'] });
    expect(query.mock.calls[0][0]).toMatch(/CAST\(job_id AS CHAR\) AS job_id/);
  });

  it('preserves a job_id above Number.MAX_SAFE_INTEGER exactly', async () => {
    const big = '9007199254740993';
    const verify = createVerifier(fakeQuery([{ kind: 'mcp_job', agent_view_id: 7, job_id: big }]));
    expect((await verify('t', { kinds: ['mcp_job'] })).jobId).toBe(big);
  });

  it('accepts an internal_rest row with or without a job_id', async () => {
    // Both are legitimate: job-owned discovery carries the id, a publisher call does not.
    const withJob = createVerifier(fakeQuery([{ kind: 'internal_rest', agent_view_id: 7, job_id: '42' }]));
    expect(await withJob('t', { kinds: ['internal_rest'] })).toEqual({ kind: 'internal_rest', agentViewId: 7, jobId: '42' });
    const without = createVerifier(fakeQuery([{ kind: 'internal_rest', agent_view_id: 7, job_id: null }]));
    expect(await without('t', { kinds: ['internal_rest'] })).toEqual({ kind: 'internal_rest', agentViewId: 7, jobId: null });
  });

  it('rejects an internal_rest row whose job_id is malformed', async () => {
    const verify = createVerifier(fakeQuery([{ kind: 'internal_rest', agent_view_id: 7, job_id: '0' }]));
    expect(await verify('t', { kinds: ['internal_rest'] })).toBeNull();
  });

  it('rejects an mcp_interactive row that carries a job_id', async () => {
    const verify = createVerifier(fakeQuery([{ kind: 'mcp_interactive', agent_view_id: 7, job_id: '42' }]));
    expect(await verify('t', { kinds: ['mcp_interactive'] })).toBeNull();
  });

  it('accepts an mcp_interactive row without a job_id', async () => {
    const verify = createVerifier(fakeQuery([{ kind: 'mcp_interactive', agent_view_id: 7, job_id: null }]));
    expect(await verify('t', { kinds: ['mcp_interactive'] })).toEqual({ kind: 'mcp_interactive', agentViewId: 7, jobId: null });
  });

  it('returns null for an unknown token', async () => {
    expect(await createVerifier(fakeQuery([]))('t', { kinds: ['mcp_job'] })).toBeNull();
  });

  it('returns null when the kind is not allowed', async () => {
    expect(await createVerifier(fakeQuery([row]))('t', { kinds: ['internal_rest'] })).toBeNull();
  });

  it('returns null when the row carries a non-positive agent_view_id', async () => {
    const bad = { kind: 'mcp_job', agent_view_id: 0, job_id: '42' };
    expect(await createVerifier(fakeQuery([bad]))('t', { kinds: ['mcp_job'] })).toBeNull();
  });

  // A viewless internal_rest row exists only for a default-scope `/config-test`. Every guard
  // that did not opt in must still refuse it, or it would reach GLOBAL config.
  it('refuses a viewless row unless the guard allows it', async () => {
    const viewless = { kind: 'internal_rest', agent_view_id: null, job_id: null };
    expect(await createVerifier(fakeQuery([viewless]))('t', { kinds: ['internal_rest'] })).toBeNull();
    expect(await createVerifier(fakeQuery([viewless]))('t', { kinds: ['internal_rest'], allowViewless: true }))
      .toEqual({ kind: 'internal_rest', agentViewId: null, jobId: null });
    const withJob = { kind: 'internal_rest', agent_view_id: null, job_id: '42' };
    expect(await createVerifier(fakeQuery([withJob]))('t', { kinds: ['internal_rest'], allowViewless: true }))
      .toBeNull();
  });

  it('allowViewless admits no viewless MCP row', async () => {
    const viewless = { kind: 'mcp_interactive', agent_view_id: null, job_id: null };
    const verify = createVerifier(fakeQuery([viewless]));
    expect(await verify('t', { kinds: ['mcp_interactive'], allowViewless: true })).toBeNull();
  });

  it('the guard forwards allowViewless to the verifier', async () => {
    const verify = vi.fn(async () => ({ kind: 'internal_rest', agentViewId: null, jobId: null }));
    const guard = createRequireCapability(verify, { kinds: ['internal_rest'], allowViewless: true }, vi.fn());
    await guard({ headers: { authorization: 'Bearer t' }, query: {} }, {}, vi.fn());
    expect(verify).toHaveBeenCalledWith('t', { kinds: ['internal_rest'], allowViewless: true });
  });

  it('returns null for an empty token without touching the DB', async () => {
    const query = fakeQuery([row]);
    expect(await createVerifier(query)(null, { kinds: ['mcp_job'] })).toBeNull();
    expect(query).not.toHaveBeenCalled();
  });

  it('excludes expired and revoked rows in SQL', async () => {
    const query = fakeQuery([]);
    await createVerifier(query)('t', { kinds: ['mcp_job'] });
    const sql = query.mock.calls[0][0];
    expect(sql).toMatch(/revoked_at IS NULL/);
    expect(sql).toMatch(/expires_at > NOW\(\)/);
  });
});

describe('requireCapability', () => {
  function res() {
    const r = { code: null, body: null };
    r.status = (c) => { r.code = c; return r; };
    r.json = (b) => { r.body = b; return r; };
    return r;
  }

  const VALID_ROWS = [
    { kind: 'mcp_job', agent_view_id: 7, job_id: '42' },
    { kind: 'mcp_interactive', agent_view_id: 7, job_id: null },
    { kind: 'internal_rest', agent_view_id: 7, job_id: null },
  ];

  it.each([
    ['missing kinds', undefined],
    ['empty kinds', []],
    ['non-array kinds', 'internal_rest'],
  ])('rejects a VALID token when the guard declares %s', async (_label, kinds) => {
    // A guard that forgot to declare its kinds must authenticate NOTHING, not everything.
    for (const row of VALID_ROWS) {
      const verify = createVerifier(fakeQuery([row]));
      expect(await verify('token', { kinds })).toBeNull();
    }
  });

  it('401s when no token is present', async () => {
    const mw = createRequireCapability(async () => null, { kinds: ['mcp_job'] }, () => {});
    const r = res(); let nexted = false;
    await mw({ headers: {}, query: {} }, r, () => { nexted = true; });
    expect(r.code).toBe(401);
    expect(nexted).toBe(false);
  });

  it('403s when a token is present but invalid', async () => {
    const mw = createRequireCapability(async () => null, { kinds: ['mcp_job'] }, () => {});
    const r = res();
    await mw({ headers: { authorization: 'Bearer bad' }, query: {} }, r, () => {});
    expect(r.code).toBe(403);
  });

  it('503s when verification itself fails', async () => {
    const mw = createRequireCapability(async () => { throw new Error('db down'); }, { kinds: ['mcp_job'] }, () => {});
    const r = res(); let nexted = false;
    await mw({ headers: { authorization: 'Bearer good' }, query: {} }, r, () => { nexted = true; });
    expect(r.code).toBe(503);
    expect(nexted).toBe(false);
  });

  it('attaches claims and calls next on success', async () => {
    const claims = { kind: 'mcp_job', agentViewId: 7, jobId: '42' };
    const mw = createRequireCapability(async () => claims, { kinds: ['mcp_job'] }, () => {});
    const req = { headers: { authorization: 'Bearer good' }, query: {} };
    let nexted = false;
    await mw(req, res(), () => { nexted = true; });
    expect(nexted).toBe(true);
    expect(req.capability).toEqual(claims);
  });

  it('never logs the raw token', async () => {
    const lines = [];
    const mw = createRequireCapability(async () => null, { kinds: ['mcp_job'] }, (...a) => lines.push(a.join(' ')));
    await mw({ headers: { authorization: 'Bearer s3cret' }, query: {} }, res(), () => {});
    expect(lines.join('\n')).not.toContain('s3cret');
  });
});

// A caller-supplied agent_view_id is never USED — the capability row is the only source of the
// scope. But silently ignoring one answers a DIFFERENT view than the caller asked about, which
// reads as success. Both transports that can carry one (a JSON body, a query string) get a 400.
describe('rejectScopeMismatch', () => {
  const res = () => {
    const r = { code: null, body: null };
    r.status = (c) => { r.code = c; return r; };
    r.json = (b) => { r.body = b; return r; };
    return r;
  };
  const req = (extra) => ({ capability: { agentViewId: 7 }, body: {}, query: {}, ...extra });

  it('passes when nothing is supplied', () => {
    const r = res();
    expect(rejectScopeMismatch(req(), r, vi.fn(), 'x')).toBe(false);
    expect(r.code).toBe(null);
  });

  it('passes when the supplied id agrees (body or query, string or number)', () => {
    for (const extra of [{ body: { agent_view_id: 7 } }, { body: { agent_view_id: '7' } },
                         { query: { agent_view_id: '7' } }]) {
      expect(rejectScopeMismatch(req(extra), res(), vi.fn(), 'x')).toBe(false);
    }
  });

  // `body ?? query` read only the body when both were present, so a query naming another view
  // slipped through with a body that agreed. Every supplied value is compared, independently.
  it('answers 400 when the body agrees but the query disagrees', () => {
    const r = res();
    const bad = req({ body: { agent_view_id: 7 }, query: { agent_view_id: 9 } });
    expect(rejectScopeMismatch(bad, r, vi.fn(), 'x')).toBe(true);
    expect(r.code).toBe(400);
  });

  it('answers 400 when the query agrees but the body disagrees', () => {
    const r = res();
    const bad = req({ body: { agent_view_id: 9 }, query: { agent_view_id: 7 } });
    expect(rejectScopeMismatch(bad, r, vi.fn(), 'x')).toBe(true);
    expect(r.code).toBe(400);
  });

  it('answers 400 for a disagreeing body id', () => {
    const r = res();
    expect(rejectScopeMismatch(req({ body: { agent_view_id: 9 } }), r, vi.fn(), 'x')).toBe(true);
    expect(r.code).toBe(400);
  });

  it('answers 400 for a disagreeing QUERY id — the scoped /health case', () => {
    const r = res();
    expect(rejectScopeMismatch(req({ query: { agent_view_id: '9' } }), r, vi.fn(), 'x')).toBe(true);
    expect(r.code).toBe(400);
  });
});
