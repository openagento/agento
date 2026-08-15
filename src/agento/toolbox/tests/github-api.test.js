import { describe, it, expect, vi, afterEach } from 'vitest';

import {
  LANES,
  REVIEW_THREADS_QUERY,
  createOpenPrsHandler,
  createVerifyHandler,
  isEnabled,
  parseRepoAllowlist,
} from '../../modules/github/toolbox/api-handlers.js';
import { VIEW_SCOPED_ENV_KEYS } from '../../modules/github/toolbox/env-guard.js';
import { createGitHubAuth } from '../../modules/github/toolbox/github-auth.js';

function mockRes() {
  return {
    statusCode: 200,
    body: null,
    status(c) { this.statusCode = c; return this; },
    json(b) { this.body = b; return this; },
  };
}

function res(body, { status = 200, link = null } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'link' ? link : null) },
    json: async () => body,
    // One body, either reader — an error fixture's `text()` is its JSON, which is what
    // `describeError` reads.
    text: async () => JSON.stringify(body),
  };
}

// A fake `fetch` dispatching on URL pathname, injected into the REAL createGitHubAuth — so every
// assertion about a URL is an assertion about the URL the module actually builds.
function makeFetch(routes) {
  return vi.fn(async (url, opts) => {
    const u = new URL(url);
    const handler = routes[u.pathname];
    if (!handler) return res([]);
    return handler(u, opts);
  });
}

function authFactoryFor(fetchSpy) {
  return (cfg) => createGitHubAuth(cfg, { fetch: fetchSpy, sleep: async () => {} });
}

const CFG = {
  enabled: '1',
  github_owner: 'acme',
  github_login: 'agent-bot',
  github_token: 'ghp_secret',
  repo_allowlist: 'api',
  poll_top: '20',
};

function openPrs(fetchSpy, { cfg = CFG, agentViewMeta = { id: 1, code: 'dev' } } = {}) {
  const deps = {
    loadScopedDbOverrides: vi.fn(async () => ({ overrides: {}, agentViewMeta })),
    loadModuleConfigs: vi.fn(async () => ({ github: cfg })),
  };
  return { handler: createOpenPrsHandler(deps, vi.fn(), authFactoryFor(fetchSpy)), deps };
}

async function callOpenPrs(routes, { body = { agent_view_id: 1, lane: 'changes' }, ...opts } = {}) {
  const fetchSpy = makeFetch(routes);
  const { handler } = openPrs(fetchSpy, opts);
  const r = mockRes();
  await handler({ body }, r);
  return { r, fetchSpy };
}

