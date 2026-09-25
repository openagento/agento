import { describe, it, expect, vi } from 'vitest';
import { createHash } from 'node:crypto';
import {
  tokenHash, extractToken, createVerifier, createRequireCapability, rejectScopeMismatch,
  createSourceLookup, NO_AUTH_SOURCES, resolveAuthTtls,
} from '../capability.js';
import { ENDPOINTS } from '../auth-context.js';
import { capabilityRow, capabilityContext } from './capability-rows.js';

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
  const row = capabilityRow('mcp_job');

  it('looks the token up by HASH, never by raw value', async () => {
    const query = fakeQuery([row]);
    await createVerifier(query)('rawtok', { endpoint: 'mcp' });
    const params = query.mock.calls[0][1];
    expect(params).toContain(hash('rawtok'));
    expect(params).not.toContain('rawtok');
  });

  it('returns the auth context v1 derived from the row', async () => {
    expect(await createVerifier(fakeQuery([row]))('t', { endpoint: 'mcp' }))
      .toEqual({ context: capabilityContext('mcp_job'), single_use: false, permitted_tools: null });
  });

  it('selects ids as exact strings in SQL, not as rounded JS numbers', async () => {
    // A fake query returning a string would prove the FAKE, not mysql2 — by then the driver
    // has already rounded a BIGINT past 2^53. The conversion must happen in the database, so
    // assert the CAST is in the statement. The LIVE-driver half (a real row, the real pool, the
    // real verifier) is step 8 of docker/smoke/toolbox-capability-smoke.sh.
    const query = fakeQuery([row]);
    await createVerifier(query)('t', { endpoint: 'mcp' });
    expect(query.mock.calls[0][0]).toMatch(/CAST\(c\.job_id AS CHAR\) AS job_id/);
    expect(query.mock.calls[0][0]).toMatch(/CAST\(c\.id AS CHAR\) AS id/);
  });

  it('preserves a job_id above Number.MAX_SAFE_INTEGER exactly', async () => {
    const big = '9007199254740993';
    const verify = createVerifier(fakeQuery([capabilityRow('mcp_job', { job_id: big })]));
    expect((await verify('t', { endpoint: 'mcp' })).context.job_id).toBe(big);
  });

  it('joins agent_view for the workspace the row must agree with', async () => {
    const query = fakeQuery([row]);
    await createVerifier(query)('t', { endpoint: 'mcp' });
    expect(query.mock.calls[0][0]).toMatch(/LEFT JOIN agent_view av ON av\.id = c\.agent_view_id/);
    const moved = createVerifier(fakeQuery([{ ...row, agent_view_workspace_id: 4 }]));
    expect(await moved('t', { endpoint: 'mcp' })).toBeNull();
  });

  it('parses JSON columns handed over as text', async () => {
    const verify = createVerifier(fakeQuery([{ ...row, allowed_transports: '["http"]' }]));
    expect((await verify('t', { endpoint: 'mcp' })).context.allowed_transports).toEqual(['http']);
    const broken = createVerifier(fakeQuery([{ ...row, allowed_transports: '["http"' }]));
    expect(await broken('t', { endpoint: 'mcp' })).toBeNull();
  });

  it('rejects every kind when allowed_transports is missing — there is no default', async () => {
    for (const kind of ['mcp_job', 'mcp_interactive', 'internal_rest']) {
      for (const value of [null, undefined, [], '[]']) {
        const verify = createVerifier(fakeQuery([capabilityRow(kind, { allowed_transports: value })]));
        for (const endpoint of ENDPOINTS) expect(await verify('t', { endpoint })).toBeNull();
      }
    }
  });

  it('rejects a guard that names no endpoint, or an unknown one, without touching the DB', async () => {
    for (const opts of [undefined, {}, { endpoint: 'admin' }, { kinds: ['mcp_job'] }]) {
      const query = fakeQuery([row]);
      expect(await createVerifier(query)('t', opts)).toBeNull();
      expect(query).not.toHaveBeenCalled();
    }
  });

  it('returns null for an unknown token', async () => {
    expect(await createVerifier(fakeQuery([]))('t', { endpoint: 'mcp' })).toBeNull();
  });

  it('returns null for an empty token without touching the DB', async () => {
    const query = fakeQuery([row]);
    expect(await createVerifier(query)(null, { endpoint: 'mcp' })).toBeNull();
    expect(query).not.toHaveBeenCalled();
  });

  it('excludes expired and revoked rows in SQL', async () => {
    const query = fakeQuery([]);
    await createVerifier(query)('t', { endpoint: 'mcp' });
    const sql = query.mock.calls[0][0];
    expect(sql).toMatch(/revoked_at IS NULL/);
    expect(sql).toMatch(/expires_at > NOW\(\)/);
  });

  it('reverify re-reads the same row by id through the same derivation', async () => {
    const query = fakeQuery([row]);
    const verify = createVerifier(query);
    expect((await verify.reverify('1', { endpoint: 'mcp' })).context).toEqual(capabilityContext('mcp_job'));
    expect(query.mock.calls[0][0]).toMatch(/c\.id = \?/);
    expect(query.mock.calls[0][1]).toEqual(['1']);
    for (const bad of [null, '', '0', 'abc', 1]) expect(await verify.reverify(bad, { endpoint: 'mcp' })).toBeNull();
  });
});

