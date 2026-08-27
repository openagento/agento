import { describe, it, expect } from 'vitest';
import { probe, required } from '../probes/http.js';

function fetchStub(impl, seen = {}) {
  return {
    fetch: async (url, opts) => {
      seen.url = url;
      seen.opts = opts;
      return impl(url, opts);
    },
  };
}

const OK = () => ({ status: 200, statusText: 'OK' });

describe('http probe', () => {
  it('declares url as its only required field', () => {
    expect(required).toEqual(['url']);
  });

  it('reports ok on the expected status', async () => {
    const seen = {};
    const out = await probe(
      { url: 'https://x.atlassian.net/rest/api/2/myself', basic: ['u@x', 'tok'], expect: 200 },
      { timeoutMs: 1000, deps: fetchStub(OK, seen) },
    );
    expect(out.status).toBe('ok');
    expect(seen.opts.method).toBe('GET');
    expect(seen.opts.headers.Authorization).toBe(`Basic ${Buffer.from('u@x:tok').toString('base64')}`);
  });

  it('never follows a redirect', async () => {
    // A 30x to another host would re-send the Authorization header there.
    // `redirect: 'manual'` is the whole defence and it must be asserted.
    const seen = {};
    await probe({ url: 'https://x/y' }, { timeoutMs: 1000, deps: fetchStub(OK, seen) });
    expect(seen.opts.redirect).toBe('manual');
  });

  it('treats a redirect as a failure rather than following it', async () => {
    const out = await probe({ url: 'https://x/y' }, {
      timeoutMs: 1000, deps: fetchStub(async () => ({ status: 302, statusText: 'Found' })),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('HTTP_302');
  });

  it('defaults to expecting 200', async () => {
    const out = await probe({ url: 'https://x/y' }, {
      timeoutMs: 1000, deps: fetchStub(async () => ({ status: 204, statusText: 'No Content' })),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('HTTP_204');
  });

  it('reports a wrong credential as fail with the HTTP status in the code', async () => {
    const out = await probe({ url: 'https://x/y', basic: ['u', 'bad'] }, {
      timeoutMs: 1000, deps: fetchStub(async () => ({ status: 401, statusText: 'Unauthorized' })),
    });
    expect(out.status).toBe('fail');
    expect(out.code).toBe('HTTP_401');
  });

  it('reports a transport failure as error, not fail', async () => {
    // Nothing was authenticated, so this is not a verdict on the credential.
    const out = await probe({ url: 'https://x/y' }, {
      timeoutMs: 1000,
      deps: fetchStub(async () => { throw new TypeError('fetch failed'); }),
    });
    expect(out.status).toBe('error');
    expect(out.code).toBe('UNREACHABLE');
  });

  it('refuses a non-http scheme', async () => {
    const out = await probe({ url: 'file:///etc/passwd' }, { timeoutMs: 1000, deps: fetchStub(OK) });
    expect(out.status).toBe('error');
    expect(out.code).toBe('BAD_URL');
  });

  it('refuses a url with embedded credentials', async () => {
    const out = await probe({ url: 'https://u:p@x/y' }, { timeoutMs: 1000, deps: fetchStub(OK) });
    expect(out.status).toBe('error');
    expect(out.code).toBe('BAD_URL');
  });

  it('refuses an unparsable url', async () => {
    const out = await probe({ url: 'not a url' }, { timeoutMs: 1000, deps: fetchStub(OK) });
    expect(out.status).toBe('error');
    expect(out.code).toBe('BAD_URL');
  });

  it('reports not_configured when the interpolated url lost its host', async () => {
    // `{jira/jira_host}/rest/...` with an unset host interpolates to
    // "/rest/..." — a relative URL. That is an unconfigured integration, not a
    // broken one. (The resolver catches an empty *required* field first; this
    // covers a host that is set to something blank-ish.)
    const out = await probe({ url: '/rest/api/2/myself' }, { timeoutMs: 1000, deps: fetchStub(OK) });
    expect(out.status).toBe('not_configured');
  });

  it('passes extra headers through', async () => {
    const seen = {};
    await probe(
      { url: 'https://x/y', headers: { Accept: 'application/json' } },
      { timeoutMs: 1000, deps: fetchStub(OK, seen) },
    );
    expect(seen.opts.headers.Accept).toBe('application/json');
  });
});