const pr = (n, login, extra = {}) => ({
  number: n,
  title: `PR ${n}`,
  updated_at: '2026-01-01T00:00:00Z',
  user: { login },
  head: { sha: `sha${n}` },
  ...extra,
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('config normalization helpers', () => {
  it('isEnabled treats only true/"1"/"true" (any case) as enabled', () => {
    for (const v of [true, '1', 'true', 'TRUE']) expect(isEnabled(v)).toBe(true);
    for (const v of [false, '0', '', undefined, 'yes']) expect(isEnabled(v)).toBe(false);
  });

  it('parseRepoAllowlist splits, trims, de-dupes and preserves order', () => {
    expect(parseRepoAllowlist(' api , web ,api, ')).toEqual(['api', 'web']);
    expect(parseRepoAllowlist('')).toEqual([]);
    expect(parseRepoAllowlist(null)).toEqual([]);
  });
});

describe('POST /api/github/verify', () => {
  it('rejects a missing/blank/non-string token without fetching anything', async () => {
    for (const body of [{}, { token: '' }, { token: '   ' }, { token: 42 }]) {
      const fetchSpy = makeFetch({});
      const handler = createVerifyHandler(vi.fn(), authFactoryFor(fetchSpy));
      const r = mockRes();
      await handler({ body }, r);
      expect(r.body).toEqual({ ok: false, status: 400, detail: 'token is required' });
      expect(fetchSpy).not.toHaveBeenCalled();
    }
  });

  it('ignores a caller-supplied api_base/host/url — the origin is a constant (SSRF guard)', async () => {
    const fetchSpy = makeFetch({ '/user': () => res({ login: 'agent-bot', id: 42 }) });
    const handler = createVerifyHandler(vi.fn(), authFactoryFor(fetchSpy));
    await handler({ body: { token: 't', api_base: 'http://evil', host: 'evil', url: 'http://evil' } }, mockRes());
    expect(fetchSpy.mock.calls[0][0]).toBe('https://api.github.com/user');
  });

  it("reports a 401 with GitHub's message — onboarding prints it to the operator", async () => {
    // The operator is the only person who can act on this, and the two failures they must tell apart
    // ("wrong token" vs "right token, permission not granted") differ ONLY in this message.
    const fetchSpy = makeFetch({ '/user': () => res({ message: 'Bad credentials' }, { status: 401 }) });
    const handler = createVerifyHandler(vi.fn(), authFactoryFor(fetchSpy));
    const r = mockRes();
    await handler({ body: { token: 't' } }, r);
    expect(r.body.ok).toBe(false);
    expect(r.body.status).toBe(401);
    expect(r.body.detail).toContain('Bad credentials');
    expect(JSON.stringify(r.body)).not.toContain('ghp_'); // never the credential itself
  });

  it('returns login + id on a 200', async () => {
    const fetchSpy = makeFetch({ '/user': () => res({ login: 'agent-bot', id: 42 }) });
    const handler = createVerifyHandler(vi.fn(), authFactoryFor(fetchSpy));
    const r = mockRes();
    await handler({ body: { token: 't' } }, r);
    expect(r.body).toEqual({ ok: true, login: 'agent-bot', id: 42 });
  });

  it('answers 500 with a generic detail when the fetch throws, never leaking the token', async () => {
    const handler = createVerifyHandler(vi.fn(), () => ({
      ghFetch: async () => { throw new Error('boom ghp_secret'); },
    }));
    const r = mockRes();
    await handler({ body: { token: 'ghp_secret' } }, r);
    expect(r.statusCode).toBe(500);
    expect(r.body).toEqual({ ok: false, status: 500, detail: 'Internal error during verification' });
    expect(JSON.stringify(r.body)).not.toContain('ghp_secret');
  });
});

describe('POST /api/github/open-prs — request validation', () => {
  it('rejects a missing / non-numeric / zero / negative agent_view_id', async () => {
    for (const id of [undefined, 'abc', 0, -1, 1.5]) {
      const { r } = await callOpenPrs({}, { body: { agent_view_id: id, lane: 'changes' } });
      expect(r.statusCode).toBe(400);
      expect(r.body.error).toMatch(/agent_view_id/);
    }
  });

  it('rejects a missing or unknown lane, naming the valid lanes', async () => {
    for (const lane of [undefined, 'commets', '']) {
      const { r } = await callOpenPrs({}, { body: { agent_view_id: 1, lane } });
      expect(r.statusCode).toBe(400);
      expect(r.body.error).toContain(LANES.join(', '));
    }
  });

  it('fails closed with 404 when the agent_view is unknown', async () => {
    const { r } = await callOpenPrs({}, { agentViewMeta: null });
    expect(r.statusCode).toBe(404);
  });

  it('answers 403 when the channel is disabled for the scope', async () => {
    const { r } = await callOpenPrs({}, { cfg: { ...CFG, enabled: '0' } });
    expect(r.statusCode).toBe(403);
  });

  it('answers 403 when owner / login / token is missing', async () => {
    for (const missing of ['github_owner', 'github_login', 'github_token']) {
      const { r } = await callOpenPrs({}, { cfg: { ...CFG, [missing]: '' } });
      expect(r.statusCode).toBe(403);
      expect(r.body.error).toMatch(/not fully configured/);
    }
  });

  it('returns an empty result and fetches nothing when the allow-list is empty', async () => {
    const { r, fetchSpy } = await callOpenPrs({}, { cfg: { ...CFG, repo_allowlist: '  ' } });
    expect(r.body).toEqual({ pull_requests: [], errors: [] });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('ignores body owner / repo_allowlist / github_login — only the scoped config authorizes', async () => {
    const routes = {
      '/repos/acme/api/pulls': () => res([pr(7, 'agent-bot')]),
      '/repos/acme/api/pulls/7/reviews': () => res([]),
    };
    const { r, fetchSpy } = await callOpenPrs(routes, {
      body: {
        agent_view_id: 1, lane: 'changes',
        owner: 'evil', repo_allowlist: 'secret-repo', github_login: 'someone-else',
      },
    });
    expect(r.body.pull_requests).toHaveLength(1);
    expect(r.body.pull_requests[0]).toMatchObject({ owner: 'acme', repo: 'api', id: 7 });
    for (const [url] of fetchSpy.mock.calls) {
      expect(url).toContain('/repos/acme/api/');
      expect(url).not.toContain('secret-repo');
    }
  });
});

describe.each(VIEW_SCOPED_ENV_KEYS)('ENV guard — %s set as a global override', (key) => {
  it('refuses /open-prs with 503 before any config resolution or fetch', async () => {
    vi.stubEnv(key, 'x');
    const fetchSpy = makeFetch({});
    const { handler, deps } = openPrs(fetchSpy);
    const r = mockRes();
    await handler({ body: { agent_view_id: 1, lane: 'comments' } }, r);
    expect(r.statusCode).toBe(503);
    expect(r.body.error).toContain(key);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(deps.loadScopedDbOverrides).not.toHaveBeenCalled();
  });

  it('refuses /verify with 503 too — a guard on one route only is not a guard', async () => {
    vi.stubEnv(key, 'x');
    const fetchSpy = makeFetch({ '/user': () => res({ login: 'agent-bot', id: 1 }) });
    const handler = createVerifyHandler(vi.fn(), authFactoryFor(fetchSpy));
    const r = mockRes();
    await handler({ body: { token: 't' } }, r);
    expect(r.statusCode).toBe(503);
    expect(r.body.error).toContain(key);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('POST /api/github/open-prs — discovery', () => {
  it('keeps only agent-authored PRs and caps on MATCHING ones, paging past the rest', async () => {
    const routes = {
      '/repos/acme/api/pulls': (u) => (u.searchParams.get('page') === '1'
        ? res([pr(1, 'other'), pr(2, 'other'), pr(3, 'other')], { link: '<https://api.github.com/x>; rel="next"' })
        : res([pr(4, 'agent-bot'), pr(5, 'agent-bot')])),
      '/repos/acme/api/pulls/4/reviews': () => res([]),
      '/repos/acme/api/pulls/5/reviews': () => res([]),
    };
    const { r, fetchSpy } = await callOpenPrs(routes, { cfg: { ...CFG, poll_top: '2' } });
    expect(r.body.pull_requests.map((p) => p.id)).toEqual([4, 5]);
    expect(fetchSpy.mock.calls.filter(([u]) => new URL(u).pathname === '/repos/acme/api/pulls')).toHaveLength(2);
  });

  it('lets `top` narrow only — never widen past poll_top', async () => {
    const routes = {
      '/repos/acme/api/pulls': () => res([pr(1, 'agent-bot'), pr(2, 'agent-bot'), pr(3, 'agent-bot')]),
      '/repos/acme/api/pulls/1/reviews': () => res([]),
      '/repos/acme/api/pulls/2/reviews': () => res([]),
      '/repos/acme/api/pulls/3/reviews': () => res([]),
    };
    const narrowed = await callOpenPrs(routes, { body: { agent_view_id: 1, lane: 'changes', top: 1 } });
    expect(narrowed.r.body.pull_requests).toHaveLength(1);

    const widened = await callOpenPrs(routes, {
      body: { agent_view_id: 1, lane: 'changes', top: 999 },
      cfg: { ...CFG, poll_top: '2' },
    });
    expect(widened.r.body.pull_requests).toHaveLength(2);
  });

  it("isolates a failing repo and carries GitHub's message into the publisher log", async () => {
    // `errors[]` is read by the operator (run_lane logs each entry) and never enters a job, so the
    // provider's own words belong here — a bare status cannot distinguish a missing repo from a token
    // that may read but not write, which is the whole diagnostic.
    const routes = {
      '/repos/acme/a/pulls': () => res({ message: 'Resource not accessible by personal access token' }, { status: 403 }),
      '/repos/acme/b/pulls': () => res([pr(9, 'agent-bot')]),
      '/repos/acme/b/pulls/9/reviews': () => res([]),
    };
    const { r } = await callOpenPrs(routes, { cfg: { ...CFG, repo_allowlist: 'a,b' } });
    expect(r.statusCode).toBe(200);
    expect(r.body.pull_requests.map((p) => p.id)).toEqual([9]);
    expect(r.body.errors).toEqual([
      { repo: 'a', error: 'HTTP 403: Resource not accessible by personal access token' },
    ]);
    expect(JSON.stringify(r.body)).not.toContain('ghp_secret');
  });

  it('redacts the token from a repo error, even if GitHub echoes it', async () => {
    const routes = {
      '/repos/acme/api/pulls': () => res({ message: 'Bad credentials: ghp_secret' }, { status: 401 }),
    };
    const { r } = await callOpenPrs(routes);
    expect(r.body.errors[0].error).toContain('[redacted]');
    expect(JSON.stringify(r.body)).not.toContain('ghp_secret');
  });
});

// --- comments lane -----------------------------------------------------------------------------

const NEXT = '<https://api.github.com/x>; rel="next"';

function commentsRoutes(over = {}) {
  return {
    '/repos/acme/api/pulls': () => res([pr(7, 'agent-bot')]),
    '/repos/acme/api/issues/7/comments': () => res([
      { id: 101, user: { login: 'reviewer' }, created_at: '2026-01-02T00:00:00Z' },
    ]),
    '/repos/acme/api/pulls/7/comments': () => res([
      { id: 201, user: { login: 'reviewer' }, created_at: '2026-01-03T00:00:00Z' },
    ]),
    '/repos/acme/api/pulls/7/reviews': () => res([
      { id: 301, user: { login: 'reviewer' }, submitted_at: '2026-01-04T00:00:00Z', state: 'COMMENTED', body: 'please fix' },
      { id: 302, user: { login: 'reviewer' }, submitted_at: '2026-01-04T00:00:00Z', state: 'APPROVED', body: '  ' },
    ]),
    '/graphql': () => res({ data: { repository: { pullRequest: { reviewThreads: {
      pageInfo: { hasNextPage: false },
      nodes: [{ id: 't1', isResolved: true, comments: { pageInfo: { hasNextPage: false }, nodes: [{ fullDatabaseId: '201' }] } }],
    } } } } }),
    '/repos/acme/api/commits/sha7': () => res({ commit: { committer: { date: '2026-01-05T00:00:00Z' } } }),
    ...over,
  };
}

const commentsBody = { agent_view_id: 1, lane: 'comments' };

describe('POST /api/github/open-prs — comments lane', () => {
  it('merges the three feedback surfaces, dropping bodiless reviews', async () => {
    const { r } = await callOpenPrs(commentsRoutes(), { body: commentsBody });
    const [record] = r.body.pull_requests;
    expect(record.comments).toEqual([
      { id: 101, author_login: 'reviewer', created_at: '2026-01-02T00:00:00Z', surface: 'issue', resolved: false },
      { id: 201, author_login: 'reviewer', created_at: '2026-01-03T00:00:00Z', surface: 'review', resolved: true },
      { id: 301, author_login: 'reviewer', created_at: '2026-01-04T00:00:00Z', surface: 'review_body', resolved: false },
    ]);
    expect(record.comments.every((c) => c.id !== undefined)).toBe(true);
    expect(record.truncated).toBeUndefined();
  });

  it('does not select the deprecated databaseId — a removed field fails the WHOLE query', () => {
    // OV-2: GraphQL validates the entire selection set, so naming a removed field would take the query
    // down rather than fall back. `fullDatabaseId` is the only id this scan may ask for.
    expect(REVIEW_THREADS_QUERY).toContain('fullDatabaseId');
    expect(REVIEW_THREADS_QUERY).not.toMatch(/(?<!full)[Dd]atabaseId/);
  });

  it('resolves a thread from fullDatabaseId alone (a string BigInt)', async () => {
    // OV-2: `fullDatabaseId` arrives as a STRING and is the ONLY id field the query selects — this is
    // the shape a post-removal schema returns. It must still match the REST comment's numeric id.
    const routes = commentsRoutes({
      '/graphql': () => res({ data: { repository: { pullRequest: { reviewThreads: {
        pageInfo: { hasNextPage: false },
        nodes: [{
          id: 't1', isResolved: true,
          comments: { pageInfo: { hasNextPage: false }, nodes: [{ fullDatabaseId: '201' }] },
        }],
      } } } } }),
    });
    const { r } = await callOpenPrs(routes, { body: commentsBody });
    const inline = r.body.pull_requests[0].comments.find((c) => c.surface === 'review');
    expect(inline.resolved).toBe(true);
  });

  it('drops comments and reviews whose user is null (deleted accounts)', async () => {
    const routes = commentsRoutes({
      '/repos/acme/api/issues/7/comments': () => res([{ id: 101, user: null, created_at: 'x' }]),
      '/repos/acme/api/pulls/7/comments': () => res([{ id: 201, user: null, created_at: 'x' }]),
      '/repos/acme/api/pulls/7/reviews': () => res([{ id: 301, user: null, submitted_at: 'x', state: 'COMMENTED', body: 'hi' }]),
    });
    const { r } = await callOpenPrs(routes, { body: commentsBody });
    expect(r.body.pull_requests[0].comments).toEqual([]);
  });

  it.each([
    ['the GraphQL call fails', { '/graphql': () => res({ message: 'nope' }, { status: 502 }) }],
    ['there are more than 100 threads', { '/graphql': () => res({ data: { repository: { pullRequest: { reviewThreads: {
      pageInfo: { hasNextPage: true }, nodes: [],
    } } } } }) }],
    ['one thread has more than 100 comments', { '/graphql': () => res({ data: { repository: { pullRequest: { reviewThreads: {
      pageInfo: { hasNextPage: false },
      nodes: [{ id: 't1', isResolved: false, comments: { pageInfo: { hasNextPage: true }, nodes: [] } }],
    } } } } }) }],
  ])('marks review_comments incomplete when %s (unknown is not "unresolved")', async (_label, over) => {
    const { r } = await callOpenPrs(commentsRoutes(over), { body: commentsBody });
    const [record] = r.body.pull_requests;
    expect(record.truncated).toContain('review_comments');
    expect(r.body.errors.some((e) => e.error.includes('review_comments'))).toBe(true);
  });

  it('reads the watermark from the head commit, never from the commits list', async () => {
    const { r, fetchSpy } = await callOpenPrs(commentsRoutes(), { body: commentsBody });
    expect(r.body.pull_requests[0].commits).toEqual([{ date: '2026-01-05T00:00:00Z' }]);
    expect(fetchSpy.mock.calls.some(([u]) => new URL(u).pathname === '/repos/acme/api/commits')).toBe(false);
  });

  it.each([
    ['the list payload carries no head sha', { '/repos/acme/api/pulls': () => res([pr(7, 'agent-bot', { head: {} })]) }],
    ['the commit fetch fails', { '/repos/acme/api/commits/sha7': () => res({}, { status: 500 }) }],
    ['the commit carries no timestamp', { '/repos/acme/api/commits/sha7': () => res({ commit: {} }) }],
  ])('blocks the comments lane instead of falling back when %s', async (_label, over) => {
    const { r } = await callOpenPrs(commentsRoutes(over), { body: commentsBody });
    const [record] = r.body.pull_requests;
    expect(record.commits).toEqual([]);
    expect(record.truncated).toContain('head_commit');
    expect(r.body.errors.some((e) => e.error.includes('head_commit'))).toBe(true);
  });

  it('reports a capped scan as truncated instead of returning it as complete', async () => {
    const routes = commentsRoutes({
      '/repos/acme/api/issues/7/comments': () => res(
        [{ id: 101, user: { login: 'reviewer' }, created_at: '2026-01-02T00:00:00Z' }], { link: NEXT },
      ),
    });
    const { r, fetchSpy } = await callOpenPrs(routes, { body: commentsBody });
    const [record] = r.body.pull_requests;
    expect(record.truncated).toContain('issue_comments');
    expect(r.body.errors.some((e) => e.error.includes('issue_comments'))).toBe(true);
    // maxPages = 5 for a comment scan: the cap is what stops it, and it stops exactly there.
    expect(fetchSpy.mock.calls.filter(([u]) => new URL(u).pathname === '/repos/acme/api/issues/7/comments'))
      .toHaveLength(5);
  });
});

describe('POST /api/github/open-prs — changes lane', () => {
  it('emits the whole non-agent review history with id/state, dropping the agent\'s own reviews', async () => {
    const routes = {
      '/repos/acme/api/pulls': () => res([pr(7, 'agent-bot')]),
      '/repos/acme/api/pulls/7/reviews': () => res([
        { id: 1, user: { login: 'reviewer' }, submitted_at: '2026-01-01T00:00:00Z', state: 'CHANGES_REQUESTED' },
        { id: 2, user: { login: 'Agent-Bot' }, submitted_at: '2026-01-02T00:00:00Z', state: 'COMMENTED' },
        { id: 3, user: { login: 'reviewer' }, submitted_at: '2026-01-03T00:00:00Z', state: 'APPROVED' },
        { id: 4, user: null, submitted_at: '2026-01-04T00:00:00Z', state: 'DISMISSED' },
      ]),
    };
    const { r } = await callOpenPrs(routes);
    expect(r.body.pull_requests[0].reviews).toEqual([
      { id: 1, user_login: 'reviewer', date: '2026-01-01T00:00:00Z', state: 'CHANGES_REQUESTED' },
      { id: 3, user_login: 'reviewer', date: '2026-01-03T00:00:00Z', state: 'APPROVED' },
    ]);
  });

  it('marks the reviews scan truncated when it hits the page cap', async () => {
    const routes = {
      '/repos/acme/api/pulls': () => res([pr(7, 'agent-bot')]),
      '/repos/acme/api/pulls/7/reviews': () => res(
        [{ id: 1, user: { login: 'reviewer' }, submitted_at: '2026-01-01T00:00:00Z', state: 'COMMENTED' }],
        { link: NEXT },
      ),
    };
    const { r } = await callOpenPrs(routes);
    expect(r.body.pull_requests[0].truncated).toContain('reviews');
    expect(r.body.errors.some((e) => e.error.includes('reviews'))).toBe(true);
  });
});