describe('new-profile rows and their sources', () => {
  const T0 = 1790000000;
  const sessionRow = {
    ...capabilityRow('mcp_job'), id: '201', kind: 'user_session', actor: 'user', subject_id: 'u-9',
    agent_view_id: null, agent_view_workspace_id: null, workspace_id: 3, job_id: null,
    source_kind: 'session', source_id: 's-1', created_at: T0 + 100, expires_at: T0 + 130,
  };
  const session = {
    kind: 'session', id: 's-1', user_id: 'u-9', workspace_id: 3, agent_view_id: null,
    permitted_tools: ['email_send'], created_at: T0, expires_at: T0 + 3600,
  };
  const caps = async () => ({ session_max_ttl: 43200, launch_max_ttl: 3600, capability_ttl: 30 });

  it('is refused while no checker is installed for its source kind (E1 ships none)', async () => {
    const verify = createVerifier(fakeQuery([sessionRow]), { resolveTtls: caps });
    expect(await verify('t', { endpoint: 'invoke' })).toBeNull();
  });

  it('asks the checker on every verification and rejects a revoked source', async () => {
    let live = true;
    const check = vi.fn(async () => (live ? session : null));
    const verify = createVerifier(fakeQuery([sessionRow]), {
      sourceCheckers: createSourceLookup([['session', check]]), resolveTtls: caps,
    });
    const first = await verify('t', { endpoint: 'invoke' });
    expect(first.single_use).toBe(true);
    expect(first.permitted_tools).toEqual(['email_send']);
    live = false;
    expect(await verify('t', { endpoint: 'invoke' })).toBeNull();
    expect(check).toHaveBeenCalledTimes(2);
    expect(check).toHaveBeenCalledWith('s-1', expect.objectContaining({ capability_kind: 'user_session' }));
  });

  it('hands the checker the row scope and the verifier\'s own query, so it can compute grants for that scope', async () => {
    const check = vi.fn(async () => session);
    const query = fakeQuery([sessionRow]);
    const verify = createVerifier(query, {
      sourceCheckers: createSourceLookup([['session', check]]), resolveTtls: caps,
    });
    await verify('t', { endpoint: 'invoke' });
    const opts = check.mock.calls[0][1];
    expect(opts).toEqual({ capability_kind: 'user_session', workspace_id: 3, agent_view_id: null, query });
  });

  it('resolves the TTL caps for the row workspace, and a resolver failure propagates (503, not open)', async () => {
    const resolveTtls = vi.fn(async () => { throw new Error('db down'); });
    const verify = createVerifier(fakeQuery([sessionRow]), {
      sourceCheckers: createSourceLookup([['session', async () => session]]), resolveTtls,
    });
    await expect(verify('t', { endpoint: 'invoke' })).rejects.toThrow('db down');
    expect(resolveTtls).toHaveBeenCalledWith(3);
  });

  it('never consults a checker for a legacy kind', async () => {
    const check = vi.fn(async () => session);
    const verify = createVerifier(fakeQuery([capabilityRow('mcp_job')]), {
      sourceCheckers: createSourceLookup([['session', check]]),
    });
    await verify('t', { endpoint: 'mcp' });
    expect(check).not.toHaveBeenCalled();
  });

  it('exposes only a lookup: nothing reachable from it or the verifier can add a checker', () => {
    const lookup = createSourceLookup([]);
    expect(Object.keys(lookup)).toEqual(['lookup']);
    expect(() => { lookup.lookup = () => async () => session; }).toThrow();
    expect(() => { lookup.extra = 1; }).toThrow();
    expect(lookup.lookup('session')).toBeNull();
    const verify = createVerifier(fakeQuery([]), { sourceCheckers: lookup });
    expect(Object.keys(verify)).toEqual(['reverify']);
    expect(NO_AUTH_SOURCES.lookup('session')).toBeNull();
    expect(Object.isFrozen(NO_AUTH_SOURCES)).toBe(true);
  });
});

