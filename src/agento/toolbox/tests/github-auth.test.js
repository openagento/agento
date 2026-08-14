import { describe, it, expect, vi } from 'vitest';
import { createGitHubAuth, linkNext } from '../../modules/github/toolbox/github-auth.js';

const CFG = { github_owner: 'acme', github_token: 'ghp_secret', github_login: 'agent-bot' };
const okRes = (body = {}, headers = {}) => ({
  ok: true, status: 200,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  json: async () => body, text: async () => JSON.stringify(body),
});
const errRes = (status, headers = {}) => ({
  ok: false, status,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  json: async () => ({}), text: async () => '',
});

describe('createGitHubAuth', () => {
  it('is not configured without an owner or a token', () => {
    expect(createGitHubAuth({}).isConfigured()).toBe(false);
    expect(createGitHubAuth({ github_owner: 'acme' }).isConfigured()).toBe(false);
    expect(createGitHubAuth(CFG).isConfigured()).toBe(true);
  });

  it('sends a Bearer token, the API version header and a JSON Accept', async () => {
    const fetch = vi.fn(async () => okRes());
    await createGitHubAuth(CFG, { fetch }).ghFetch(['user']);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('https://api.github.com/user');
    expect(opts.headers.Authorization).toBe('Bearer ghp_secret');
    expect(opts.headers['X-GitHub-Api-Version']).toBe('2022-11-28');
    expect(opts.headers.Accept).toBe('application/vnd.github+json');
  });

  it('ignores any attempt to redirect the API host through config', async () => {
    const fetch = vi.fn(async () => okRes());
    await createGitHubAuth({ ...CFG, github_api_base: 'https://evil.test' }, { fetch }).ghFetch(['user']);
    expect(fetch.mock.calls[0][0]).toBe('https://api.github.com/user');
  });

  it('rejects absolute URLs smuggled through a path segment', async () => {
    const auth = createGitHubAuth(CFG, { fetch: vi.fn() });
    await expect(auth.ghFetch(['https://evil.test/x'])).rejects.toThrow(/absolute URLs/);
    await expect(auth.ghFetch([])).rejects.toThrow(/non-empty/);
  });

  it('rejects a repos path whose owner differs from the configured owner', async () => {
    const auth = createGitHubAuth(CFG, { fetch: vi.fn() });
    await expect(auth.ghFetch(['repos', 'evil', 'api', 'pulls'])).rejects.toThrow(/owner/);
  });

  it('encodes path segments', async () => {
    const fetch = vi.fn(async () => okRes());
    await createGitHubAuth(CFG, { fetch }).ghFetch(['repos', 'acme', 'a b', 'pulls']);
    expect(fetch.mock.calls[0][0]).toBe('https://api.github.com/repos/acme/a%20b/pulls');
  });

  it('retries a 429 for any method and honours retry-after', async () => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn()
      .mockResolvedValueOnce(errRes(429, { 'retry-after': '2' }))
      .mockResolvedValueOnce(okRes());
    const res = await createGitHubAuth(CFG, { fetch, sleep }).ghFetch(['user'], { method: 'POST' });
    expect(res.ok).toBe(true);
    expect(sleep).toHaveBeenCalledWith(2000);
  });

  it('retries a 403 that is a rate limit (retry-after OR x-ratelimit-remaining: 0), not a plain 403', async () => {
    const sleep = vi.fn(async () => {});
    // primary limit
    const fetch1 = vi.fn()
      .mockResolvedValueOnce(errRes(403, { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1' }))
      .mockResolvedValueOnce(okRes());
    expect((await createGitHubAuth(CFG, { fetch: fetch1, sleep }).ghFetch(['user'])).ok).toBe(true);

    // secondary limit: 403 + retry-after, no x-ratelimit-remaining header at all
    const fetch2 = vi.fn()
      .mockResolvedValueOnce(errRes(403, { 'retry-after': '3' }))
      .mockResolvedValueOnce(okRes());
    expect((await createGitHubAuth(CFG, { fetch: fetch2, sleep }).ghFetch(['user'])).ok).toBe(true);
    expect(sleep).toHaveBeenLastCalledWith(3000);

    const fetch3 = vi.fn(async () => errRes(403)); // plain 403 = forbidden, NOT a rate limit
    const res = await createGitHubAuth(CFG, { fetch: fetch3, sleep }).ghFetch(['user']);
    expect(res.status).toBe(403);
    expect(fetch3).toHaveBeenCalledTimes(1);
  });

  it('gives up rather than retrying earlier than GitHub instructed', async () => {
    // GitHub: "you should not retry your request until after that many seconds has elapsed".
    // A wait longer than the cap means this poll is over — the next cron tick retries.
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn(async () => errRes(429, { 'retry-after': '600' }));
    const res = await createGitHubAuth(CFG, { fetch, sleep }).ghFetch(['user']);
    expect(res.status).toBe(429);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it('gives up on an UNINSTRUCTED rate limit rather than holding the poll for GitHub\'s minimum minute', async () => {
    // No retry-after and no x-ratelimit headers ⇒ GitHub's documented floor is one minute, which exceeds
    // RATE_LIMIT_MAX_WAIT_MS. Waiting less is what gets an integration banned; waiting a minute outlives
    // the publisher's 60s read timeout. So the only correct move is to return and let the next tick retry.
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn(async () => errRes(429));
    const res = await createGitHubAuth(CFG, { fetch, sleep }).ghFetch(['user']);
    expect(sleep).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(res.status).toBe(429);
  });

  it('honours an instructed wait that fits inside the cap, once only', async () => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn()
      .mockResolvedValueOnce(errRes(429, { 'retry-after': '10' }))
      .mockResolvedValueOnce(errRes(429, { 'retry-after': '10' }));
    const res = await createGitHubAuth(CFG, { fetch, sleep }).ghFetch(['user']);
    expect(sleep).toHaveBeenCalledTimes(1);
    expect(sleep).toHaveBeenCalledWith(10000);  // exactly as instructed, never less
    expect(fetch).toHaveBeenCalledTimes(2);     // MAX_RATE_LIMIT_RETRIES = 1
    expect(res.status).toBe(429);
  });

  it('retries a 5xx for GET only, never for a mutating POST', async () => {
    const sleep = vi.fn(async () => {});
    const getFetch = vi.fn().mockResolvedValueOnce(errRes(502)).mockResolvedValueOnce(okRes());
    expect((await createGitHubAuth(CFG, { fetch: getFetch, sleep }).ghFetch(['user'])).ok).toBe(true);

    const postFetch = vi.fn(async () => errRes(502));
    const res = await createGitHubAuth(CFG, { fetch: postFetch, sleep }).ghFetch(['user'], { method: 'POST' });
    expect(res.status).toBe(502);
    expect(postFetch).toHaveBeenCalledTimes(1);
  });

  it('gives up after MAX_RETRIES on a retryable 5xx GET', async () => {
    const sleep = vi.fn(async () => {});
    const fetch = vi.fn(async () => errRes(503));
    const res = await createGitHubAuth(CFG, { fetch, sleep }).ghFetch(['user']);
    expect(res.status).toBe(503);
    expect(fetch).toHaveBeenCalledTimes(4); // initial + MAX_RETRIES(3)
  });

  it('ghGraphql posts to the graphql endpoint and throws on GraphQL errors', async () => {
    const fetch = vi.fn(async () => okRes({ data: { ok: 1 } }));
    const auth = createGitHubAuth(CFG, { fetch });
    expect(await auth.ghGraphql('query{x}', { a: 1 })).toEqual({ ok: 1 });
    expect(fetch.mock.calls[0][0]).toBe('https://api.github.com/graphql');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ query: 'query{x}', variables: { a: 1 } });

    const errFetch = vi.fn(async () => okRes({ errors: [{ message: 'nope' }] }));
    await expect(createGitHubAuth(CFG, { fetch: errFetch }).ghGraphql('q', {})).rejects.toThrow(/GraphQL/);
  });

  it('never echoes the token in an error message', async () => {
    const fetch = vi.fn(async () => { throw new Error('network down'); });
    await expect(createGitHubAuth(CFG, { fetch }).ghFetch(['user'])).rejects.not.toThrow(/ghp_secret/);
  });
});

describe('linkNext', () => {
  it('detects rel="next"', () => {
    expect(linkNext('<https://api.github.com/x?page=2>; rel="next", <...>; rel="last"')).toBe(true);
    expect(linkNext('<https://api.github.com/x?page=1>; rel="prev"')).toBe(false);
    expect(linkNext(null)).toBe(false);
  });
});
