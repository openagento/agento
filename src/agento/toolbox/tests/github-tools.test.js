import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, it, expect, vi, afterEach } from 'vitest';
import { z } from 'zod';

import { register } from '../../modules/github/toolbox/github.js';
import { VIEW_SCOPED_ENV_KEYS } from '../../modules/github/toolbox/env-guard.js';
import { createGitHubAuth } from '../../modules/github/toolbox/github-auth.js';

const MODULE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../modules/github');
const DECLARED_TOOLS = JSON.parse(fs.readFileSync(path.join(MODULE_DIR, 'module.json'), 'utf8'))
  .tools.map((t) => t.name).sort();

function makeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

function res(body, { status = 200, link = null, text = null } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'link' ? link : null) },
    json: async () => body,
    text: async () => (text === null ? '' : text),
  };
}

// Dispatch on URL pathname, through the REAL createGitHubAuth — so every URL assertion is about the
// URL the module actually builds, and the owner guard inside the auth layer is exercised too.
function makeFetch(routes) {
  return vi.fn(async (url, opts) => {
    const u = new URL(url);
    const handler = routes[u.pathname];
    if (!handler) return res([]);
    return handler(u, opts);
  });
}

const CFG = {
  github_owner: 'acme',
  github_login: 'agent-bot',
  github_token: 'ghp_secret',
  repo_allowlist: 'api,web',
};

function ctx(fetchSpy, overrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: { github: CFG },
    isToolEnabled: () => true,
    githubAuthFactory: (cfg) => createGitHubAuth(cfg, { fetch: fetchSpy, sleep: async () => {} }),
    ...overrides,
  };
}

// Registers every tool against `routes` and returns { tools, fetchSpy, log }.
function setup(routes = {}, overrides = {}) {
  const fetchSpy = makeFetch(routes);
  const s = makeServer();
  const c = ctx(fetchSpy, overrides);
  register(s, c);
  return { tools: s.tools, fetchSpy, log: c.log };
}

const OPEN_PR = { number: 7, state: 'open', head: { sha: 'headsha' } };
const openPrRoute = { '/repos/acme/api/pulls/7': () => res(OPEN_PR) };
const writeBodies = (fetchSpy) => fetchSpy.mock.calls
  .filter(([, o]) => o && o.method && o.method !== 'GET')
  .map(([u, o]) => ({ url: new URL(u), body: JSON.parse(o.body) }));

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('registration: opt-in, names, session isolation', () => {
  it('registers exactly the tools declared in module.json', () => {
    const { tools } = setup();
    expect(Object.keys(tools).sort()).toEqual(DECLARED_TOOLS);
    expect(DECLARED_TOOLS).toHaveLength(8);
  });

  it('registers nothing when no tool is enabled, and only what the predicate allows', () => {
    expect(Object.keys(setup({}, { isToolEnabled: () => false }).tools)).toEqual([]);
    const only = setup({}, { isToolEnabled: (n) => n === 'github_get_pr' }).tools;
    expect(Object.keys(only)).toEqual(['github_get_pr']);
  });

  it('holds no resolved value at module scope — two sessions keep their own owner and allow-list', async () => {
    const fetchA = makeFetch({ '/repos/acme/api/pulls/7': () => res(OPEN_PR) });
    const fetchB = makeFetch({ '/repos/other/lib/pulls/7': () => res(OPEN_PR) });
    const a = makeServer();
    const b = makeServer();
    register(a, ctx(fetchA));
    register(b, ctx(fetchB, {
      moduleConfigs: { github: { ...CFG, github_owner: 'other', repo_allowlist: 'lib' } },
    }));

    await a.tools.github_get_pr.handler({ repo: 'api', pr_number: 7 });
    await b.tools.github_get_pr.handler({ repo: 'lib', pr_number: 7 });
    expect(fetchA.mock.calls[0][0]).toBe('https://api.github.com/repos/acme/api/pulls/7');
    expect(fetchB.mock.calls[0][0]).toBe('https://api.github.com/repos/other/lib/pulls/7');
    // Each session enforces ITS OWN allow-list, in both directions.
    expect((await a.tools.github_get_pr.handler({ repo: 'lib', pr_number: 7 })).isError).toBe(true);
    expect((await b.tools.github_get_pr.handler({ repo: 'api', pr_number: 7 })).isError).toBe(true);
  });
});

