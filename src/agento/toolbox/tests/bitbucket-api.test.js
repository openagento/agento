import { describe, it, expect, vi } from 'vitest';

import {
  createOpenPrsHandler,
  createVerifyHandler,
  isEnabled,
  parseRepoAllowlist,
} from '../../modules/bitbucket/toolbox/api-handlers.js';

function mockRes() {
  return {
    statusCode: 200,
    body: null,
    status(c) { this.statusCode = c; return this; },
    json(b) { this.body = b; return this; },
  };
}

function jsonRes(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => '' };
}

// A fake auth whose bbFetch dispatches on the joined path; `page` query controls pagination.
function fakeAuth(routes) {
  return {
    isConfigured: () => true,
    bbFetch: vi.fn(async (segments, opts = {}) => {
      const key = segments.join('/');
      const handler = routes[key];
      if (!handler) return jsonRes({ values: [] });
      return handler(segments, opts);
    }),
  };
}

describe('config normalization helpers', () => {
  it('isEnabled treats only "1"/true/"true" as enabled (config.json false ⇒ off)', () => {
    expect(isEnabled('1')).toBe(true);
    expect(isEnabled(true)).toBe(true);
    expect(isEnabled('true')).toBe(true);
    expect(isEnabled(false)).toBe(false);
    expect(isEnabled('0')).toBe(false);
    expect(isEnabled(undefined)).toBe(false);
  });

  it('parseRepoAllowlist splits/trims/dedupes', () => {
    expect(parseRepoAllowlist(' api , web ,api ')).toEqual(['api', 'web']);
    expect(parseRepoAllowlist('')).toEqual([]);
  });
});

describe('POST /api/bitbucket/verify', () => {
  it('returns ok:false 400 when fields are missing (no token echoed)', async () => {
    const handler = createVerifyHandler(vi.fn(), () => ({ isConfigured: () => false }));
    const res = mockRes();
    await handler({ body: { workspace: 'acme' } }, res);
    expect(res.body.ok).toBe(false);
    expect(res.body.status).toBe(400);
  });

  it('returns the account_uuid on a 200 from GET /user', async () => {
    const auth = { isConfigured: () => true, bbFetch: async () => jsonRes({ uuid: '{me}', nickname: 'agent' }) };
    const handler = createVerifyHandler(vi.fn(), () => auth);
    const res = mockRes();
    await handler({ body: { workspace: 'acme', email: 'e', api_token: 't' } }, res);
    expect(res.body).toEqual({ ok: true, account_uuid: '{me}', username: 'agent' });
  });

  it('returns ok:false on an auth failure without echoing the token', async () => {
    const auth = { isConfigured: () => true, bbFetch: async () => jsonRes({}, 401) };
    const handler = createVerifyHandler(vi.fn(), () => auth);
    const res = mockRes();
    await handler({ body: { workspace: 'acme', email: 'e', api_token: 'super-secret' } }, res);
    expect(res.body.ok).toBe(false);
    expect(res.body.status).toBe(401);
    expect(JSON.stringify(res.body)).not.toContain('super-secret');
  });
});

