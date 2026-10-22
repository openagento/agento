import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { register } from '../../modules/jira/toolbox/jira.js';
import { FileManager, ConverterRegistry } from '../file-manager.js';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Jira attachments in the thread's downloads dir (docs/architecture/threads.md): the turns of
// one thread share the files, so three comments on one issue fetch each attachment once.

const JIRA_CFG = { jira_host: 'https://example.atlassian.net', jira_user: 'u@example.com', jira_token: 'tok' };

function makeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

function attachment(id, filename) {
  return {
    id, filename, mimeType: 'text/plain', size: 10, created: '2026-01-01T00:00:00.000Z',
    content: `https://example.atlassian.net/secure/attachment/${id}/x`,
  };
}

// The issue fetch answers JSON, a content URL its body: one mock for any order.
function siteFetch(attachments, { key = 'AI-1' } = {}) {
  return vi.fn(async (url) => {
    const att = attachments.find((a) => a.content === url);
    if (att) return { ok: true, arrayBuffer: async () => new globalThis.TextEncoder().encode(`body ${att.id}`).buffer };
    if (url.includes('/rest/api/3/attachment/')) {
      const id = url.split('/').pop();
      return { ok: true, json: async () => attachments.find((a) => a.id === id) };
    }
    return { ok: true, json: async () => ({ key, fields: { summary: 'S', status: { name: 'Open' }, attachment: attachments } }) };
  });
}

function contentFetches(fetchMock) {
  return fetchMock.mock.calls.filter(([url]) => url.includes('/secure/attachment/'));
}

function fileManager() {
  return new FileManager({
    converterRegistry: new ConverterRegistry(),
    allowedExtensions: new Set(['.txt']),
    maxFileSize: 1_000_000,
    log: () => {},
  });
}

const made = [];

function downloadsRoot() {
  const top = fs.mkdtempSync(path.join(os.tmpdir(), 'jira-thread-'));
  made.push(top);
  const root = path.join(top, 'downloads');
  fs.mkdirSync(root);
  return root;
}

function ctx(overrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: { jira: { ...JIRA_CFG } },
    isToolEnabled: () => true,
    artifactsDir: '/tmp/art',
    downloadsDir: null,
    fileManager: fileManager(),
    ...overrides,
  };
}

async function getIssue(c) {
  const s = makeServer();
  register(s, c);
  const res = await s.tools.jira_get_issue.handler({ user: 'u@example.com', issue_key: 'AI-1' });
  return JSON.parse(res.content[0].text);
}

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => { for (const top of made.splice(0)) fs.rmSync(top, { recursive: true, force: true }); });

describe('jira attachments in the thread downloads dir', () => {
  it('three comments on one issue download each attachment once', async () => {
    const atts = [attachment('11', 'spec.txt'), attachment('12', 'log.txt')];
    const fetchMock = siteFetch(atts);
    vi.stubGlobal('fetch', fetchMock);
    const downloadsDir = downloadsRoot();
    const paths = [];
    for (let turn = 0; turn < 3; turn += 1) {
      // Each turn is its own run (its own run dir and session) in one thread.
      const issue = await getIssue(ctx({ artifactsDir: `/tmp/art-${turn}`, downloadsDir }));
      paths.push(issue.Attachments.map((a) => a.localPath).sort());
    }
    expect(contentFetches(fetchMock)).toHaveLength(2);
    expect(paths[1]).toEqual(paths[0]);
    expect(paths[2]).toEqual(paths[0]);
    expect(paths[0]).toEqual([
      path.join(downloadsDir, 'jira', 'AI-1', '11_spec.txt'),
      path.join(downloadsDir, 'jira', 'AI-1', '12_log.txt'),
    ]);
    expect(fs.readFileSync(paths[0][0], 'utf8')).toBe('body 11');
  });

  it('jira_get_attachment reuses the file jira_get_issue downloaded', async () => {
    const atts = [attachment('21', 'a.txt')];
    const fetchMock = siteFetch(atts);
    vi.stubGlobal('fetch', fetchMock);
    const c = ctx({ downloadsDir: downloadsRoot() });
    const issue = await getIssue(c);
    const s = makeServer();
    register(s, c);
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: 'AI-1', attachment_id: '21' });
    expect(JSON.parse(res.content[0].text).localPath).toBe(issue.Attachments[0].localPath);
    expect(contentFetches(fetchMock)).toHaveLength(1);
  });

  it.each([undefined, '', '../1'])('jira_get_attachment refuses a Jira answer with the id %j', async (id) => {
    const atts = [{ ...attachment('22', 'a.txt'), id }];
    const fetchMock = vi.fn(async (url) => (url.includes('/rest/api/3/attachment/')
      ? { ok: true, json: async () => atts[0] }
      : { ok: true, arrayBuffer: async () => new globalThis.TextEncoder().encode('x').buffer }));
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx({ downloadsDir: downloadsRoot() }));
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: 'AI-1', attachment_id: '22' });
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('Jira gave no attachment id');
    expect(contentFetches(fetchMock)).toHaveLength(0);
  });

  it.each([undefined, '', '../1'])('jira_get_issue skips an attachment with the id %j', async (id) => {
    const fetchMock = siteFetch([{ ...attachment('23', 'a.txt'), id }]);
    vi.stubGlobal('fetch', fetchMock);
    await getIssue(ctx({ downloadsDir: downloadsRoot() }));
    expect(contentFetches(fetchMock)).toHaveLength(0);
  });

  it('a file name from Jira cannot leave the issue dir', async () => {
    vi.stubGlobal('fetch', siteFetch([attachment('31', '../../../escape.txt')]));
    const downloadsDir = downloadsRoot();
    const issue = await getIssue(ctx({ downloadsDir }));
    expect(issue.Attachments[0].localPath).toBe(path.join(downloadsDir, 'jira', 'AI-1', '31_escape.txt'));
  });

  it('an issue key from Jira that is not a key downloads nothing', async () => {
    const fetchMock = siteFetch([attachment('41', 'a.txt')], { key: '../x' });
    vi.stubGlobal('fetch', fetchMock);
    const issue = await getIssue(ctx({ downloadsDir: downloadsRoot() }));
    expect(contentFetches(fetchMock)).toHaveLength(0);
    expect(issue.Attachments[0].error).toMatch(/invalid issue key/);
  });

  it('with no thread the run dir gets a generated name and no reuse', async () => {
    vi.stubGlobal('fetch', siteFetch([attachment('51', 'a.txt')]));
    const download = vi.fn(async () => ({ skipped: false, localPath: '/tmp/art/jira/AI-1/a_1.txt', convertedPath: null }));
    await getIssue(ctx({ fileManager: { download } }));
    const options = download.mock.calls[0][2];
    expect(options).toMatchObject({ dir: '/tmp/art/jira/AI-1' });
    expect(options.root).toBeUndefined();
    expect(options.name).toBeUndefined();
    expect(options.skipExisting).toBeUndefined();
  });
});