describe.each(VIEW_SCOPED_ENV_KEYS)('ENV guard at registration — %s', (key) => {
  it('registers zero tools and logs one WARN naming the offending key', () => {
    vi.stubEnv(key, 'x');
    const { tools, log } = setup();
    expect(Object.keys(tools)).toEqual([]);
    const warns = log.mock.calls.filter(([, level]) => level === 'WARN');
    expect(warns).toHaveLength(1);
    expect(warns[0][2]).toContain(key);
  });
});

describe('every tool is bounded by config, not by its arguments', () => {
  const repoArgs = {
    github_get_pr: { pr_number: 7 },
    github_get_pr_diff: { pr_number: 7 },
    github_get_pr_comments: { pr_number: 7 },
    github_get_pr_reviews: { pr_number: 7 },
    github_add_comment: { pr_number: 7, content: 'hi' },
    github_resolve_thread: { pr_number: 7, comment_id: 1 },
    github_set_review: { pr_number: 7, decision: 'approve' },
    github_create_pr: { title: 't', head_branch: 'h', base_branch: 'main' },
  };

  it('declares no `owner` parameter anywhere — the owner is config-only', () => {
    const { tools } = setup();
    for (const name of DECLARED_TOOLS) expect(tools[name].schema).not.toHaveProperty('owner');
  });

  it.each(Object.entries(repoArgs))('%s rejects a repo outside the allow-list without fetching', async (name, args) => {
    const { tools, fetchSpy } = setup();
    const out = await tools[name].handler({ repo: 'forbidden', ...args });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('not in the allow-list');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each(Object.entries(repoArgs))('%s reports "not configured" when the scope has no token', async (name, args) => {
    const { tools, fetchSpy } = setup({}, { moduleConfigs: { github: { ...CFG, github_token: '' } } });
    const out = await tools[name].handler({ repo: 'api', ...args });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toMatch(/not configured/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('ignores an extra `owner` argument and still targets the configured owner', async () => {
    const { tools, fetchSpy } = setup(openPrRoute);
    await tools.github_get_pr.handler({ repo: 'api', pr_number: 7, owner: 'evil' });
    expect(fetchSpy.mock.calls[0][0]).toBe('https://api.github.com/repos/acme/api/pulls/7');
  });
});

describe('write tools require an OPEN pull request', () => {
  const calls = {
    github_add_comment: { repo: 'api', pr_number: 7, content: 'hi' },
    github_resolve_thread: { repo: 'api', pr_number: 7, comment_id: 1 },
    github_set_review: { repo: 'api', pr_number: 7, decision: 'approve' },
  };

  it.each(Object.entries(calls).flatMap(([name]) => [
    [name, 'closed', { state: 'closed' }],
    [name, 'merged', { state: 'closed', merged: true }],
  ]))('%s refuses a %s PR and issues no write', async (name, _label, prState) => {
    const { tools, fetchSpy } = setup({ '/repos/acme/api/pulls/7': () => res({ ...OPEN_PR, ...prState }) });
    const out = await tools[name].handler(calls[name]);
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toMatch(/not open/);
    expect(writeBodies(fetchSpy)).toEqual([]);
  });
});

describe('github_get_pr_diff', () => {
  it('asks for the diff media type and returns the raw text', async () => {
    const { tools, fetchSpy } = setup({
      '/repos/acme/api/pulls/7': () => res(null, { text: 'diff --git a b' }),
    });
    const out = await tools.github_get_pr_diff.handler({ repo: 'api', pr_number: 7 });
    expect(out.content[0].text).toBe('diff --git a b');
    expect(fetchSpy.mock.calls[0][1].headers.Accept).toBe('application/vnd.github.diff');
  });

  it('turns a 406 into an actionable "too large" message', async () => {
    const { tools } = setup({ '/repos/acme/api/pulls/7': () => res({}, { status: 406 }) });
    const out = await tools.github_get_pr_diff.handler({ repo: 'api', pr_number: 7 });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toMatch(/too large/);
  });
});

describe('github_get_pr_comments / github_get_pr_reviews read the WHOLE collection', () => {
  const NEXT = '<https://api.github.com/x>; rel="next"';

  it('merges both comment surfaces and names only the surface that hit the cap', async () => {
    const { tools, fetchSpy } = setup({
      '/repos/acme/api/issues/7/comments': () => res([{ id: 101, body: 'conversation' }]),
      // A full page that still advertises `rel="next"` on every page: the scan runs to the cap (5).
      '/repos/acme/api/pulls/7/comments': (u) => res(
        [{ id: 200 + Number(u.searchParams.get('page')), body: 'inline' }], { link: NEXT },
      ),
    });
    const out = await tools.github_get_pr_comments.handler({ repo: 'api', pr_number: 7 });
    const data = JSON.parse(out.content[0].text);
    expect(data.comments.map((c) => [c.id, c.surface])).toEqual([
      [101, 'conversation'], [201, 'inline'], [202, 'inline'], [203, 'inline'], [204, 'inline'], [205, 'inline'],
    ]);
    expect(data.truncated).toEqual(['inline']);
    const paths = new Set(fetchSpy.mock.calls.map(([u]) => new URL(u).pathname));
    expect(paths.has('/repos/acme/api/issues/7/comments')).toBe(true);
    expect(paths.has('/repos/acme/api/pulls/7/comments')).toBe(true);
  });

  it('pages the review list and reports truncation at the cap', async () => {
    const { tools, fetchSpy } = setup({
      '/repos/acme/api/pulls/7/reviews': () => res([{ id: 1, state: 'COMMENTED' }], { link: NEXT }),
    });
    const out = await tools.github_get_pr_reviews.handler({ repo: 'api', pr_number: 7 });
    expect(JSON.parse(out.content[0].text).truncated).toBe(true);
    expect(fetchSpy).toHaveBeenCalledTimes(5);
  });
});

describe('github_add_comment', () => {
  const routes = { ...openPrRoute, '/repos/acme/api/issues/7/comments': () => res({ id: 1 }) };

  it('posts a conversation comment when neither inline nor in_reply_to is given', async () => {
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_add_comment.handler({ repo: 'api', pr_number: 7, content: 'hi' });
    expect(out.isError).toBeUndefined();
    const [write] = writeBodies(fetchSpy);
    expect(write.url.pathname).toBe('/repos/acme/api/issues/7/comments');
    expect(write.body).toEqual({ body: 'hi' });
  });

  it('anchors an inline comment to the PR head sha it already fetched', async () => {
    const { tools, fetchSpy } = setup({ ...routes, '/repos/acme/api/pulls/7/comments': () => res({ id: 2 }) });
    await tools.github_add_comment.handler({
      repo: 'api', pr_number: 7, content: 'fix', inline: { path: 'a.js', line: 3, side: 'RIGHT' },
    });
    const [write] = writeBodies(fetchSpy);
    expect(write.url.pathname).toBe('/repos/acme/api/pulls/7/comments');
    expect(write.body).toEqual({ body: 'fix', path: 'a.js', line: 3, commit_id: 'headsha', side: 'RIGHT' });
  });

  it('replies inside a thread when in_reply_to is given', async () => {
    const { tools, fetchSpy } = setup({ ...routes, '/repos/acme/api/pulls/7/comments': () => res({ id: 3 }) });
    await tools.github_add_comment.handler({ repo: 'api', pr_number: 7, content: 'ack', in_reply_to: 201 });
    const [write] = writeBodies(fetchSpy);
    expect(write.url.pathname).toBe('/repos/acme/api/pulls/7/comments');
    expect(write.body).toEqual({ body: 'ack', in_reply_to: 201 });
  });

  it('refuses inline + in_reply_to together, before even the open-state GET', async () => {
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_add_comment.handler({
      repo: 'api', pr_number: 7, content: 'x', in_reply_to: 201, inline: { path: 'a.js', line: 3 },
    });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('inline');
    expect(out.content[0].text).toContain('in_reply_to');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('rejects an inline comment with no line, without any fetch', async () => {
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_add_comment.handler({
      repo: 'api', pr_number: 7, content: 'x', inline: { path: 'a.js' },
    });
    expect(out.isError).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
    // The registered SHAPE carries the same requirement, so the SDK rejects it before the handler too.
    expect(z.object(tools.github_add_comment.schema)
      .safeParse({ repo: 'api', pr_number: 7, content: 'x', inline: { path: 'a.js' } }).success).toBe(false);
  });
});

describe('github_set_review', () => {
  it.each([
    ['approve', 'APPROVE'],
    ['request_changes', 'REQUEST_CHANGES'],
    ['comment', 'COMMENT'],
  ])('maps %s to the %s event', async (decision, event) => {
    const { tools, fetchSpy } = setup({ ...openPrRoute, '/repos/acme/api/pulls/7/reviews': () => res({ id: 1 }) });
    const out = await tools.github_set_review.handler({ repo: 'api', pr_number: 7, decision, body: 'because' });
    expect(out.isError).toBeUndefined();
    expect(writeBodies(fetchSpy)[0].body).toEqual({ event, body: 'because' });
  });

  it.each(['request_changes', 'comment'])('rejects %s without a body, client-side', async (decision) => {
    const { tools, fetchSpy } = setup(openPrRoute);
    const out = await tools.github_set_review.handler({ repo: 'api', pr_number: 7, decision });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toMatch(/requires a non-empty body/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('reports a 422 generically, naming self-review as ONE possible cause', async () => {
    const { tools } = setup({
      ...openPrRoute,
      '/repos/acme/api/pulls/7/reviews': () => res({ message: 'Unprocessable' }, { status: 422 }),
    });
    const out = await tools.github_set_review.handler({ repo: 'api', pr_number: 7, decision: 'approve' });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('422 validation failed');
    expect(out.content[0].text).toContain('one common cause');
  });
});

describe('github_resolve_thread', () => {
  const graphqlRoutes = (queryResult) => ({
    ...openPrRoute,
    '/graphql': (_u, opts) => (JSON.parse(opts.body).query.includes('mutation')
      ? res({ data: { resolveReviewThread: { thread: { id: 't1', isResolved: true } } } })
      : res({ data: queryResult })),
  });

  const threads = (nodes, hasNextPage = false) => ({
    repository: { pullRequest: { reviewThreads: { pageInfo: { hasNextPage }, nodes } } },
  });
  const thread = (id, ids, commentsHasNext = false) => ({
    id, isResolved: false, comments: { pageInfo: { hasNextPage: commentsHasNext }, nodes: ids.map((d) => ({ fullDatabaseId: String(d) })) },
  });

  it('resolves the thread containing the comment', async () => {
    const { tools, fetchSpy } = setup(graphqlRoutes(threads([thread('t0', [999]), thread('t1', [201])])));
    const out = await tools.github_resolve_thread.handler({ repo: 'api', pr_number: 7, comment_id: '201' });
    expect(out.isError).toBeUndefined();
    const mutation = fetchSpy.mock.calls.map(([, o]) => o).filter((o) => o.body && JSON.parse(o.body).query?.includes('mutation'));
    expect(JSON.parse(mutation[0].body).variables).toEqual({ threadId: 't1' });
  });

  it('matches a comment id above 2^53 exactly, via fullDatabaseId', async () => {
    // `databaseId` (Int) is deprecated in favour of `fullDatabaseId` (BigInt, a STRING). Here the id
    // never passes through a JS number: the GraphQL string is compared to the tool argument's string,
    // so two ids that differ only past the double's precision must NOT be treated as the same thread.
    const big = '9007199254740993';
    const near = '9007199254740992';
    const bigThread = (id, full) => ({
      id, isResolved: false, comments: { pageInfo: { hasNextPage: false }, nodes: [{ fullDatabaseId: full }] },
    });
    const { tools, fetchSpy } = setup(graphqlRoutes(threads([bigThread('t0', near), bigThread('t1', big)])));
    const out = await tools.github_resolve_thread.handler({ repo: 'api', pr_number: 7, comment_id: big });
    expect(out.isError).toBeUndefined();
    const mutation = fetchSpy.mock.calls.map(([, o]) => o).filter((o) => o.body && JSON.parse(o.body).query?.includes('mutation'));
    expect(JSON.parse(mutation[0].body).variables).toEqual({ threadId: 't1' });
  });

  it('reports a definitive miss when the result is COMPLETE, and issues no mutation', async () => {
    const { tools, fetchSpy } = setup(graphqlRoutes(threads([thread('t1', [201])])));
    const out = await tools.github_resolve_thread.handler({ repo: 'api', pr_number: 7, comment_id: 999 });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('no review thread contains comment 999');
    expect(writeBodies(fetchSpy).filter((w) => w.body.query?.includes('mutation'))).toEqual([]);
  });

  it.each([
    ['more threads than one query returns', () => threads([thread('t1', [201])], true)],
    ['more comments in a thread than one query returns', () => threads([thread('t1', [201], true)])],
  ])('never claims a definitive miss when there are %s', async (_label, build) => {
    const { tools, fetchSpy } = setup(graphqlRoutes(build()));
    const out = await tools.github_resolve_thread.handler({ repo: 'api', pr_number: 7, comment_id: 999 });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('could not determine the thread for comment 999');
    expect(out.content[0].text).toContain('Nothing was resolved.');
    expect(writeBodies(fetchSpy).filter((w) => w.body.query?.includes('mutation'))).toEqual([]);
  });
});

describe('github_create_pr', () => {
  const routes = { '/repos/acme/api/pulls': () => res({ number: 12, html_url: 'https://github.com/acme/api/pull/12' }) };

  it('posts title/head/base/body with head = branch for a same-owner branch', async () => {
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_create_pr.handler({
      repo: 'api', title: 't', head_branch: 'feat', base_branch: 'main', body: 'why',
    });
    expect(out.content[0].text).toContain('#12');
    expect(writeBodies(fetchSpy)[0].body).toEqual({ title: 't', head: 'feat', base: 'main', body: 'why' });
  });

  it('qualifies head with the owner when head_owner is the configured owner', async () => {
    const { tools, fetchSpy } = setup(routes);
    await tools.github_create_pr.handler({
      repo: 'api', title: 't', head_branch: 'feat', base_branch: 'main', head_owner: 'acme',
    });
    expect(writeBodies(fetchSpy)[0].body.head).toBe('acme:feat');
  });

  it('refuses a cross-owner fork before any fetch', async () => {
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_create_pr.handler({
      repo: 'api', title: 't', head_branch: 'feat', base_branch: 'main', head_owner: 'attacker',
    });
    expect(out.isError).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('refuses an owner smuggled through head_branch, before any fetch', async () => {
    // The cross-repo head has TWO spellings — `head_owner` and the "owner:branch" form of the branch
    // itself. Validating only the first would leave the second wide open.
    const { tools, fetchSpy } = setup(routes);
    const out = await tools.github_create_pr.handler({
      repo: 'api', title: 't', head_branch: 'attacker:feat', base_branch: 'main',
    });
    expect(out.isError).toBe(true);
    expect(out.content[0].text).toContain('bare branch name');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('requires base_branch in the registered schema — GitHub has no default', () => {
    const { tools } = setup(routes);
    const schema = z.object(tools.github_create_pr.schema);
    expect(schema.safeParse({ repo: 'api', title: 't', head_branch: 'feat' }).success).toBe(false);
    expect(schema.safeParse({ repo: 'api', title: 't', head_branch: 'feat', base_branch: 'main' }).success).toBe(true);
  });
});

describe('no error path leaks the token', () => {
  it.each([
    ['github_get_pr', { repo: 'api', pr_number: 7 }],
    ['github_get_pr_diff', { repo: 'api', pr_number: 7 }],
    ['github_get_pr_comments', { repo: 'api', pr_number: 7 }],
    ['github_get_pr_reviews', { repo: 'api', pr_number: 7 }],
    ['github_add_comment', { repo: 'api', pr_number: 7, content: 'hi' }],
    ['github_resolve_thread', { repo: 'api', pr_number: 7, comment_id: 1 }],
    ['github_set_review', { repo: 'api', pr_number: 7, decision: 'approve' }],
    ['github_create_pr', { repo: 'api', title: 't', head_branch: 'f', base_branch: 'main' }],
  ])('%s', async (name, args) => {
    // Every route fails, so each tool takes its error path.
    const { tools, log } = setup(new Proxy({}, {
      get: () => () => res({ message: 'boom ghp_secret' }, { status: 500 }),
      has: () => true,
    }));
    const out = await tools[name].handler(args);
    expect(out.isError).toBe(true);
    expect(JSON.stringify(out)).not.toContain('ghp_secret');
    expect(JSON.stringify(log.mock.calls)).not.toContain('ghp_secret');
  });
});
