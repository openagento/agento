import { describe, it, expect, vi, beforeEach } from 'vitest';

import {
  createSearchHandler,
  createCommentsHandler,
} from '../../modules/jira/toolbox/api-handlers.js';

function mockRes() {
  return {
    status: vi.fn().mockReturnThis(),
    json: vi.fn().mockReturnThis(),
  };
}

const validConfig = {
  host: 'https://example.atlassian.net',
  user: 'u@example.com',
  token: 'tok',
};

describe('jira /api/jira/search handler', () => {
  let log;
  beforeEach(() => {
    log = vi.fn();
    vi.unstubAllGlobals();
  });

  it('logs ERROR when jql is missing and returns 400', async () => {
    const handler = createSearchHandler(async () => validConfig, log);
    const res = mockRes();
    await handler({ body: { agent_view_id: 7 }, capability: { kind: 'internal_rest', agentViewId: 7, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledWith(400);
    expect(log).toHaveBeenCalledWith(
      'api/jira/search', 'ERROR',
      expect.stringContaining('agent_view_id=7'),
    );
    expect(log.mock.calls[0][2]).toMatch(/jql/);
  });

  it('logs ERROR when jira config is missing and returns 500', async () => {
    const handler = createSearchHandler(async () => ({ host: null, user: null, token: null }), log);
    const res = mockRes();
    await handler({ body: { jql: 'project=AI', agent_view_id: 9 }, capability: { kind: 'internal_rest', agentViewId: 9, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledWith(500);
    expect(log).toHaveBeenCalledWith(
      'api/jira/search', 'ERROR',
      expect.stringContaining('agent_view_id=9'),
    );
    expect(log.mock.calls[0][2]).toMatch(/missing/i);
  });

  it('does not send a second response when fetch throws (return after catch)', async () => {
    const handler = createSearchHandler(async () => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const res = mockRes();
    await handler({ body: { jql: 'project=AI' }, capability: { kind: 'internal_rest', agentViewId: 7, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledTimes(1);
    expect(res.status).toHaveBeenCalledWith(500);
    expect(res.json).toHaveBeenCalledTimes(1);
  });
});

describe('jira /api/jira/issue/comments handler', () => {
  let log;
  beforeEach(() => {
    log = vi.fn();
    vi.unstubAllGlobals();
  });

  it('logs ERROR when issue_key is missing and returns 400', async () => {
    const handler = createCommentsHandler(async () => validConfig, log);
    const res = mockRes();
    await handler({ body: { agent_view_id: 3 }, capability: { kind: 'internal_rest', agentViewId: 3, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledWith(400);
    expect(log).toHaveBeenCalledWith(
      'api/jira/issue/comments', 'ERROR',
      expect.stringContaining('agent_view_id=3'),
    );
    expect(log.mock.calls[0][2]).toMatch(/issue_key/);
  });

  it('logs ERROR when jira config is missing and returns 500', async () => {
    const handler = createCommentsHandler(async () => ({ host: validConfig.host, user: validConfig.user, token: null }), log);
    const res = mockRes();
    await handler({ body: { issue_key: 'AI-1', agent_view_id: 4 }, capability: { kind: 'internal_rest', agentViewId: 4, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledWith(500);
    expect(log).toHaveBeenCalledWith(
      'api/jira/issue/comments', 'ERROR',
      expect.stringContaining('agent_view_id=4'),
    );
  });

  it('does not send a second response when fetch throws (return after catch)', async () => {
    const handler = createCommentsHandler(async () => validConfig, log);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('boom')));
    const res = mockRes();
    await handler({ body: { issue_key: 'AI-1' }, capability: { kind: 'internal_rest', agentViewId: 7, jobId: null } }, res);

    expect(res.status).toHaveBeenCalledTimes(1);
    expect(res.status).toHaveBeenCalledWith(500);
    expect(res.json).toHaveBeenCalledTimes(1);
  });
});