describe('POST /api/bitbucket/open-prs', () => {
  const baseCfg = {
    enabled: '1', bitbucket_workspace: 'acme', bitbucket_account_uuid: '{me}',
    bitbucket_email: 'e@x.com', bitbucket_api_token: 'tok', repo_allowlist: 'api', poll_top: '20',
  };

  function deps({ meta = { id: 7 }, cfg = baseCfg } = {}) {
    return {
      loadScopedDbOverrides: async () => ({ overrides: {}, agentViewMeta: meta }),
      loadModuleConfigs: async () => ({ bitbucket: cfg }),
    };
  }

  it('fails closed (404) on an unknown agent_view', async () => {
    const handler = createOpenPrsHandler(deps({ meta: null }), vi.fn(), () => fakeAuth({}));
    const res = mockRes();
    await handler({ body: { agent_view_id: 999, lane: 'comments' } }, res);
    expect(res.statusCode).toBe(404);
  });

  it('returns 403 when the channel is disabled for the scope', async () => {
    const handler = createOpenPrsHandler(
      deps({ cfg: { ...baseCfg, enabled: '0' } }), vi.fn(), () => fakeAuth({}),
    );
    const res = mockRes();
    await handler({ body: { agent_view_id: 7, lane: 'comments' } }, res);
    expect(res.statusCode).toBe(403);
  });

  it('400 when agent_view_id is missing', async () => {
    const handler = createOpenPrsHandler(deps(), vi.fn(), () => fakeAuth({}));
    const res = mockRes();
    await handler({ body: { lane: 'comments' } }, res);
    expect(res.statusCode).toBe(400);
  });

  it('uses the SCOPED account_uuid in the query (ignores any caller-supplied value) and normalizes a comments record', async () => {
    const auth = fakeAuth({
      'repositories/acme/api/pullrequests': () => jsonRes({ values: [{ id: 42, title: 'X', updated_on: 'T' }] }),
      'repositories/acme/api/pullrequests/42/comments': () => jsonRes({
        values: [{ id: 'c1', user: { uuid: '{rev}' }, created_on: 'T3', deleted: false, resolution: null }],
      }),
      'repositories/acme/api/pullrequests/42/commits': () => jsonRes({ values: [{ date: 'T1' }] }),
    });
    const handler = createOpenPrsHandler(deps(), vi.fn(), () => auth);
    const res = mockRes();
    // caller tries to inject a different uuid/allowlist — must be ignored.
    await handler({ body: { agent_view_id: 7, lane: 'comments', account_uuid: '{evil}', repo_allowlist: 'secret' } }, res);

    const listCall = auth.bbFetch.mock.calls.find((c) => c[0].join('/') === 'repositories/acme/api/pullrequests');
    expect(listCall[1].query.q).toContain('author.uuid="{me}"'); // scoped, not caller-supplied
    expect(res.body.pull_requests).toHaveLength(1);
    const rec = res.body.pull_requests[0];
    expect(rec.comments[0]).toEqual({ id: 'c1', author_uuid: '{rev}', created_on: 'T3', deleted: false, resolved: false });
    expect(rec.commits).toEqual([{ date: 'T1' }]);
    // a repo outside the allow-list ("secret") is never queried.
    expect(auth.bbFetch.mock.calls.some((c) => c[0].includes('secret'))).toBe(false);
  });

  it('comments lane: the /commits request carries NO `page` param (Bitbucket Cloud returns HTTP 400 otherwise)', async () => {
    const auth = fakeAuth({
      'repositories/acme/api/pullrequests': () => jsonRes({ values: [{ id: 42, title: 'X', updated_on: 'T' }] }),
      'repositories/acme/api/pullrequests/42/comments': () => jsonRes({ values: [] }),
      // Emulate Bitbucket Cloud: /commits rejects a page-number query with HTTP 400 "Invalid page".
      'repositories/acme/api/pullrequests/42/commits': (_segments, opts) => (
        'page' in (opts.query || {})
          ? jsonRes({ error: { message: 'Invalid page' } }, 400)
          : jsonRes({ values: [{ date: 'T1' }] })
      ),
    });
    const handler = createOpenPrsHandler(deps(), vi.fn(), () => auth);
    const res = mockRes();
    await handler({ body: { agent_view_id: 7, lane: 'comments' } }, res);

    const commitsCall = auth.bbFetch.mock.calls.find(
      (c) => c[0].join('/') === 'repositories/acme/api/pullrequests/42/commits',
    );
    expect(commitsCall[1].query).not.toHaveProperty('page'); // the regression guard
    expect(commitsCall[1].query.pagelen).toBe(50);
    // and the record still publishes (no per-repo drop from a 400)
    expect(res.body.errors).toHaveLength(0);
    expect(res.body.pull_requests[0].commits).toEqual([{ date: 'T1' }]);
  });

  it('isolates a per-repo failure: one repo 500 ⇒ errors[] while the other repo still returns', async () => {
    const auth = fakeAuth({
      'repositories/acme/bad/pullrequests': () => jsonRes({}, 500),
      'repositories/acme/api/pullrequests': () => jsonRes({ values: [{ id: 1, title: 'X', updated_on: 'T' }] }),
      'repositories/acme/api/pullrequests/1/comments': () => jsonRes({ values: [] }),
      'repositories/acme/api/pullrequests/1/commits': () => jsonRes({ values: [] }),
    });
    const handler = createOpenPrsHandler(
      deps({ cfg: { ...baseCfg, repo_allowlist: 'bad,api' } }), vi.fn(), () => auth,
    );
    const res = mockRes();
    await handler({ body: { agent_view_id: 7, lane: 'comments' } }, res);
    expect(res.body.errors).toHaveLength(1);
    expect(res.body.errors[0].repo).toBe('bad');
    expect(res.body.pull_requests).toHaveLength(1);
  });

  it('clamps to effective_top and stops paginating after it', async () => {
    const auth = fakeAuth({
      'repositories/acme/api/pullrequests': () => jsonRes({
        values: [{ id: 1, title: 'a' }, { id: 2, title: 'b' }, { id: 3, title: 'c' }],
      }),
      'repositories/acme/api/pullrequests/1/comments': () => jsonRes({ values: [] }),
      'repositories/acme/api/pullrequests/1/commits': () => jsonRes({ values: [] }),
      'repositories/acme/api/pullrequests/2/comments': () => jsonRes({ values: [] }),
      'repositories/acme/api/pullrequests/2/commits': () => jsonRes({ values: [] }),
    });
    const handler = createOpenPrsHandler(
      deps({ cfg: { ...baseCfg, poll_top: '2' } }), vi.fn(), () => auth,
    );
    const res = mockRes();
    await handler({ body: { agent_view_id: 7, lane: 'comments' } }, res);
    expect(res.body.pull_requests).toHaveLength(2); // 3 available, capped to 2
  });

  it('changes lane drops the agent\'s own changes_request and returns reviewers only', async () => {
    const auth = fakeAuth({
      'repositories/acme/api/pullrequests': () => jsonRes({ values: [{ id: 9, title: 'X' }] }),
      'repositories/acme/api/pullrequests/9/activity': () => jsonRes({
        values: [
          { changes_requested: { date: 'T2', user: { uuid: '{rev}' } } },
          { changes_requested: { date: 'T3', user: { uuid: '{me}' } } }, // agent's own — dropped
          { approval: { date: 'T1', user: { uuid: '{rev}' } } }, // not a changes_request
        ],
      }),
    });
    const handler = createOpenPrsHandler(deps(), vi.fn(), () => auth);
    const res = mockRes();
    await handler({ body: { agent_view_id: 7, lane: 'changes' } }, res);
    const rec = res.body.pull_requests[0];
    expect(rec.changes_requests).toEqual([{ user_uuid: '{rev}', date: 'T2' }]);
  });
});
