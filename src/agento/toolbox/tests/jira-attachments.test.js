import { describe, it, expect, vi, beforeEach } from 'vitest';
import { register } from '../../modules/jira/toolbox/jira.js';
import { FileManager, ConverterRegistry } from '../file-manager.js';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function makeServer() {
  const tools = {};
  return { tools, tool(name, desc, schema, handler) { tools[name] = { desc, schema, handler }; } };
}

const JIRA_CFG = { jira_host: 'https://example.atlassian.net', jira_user: 'u@example.com', jira_token: 'tok' };

// Build N attachments with explicit ids/filenames/created dates.
function makeAttachments(specs) {
  return specs.map((s, i) => ({
    id: s.id ?? `att${i}`,
    filename: s.filename ?? `file${i}.txt`,
    mimeType: s.mimeType ?? 'text/plain',
    size: s.size ?? 100,
    created: s.created ?? `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00.000Z`,
    content: s.content ?? `https://example.atlassian.net/secure/attachment/${s.id ?? `att${i}`}/x`,
  }));
}

function stubIssueFetch(attachments, { key = 'AI-1' } = {}) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ key, fields: { summary: 'S', status: { name: 'Open' }, attachment: attachments } }),
  }));
}

// Mock fileManager whose download always succeeds, recording the order of requested filenames.
function successFileManager() {
  const calls = [];
  return {
    calls,
    download: vi.fn(async (url, filename) => {
      calls.push(filename);
      return { skipped: false, localPath: `/tmp/art/jira/AI-1/${filename}`, convertedPath: null, conversionError: null };
    }),
  };
}

function ctx(overrides = {}) {
  return {
    log: vi.fn(),
    moduleConfigs: { jira: { ...JIRA_CFG } },
    isToolEnabled: (n) => n !== 'jira_get_attachment',
    artifactsDir: '/tmp/art',
    fileManager: successFileManager(),
    ...overrides,
  };
}

async function callGetIssue(c) {
  const s = makeServer();
  register(s, c);
  const res = await s.tools.jira_get_issue.handler({ user: 'u@example.com', issue_key: 'AI-1' });
  return { res, issue: JSON.parse(res.content[0].text) };
}

beforeEach(() => vi.unstubAllGlobals());

