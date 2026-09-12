import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { createJiraProxyHandler } from '../../modules/jira/toolbox/jira-proxy.js';

describe('createJiraProxyHandler', () => {
  const log = vi.fn();
  const validConfig = {
    host: 'https://test.atlassian.net',
    user: 'u@test.com',
    token: 'tok123',
  };

  function mockReqRes(body = {}) {
    const req = { body };
    const res = { status: vi.fn().mockReturnThis(), json: vi.fn() };
    return { req, res };
  }

  beforeEach(() => {
    log.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('rejects missing method/path', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    const { req, res } = mockReqRes({});
    await handler(req, res);

    expect(res.status).toHaveBeenCalledWith(400);
    expect(res.json).toHaveBeenCalledWith(
      expect.objectContaining({ error: expect.stringContaining('method and path are required') }),
    );
  });

  it('rejects invalid HTTP method', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    const { req, res } = mockReqRes({ method: 'PATCH', path: '/rest/api/3/field' });
    await handler(req, res);

    expect(res.status).toHaveBeenCalledWith(400);
    expect(res.json).toHaveBeenCalledWith(
      expect.objectContaining({ error: expect.stringContaining('Invalid method') }),
    );
  });

  it('returns 500 when Jira not configured', async () => {
    const handler = createJiraProxyHandler(() => ({ host: null, user: null, token: null }), log);
    const { req, res } = mockReqRes({ method: 'GET', path: '/rest/api/3/field' });
    await handler(req, res);

    expect(res.status).toHaveBeenCalledWith(500);
    expect(res.json).toHaveBeenCalledWith(
      expect.objectContaining({ error: expect.stringContaining('not configured') }),
    );
  });

  it('proxies successful GET with ok: true', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    const mockData = [{ id: 'f1', name: 'Summary' }];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(mockData)),
    }));

    const { req, res } = mockReqRes({ method: 'GET', path: '/rest/api/3/field' });
    await handler(req, res);

    expect(res.json).toHaveBeenCalledWith({ ok: true, status: 200, data: mockData });

    const [url, opts] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect(url).toBe('https://test.atlassian.net/rest/api/3/field');
    expect(opts.headers['Authorization']).toBe(
      `Basic ${Buffer.from('u@test.com:tok123').toString('base64')}`,
    );
  });

  it('proxies Jira error with ok: false', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 403,
      text: () => Promise.resolve(JSON.stringify({ errorMessages: ['Forbidden'] })),
    }));

    const { req, res } = mockReqRes({ method: 'POST', path: '/rest/api/3/field', body: { name: 'Test' } });
    await handler(req, res);

    expect(res.status).toHaveBeenCalledWith(200);
    expect(res.json).toHaveBeenCalledWith({
      ok: false, status: 403, data: { errorMessages: ['Forbidden'] },
    });
  });

  it('does not send body on GET requests', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      text: () => Promise.resolve('{}'),
    }));

    const { req, res } = mockReqRes({ method: 'GET', path: '/rest/api/3/field', body: { ignored: true } });
    await handler(req, res);

    const [, opts] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect(opts.body).toBeUndefined();
  });

  it('returns 500 on network error', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));

    const { req, res } = mockReqRes({ method: 'GET', path: '/rest/api/3/myself' });
    await handler(req, res);

    expect(res.status).toHaveBeenCalledWith(500);
    expect(res.json).toHaveBeenCalledWith({ error: 'ECONNREFUSED' });
  });

  it('logs ERROR when method/path missing on 400', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    const { req, res } = mockReqRes({ agent_view_id: 5 });
    await handler(req, res);

    expect(log).toHaveBeenCalledWith(
      'api/jira/request', 'ERROR',
      expect.stringContaining('agent_view_id=5'),
    );
  });

  it('logs ERROR on invalid method', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    const { req, res } = mockReqRes({ method: 'PATCH', path: '/x', agent_view_id: 6 });
    await handler(req, res);

    expect(log).toHaveBeenCalledWith(
      'api/jira/request', 'ERROR',
      expect.stringContaining('invalid method rejected'),
    );
  });

  it('logs ERROR when jira not configured on 500', async () => {
    const handler = createJiraProxyHandler(
      () => ({ host: null, user: null, token: null }),
      log,
    );
    const { req, res } = mockReqRes({ method: 'GET', path: '/x', agent_view_id: 8 });
    await handler(req, res);

    expect(log).toHaveBeenCalledWith(
      'api/jira/request', 'ERROR',
      expect.stringContaining('agent_view_id=8'),
    );
  });

  it('does not double-respond on network error (return after catch)', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const { req, res } = mockReqRes({ method: 'GET', path: '/x' });
    await handler(req, res);

    expect(res.status).toHaveBeenCalledTimes(1);
    expect(res.status).toHaveBeenCalledWith(500);
    expect(res.json).toHaveBeenCalledTimes(1);
  });

  it('ignores a request-supplied jira_host and uses the configured host', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      text: () => Promise.resolve('{}'),
    }));

    const { req, res } = mockReqRes({
      method: 'GET', path: '/rest/api/3/myself', jira_host: 'https://evil.example',
    });
    await handler(req, res);

    const [url] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect(url).toBe('https://test.atlassian.net/rest/api/3/myself');
  });

  it('logs a WARN when jira_host is supplied and ignored', async () => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      text: () => Promise.resolve('{}'),
    }));

    const { req, res } = mockReqRes({
      method: 'GET', path: '/rest/api/3/myself', jira_host: 'https://evil.example',
    });
    await handler(req, res);

    expect(log).toHaveBeenCalledWith(
      'api/jira/request', 'WARN',
      expect.stringContaining('jira_host'),
    );
  });

  it.each([
    ['@evil.example/rest/api/3/myself', 'authority via userinfo'],
    ['//evil.example/x', 'protocol-relative'],
    ['rest/api/3/myself', 'no leading slash'],
    [42, 'not a string'],
  ])('rejects path %s (%s) without sending the credential', async (path) => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn());

    const { req, res } = mockReqRes({ method: 'GET', path });
    await handler(req, res);

    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(res.status).toHaveBeenCalledWith(400);
    expect(res.json).toHaveBeenCalledWith(
      expect.objectContaining({ error: expect.stringContaining('host-relative') }),
    );
  });

  // The 400 response already tells the caller what was wrong, so the operator log
  // never has to carry the caller's own string — a fixed reason cannot disclose it.
  it.each([
    ['path', { method: 'GET', path: '@evil.example/x' }, 'path rejected'],
    ['method', { method: 'SNITCH evil.example', path: '/rest/api/3/myself' }, 'invalid method'],
  ])('does not put the rejected %s into the operator log', async (_field, payload, reason) => {
    const handler = createJiraProxyHandler(() => validConfig, log);
    vi.stubGlobal('fetch', vi.fn());

    const { req, res } = mockReqRes(payload);
    await handler(req, res);

    const details = log.mock.calls.map((c) => c[2]).join(' | ');
    expect(details).not.toContain('evil.example');
    expect(details.toLowerCase()).toContain(reason);
  });
});