describe('resolveAuthTtls', () => {
  it('reads only default and workspace rows — never agent_view — and clamps', async () => {
    const query = vi.fn(async (_sql, [scope]) => [scope === 'workspace'
      ? [{ path: 'core/auth/capability_ttl', value: '900', encrypted: 0 }]
      : [{ path: 'core/auth/session_max_ttl', value: '600', encrypted: 0 }]]);
    const caps = await resolveAuthTtls(query, 3);
    expect(caps.capability_ttl).toBe(300);
    expect(caps.session_max_ttl).toBe(600);
    const scopes = query.mock.calls.map(c => c[1]);
    expect(scopes).toEqual([['default', 0], ['workspace', 3]]);
    expect(query.mock.calls.every(c => !/agent_view/.test(c[0]))).toBe(true);
  });

  it('throws on a query failure instead of falling back to the default', async () => {
    await expect(resolveAuthTtls(async () => { throw new Error('db down'); }, 3)).rejects.toThrow('db down');
  });

  it('refuses an encrypted row', async () => {
    const query = async () => [[{ path: 'core/auth/capability_ttl', value: 'x', encrypted: 1 }]];
    await expect(resolveAuthTtls(query, null)).rejects.toThrow(/encrypted/);
  });
});

describe('requireCapability', () => {
  function res() {
    const r = { code: null, body: null };
    r.status = (c) => { r.code = c; return r; };
    r.json = (b) => { r.body = b; return r; };
    return r;
  }

  it('forwards its endpoint to the verifier', async () => {
    const verify = vi.fn(async () => ({ context: capabilityContext('internal_rest', { agent_view_id: null }) }));
    const guard = createRequireCapability(verify, { endpoint: 'config_test' }, vi.fn());
    await guard({ headers: { authorization: 'Bearer t' }, query: {} }, {}, vi.fn());
    expect(verify).toHaveBeenCalledWith('t', { endpoint: 'config_test' });
  });

  it('401s when no token is present', async () => {
    const mw = createRequireCapability(async () => null, { endpoint: 'mcp' }, () => {});
    const r = res(); let nexted = false;
    await mw({ headers: {}, query: {} }, r, () => { nexted = true; });
    expect(r.code).toBe(401);
    expect(nexted).toBe(false);
  });

  it('403s when a token is present but invalid', async () => {
    const mw = createRequireCapability(async () => null, { endpoint: 'mcp' }, () => {});
    const r = res();
    await mw({ headers: { authorization: 'Bearer bad' }, query: {} }, r, () => {});
    expect(r.code).toBe(403);
  });

  it('503s when verification itself fails', async () => {
    const mw = createRequireCapability(async () => { throw new Error('db down'); }, { endpoint: 'mcp' }, () => {});
    const r = res(); let nexted = false;
    await mw({ headers: { authorization: 'Bearer good' }, query: {} }, r, () => { nexted = true; });
    expect(r.code).toBe(503);
    expect(nexted).toBe(false);
  });

  it('attaches the context and calls next on success', async () => {
    const context = capabilityContext('mcp_job');
    const mw = createRequireCapability(async () => ({ context, single_use: false }), { endpoint: 'mcp' }, () => {});
    const req = { headers: { authorization: 'Bearer good' }, query: {} };
    let nexted = false;
    await mw(req, res(), () => { nexted = true; });
    expect(nexted).toBe(true);
    expect(req.capability).toEqual(context);
  });

  it('refuses a query-string token at invoke outright, even beside a valid header', async () => {
    const verify = vi.fn(async () => ({ context: capabilityContext('mcp_job') }));
    const mw = createRequireCapability(verify, { endpoint: 'invoke' }, () => {});
    for (const query of [{ cap: 'tok' }, { cap: '' }]) {
      const r = res(); let nexted = false;
      await mw({ headers: { authorization: 'Bearer tok' }, query }, r, () => { nexted = true; });
      expect(r.code).toBe(401);
      expect(nexted).toBe(false);
    }
    expect(verify).not.toHaveBeenCalled();
  });

  it('never logs the raw token', async () => {
    const lines = [];
    const mw = createRequireCapability(async () => null, { endpoint: 'mcp' }, (...a) => lines.push(a.join(' ')));
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
  const req = (extra) => ({ capability: { agent_view_id: 7 }, body: {}, query: {}, ...extra });

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