describe('jira_get_issue attachment limit', () => {
  it('B2: Attachments[] contains ALL attachments regardless of limit', async () => {
    const atts = makeAttachments(Array.from({ length: 12 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
    stubIssueFetch(atts);
    const fm = successFileManager();
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 10 } }, fileManager: fm }));
    expect(issue.Attachments).toHaveLength(12);
    expect(fm.download).toHaveBeenCalledTimes(10);
  });

  it('B1: downloads the N newest by created (descending)', async () => {
    // Mixed dates: newest are a-2026-12, a-2026-11, a-2026-10
    const atts = [
      { id: 'old1', filename: 'old1.txt', created: '2026-01-01T00:00:00Z' },
      { id: 'new1', filename: 'new1.txt', created: '2026-12-01T00:00:00Z' },
      { id: 'mid1', filename: 'mid1.txt', created: '2026-06-01T00:00:00Z' },
      { id: 'new2', filename: 'new2.txt', created: '2026-11-01T00:00:00Z' },
      { id: 'old2', filename: 'old2.txt', created: '2026-02-01T00:00:00Z' },
    ].map((s) => makeAttachments([s])[0]);
    stubIssueFetch(atts);
    const fm = successFileManager();
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 2 } }, fileManager: fm }));
    expect(fm.calls.sort()).toEqual(['new1.txt', 'new2.txt']);
    const downloaded = issue.Attachments.filter((a) => a.localPath !== null).map((a) => a.filename).sort();
    expect(downloaded).toEqual(['new1.txt', 'new2.txt']);
  });

  it('A4: limit 0 means download all', async () => {
    const atts = makeAttachments(Array.from({ length: 12 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
    stubIssueFetch(atts);
    const fm = successFileManager();
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 0 } }, fileManager: fm }));
    expect(fm.download).toHaveBeenCalledTimes(12);
    expect(issue.Attachments.every((a) => a.localPath !== null)).toBe(true);
  });

  it('A3: limit value drives how many are downloaded (scope-resolved value honored)', async () => {
    const atts = makeAttachments(Array.from({ length: 8 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
    stubIssueFetch(atts);
    const fm = successFileManager();
    // Simulates an agent_view override resolving to "3" (DB values arrive as strings).
    await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: '3' } }, fileManager: fm }));
    expect(fm.download).toHaveBeenCalledTimes(3);
  });

  it('F2: invalid/negative/partial limit falls back to default 10', async () => {
    for (const bad of ['-5', '3abc', '', null, undefined]) {
      const atts = makeAttachments(Array.from({ length: 12 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
      stubIssueFetch(atts);
      const fm = successFileManager();
      await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: bad } }, fileManager: fm }));
      expect(fm.download).toHaveBeenCalledTimes(10);
    }
  });

  it('C1: over-limit entries have a limit error + jira_get_attachment hint and null localPath', async () => {
    const atts = makeAttachments(Array.from({ length: 12 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt`, created: `2026-01-${String(i + 1).padStart(2, '0')}T00:00:00Z` })));
    stubIssueFetch(atts);
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 10 } } }));
    const skipped = issue.Attachments.filter((a) => a.localPath === null);
    expect(skipped).toHaveLength(2);
    for (const s of skipped) {
      expect(s.error).toMatch(/skipped.*limit/i);
      expect(s.error).toContain('jira_get_attachment');
    }
  });

  it('C2: no entry has localPath === null && error === null (no silent loss)', async () => {
    const atts = makeAttachments(Array.from({ length: 12 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
    stubIssueFetch(atts);
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 10 } } }));
    for (const a of issue.Attachments) {
      expect(a.localPath === null && a.error === null).toBe(false);
    }
  });

  it('C3: a real download failure keeps the original reason (not the limit message)', async () => {
    const atts = makeAttachments([{ id: 'a0', filename: 'f0.txt' }]);
    stubIssueFetch(atts);
    const fm = { download: vi.fn(async () => ({ skipped: true, skipReason: 'Download failed: HTTP 500' })) };
    const { issue } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 10 } }, fileManager: fm }));
    expect(issue.Attachments[0].error).toBe('Download failed: HTTP 500');
    expect(issue.Attachments[0].error).not.toMatch(/skipped.*limit/i);
    expect(issue.Attachments[0].localPath).toBeNull();
  });

  it('F1: in-flight downloads never exceed the concurrency pool', async () => {
    const atts = makeAttachments(Array.from({ length: 20 }, (_, i) => ({ id: `a${i}`, filename: `f${i}.txt` })));
    stubIssueFetch(atts);
    let inFlight = 0;
    let maxInFlight = 0;
    const fm = {
      download: vi.fn(async (url, filename) => {
        inFlight++;
        maxInFlight = Math.max(maxInFlight, inFlight);
        await new Promise((r) => setTimeout(r, 5));
        inFlight--;
        return { skipped: false, localPath: `/tmp/art/jira/AI-1/${filename}`, convertedPath: null, conversionError: null };
      }),
    };
    await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 0 } }, fileManager: fm }));
    expect(fm.download).toHaveBeenCalledTimes(20);
    expect(maxInFlight).toBeLessThanOrEqual(6);
    expect(maxInFlight).toBeGreaterThan(1); // proves it IS concurrent, not serial
  });

  it('E1: jira_get_issue output contains no token, Basic auth, or content URL', async () => {
    const atts = makeAttachments([{ id: 'a0', filename: 'f0.txt', content: 'https://example.atlassian.net/secret-content-url/a0' }]);
    stubIssueFetch(atts);
    const { res } = await callGetIssue(ctx({ moduleConfigs: { jira: { ...JIRA_CFG, attachment_download_limit: 10 } } }));
    const text = res.content[0].text;
    expect(text).not.toContain('tok');
    expect(text).not.toContain('Basic ');
    expect(text).not.toContain('secret-content-url');
  });
});

function realFileManager() {
  return new FileManager({
    converterRegistry: new ConverterRegistry(),
    allowedExtensions: new Set(['.txt', '.pdf', '.png']),
    maxFileSize: 50 * 1024 * 1024,
    log: () => {},
  });
}

describe('jira_get_attachment tool', () => {
  it('D3: opt-in — not registered unless isToolEnabled returns true', () => {
    const sOff = makeServer();
    register(sOff, ctx({ isToolEnabled: (n) => n !== 'jira_get_attachment' }));      // attachment flag false
    expect(sOff.tools.jira_get_attachment).toBeUndefined();
    expect(sOff.tools.jira_get_issue).toBeDefined();

    const sOn = makeServer();
    register(sOn, ctx({ isToolEnabled: () => true }));
    expect(sOn.tools.jira_get_attachment).toBeDefined();
  });

  it('D1: schema exposes user, issue_key, attachment_id', () => {
    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true }));
    expect(Object.keys(s.tools.jira_get_attachment.schema).sort()).toEqual(['attachment_id', 'issue_key', 'user']);
  });

  it('D2: downloads the requested attachment to artifacts/jira/<KEY>/ (file exists on disk)', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jira-att-'));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ id: '900', filename: 'evidence.txt', mimeType: 'text/plain', size: 11, content: 'https://example.atlassian.net/secure/attachment/900/evidence.txt' }) })
      .mockResolvedValueOnce({ ok: true, arrayBuffer: () => Promise.resolve(new globalThis.TextEncoder().encode('hello world').buffer) });
    vi.stubGlobal('fetch', fetchMock);

    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true, artifactsDir: tmp, fileManager: realFileManager() }));
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: 'AI-1', attachment_id: '900' });
    const out = JSON.parse(res.content[0].text);

    expect(res.isError).toBeUndefined();
    expect(out.localPath).not.toBeNull();
    expect(out.localPath).toContain(path.join(tmp, 'jira', 'AI-1'));
    expect(fs.existsSync(out.localPath)).toBe(true);
    expect(fs.readFileSync(out.localPath, 'utf-8')).toBe('hello world');
  });

  it('D4: unknown attachment_id → isError, not a silent null', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve('{"errorMessages":["not found"]}') });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true }));
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: 'AI-1', attachment_id: 'nope' });
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toMatch(/404/);
  });

  it('F1/security: rejects a path-traversal issue_key (isError, no fetch, no download)', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const fm = { download: vi.fn() };
    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true, fileManager: fm }));
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: '../../etc/passwd', attachment_id: '900' });
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toMatch(/invalid issue_key/);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(fm.download).not.toHaveBeenCalled();
  });

  it('F1/security: Zod schema also rejects a traversal issue_key, accepts a real key', () => {
    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true }));
    expect(s.tools.jira_get_attachment.schema.issue_key.safeParse('../../x').success).toBe(false);
    expect(s.tools.jira_get_attachment.schema.issue_key.safeParse('AI-1').success).toBe(true);
  });

  it('E1: output contains no token, Basic auth, or content URL', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jira-att-'));
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ id: '901', filename: 'doc.txt', mimeType: 'text/plain', size: 3, content: 'https://example.atlassian.net/secret-content-url/901' }) })
      .mockResolvedValueOnce({ ok: true, arrayBuffer: () => Promise.resolve(new globalThis.TextEncoder().encode('abc').buffer) });
    vi.stubGlobal('fetch', fetchMock);
    const s = makeServer();
    register(s, ctx({ isToolEnabled: () => true, artifactsDir: tmp, fileManager: realFileManager() }));
    const res = await s.tools.jira_get_attachment.handler({ user: 'u@example.com', issue_key: 'AI-1', attachment_id: '901' });
    const text = res.content[0].text;
    expect(text).not.toContain('tok');
    expect(text).not.toContain('Basic ');
    expect(text).not.toContain('secret-content-url');
  });
});
